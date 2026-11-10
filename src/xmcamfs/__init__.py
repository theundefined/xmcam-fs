"""xmcam-fs - mount a Xiongmai / iCSee / XMeye camera as a FUSE filesystem.

Browse a camera's recording archive as ordinary files, sshfs-style, backed by
the :mod:`xmcam` library. Each recording appears twice:

* under ``mp4/`` as a playable MP4 (produced lazily on first open and cached), and
* under ``raw/`` as the original Xiongmai ``.xmraw`` container.

The filesystem is read-only and talks only to the local network - no cloud.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("xmcam-fs")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"
