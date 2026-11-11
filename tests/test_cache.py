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
