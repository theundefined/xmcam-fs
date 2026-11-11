"""Tests for the activity tracker (progress, ETA, snapshot)."""

from __future__ import annotations

from xmcamfs.activity import ActivityTracker, fmt_eta, human_bytes


def test_fmt_eta():
    assert fmt_eta(None) == "--"
    assert fmt_eta(5) == "00:05"
    assert fmt_eta(125) == "02:05"
    assert fmt_eta(3661) == "1:01:01"


def test_human_bytes():
    assert human_bytes(512) == "512.0B"
    assert human_bytes(2 * 1024 * 1024) == "2.0MB"


def test_snapshot_lifecycle():
    t = ActivityTracker()
    t.access("08.00.00-08.05.00.mp4", "mp4", hit=True)
    t.start("k", "08.10.24-08.10.41.M.mp4", "mp4", 2_600_000)
    t.update("k", 1_300_000)

    snap = t.snapshot()
    assert "Active downloads:" in snap
    assert "08.10.24-08.10.41.M.mp4" in snap
    assert "ETA" in snap
    assert "Recent access:" in snap
    assert "08.00.00-08.05.00.mp4" in snap

    t.finish("k")
    snap2 = t.snapshot()
    assert "Active downloads: none" in snap2
    assert "Completed this session: 1" in snap2


def test_converting_phase_shown():
    t = ActivityTracker()
    t.start("k", "clip.mp4", "mp4", 1000)
    t.phase("k", "converting")
    assert "converting" in t.snapshot()


def test_recent_access_shows_size_and_speed(monkeypatch):
    import xmcamfs.activity as act

    clock = {"t": 100.0}
    monkeypatch.setattr(act.time, "monotonic", lambda: clock["t"])
    t = ActivityTracker()
    t.access("cached.mp4", "mp4", hit=True, size=3 * 1024 * 1024)
    t.access("clip.mp4", "mp4", hit=False)
    t.start("k", "clip.mp4", "mp4", 2_000_000)
    assert "fetch  ..." in t.snapshot()

    clock["t"] = 110.0  # 10 s of downloading 2 MB ...
    t.update("k", 2 * 1024 * 1024)
    t.phase("k", "converting")
    clock["t"] = 150.0  # ... then 40 s of conversion must not count towards the speed
    t.finish("k", ok=True, size=2_500_000)

    snap = t.snapshot()
    assert "cached.mp4  [mp4]  hit  3.0MB" in snap
    line = next(ln for ln in snap.splitlines() if "clip.mp4" in ln and "fetch" in ln)
    assert "2.4MB" in line  # final file size
    assert "204.8KB/s" in line  # 2 MiB / 10 s


def test_failed_fetch_is_marked():
    t = ActivityTracker()
    t.access("bad.mp4", "mp4", hit=False)
    t.start("k", "bad.mp4", "mp4", 1000)
    t.finish("k", ok=False)
    snap = t.snapshot()
    assert "fetch  FAILED" in snap
    assert "Completed this session: 0" in snap
