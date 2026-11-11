"""Run xmcam-fs in the background (classic double fork, with a readiness pipe).

The foreground process stays attached until the mount actually exists, so errors
(bad credentials, FUSE refusing the mount) still show up on the terminal. It must
be called before any thread, trio or pyfuse3 state exists.
"""

from __future__ import annotations

import os
import sys
from typing import Callable

#: Longest the launching process waits for the mount to come up.
READY_TIMEOUT = 60.0


def daemonize() -> Callable[[str], None]:
    """Detach from the terminal; return ``notify(message)`` for the detached process.

    In the launching process this never returns: it waits for the daemon to call
    ``notify("ok: ...")`` (exit 0) or ``notify("error: ...")`` (exit 1).
    In the detached process it returns ``notify``; call it once, after mounting.
    """
    read_fd, write_fd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid > 0:
        os.close(write_fd)
        _wait_for_daemon(read_fd, pid)  # does not return
    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)  # first child: only needed to drop the session leader role

    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)
    os.chdir("/")

    def notify(message: str) -> None:
        nonlocal write_fd
        if write_fd is None:
            return
        try:
            os.write(write_fd, message.encode("utf-8", "replace"))
            os.close(write_fd)
        except OSError:
            pass
        write_fd = None

    return notify


def _wait_for_daemon(read_fd: int, child_pid: int) -> None:
    import select

    os.waitpid(child_pid, 0)  # the first child exits immediately after its own fork
    ready, _, _ = select.select([read_fd], [], [], READY_TIMEOUT)
    data = os.read(read_fd, 65536).decode("utf-8", "replace") if ready else ""
    if data.startswith("ok"):
        print(data[2:].lstrip(": ").strip() or "mounted in the background")
        sys.exit(0)
    if data.startswith("error"):
        sys.exit(data.strip())
    sys.exit("xmcam-fs background process failed to start (see the log file)" if not ready else "background process exited")
