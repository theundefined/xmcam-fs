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
import time

import pyfuse3
import trio
from pyfuse3 import EntryAttributes, FileInfo, FUSEError

from .cache import SegmentCache
from .tree import ROOT_INODE, FsTree, Node

log = logging.getLogger("xmcamfs")

#: Virtual status file at the mount root (``cat <mnt>/.status``).
STATUS_NAME = ".status"
#: Reserved inode for the status file; the recording tree never reaches it.
STATUS_INODE = 2**31 - 1


class CameraFs(pyfuse3.Operations):
    def __init__(self, tree: FsTree, cache: SegmentCache, tracker=None, refresher=None) -> None:
        super().__init__()
        self.tree = tree
        self.cache = cache
        self.tracker = tracker  # optional ActivityTracker
        self.refresher = refresher  # optional Refresher; poked when a download finishes
        self._uid = os.getuid()
        self._gid = os.getgid()
        self._handles: dict[int, int] = {}  # fh -> os file descriptor
        self._ts_jobs: dict[int, object] = {}  # fh -> TsJob, for handles on a ts file that is still being produced
        self._status_buffers: dict[int, bytes] = {}  # fh -> status snapshot
        self._next_fh = 1

    async def invalidate_removed(self, removed) -> None:
        """Make the kernel forget entries the tree dropped (recordings overwritten on the camera)."""
        for parent, name, inode in removed:
            try:
                pyfuse3.invalidate_entry_async(parent, os.fsencode(name), deleted=inode, ignore_enoent=True)
            except Exception as exc:  # noqa: BLE001 - e.g. entry never cached
                log.debug("could not invalidate %s: %s", name, exc)

    # -- status virtual file --------------------------------------------------
    def _status_bytes(self) -> bytes:
        if self.tracker is not None:
            return self.tracker.snapshot().encode("utf-8")
        return b"xmcam-fs: activity tracking is disabled (mount with --progress to enable).\n"

    def _status_attr(self) -> EntryAttributes:
        e = EntryAttributes()
        e.st_ino = STATUS_INODE
        e.st_mode = stat.S_IFREG | 0o444
        e.st_size = len(self._status_bytes())
        e.st_nlink = 1
        now = int(time.time() * 1e9)
        e.st_atime_ns = e.st_mtime_ns = e.st_ctime_ns = now
        e.st_uid = self._uid
        e.st_gid = self._gid
        e.st_blksize = 512
        e.st_blocks = (e.st_size + 511) // 512
        e.attr_timeout = 0  # dynamic content: never cache
        e.entry_timeout = 0
        return e

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
        if not node.is_dir and node.view == "ts" and self._ts_in_progress(node):
            e.attr_timeout = 0  # the size estimate is replaced by the real one when it finishes
        return e

    def _ts_in_progress(self, node: Node) -> bool:
        try:
            return self.cache is not None and self.cache.ts_job_active(node.recording)
        except Exception:  # noqa: BLE001 - best-effort
            return False

    async def getattr(self, inode, ctx=None):
        if inode == STATUS_INODE:
            return self._status_attr()
        node = self.tree.get(inode)
        if node is None:
            raise FUSEError(errno.ENOENT)
        return self._attr(node)

    async def lookup(self, parent_inode, name, ctx=None):
        name_s = os.fsdecode(name)
        if parent_inode == ROOT_INODE and name_s == STATUS_NAME:
            return self._status_attr()
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
        entries = [(os.fsencode(n.name), self._attr(n)) for n in self.tree.list_children(inode)]
        if inode == ROOT_INODE:  # expose the status file (visible with ls -a)
            entries.append((os.fsencode(STATUS_NAME), self._status_attr()))
        for idx in range(start_id, len(entries)):
            name_b, attr = entries[idx]
            if not pyfuse3.readdir_reply(token, name_b, attr, idx + 1):
                break

    async def releasedir(self, fh):
        return

    # -- files ----------------------------------------------------------------
    async def open(self, inode, flags, ctx):
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise FUSEError(errno.EROFS)

        if inode == STATUS_INODE:
            fh = self._next_fh
            self._next_fh += 1
            self._status_buffers[fh] = self._status_bytes()
            info = FileInfo(fh=fh)
            info.direct_io = True  # dynamic content; don't let the kernel cache it
            return info

        node = self.tree.get(inode)
        if node is None:
            raise FUSEError(errno.ENOENT)
        if node.is_dir:
            raise FUSEError(errno.EISDIR)

        # Record the access (and whether the segment is already cached).
        hit = False
        cached_size = None
        try:
            cached = self.cache.path_for(node.recording, node.view or "mp4")
            cached_size = cached.stat().st_size if cached.exists() else None
            hit = bool(cached_size)
        except Exception:  # noqa: BLE001 - best-effort
            hit = False
        if self.tracker is not None:
            self.tracker.access(node.name, node.view or "mp4", hit, size=cached_size)

        if node.view == "ts" and not hit:
            return await self._open_ts(node)

        # Produce (download + maybe convert) the segment off the event loop.
        try:
            path = await trio.to_thread.run_sync(self.cache.ensure, node.recording, node.view, node.name)
        except Exception as exc:  # noqa: BLE001 - surface any failure as EIO
            log.error("failed to produce %s: %s", node.name, exc)
            if self.refresher is not None and not hit:
                self.refresher.poke()
            raise FUSEError(errno.EIO)
        if self.refresher is not None and not hit:
            self.refresher.poke()  # the camera is free again: catch up on new recordings

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

    async def _open_ts(self, node: Node) -> FileInfo:
        """Open a ts file that is not cached yet: stream it while it is being produced.

        Returns as soon as the first bytes exist. The handle is non-seekable and uses
        direct I/O, so players read it like a pipe (no probing of the not yet existing
        end of the file) and the kernel never serves a not yet complete page.
        """
        token = trio.lowlevel.current_trio_token()
        try:
            job = self.cache.ts_job(node.recording, node.name)
            if job is None:  # finished and cached in the meantime
                return await self._open_cached(node)
            job.acquire()
            try:
                await job.wait_started()
                fd = job.open_fd()
            except BaseException:
                job.release()
                raise
        except FUSEError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("failed to open %s: %s", node.name, exc)
            raise FUSEError(errno.EIO)
        if self.refresher is not None:
            job.callbacks.append(lambda _job: token.run_sync_soon(self.refresher.poke))
        fh = self._next_fh
        self._next_fh += 1
        self._handles[fh] = fd
        self._ts_jobs[fh] = job
        info = FileInfo(fh=fh)
        info.direct_io = True
        info.nonseekable = True
        return info

    async def _open_cached(self, node: Node) -> FileInfo:
        path = self.cache.path_for(node.recording, node.view or "mp4")
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            log.error("cannot open cached file %s: %s", path, exc)
            raise FUSEError(errno.EIO)
        fh = self._next_fh
        self._next_fh += 1
        self._handles[fh] = fd
        info = FileInfo(fh=fh)
        info.keep_cache = True
        return info

    async def read(self, fh, off, size):
        buf = self._status_buffers.get(fh)
        if buf is not None:
            return buf[off : off + size]
        fd = self._handles.get(fh)
        if fd is None:
            raise FUSEError(errno.EBADF)
        job = self._ts_jobs.get(fh)
        if job is not None:
            try:
                return await job.read(fd, off, size)
            except OSError as exc:
                log.error("ts read error on fh %s: %s", fh, exc)
                raise FUSEError(errno.EIO)
        try:
            return os.pread(fd, size, off)
        except OSError as exc:
            log.error("read error on fh %s: %s", fh, exc)
            raise FUSEError(errno.EIO)

    async def release(self, fh):
        self._status_buffers.pop(fh, None)
        job = self._ts_jobs.pop(fh, None)
        if job is not None:
            job.release()
        fd = self._handles.pop(fh, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
