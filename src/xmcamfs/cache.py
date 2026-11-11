"""On-disk cache of camera segments: download once, serve from disk.

A segment is slow to fetch (DVRIP streams at roughly realtime/2), so each one is
produced exactly once and then read straight off disk. Because a Xiongmai camera
has a single playback channel, downloads are serialised with one lock -- running
two ``OPPlayBack`` sessions at once provokes ``Ret 103``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

from xmcam.dvrip import DVRIPClient
from xmcam.exceptions import XMCamError
from xmcam.media import convert_raw, have_ffmpeg
from xmcam.models import Credentials, Recording
from xmcam.playback import download_recording

if TYPE_CHECKING:  # pragma: no cover
    from .stream import TsJob

log = logging.getLogger("xmcamfs")

#: Subdirectories of the cache that hold finished segments (what the size limit counts).
SEGMENT_DIRS = ("raw", "mp4", "ts")

#: Cache files used less than this long ago are never evicted (they may be about to be opened).
EVICT_GRACE = 30.0

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.IGNORECASE)


def parse_size(text: str) -> int:
    """Parse ``100M`` / ``2G`` / ``512k`` / ``123456`` into bytes (``0`` = unlimited)."""
    match = _SIZE_RE.match(str(text))
    if not match:
        raise ValueError(f"invalid size {text!r} (examples: 100M, 2G, 0 for unlimited)")
    number, unit = match.groups()
    return int(float(number) * 1024 ** ("bkmgt".index(unit.lower() or "b")))


def _safe_stem(rec: Recording) -> str:
    """A flat, collision-free filename stem derived from the recording path.

    The camera path already contains the date and a unique ``[@token]``, so
    flattening it yields a stable, unique name.
    """
    stem = (rec.filename or "recording").strip("/").replace("/", "_")
    for ch in "[]":
        stem = stem.replace(ch, "")
    for ext in (".h264", ".h265", ".jpg"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem or "recording"


class SegmentCache:
    """Produce and cache the ``mp4`` / ``raw`` rendering of a recording."""

    def __init__(
        self,
        host: str,
        credentials: Credentials,
        cache_dir,
        port: int = 34567,
        channel: int = 0,
        stream: str = "Main",
        tracker=None,
        max_bytes: int = 0,
        ts_on_close: str = "cancel",
    ) -> None:
        self.host = host
        self.creds = credentials
        self.cache_dir = Path(cache_dir)
        self.port = port
        self.channel = channel
        self.stream = stream
        self.tracker = tracker  # optional ActivityTracker for progress/ETA
        self._download_lock = threading.Lock()  # one playback channel per camera
        self.max_bytes = max_bytes  # soft cap on cached footage; 0 = unlimited
        self.ts_on_close = ts_on_close  # "cancel" | "finish": what a ts download does when its last reader closes
        self._jobs: Dict[Path, "TsJob"] = {}  # ts segments being produced right now
        self._jobs_lock = threading.Lock()

        for sub in (*SEGMENT_DIRS, ".tmp"):
            (self.cache_dir / sub).mkdir(parents=True, exist_ok=True)

    # -- public ---------------------------------------------------------------
    def path_for(self, rec: Recording, view: str) -> Path:
        """The cache path for this (recording, view), whether or not it exists yet."""
        stem = _safe_stem(rec)
        view = view.lower()
        if view == "raw":
            return self.cache_dir / "raw" / f"{stem}.xmraw"
        if view == "ts":
            return self.cache_dir / "ts" / f"{stem}.ts"
        return self.cache_dir / "mp4" / f"{stem}.mp4"

    def is_busy(self) -> bool:
        """True while a segment is being downloaded/converted."""
        return self._download_lock.locked()

    def ensure(self, rec: Recording, view: str, display: str = "") -> Path:
        """Return the cache path, downloading/converting once if needed (BLOCKING).

        *display* is the user-facing name reported in progress/status output.
        """
        dest = self.path_for(rec, view)
        if self._touch_if_cached(dest):
            return dest
        if view.lower() == "ts":  # progressive view: run the job to the end
            job = self.ts_job(rec, display)
            if job is not None:
                job.acquire()  # a blocking caller is a reader: never cancelled
                job._thread.join()
                if job.error:
                    raise XMCamError(f"ts rendering failed: {job.error}")
            return dest

        with self._download_lock:
            # Re-check: another thread may have produced it while we waited.
            if self._touch_if_cached(dest):
                return dest
            key = str(dest)
            progress = None
            if self.tracker is not None:
                self.tracker.start(key, display or _safe_stem(rec), view.lower(), rec.length_bytes)
                progress = lambda n, _k=key: self.tracker.update(_k, n)  # noqa: E731
            ok = False
            try:
                if view.lower() == "raw":
                    self._download_raw(rec, dest, progress=progress)
                else:
                    self._produce_mp4(rec, dest, progress=progress, key=key)
                ok = True
            finally:
                if self.tracker is not None:
                    self.tracker.finish(key, ok=ok, size=dest.stat().st_size if ok else None)
            self.evict(keep=dest)
        return dest

    # -- progressive ts view ----------------------------------------------------
    def ts_job(self, rec: Recording, display: str = "") -> Optional["TsJob"]:
        """The running job for this segment's ts rendering (started if needed).

        Returns ``None`` when the finished file is already cached.
        """
        from .stream import TsJob

        dest = self.path_for(rec, "ts")
        if self._touch_if_cached(dest):
            return None
        with self._jobs_lock:
            job = self._jobs.get(dest)
            if job is None or job.done:
                job = TsJob(self, rec, display or _safe_stem(rec), dest, on_close=self.ts_on_close)
                self._jobs[dest] = job
                job.start()
        return job

    def ts_job_active(self, rec: Recording) -> bool:
        """True while this segment's ts rendering is still being produced."""
        with self._jobs_lock:
            job = self._jobs.get(self.path_for(rec, "ts"))
            return job is not None and not job.done

    # -- size limit -----------------------------------------------------------
    @staticmethod
    def _touch_if_cached(dest: Path) -> bool:
        """True if *dest* is a non-empty cached file; marks it as just used (LRU)."""
        try:
            if dest.stat().st_size <= 0:
                return False
            os.utime(dest)
            return True
        except OSError:
            return False

    def usage(self) -> int:
        """Total bytes of cached segments (temporary files not counted)."""
        total = 0
        for sub in SEGMENT_DIRS:
            for f in (self.cache_dir / sub).iterdir():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def evict(self, keep: Optional[Path] = None) -> int:
        """Delete least-recently-used segments until under ``max_bytes``; returns bytes freed.

        The limit is soft: *keep* and anything used within :data:`EVICT_GRACE`
        seconds survive, so a single segment bigger than the limit still works.
        A reader that already has a file open keeps reading it after unlink.
        """
        if self.max_bytes <= 0:
            return 0
        files = []
        for sub in SEGMENT_DIRS:
            for f in (self.cache_dir / sub).iterdir():
                try:
                    st = f.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, f))
        total = sum(size for _, size, _ in files)
        freed = 0
        now = time.time()
        for mtime, size, f in sorted(files):
            if total <= self.max_bytes:
                break
            if f == keep or now - mtime < EVICT_GRACE or f.name.endswith(".part"):
                continue
            try:
                f.unlink()
            except OSError:
                continue
            log.info("cache over limit: evicted %s (%d bytes)", f.name, size)
            total -= size
            freed += size
        return freed

    # -- internals ------------------------------------------------------------
    def _client(self) -> DVRIPClient:
        cam = DVRIPClient(self.host, user=self.creds.username, password=self.creds.password, port=self.port)
        cam.login_or_raise()
        return cam

    def _download_raw(self, rec: Recording, dest: Path, progress=None) -> None:
        tmp = dest.with_name(dest.name + ".part")
        cam = self._client()
        try:
            download_recording(cam, rec, tmp, progress=progress, channel=self.channel, stream=self.stream)
        finally:
            cam.close()
        tmp.replace(dest)

    def _produce_mp4(self, rec: Recording, dest: Path, progress=None, key=None) -> None:
        if not have_ffmpeg():
            raise XMCamError("ffmpeg not found; cannot produce the MP4 view")
        stem = _safe_stem(rec)
        raw_tmp = self.cache_dir / ".tmp" / (stem + ".xmraw")
        self._download_raw(rec, raw_tmp, progress=progress)
        if self.tracker is not None and key is not None:
            self.tracker.phase(key, "converting")
        # The temp output must keep a real ``.mp4`` extension so ffmpeg can infer
        # the muxer (a ``.part`` suffix makes it fail with "no output format").
        tmp_mp4 = self.cache_dir / ".tmp" / (stem + ".partial.mp4")
        ok = convert_raw(raw_tmp, tmp_mp4, keep_raw=False)
        if not ok or not tmp_mp4.exists():
            if tmp_mp4.exists():
                tmp_mp4.unlink()
            raise XMCamError(f"MP4 conversion failed for {rec.filename}")
        tmp_mp4.replace(dest)
