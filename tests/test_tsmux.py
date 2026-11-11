"""The video-only MPEG-TS writer and the audio-driven frame clock."""

from __future__ import annotations

import random

from xmcamfs.tsmux import PID_VIDEO, FrameClock, TsVideoWriter


def _packets(data: bytes):
    assert len(data) % 188 == 0
    return [data[i : i + 188] for i in range(0, len(data), 188)]


def test_packets_are_aligned_and_counted_per_pid():
    w = TsVideoWriter("h265")
    tables = _packets(w.tables())
    assert [(p[1] & 0x1F) << 8 | p[2] for p in tables] == [0x0000, 0x1000]
    assert all(p[0] == 0x47 for p in tables)

    frame_sizes = [1, 183, 184, 185, 400, 5000]  # exercise every stuffing case
    cc = []
    for n in frame_sizes:
        pkts = _packets(w.frame(b"\x11" * n, 1.0, key=n == 5000))
        assert pkts[0][1] & 0x40  # payload_unit_start on the first packet of a PES
        assert all(not (p[1] & 0x40) for p in pkts[1:])
        assert all(((p[1] & 0x1F) << 8 | p[2]) == PID_VIDEO for p in pkts)
        cc += [p[3] & 0x0F for p in pkts]
    assert all((b - a) % 16 == 1 for a, b in zip(cc, cc[1:]))  # continuity counter increments


def test_pes_carries_payload_and_pts():
    w = TsVideoWriter("h264")
    payload = bytes(range(256)) * 3
    pkts = _packets(w.frame(payload, 2.5, key=True))
    body = b""
    for p in pkts:
        afc = (p[3] >> 4) & 0x3
        off = 4
        if afc & 0x2:
            off += 1 + p[4]
        body += p[off:]
    assert body[:4] == b"\x00\x00\x01\xe0" and body[6:9] == b"\x80\x80\x05"
    pts = ((body[9] >> 1) & 0x07) << 30 | body[10] << 22 | (body[11] >> 1) << 15 | body[12] << 7 | body[13] >> 1
    assert pts == round((2.5 + 1.0) * 90000)  # START_OFFSET of one second
    assert body[14:] == payload
    assert pkts[0][5] & 0x40  # random access indicator for the key frame
    assert pkts[0][5] & 0x10  # PCR present


def test_frame_clock_follows_the_audio_clock_without_drift():
    rnd = random.Random(7)
    true_fps = 11.96  # the camera really runs at ~12, whatever the header says
    clock = FrameClock(12)
    last = -1.0
    err = []
    for k in range(7200):  # 10 minutes
        t_true = k / true_fps
        audio = int(t_true * 25) / 25.0  # audio arrives in 40 ms chunks
        t = clock.next(audio + rnd.uniform(0, 0.002))
        assert t > last  # strictly increasing: no duplicates, no going back
        last = t
        err.append(t - t_true)
    assert max(abs(e) for e in err[600:]) < 0.12  # follows the audio clock to within ~one audio chunk
    assert abs(err[-1]) < 0.1  # no accumulated drift (a fixed 12 fps would be 1.7 s off here)


def test_frame_clock_without_audio_uses_the_nominal_rate():
    clock = FrameClock(10)
    stamps = [clock.next(None) for _ in range(5)]
    assert [round(b - a, 6) for a, b in zip(stamps, stamps[1:])] == [0.1] * 4
