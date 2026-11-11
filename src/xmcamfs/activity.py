"""In-memory activity tracker: what is being accessed and downloaded, with ETA.

A single :class:`ActivityTracker` is shared by the cache (which reports download
progress) and the FUSE layer (which reports file accesses and serves the
``/.status`` virtual file). It is thread-safe: downloads run in worker threads.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, TextIO


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def fmt_eta(seconds: Optional[float]) -> str:
    """Format an ETA as ``M:SS`` (or ``H:MM:SS``); ``--`` when unknown."""
    if seconds is None:
        return "--"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


@dataclass
class Access:
    """One line of the "Recent access" list."""

    ts: float
    name: str
    view: str
    kind: str  # "hit" (served from cache) | "fetch" (downloaded)
    size: Optional[int] = None  # bytes of the produced/cached file
    speed: Optional[float] = None  # download speed, bytes/s (fetch only)
    state: str = "pending"  # fetch only: "pending" | "done" | "failed" | "cancelled"


@dataclass
class Download:
    name: str
    view: str
    total: int  # expected bytes (container size); may be 0 if unknown
    done: int = 0
    phase: str = "downloading"  # "downloading" | "converting"
    start: float = field(default_factory=lambda: time.monotonic())
    last_print: float = 0.0
    dl_seconds: Optional[float] = None  # length of the download phase (set when conversion starts)

    @property
    def speed(self) -> float:
        elapsed = time.monotonic() - self.start
        return self.done / elapsed if elapsed > 0 else 0.0

    @property
    def final_speed(self) -> float:
        """Download speed over the download phase only (conversion time excluded)."""
        seconds = self.dl_seconds if self.dl_seconds is not None else time.monotonic() - self.start
        return self.done / seconds if seconds > 0 else 0.0

    @property
    def eta(self) -> Optional[float]:
        speed = self.speed
        if speed <= 0 or self.total <= 0:
            return None
        return max(self.total - self.done, 0) / speed

    @property
    def pct(self) -> float:
        # Clamp at 100: total is an estimate, so done can nudge slightly past it.
        return min(self.done / self.total * 100, 100.0) if self.total else 0.0


class ActivityTracker:
    """Collect live download/access state and render it as text."""

    def __init__(self, progress_stream: Optional[TextIO] = None) -> None:
        self._lock = threading.Lock()
        self._active: Dict[str, Download] = {}
        self._recent: Deque[Access] = deque(maxlen=20)
        self._completed = 0
        self._stream = progress_stream  # live \r progress lines when set (a TTY)

    # -- access log -----------------------------------------------------------
    def access(self, name: str, view: str, hit: bool, size: Optional[int] = None) -> None:
        """Record a file access; *size* is the cached file's size for a hit."""
        with self._lock:
            self._recent.appendleft(
                Access(time.time(), name, view, "hit" if hit else "fetch", size=size if hit else None)
            )

    # -- download lifecycle (called from worker threads) ----------------------
    def start(self, key: str, name: str, view: str, total: int) -> None:
        download = Download(name=name, view=view, total=total)
        with self._lock:
            self._active[key] = download
        self._maybe_print(download, force=True)

    def update(self, key: str, done: int) -> None:
        with self._lock:
            download = self._active.get(key)
            if download is not None:
                download.done = done
        if download is not None:
            self._maybe_print(download)

    def phase(self, key: str, phase: str) -> None:
        with self._lock:
            download = self._active.get(key)
            if download is not None:
                download.phase = phase
                if phase == "converting" and download.dl_seconds is None:
                    download.dl_seconds = time.monotonic() - download.start
        if download is not None:
            self._maybe_print(download, force=True)

    def finish(self, key: str, ok: bool = True, size: Optional[int] = None, cancelled: bool = False) -> None:
        """A download ended. *size* is the size of the produced file (when ok)."""
        with self._lock:
            download = self._active.pop(key, None)
            if ok:
                self._completed += 1
            if download is not None:
                for entry in self._recent:  # newest first: the access that triggered this fetch
                    if (
                        entry.kind == "fetch"
                        and entry.state == "pending"
                        and (entry.name, entry.view)
                        == (
                            download.name,
                            download.view,
                        )
                    ):
                        entry.state = "done" if ok else ("cancelled" if cancelled else "failed")
                        if ok:
                            entry.size = size if size is not None else download.done
                            entry.speed = download.final_speed
                        break
        if download is not None and self._stream is not None:
            self._stream.write(f"\r  done: {download.name} ({human_bytes(download.total)})" + " " * 24 + "\n")
            self._stream.flush()

    # -- rendering ------------------------------------------------------------
    def _maybe_print(self, download: Download, force: bool = False) -> None:
        if self._stream is None:
            return
        now = time.monotonic()
        if force or now - download.last_print >= 0.5:
            download.last_print = now
            if download.phase == "converting":
                self._stream.write(f"\r  {download.name}  converting (ffmpeg)...   ")
            else:
                self._stream.write(
                    f"\r  {download.name}  {download.pct:4.0f}%  "
                    f"{human_bytes(download.speed)}/s  ETA {fmt_eta(download.eta)}   "
                )
            self._stream.flush()

    @staticmethod
    def _describe(entry: Access) -> str:
        if entry.kind == "hit":
            return "hit" + (f"  {human_bytes(entry.size)}" if entry.size else "")
        if entry.state == "failed":
            return "fetch  FAILED"
        if entry.state == "cancelled":
            return "fetch  cancelled"
        if entry.state == "pending":
            return "fetch  ..."
        speed = f"  {human_bytes(entry.speed)}/s" if entry.speed else ""
        return f"fetch  {human_bytes(entry.size or 0)}{speed}"

    def snapshot(self) -> str:
        """A human-readable status report (used for the ``/.status`` file)."""
        with self._lock:
            active: List[Download] = list(self._active.values())
            recent = list(self._recent)
            completed = self._completed

        lines = ["xmcam-fs status", ""]
        if active:
            lines.append("Active downloads:")
            for d in active:
                if d.phase == "converting":
                    lines.append(f"  {d.name}  [{d.view}]  converting (ffmpeg)...")
                else:
                    lines.append(
                        f"  {d.name}  [{d.view}]  {d.pct:4.0f}%  "
                        f"{human_bytes(d.done)}/{human_bytes(d.total)}  "
                        f"{human_bytes(d.speed)}/s  ETA {fmt_eta(d.eta)}"
                    )
        else:
            lines.append("Active downloads: none")

        lines += ["", "Recent access:"]
        if recent:
            for entry in recent[:12]:
                stamp = time.strftime("%H:%M:%S", time.localtime(entry.ts))
                lines.append(f"  {stamp}  {entry.name}  [{entry.view}]  {self._describe(entry)}")
        else:
            lines.append("  (none yet)")

        lines += ["", f"Completed this session: {completed}", ""]
        return "\n".join(lines)
