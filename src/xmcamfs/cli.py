"""Command-line entry point: mount a camera as a FUSE filesystem.

Usage::

    xmcam-fs <camera-ip> <mountpoint> [--days N] [--stream Main|Sub] ...

Credentials come from ``--user/--password`` or ``XMCAM_USER``/``XMCAM_PASS``;
if absent they are recovered from anonymous ONVIF (the trick the iCSee app uses),
which only works on cameras you own.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from xmcam.credentials import recover_credentials
from xmcam.dvrip import DVRIP_PORT, DVRIPClient
from xmcam.exceptions import XMCamError
from xmcam.files import list_recordings_range
from xmcam.models import Credentials, Recording

log = logging.getLogger("xmcamfs")


def _resolve_credentials(args) -> Credentials:
    user = args.user or os.environ.get("XMCAM_USER")
    password = args.password if args.password is not None else os.environ.get("XMCAM_PASS")
    if password:
        return Credentials(username=user or "admin", password=password, source="manual")
    creds = recover_credentials(args.host, port=args.onvif_port)
    if creds is None:
        sys.exit(
            f"[{args.host}] no password given and ONVIF recovery failed; "
            "pass --user/--password or set XMCAM_USER/XMCAM_PASS."
        )
    log.info("recovered credentials from anonymous ONVIF (user=%s)", creds.username)
    return creds


def _default_cache_dir(host: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "xmcam-fs" / host


def _default_log_file(host: str) -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "xmcam-fs" / f"{host}.log"


def fetch_recordings(args, creds: Credentials, start: datetime, end: datetime) -> List[Recording]:
    """Query the camera for recordings in a window (raises :class:`XMCamError` on failure)."""
    cam = DVRIPClient(args.host, user=creds.username, password=creds.password, port=args.port)
    cam.login_or_raise()
    try:
        return list_recordings_range(
            cam,
            start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
            channel=args.channel,
            stream=args.stream,
        )
    finally:
        cam.close()


def _load_recordings(args, creds: Credentials):
    end = datetime.now()
    return fetch_recordings(args, creds, end - timedelta(days=args.days), end)


def _size_arg(text: str) -> int:
    from .cache import parse_size

    try:
        return parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xmcam-fs",
        description="Mount a Xiongmai / iCSee / XMeye camera as a FUSE filesystem (read-only).",
    )
    p.add_argument("host", help="camera IP address")
    p.add_argument("mountpoint", help="existing empty directory to mount on")
    p.add_argument("--days", type=int, default=7, help="how many past days to index (default 7)")
    p.add_argument("--stream", default="Main", choices=["Main", "Sub"])
    p.add_argument("--channel", type=int, default=0)
    p.add_argument("--port", type=int, default=DVRIP_PORT, help="DVRIP port (default 34567)")
    p.add_argument("--onvif-port", type=int, default=8899, dest="onvif_port")
    p.add_argument("--user", help="username (or $XMCAM_USER)")
    p.add_argument("--password", help="password (or $XMCAM_PASS)")
    p.add_argument("--cache-dir", dest="cache_dir", help="where to cache downloaded segments")
    p.add_argument(
        "--cache-max-size",
        dest="cache_max_size",
        default="500M",
        type=_size_arg,
        help="soft cap on cached footage; least recently used segments are evicted (default 500M, e.g. 2G, 0 = unlimited)",
    )
    p.add_argument(
        "--refresh",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="how often to look for new recordings on the camera (default 60, 0 = never)",
    )
    p.add_argument("--foreground", action="store_true", help="stay attached (default; Ctrl-C to unmount)")
    p.add_argument(
        "-b",
        "--background",
        action="store_true",
        help="detach after mounting and keep running in the background (stop with: fusermount3 -u MOUNTPOINT)",
    )
    p.add_argument("--log-file", dest="log_file", help="log file for --background (default ~/.local/state/xmcam-fs/<host>.log)")
    p.add_argument(
        "--progress", action="store_true", help="show live download progress with ETA (when run in a terminal)"
    )
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--debug-fuse", action="store_true", dest="debug_fuse", help="FUSE protocol debug")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.background and args.foreground:
        sys.exit("--background and --foreground are mutually exclusive")

    # Absolute paths up front: a background process changes its working directory.
    mountpoint = Path(args.mountpoint).resolve()
    if not mountpoint.is_dir():
        sys.exit(f"mountpoint {mountpoint} does not exist or is not a directory")

    # Imports that pull in pyfuse3/trio are deferred so --help works without FUSE.
    import pyfuse3
    import trio

    from .cache import SegmentCache
    from .fs import CameraFs
    from .refresh import Refresher
    from .tree import FsTree

    try:
        creds = _resolve_credentials(args)
        log.info("indexing recordings from the last %d day(s)...", args.days)
        recordings = _load_recordings(args, creds)
    except XMCamError as exc:
        sys.exit(f"Error: {exc}")

    notify = None
    if args.background:
        log_file = Path(args.log_file).resolve() if args.log_file else _default_log_file(args.host)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        from .daemon import daemonize

        notify = daemonize()  # the launching process waits in here and exits; we are the daemon now
        logging.basicConfig(
            filename=str(log_file),
            level=logging.DEBUG if args.debug else logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            force=True,
        )
        log.info("xmcam-fs %s starting in the background (pid %d)", args.host, os.getpid())

    try:
        from .activity import ActivityTracker

        progress_stream = sys.stderr if (args.progress and sys.stderr.isatty() and not args.background) else None
        tracker = ActivityTracker(progress_stream)

        cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else _default_cache_dir(args.host)
        cache = SegmentCache(
            args.host,
            creds,
            cache_dir,
            port=args.port,
            channel=args.channel,
            stream=args.stream,
            tracker=tracker,
            max_bytes=args.cache_max_size,
        )

        tree = FsTree()
        refresher = Refresher(
            tree,
            lambda start, end: fetch_recordings(args, creds, start, end),
            cache=cache,
            interval=args.refresh,
            days=args.days,
        )
        refresher.apply(recordings)  # newest segment is held back until it stops growing
        log.info("indexed %d recording(s)", len(recordings))

        fs = CameraFs(tree, cache, tracker=tracker, refresher=refresher if args.refresh > 0 else None)
        refresher.on_removed = fs.invalidate_removed
        log.info("status file: cat %s/.status", mountpoint)
        fuse_options = set(pyfuse3.default_options)
        fuse_options.add(f"fsname=xmcam-{args.host}")
        fuse_options.add("ro")
        if args.debug_fuse:
            fuse_options.add("debug")

        pyfuse3.init(fs, str(mountpoint), fuse_options)
    except BaseException as exc:
        if notify is not None:
            log.error("startup failed: %s", exc)
            notify(f"error: {exc}")
        raise
    log.info("mounted %s at %s (cache: %s, limit: %s)", args.host, mountpoint, cache_dir, _fmt_limit(args.cache_max_size))
    if notify is not None:
        notify(f"ok: mounted {args.host} at {mountpoint} in the background (pid {os.getpid()}, log: {log_file})")

    async def serve() -> None:
        async with trio.open_nursery() as nursery:
            if args.refresh > 0:
                nursery.start_soon(refresher.run)
            await pyfuse3.main()
            nursery.cancel_scope.cancel()

    try:
        trio.run(serve)
    except KeyboardInterrupt:
        log.info("unmounting...")
    except Exception:
        pyfuse3.close(unmount=False)
        raise
    finally:
        pyfuse3.close()


def _fmt_limit(n: int) -> str:
    from .activity import human_bytes

    return human_bytes(n) if n > 0 else "unlimited"


if __name__ == "__main__":
    main()
