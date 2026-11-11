"""Refresher: settling of the in-flight segment, window, failure tolerance."""

from datetime import datetime, timedelta

import trio

from xmcam.models import Recording
from xmcamfs.refresh import Refresher
from xmcamfs.tree import ROOT_INODE, FsTree

NOW = datetime(2026, 11, 11, 19, 45, 0)


def rec(begin_min, end_min, token, length=100):
    b = datetime(2026, 11, 11, 19, begin_min, 0)
    e = datetime(2026, 11, 11, 19, end_min, 0)
    return Recording(
        filename=f"/idea0/2026-11-11/001/{b:%H.%M.%S}-{e:%H.%M.%S}[R][@{token}][0].h264",
        begin_time=b,
        end_time=e,
        length_bytes=length,
    )


def listing(tree):
    d = tree.lookup(ROOT_INODE, "2026-11-11")
    if d is None:
        return []
    return [n.name for n in tree.list_children(tree.lookup(d.inode, "raw").inode)]


class FakeCache:
    def __init__(self, busy=False):
        self.busy = busy

    def is_busy(self):
        return self.busy


def make(fetch, cache=None):
    return Refresher(FsTree(), fetch, cache=cache, interval=60, days=7, clock=lambda: NOW)


def test_newest_segment_is_held_back_until_unchanged():
    r = make(lambda s, e: [])
    done, live = rec(30, 35, "a1"), rec(35, 40, "a2")
    r.apply([done, live])
    assert listing(r.tree) == ["19.30.00-19.35.00.xmraw"]  # live one hidden
    r.apply([done, rec(35, 41, "a2", length=150)])  # still growing
    assert listing(r.tree) == ["19.30.00-19.35.00.xmraw"]
    r.apply([done, rec(35, 41, "a2", length=150)])  # unchanged twice -> settled
    assert listing(r.tree) == ["19.30.00-19.35.00.xmraw", "19.35.00-19.41.00.xmraw"]


def test_segment_settles_as_soon_as_a_newer_one_exists():
    r = make(lambda s, e: [])
    r.apply([rec(30, 35, "a1")])
    assert listing(r.tree) == []
    r.apply([rec(30, 35, "a1"), rec(35, 40, "a2")])
    assert listing(r.tree) == ["19.30.00-19.35.00.xmraw"]


def test_window_starts_before_newest_settled_segment():
    r = make(lambda s, e: [])
    assert r.window()[0] == NOW - timedelta(days=7)
    r.apply([rec(30, 35, "a1"), rec(35, 40, "a2")])
    start, end = r.window()
    assert start == datetime(2026, 11, 11, 19, 35, 0) - timedelta(minutes=15)
    assert end > NOW


def test_tick_merges_fetched_recordings():
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return [rec(30, 35, "a1"), rec(35, 40, "a2")]

    r = make(fetch)
    assert trio.run(r.tick) is True
    assert listing(r.tree) == ["19.30.00-19.35.00.xmraw"]
    assert len(calls) == 1


def test_tick_survives_camera_errors():
    def fetch(start, end):
        raise OSError("connection closed while reading reply")

    r = make(fetch)
    assert trio.run(r.tick) is False
    assert listing(r.tree) == []


def test_tick_is_skipped_while_downloading():
    calls = []
    r = make(lambda s, e: calls.append(1) or [], cache=FakeCache(busy=True))
    assert trio.run(r.tick) is False
    assert calls == []


def test_outdated_cache_file_is_dropped_when_a_known_segment_changes(tmp_path):
    class Cache(FakeCache):
        def path_for(self, rec, view):
            return tmp_path / f"{view}-{rec.length_bytes}"

    cache = Cache()
    r = make(lambda s, e: [], cache=cache)
    older = rec(25, 30, "a0")
    r.apply([older, rec(30, 35, "a1", length=100), rec(35, 40, "a2")])  # a1 settled
    stale = tmp_path / "raw-100"
    stale.write_bytes(b"x")
    r.apply([older, rec(30, 35, "a1", length=120), rec(35, 40, "a2")])  # camera reports a different size
    assert not stale.exists()
