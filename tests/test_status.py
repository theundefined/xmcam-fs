"""The /.status virtual file must be lookup-able and readable."""

from __future__ import annotations

import stat

import trio

from xmcamfs.activity import ActivityTracker
from xmcamfs.fs import STATUS_INODE, STATUS_NAME, CameraFs
from xmcamfs.tree import ROOT_INODE, FsTree


def _fs():
    return CameraFs(FsTree.from_recordings([]), cache=None, tracker=ActivityTracker())


def test_status_getattr_is_regular_file():
    attr = trio.run(_fs().getattr, STATUS_INODE)
    assert stat.S_ISREG(attr.st_mode)
    assert attr.st_size > 0


def test_status_lookup_at_root():
    attr = trio.run(_fs().lookup, ROOT_INODE, STATUS_NAME.encode())
    assert attr.st_ino == STATUS_INODE


def test_status_open_and_read():
    fs = _fs()
    info = trio.run(fs.open, STATUS_INODE, 0, None)
    data = trio.run(fs.read, info.fh, 0, 65536)
    assert b"xmcam-fs status" in data
    trio.run(fs.release, info.fh)
