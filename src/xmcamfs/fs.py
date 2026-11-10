"""pyfuse3 ``Operations`` that serve a camera's recordings as a read-only tree.

Directory metadata comes from an in-memory :class:`~xmcamfs.tree.FsTree`; file
contents are produced on demand by :class:`~xmcamfs.cache.SegmentCache`. The
slow work (downloading and converting a segment) happens on ``open`` in a worker
thread, so ``ls`` stays instant and the trio event loop never blocks.
"""
from __future__ import annotations

import errno
import logging
import os
import stat

import pyfuse3
import trio
from pyfuse3 import EntryAttributes, FileInfo, FUSEError

from .cache import SegmentCache
from .tree import FsTree, Node

log = logging.getLogger("xmcamfs")


class CameraFs(pyfuse3.Operations):
    def __init__(self, tree: FsTree, cache: SegmentCache) -> None:
        super().__init__()
        self.tree = tree
        self.cache = cache
        self._uid = os.getuid()
        self._gid = os.getgid()
        self._handles: dict[int, int] = {}  # fh -> os file descriptor
        self._next_fh = 1

    def _file_size(self, node: Node) -> int:
        """Exact size once the segment is cached, else the node's estimate.

        The estimate (``node.size`` = container length) is an upper bound, so a
        reader never truncates; once produced we report the real on-disk size so
        ``ls`` is accurate.
        """
        if node.recording is not None and self.cache is not None:
            try:
                path = self.cache.path_for(node.recording, node.view or "mp4")
                if path.exists():
                    real = path.stat().st_size
                    if real > 0:
                        return real
            except Exception:  # noqa: BLE001 - size is best-effort
                pass
        return node.size

    # -- attributes -----------------------------------------------------------
    def _attr(self, node: Node) -> EntryAttributes:
        e = EntryAttributes()
        e.st_ino = node.inode
        if node.is_dir:
            e.st_mode = stat.S_IFDIR | 0o555
            e.st_size = 0
            e.st_nlink = 2
        else:
            e.st_mode = stat.S_IFREG | 0o444
            e.st_size = self._file_size(node)
            e.st_nlink = 1
        stamp = int((node.mtime or 0.0) * 1e9)
        e.st_atime_ns = stamp
        e.st_mtime_ns = stamp
        e.st_ctime_ns = stamp
        e.st_uid = self._uid
        e.st_gid = self._gid
        e.st_blksize = 512
        e.st_blocks = (e.st_size + 511) // 512
        e.attr_timeout = 10
        e.entry_timeout = 10
        return e

    async def getattr(self, inode, ctx=None):
        node = self.tree.get(inode)
        if node is None:
            raise FUSEError(errno.ENOENT)
        return self._attr(node)

    async def lookup(self, parent_inode, name, ctx=None):
        name_s = os.fsdecode(name)
        if name_s == ".":
            node = self.tree.get(parent_inode)
        elif name_s == "..":
            parent = self.tree.get(parent_inode)
            node = self.tree.get(parent.parent) if parent else None
        else:
            node = self.tree.lookup(parent_inode, name_s)
        if node is None:
            raise FUSEError(errno.ENOENT)
        return self._attr(node)

    # -- directories ----------------------------------------------------------
    async def opendir(self, inode, ctx):
        node = self.tree.get(inode)
        if node is None:
            raise FUSEError(errno.ENOENT)
        if not node.is_dir:
            raise FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, inode, start_id, token):
        children = self.tree.list_children(inode)
        for idx in range(start_id, len(children)):
            node = children[idx]
            if not pyfuse3.readdir_reply(
                token, os.fsencode(node.name), self._attr(node), idx + 1
            ):
                break

    async def releasedir(self, fh):
        return

    # -- files ----------------------------------------------------------------
    async def open(self, inode, flags, ctx):
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise FUSEError(errno.EROFS)
        node = self.tree.get(inode)
        if node is None:
            raise FUSEError(errno.ENOENT)
        if node.is_dir:
            raise FUSEError(errno.EISDIR)

        # Produce (download + maybe convert) the segment off the event loop.
        try:
            path = await trio.to_thread.run_sync(self.cache.ensure, node.recording, node.view)
        except Exception as exc:  # noqa: BLE001 - surface any failure as EIO
            log.error("failed to produce %s: %s", node.name, exc)
            raise FUSEError(errno.EIO)

        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            log.error("cannot open cached file %s: %s", path, exc)
            raise FUSEError(errno.EIO)

        # Now the real size is known; correct the node so later stat() is exact.
        try:
            node.size = os.fstat(fd).st_size
        except OSError:
            pass

        fh = self._next_fh
        self._next_fh += 1
        self._handles[fh] = fd
        info = FileInfo(fh=fh)
        info.keep_cache = True
        return info

    async def read(self, fh, off, size):
        fd = self._handles.get(fh)
        if fd is None:
            raise FUSEError(errno.EBADF)
        try:
            return os.pread(fd, size, off)
        except OSError as exc:
            log.error("read error on fh %s: %s", fh, exc)
            raise FUSEError(errno.EIO)

    async def release(self, fh):
        fd = self._handles.pop(fh, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
