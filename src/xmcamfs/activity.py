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
from typing import Deque, Dict, List, Optional, TextIO, Tuple


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
class Download:
    name: str
    view: str
    total: int  # expected bytes (container size); may be 0 if unknown
    done: int = 0
    phase: str = "downloading"  # "downloading" | "converting"
    start: float = field(default_factory=time.monotonic)
    last_print: float = 0.0

    @property
    def speed(self) -> float:
        elapsed = time.monotonic() - self.start
        return self.done / elapsed if elapsed > 0 else 0.0

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
        self._recent: Deque[Tuple[float, str, str, str]] = deque(maxlen=20)
        self._completed = 0
        self._stream = progress_stream  # live \r progress lines when set (a TTY)

    # -- access log -----------------------------------------------------------
    def access(self, name: str, view: str, hit: bool) -> None:
        with self._lock:
            self._recent.appendleft((time.time(), name, view, "hit" if hit else "fetch"))

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
        if download is not None:
            self._maybe_print(download, force=True)

    def finish(self, key: str) -> None:
        with self._lock:
            download = self._active.pop(key, None)
            self._completed += 1
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
            for ts, name, view, kind in recent[:12]:
                stamp = time.strftime("%H:%M:%S", time.localtime(ts))
                lines.append(f"  {stamp}  {name}  [{view}]  {kind}")
        else:
            lines.append("  (none yet)")

        lines += ["", f"Completed this session: {completed}", ""]
        return "\n".join(lines)
