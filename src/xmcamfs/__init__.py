"""xmcam-fs - mount a Xiongmai / iCSee / XMeye camera as a FUSE filesystem.

Browse a camera's recording archive as ordinary files, sshfs-style, backed by
the :mod:`xmcam` library. Each recording appears twice:

* under ``mp4/`` as a playable MP4 (produced lazily on first open and cached), and
* under ``raw/`` as the original Xiongmai ``.xmraw`` container.

The filesystem is read-only and talks only to the local network - no cloud.
"""
from __future__ import annotations

__version__ = "0.1.0"
