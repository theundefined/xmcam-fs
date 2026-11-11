"""The progressive ts view: producer job, readers, close policy. Uses a real ffmpeg (skipped without)."""

from __future__ import annotations

import errno
import json
import os
import shutil
import struct
import subprocess
import threading
import time
from datetime import datetime

import pytest
import trio

from xmcam.media import have_ffmpeg
from xmcam.models import Credentials, Recording
from xmcamfs import stream as stream_mod
from xmcamfs.activity import ActivityTracker
from xmcamfs.cache import SegmentCache
from xmcamfs.fs import CameraFs
from xmcamfs.tree import FsTree

pytestmark = pytest.mark.skipif(not have_ffmpeg() or shutil.which("ffprobe") is None, reason="needs ffmpeg/ffprobe")

SECONDS = 4
FPS = 12


def _h264_access_units(tmp_path):
    """Encode a few seconds of test video; return the Annex-B access units (list of bytes)."""
    out = tmp_path / "v.h264"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={FPS}"]
    cmd += [
        "-t",
        str(SECONDS),
        "-c:v",
        "libx264",
        "-g",
        str(FPS),
        "-bf",
        "0",
        "-x264-params",
        "aud=1",
        "-f",
        "h264",
        str(out),
    ]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        pytest.skip("ffmpeg has no libx264")
    data = out.read_bytes()
    aud = b"\x00\x00\x00\x01\x09"
    parts = data.split(aud)
    return [aud + p for p in parts if p]


def _xm_blob(tmp_path):
    """The encoded video wrapped in Xiongmai frames, interleaved with 8 kHz A-law audio."""
    units = _h264_access_units(tmp_path)
    blob = bytearray()
    audio_done = 0
    for i, au in enumerate(units):
        is_idr = b"\x00\x00\x01\x65" in au or b"\x00\x00\x00\x01\x65" in au
        if is_idr:
            blob += b"\x00\x00\x01\xfc" + struct.pack("<BBBBII", 2, FPS, 40, 30, 0, len(au)) + au
        else:
            blob += b"\x00\x00\x01\xfd" + struct.pack("<I", len(au)) + au
        while audio_done / 25.0 <= (i + 1) / FPS and audio_done < SECONDS * 25:
            blob += b"\x00\x00\x01\xfa" + struct.pack("<BBH", 14, 2, 320) + b"\xd5" * 320  # A-law silence
            audio_done += 1
    return bytes(blob)


@pytest.fixture(scope="module")
def blob(tmp_path_factory):
    return _xm_blob(tmp_path_factory.mktemp("xm"))


def _rec(length):
    return Recording(
        filename="/idea0/2026-11-11/001/10.00.00-10.00.04[R][@abcd][0].h264",
        begin_time=datetime(2026, 11, 11, 10, 0, 0),
        end_time=datetime(2026, 11, 11, 10, 0, 4),
        length_bytes=length,
    )


class _Cam:
    def close(self):
        pass


def _cache(tmp_path, monkeypatch, blob, chunk=8192, gate=None, endless=False, on_close="cancel"):
    """A SegmentCache whose 'camera' replays *blob* (optionally pausing on *gate* before the tail)."""

    def fake_iter(cam, rec, channel=0, stream="Main", **kw):
        pos = 0
        while pos < len(blob):
            if gate is not None and pos >= len(blob) // 2:
                gate.wait(timeout=30)
            yield blob[pos : pos + chunk]
            pos += chunk
            if endless:
                time.sleep(0.05)
        while endless:  # never ends: the job must be cancelled
            time.sleep(0.05)
            yield b""

    monkeypatch.setattr(stream_mod, "iter_recording", fake_iter)
    monkeypatch.setattr(stream_mod, "PROBE_BYTES", 4096)  # the synthetic clip is tiny
    monkeypatch.setattr(SegmentCache, "_client", lambda self: _Cam())
    cache = SegmentCache(
        "192.0.2.10", Credentials("u", "p"), tmp_path / "cache", tracker=ActivityTracker(), ts_on_close=on_close
    )
    return cache


def _probe(path):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type,duration,start_time",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    ).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def test_ensure_ts_produces_a_playable_file_with_audio(tmp_path, monkeypatch, blob):
    cache = _cache(tmp_path, monkeypatch, blob)
    path = cache.ensure(_rec(len(blob)), "ts", "clip.ts")
    assert path.suffix == ".ts" and path.stat().st_size > 0
    assert not list(path.parent.glob("*.part"))
    streams = _probe(path)
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    assert abs(float(streams["video"]["duration"]) - SECONDS) < 0.6
    assert abs(float(streams["video"]["duration"]) - float(streams["audio"]["duration"])) < 0.5  # in sync
    assert abs(float(streams["video"]["start_time"]) - float(streams["audio"]["start_time"])) < 0.2
    # Second call is served from the cache: no new job.
    assert cache.ts_job(_rec(len(blob))) is None
    status = cache.tracker.snapshot()
    assert "Completed this session: 1" in status


def test_ts_is_readable_while_still_downloading(tmp_path, monkeypatch, blob):
    gate = threading.Event()
    cache = _cache(tmp_path, monkeypatch, blob, gate=gate)
    rec = _rec(len(blob))

    async def scenario():
        job = cache.ts_job(rec, "clip.ts")
        job.acquire()
        await job.wait_started()
        assert not job.done  # the tail is held back, yet the first bytes are already here
        fd = job.open_fd()
        head = await job.read(fd, 0, 188 * 4)
        assert head[0] == 0x47 and len(head) == 188 * 4
        assert cache.ts_job_active(rec)
        gate.set()
        pos = len(head)
        while True:  # keep reading to the end
            chunk = await job.read(fd, pos, 65536)
            if not chunk:
                break
            pos += len(chunk)
        os.close(fd)
        job.release()
        return pos

    total = trio.run(scenario)
    assert total == cache.path_for(rec, "ts").stat().st_size  # what was streamed is exactly what got cached
    assert not cache.ts_job_active(rec)


def test_cancel_when_last_reader_closes(tmp_path, monkeypatch, blob):
    cache = _cache(tmp_path, monkeypatch, blob, endless=True)
    monkeypatch.setattr(stream_mod, "CLOSE_GRACE", 0.3)
    rec = _rec(len(blob) * 100)

    async def scenario():
        job = cache.ts_job(rec, "clip.ts")
        job.grace = 0.3
        job.acquire()
        await job.wait_started()
        job.release()
        for _ in range(100):
            if job.done:
                break
            await trio.sleep(0.1)
        return job

    job = trio.run(scenario)
    assert job.done and job.error == "cancelled"
    assert not list((tmp_path / "cache" / "ts").iterdir())  # partial output removed
    assert "cancelled" in cache.tracker.snapshot() or "Active downloads: none" in cache.tracker.snapshot()


def test_finish_policy_keeps_downloading_after_close(tmp_path, monkeypatch, blob):
    gate = threading.Event()
    cache = _cache(tmp_path, monkeypatch, blob, gate=gate, on_close="finish")
    rec = _rec(len(blob))

    async def scenario():
        job = cache.ts_job(rec, "clip.ts")
        job.grace = 0.1
        job.acquire()
        await job.wait_started()
        job.release()
        await trio.sleep(0.5)  # well past the grace period
        assert not job.done and not job.cancelled
        gate.set()
        for _ in range(200):
            if job.done:
                break
            await trio.sleep(0.1)
        return job

    job = trio.run(scenario)
    assert job.done and job.error is None
    assert cache.path_for(rec, "ts").exists()


def test_fs_open_read_release_on_ts(tmp_path, monkeypatch, blob):
    gate = threading.Event()
    cache = _cache(tmp_path, monkeypatch, blob, gate=gate)
    rec = _rec(len(blob))
    tree = FsTree.from_recordings([rec])
    fs = CameraFs(tree, cache, tracker=cache.tracker)
    date = tree.lookup(1, "2026-11-11")
    ts_dir = tree.lookup(date.inode, "ts")
    node = tree.list_children(ts_dir.inode)[0]
    assert node.name.endswith(".ts")

    async def scenario():
        info = await fs.open(node.inode, os.O_RDONLY, None)
        assert info.direct_io and info.nonseekable  # streamed like a pipe while it is produced
        attr = await fs.getattr(node.inode)
        assert attr.attr_timeout == 0 and attr.st_size >= len(blob)  # estimate, never smaller than the real size
        data = await fs.read(info.fh, 0, 188 * 8)
        assert data[0] == 0x47
        gate.set()
        pos = 0
        while True:
            chunk = await fs.read(info.fh, pos, 1 << 20)
            if not chunk:
                break
            pos += len(chunk)
        await fs.release(info.fh)
        for _ in range(50):  # the job publishes the cached file just after the last byte
            if not cache.ts_job_active(rec):
                break
            await trio.sleep(0.05)
        final = await fs.getattr(node.inode)
        return pos, final

    pos, final = trio.run(scenario)
    assert final.st_size == pos == cache.path_for(rec, "ts").stat().st_size
    assert final.attr_timeout > 0

    # A later open is a normal cached, seekable file.
    async def reopen():
        info = await fs.open(node.inode, os.O_RDONLY, None)
        assert not info.nonseekable
        await fs.release(info.fh)

    trio.run(reopen)


def test_ts_open_fails_with_eio_when_ffmpeg_is_missing(tmp_path, monkeypatch, blob):
    cache = _cache(tmp_path, monkeypatch, blob)
    monkeypatch.setattr(stream_mod, "have_ffmpeg", lambda: False)
    rec = _rec(len(blob))
    tree = FsTree.from_recordings([rec])
    fs = CameraFs(tree, cache)
    node = tree.list_children(tree.lookup(tree.lookup(1, "2026-11-11").inode, "ts").inode)[0]
    with pytest.raises(Exception) as exc:
        trio.run(fs.open, node.inode, os.O_RDONLY, None)
    assert getattr(exc.value, "errno", None) == errno.EIO
