"""Offline tests for SegmentCache (download/convert are mocked, no network)."""

from __future__ import annotations

from datetime import datetime

import pytest

from xmcam.models import Credentials, Recording
from xmcamfs import cache as cache_mod
from xmcamfs.cache import SegmentCache, _safe_stem


class _DummyCam:
    def close(self):
        pass


def _rec():
    return Recording(
        filename="/idea0/2026-11-10/001/08.00.00-08.05.00[R][@4ab2][0].h264",
        begin_time=datetime(2026, 11, 10, 8, 0, 0),
        end_time=datetime(2026, 11, 10, 8, 5, 0),
        length_bytes=49 * 1024 * 1024,
    )


def _make_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(SegmentCache, "_client", lambda self: _DummyCam())
    return SegmentCache("192.0.2.10", Credentials("rryp", "TESTPASS"), tmp_path)


def test_safe_stem_strips_extension_and_brackets():
    stem = _safe_stem(_rec())
    assert "[" not in stem and "/" not in stem
    assert not stem.endswith(".h264")
    assert "@4ab2" in stem  # keeps the unique token


def test_path_for():
    cache = SegmentCache.__new__(SegmentCache)
    cache.cache_dir = __import__("pathlib").Path("/tmp/x")
    assert cache.path_for(_rec(), "raw").name.endswith(".xmraw")
    assert cache.path_for(_rec(), "mp4").name.endswith(".mp4")


def test_ensure_raw_downloads_once(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_download(cam, rec, out_path, progress=None, channel=0, stream="Main"):
        calls["n"] += 1
        with open(out_path, "wb") as fh:
            fh.write(b"RAWDATA")
        return 7

    monkeypatch.setattr(cache_mod, "download_recording", fake_download)
    cache = _make_cache(tmp_path, monkeypatch)

    path = cache.ensure(_rec(), "raw")
    assert path.exists() and path.read_bytes() == b"RAWDATA"
    # Second call is served from cache; no new download.
    cache.ensure(_rec(), "raw")
    assert calls["n"] == 1


def test_ensure_mp4_downloads_then_converts(tmp_path, monkeypatch):
    def fake_download(cam, rec, out_path, progress=None, channel=0, stream="Main"):
        with open(out_path, "wb") as fh:
            fh.write(b"XMRAW")
        return 5

    seen = {}

    def fake_convert(raw_path, mp4_path, keep_raw=False):
        # Regression guard: ffmpeg infers the muxer from the extension, so the
        # output path handed to conversion must really end in ".mp4".
        seen["out"] = str(mp4_path)
        with open(mp4_path, "wb") as fh:
            fh.write(b"MP4DATA")
        import os

        os.remove(raw_path)
        return True

    monkeypatch.setattr(cache_mod, "download_recording", fake_download)
    monkeypatch.setattr(cache_mod, "convert_raw", fake_convert)
    monkeypatch.setattr(cache_mod, "have_ffmpeg", lambda: True)
    cache = _make_cache(tmp_path, monkeypatch)

    path = cache.ensure(_rec(), "mp4")
    assert path.exists() and path.read_bytes() == b"MP4DATA"
    assert path.suffix == ".mp4"
    assert seen["out"].endswith(".mp4")


def test_ensure_mp4_requires_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "have_ffmpeg", lambda: False)
    monkeypatch.setattr(cache_mod, "download_recording", lambda *a, **k: 0)
    cache = _make_cache(tmp_path, monkeypatch)
    with pytest.raises(Exception):
        cache.ensure(_rec(), "mp4")


def test_parse_size():
    from xmcamfs.cache import parse_size

    assert parse_size("100M") == 100 * 1024**2
    assert parse_size("2g") == 2 * 1024**3
    assert parse_size("512k") == 512 * 1024
    assert parse_size("1234") == 1234
    assert parse_size("0") == 0
    with pytest.raises(ValueError):
        parse_size("lots")


def _seed(tmp_path, monkeypatch, max_bytes, files):
    """Create cache files as (subdir, name, size, age_seconds)."""
    import os
    import time

    cache = _make_cache(tmp_path, monkeypatch)
    cache.max_bytes = max_bytes
    for sub, name, size, age in files:
        p = tmp_path / sub / name
        p.write_bytes(b"x" * size)
        t = time.time() - age
        os.utime(p, (t, t))
    return cache


def test_evict_removes_least_recently_used_first(tmp_path, monkeypatch):
    cache = _seed(
        tmp_path,
        monkeypatch,
        250,
        [("raw", "old.xmraw", 100, 1000), ("mp4", "mid.mp4", 100, 500), ("raw", "new.xmraw", 100, 100)],
    )
    freed = cache.evict()
    assert freed == 100
    assert not (tmp_path / "raw" / "old.xmraw").exists()
    assert (tmp_path / "mp4" / "mid.mp4").exists() and (tmp_path / "raw" / "new.xmraw").exists()


def test_evict_never_removes_keep_or_recent_files(tmp_path, monkeypatch):
    cache = _seed(tmp_path, monkeypatch, 10, [("raw", "fresh.xmraw", 100, 1), ("raw", "keep.xmraw", 100, 1000)])
    cache.evict(keep=tmp_path / "raw" / "keep.xmraw")
    assert (tmp_path / "raw" / "fresh.xmraw").exists()  # inside the grace period
    assert (tmp_path / "raw" / "keep.xmraw").exists()  # explicitly protected


def test_evict_unlimited_and_part_files(tmp_path, monkeypatch):
    cache = _seed(tmp_path, monkeypatch, 0, [("raw", "a.xmraw", 100, 1000)])
    assert cache.evict() == 0
    cache.max_bytes = 10
    (tmp_path / "raw" / "dl.xmraw.part").write_bytes(b"x" * 100)
    import os

    os.utime(tmp_path / "raw" / "dl.xmraw.part", (1, 1))
    cache.evict()
    assert (tmp_path / "raw" / "dl.xmraw.part").exists()  # in-flight download is never touched
    assert not (tmp_path / "raw" / "a.xmraw").exists()


def test_ensure_evicts_after_producing(tmp_path, monkeypatch):
    def fake_download(cam, rec, out_path, progress=None, channel=0, stream="Main"):
        with open(out_path, "wb") as fh:
            fh.write(b"N" * 100)
        return 100

    monkeypatch.setattr(cache_mod, "download_recording", fake_download)
    cache = _seed(tmp_path, monkeypatch, 150, [("raw", "old.xmraw", 100, 1000)])
    path = cache.ensure(_rec(), "raw")
    assert path.exists()
    assert not (tmp_path / "raw" / "old.xmraw").exists()
