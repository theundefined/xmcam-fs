"""Light tests for the FUSE operations layer (no real mount).

The async handlers are driven directly with trio; only getattr/lookup are
exercised, since readdir/open need kernel-supplied tokens/fds.
"""

from __future__ import annotations

import errno
import stat
from datetime import datetime

import pytest
import trio

from xmcam.models import Recording
from xmcamfs.fs import CameraFs
from xmcamfs.tree import ROOT_INODE, FsTree


def _tree():
    recs = [
        Recording(
            filename="/idea0/2026-11-10/001/08.00.00-08.05.00[R][@4ab2][0].h264",
            begin_time=datetime(2026, 11, 10, 8, 0, 0),
            end_time=datetime(2026, 11, 10, 8, 5, 0),
            length_bytes=49 * 1024 * 1024,
        )
    ]
    return FsTree.from_recordings(recs)


def _fs():
    return CameraFs(_tree(), cache=None)


def test_getattr_root_is_dir():
    fs = _fs()
    attr = trio.run(fs.getattr, ROOT_INODE)
    assert stat.S_ISDIR(attr.st_mode)


def test_getattr_missing_inode():
    fs = _fs()
    with pytest.raises(Exception) as exc:
        trio.run(fs.getattr, 99999)
    assert getattr(exc.value, "errno", None) == errno.ENOENT


def test_lookup_date_then_view_then_file():
    fs = _fs()
    tree = fs.tree

    async def walk():
        date = await fs.lookup(ROOT_INODE, b"2026-11-10")
        assert stat.S_ISDIR(date.st_mode)
        mp4 = await fs.lookup(date.st_ino, b"mp4")
        assert stat.S_ISDIR(mp4.st_mode)
        files = tree.list_children(mp4.st_ino)
        assert files, "mp4 view should contain a file"
        fattr = await fs.lookup(mp4.st_ino, files[0].name.encode())
        assert stat.S_ISREG(fattr.st_mode)
        assert fattr.st_size == 49 * 1024 * 1024
        return True

    assert trio.run(walk)


def test_lookup_missing_name():
    fs = _fs()
    with pytest.raises(Exception) as exc:
        trio.run(fs.lookup, ROOT_INODE, b"nope")
    assert getattr(exc.value, "errno", None) == errno.ENOENT


def test_file_size_uses_real_cached_size(tmp_path):
    """Once a segment is cached, getattr reports the real on-disk size."""
    tree = _tree()
    real = tmp_path / "cached.mp4"
    real.write_bytes(b"X" * 1234)

    class _Cache:
        def path_for(self, recording, view):
            return real

    fs = CameraFs(tree, cache=_Cache())
    # navigate root -> date -> mp4 -> file
    date = tree.list_children(ROOT_INODE)[0]
    mp4 = next(c for c in tree.list_children(date.inode) if c.name == "mp4")
    file_node = tree.list_children(mp4.inode)[0]
    attr = trio.run(fs.getattr, file_node.inode)
    assert attr.st_size == 1234  # real cached size, not the length estimate
