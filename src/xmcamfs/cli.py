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

from xmcam.credentials import recover_credentials
from xmcam.dvrip import DVRIP_PORT, DVRIPClient
from xmcam.exceptions import XMCamError
from xmcam.files import list_recordings_range
from xmcam.models import Credentials

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


def _load_recordings(args, creds: Credentials):
    end = datetime.now()
    start = end - timedelta(days=args.days)
    cam = DVRIPClient(args.host, user=creds.username, password=creds.password, port=args.port)
    if not cam.login():
        from xmcam.dvrip import RET_MEANING

        sys.exit(f"[{args.host}] DVRIP login failed ({RET_MEANING.get(cam.last_login_ret)}).")
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
    p.add_argument("--foreground", action="store_true", help="stay attached (default; Ctrl-C to unmount)")
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

    mountpoint = Path(args.mountpoint)
    if not mountpoint.is_dir():
        sys.exit(f"mountpoint {mountpoint} does not exist or is not a directory")

    # Imports that pull in pyfuse3/trio are deferred so --help works without FUSE.
    import pyfuse3
    import trio

    from .cache import SegmentCache
    from .fs import CameraFs
    from .tree import FsTree

    try:
        creds = _resolve_credentials(args)
        log.info("indexing recordings from the last %d day(s)...", args.days)
        recordings = _load_recordings(args, creds)
    except XMCamError as exc:
        sys.exit(f"Error: {exc}")

    tree = FsTree.from_recordings(recordings)
    log.info("indexed %d recording(s)", len(recordings))

    from .activity import ActivityTracker

    progress_stream = sys.stderr if (args.progress and sys.stderr.isatty()) else None
    tracker = ActivityTracker(progress_stream)

    cache_dir = Path(args.cache_dir) if args.cache_dir else _default_cache_dir(args.host)
    cache = SegmentCache(
        args.host,
        creds,
        cache_dir,
        port=args.port,
        channel=args.channel,
        stream=args.stream,
        tracker=tracker,
    )

    fs = CameraFs(tree, cache, tracker=tracker)
    log.info("status file: cat %s/.status", mountpoint)
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add(f"fsname=xmcam-{args.host}")
    fuse_options.add("ro")
    if args.debug_fuse:
        fuse_options.add("debug")

    pyfuse3.init(fs, str(mountpoint), fuse_options)
    log.info("mounted %s at %s (cache: %s)", args.host, mountpoint, cache_dir)
    try:
        trio.run(pyfuse3.main)
    except KeyboardInterrupt:
        log.info("unmounting...")
    except Exception:
        pyfuse3.close(unmount=False)
        raise
    finally:
        pyfuse3.close()


if __name__ == "__main__":
    main()
