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
