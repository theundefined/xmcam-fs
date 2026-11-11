"""On-disk cache of camera segments: download once, serve from disk.

A segment is slow to fetch (DVRIP streams at roughly realtime/2), so each one is
produced exactly once and then read straight off disk. Because a Xiongmai camera
has a single playback channel, downloads are serialised with one lock -- running
two ``OPPlayBack`` sessions at once provokes ``Ret 103``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from xmcam.dvrip import DVRIPClient
from xmcam.exceptions import XMCamError
from xmcam.media import convert_raw, have_ffmpeg
from xmcam.models import Credentials, Recording
from xmcam.playback import download_recording


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
    ) -> None:
        self.host = host
        self.creds = credentials
        self.cache_dir = Path(cache_dir)
        self.port = port
        self.channel = channel
        self.stream = stream
        self.tracker = tracker  # optional ActivityTracker for progress/ETA
        self._download_lock = threading.Lock()  # one playback channel per camera

        for sub in ("raw", "mp4", ".tmp"):
            (self.cache_dir / sub).mkdir(parents=True, exist_ok=True)

    # -- public ---------------------------------------------------------------
    def path_for(self, rec: Recording, view: str) -> Path:
        """The cache path for this (recording, view), whether or not it exists yet."""
        stem = _safe_stem(rec)
        if view.lower() == "raw":
            return self.cache_dir / "raw" / f"{stem}.xmraw"
        return self.cache_dir / "mp4" / f"{stem}.mp4"

    def ensure(self, rec: Recording, view: str, display: str = "") -> Path:
        """Return the cache path, downloading/converting once if needed (BLOCKING).

        *display* is the user-facing name reported in progress/status output.
        """
        dest = self.path_for(rec, view)
        if dest.exists() and dest.stat().st_size > 0:
            return dest

        with self._download_lock:
            # Re-check: another thread may have produced it while we waited.
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            key = str(dest)
            progress = None
            if self.tracker is not None:
                self.tracker.start(key, display or _safe_stem(rec), view.lower(), rec.length_bytes)
                progress = lambda n, _k=key: self.tracker.update(_k, n)  # noqa: E731
            try:
                if view.lower() == "raw":
                    self._download_raw(rec, dest, progress=progress)
                else:
                    self._produce_mp4(rec, dest, progress=progress, key=key)
            finally:
                if self.tracker is not None:
                    self.tracker.finish(key)
        return dest

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
