"""The motion marker (".M") must appear in FUSE file names for motion clips."""

from __future__ import annotations

from datetime import datetime

from xmcam.models import Recording
from xmcamfs.tree import ROOT_INODE, FsTree


def _rec(h, m, s, event):
    begin = datetime(2026, 11, 10, h, m, s)
    end = datetime(2026, 11, 10, h, m, s + 5)
    return Recording(
        filename=f"/idea0/2026-11-10/001/{h:02}.{m:02}.{s:02}-..[{event}][@x][0].h264",
        begin_time=begin,
        end_time=end,
        length_bytes=1000,
        event=event,
    )


def test_motion_files_are_tagged():
    tree = FsTree.from_recordings([_rec(8, 0, 0, "R"), _rec(8, 10, 24, "M")])
    date = tree.list_children(ROOT_INODE)[0]
    mp4 = next(c for c in tree.list_children(date.inode) if c.name == "mp4")
    names = {n.name for n in tree.list_children(mp4.inode)}
    assert "08.00.00-08.00.05.mp4" in names  # regular: unmarked
    assert "08.10.24-08.10.29.M.mp4" in names  # motion: ".M" marker
    # raw view mirrors the marker.
    raw = next(c for c in tree.list_children(date.inode) if c.name == "raw")
    raw_names = {n.name for n in tree.list_children(raw.inode)}
    assert "08.10.24-08.10.29.M.xmraw" in raw_names
