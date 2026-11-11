"""A tiny video-only MPEG-TS writer that stamps every frame with a presentation time.

Why: the raw Xiongmai stream carries no timestamps, and the camera's frame rate is
not constant (measured 11.96-12.11 fps against a nominal 12), so any fixed
``-framerate`` makes the picture drift against the audio by seconds over a
10-minute segment. The audio, on the other hand, is a clock: it is interleaved with
the video, so the audio received before a frame is the frame's capture time.
:class:`FrameClock` turns that into smooth timestamps, :class:`TsVideoWriter` puts
them into a TS that ffmpeg then muxes (``-c copy``) with the audio.
"""

from __future__ import annotations

from typing import Dict, Optional

PID_PMT = 0x1000
PID_VIDEO = 0x0100
_STREAM_TYPES = {"h264": 0x1B, "h265": 0x24, "mpeg4": 0x10}
#: PTS of the first frame; leaves room for the PCR to run slightly ahead of it.
START_OFFSET = 1.0


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def _section(table_id: int, body: bytes) -> bytes:
    """A PSI section: *body* starts with the 5-byte long-header remainder (id, version, section numbers)."""
    length = len(body) + 4  # everything after the length field: the body (incl. its 5-byte header) + CRC
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF])
    rest = head + body
    return rest + _crc32_mpeg(rest).to_bytes(4, "big")


class FrameClock:
    """Smooth frame timestamps that follow the audio clock.

    Each frame advances by one nominal frame period and is pulled towards the time
    implied by the audio received so far, with a small gain: the result has no
    duplicate or backwards steps (audio arrives in 40 ms chunks), tracks the true
    frame rate within a few tens of milliseconds, and never drifts.
    """

    def __init__(self, fps: float, gain: float = 0.03) -> None:
        self.period = 1.0 / max(fps, 1.0)
        self.gain = gain
        self._t: Optional[float] = None

    def next(self, audio_seconds: Optional[float]) -> float:
        """Timestamp (seconds from the start) of the next frame."""
        if self._t is None:
            self._t = 0.0
        else:
            t = self._t + self.period
            if audio_seconds is not None:
                t += self.gain * (audio_seconds - t)
            self._t = max(t, self._t + 0.001)
        return self._t


class TsVideoWriter:
    """Wrap video access units into MPEG-TS packets (one PES per frame, PTS = DTS)."""

    def __init__(self, codec: str) -> None:
        self.stream_type = _STREAM_TYPES.get(codec, _STREAM_TYPES["h265"])
        self._cc: Dict[int, int] = {}

    # -- public -------------------------------------------------------------------
    def tables(self) -> bytes:
        """PAT + PMT packets."""
        pat = _section(
            0x00,
            (1).to_bytes(2, "big") + b"\xc1\x00\x00" + (1).to_bytes(2, "big") + (0xE000 | PID_PMT).to_bytes(2, "big"),
        )
        pmt_body = (
            (1).to_bytes(2, "big")
            + b"\xc1\x00\x00"
            + (0xE000 | PID_VIDEO).to_bytes(2, "big")  # PCR PID
            + (0xF000).to_bytes(2, "big")  # no program info
            + bytes([self.stream_type])
            + (0xE000 | PID_VIDEO).to_bytes(2, "big")
            + (0xF000).to_bytes(2, "big")  # no ES info
        )
        pmt = _section(0x02, pmt_body)
        return self._packets(0x0000, b"\x00" + pat, psi=True) + self._packets(PID_PMT, b"\x00" + pmt, psi=True)

    def frame(self, data: bytes, seconds: float, key: bool = False) -> bytes:
        """One access unit stamped with *seconds* (from the start of the recording)."""
        pts = int(round((seconds + START_OFFSET) * 90000)) & 0x1FFFFFFFF
        pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + self._pts(pts) + data
        pcr = (pts - 9000) & 0x1FFFFFFFF  # the clock runs 100 ms ahead of the presentation time
        return self._packets(PID_VIDEO, pes, pcr=pcr, random_access=key)

    # -- internals -------------------------------------------------------------------
    @staticmethod
    def _pts(pts: int) -> bytes:
        return bytes(
            [
                0x21 | ((pts >> 29) & 0x0E),
                (pts >> 22) & 0xFF,
                0x01 | ((pts >> 14) & 0xFE),
                (pts >> 7) & 0xFF,
                0x01 | ((pts << 1) & 0xFE),
            ]
        )

    def _packets(
        self, pid: int, payload: bytes, pcr: Optional[int] = None, random_access: bool = False, psi: bool = False
    ) -> bytes:
        out = bytearray()
        if psi:
            payload = payload.ljust(184, b"\xff")  # tables are padded inside the payload, not with an adaptation field
        pos, n, first = 0, len(payload), True
        while pos < n:
            remaining = n - pos
            af = bytearray()
            if first and (pcr is not None or random_access):
                af.append((0x40 if random_access else 0) | (0x10 if pcr is not None else 0))
                if pcr is not None:
                    af += bytes(
                        [
                            (pcr >> 25) & 0xFF,
                            (pcr >> 17) & 0xFF,
                            (pcr >> 9) & 0xFF,
                            (pcr >> 1) & 0xFF,
                            ((pcr & 1) << 7) | 0x7E,
                            0x00,
                        ]
                    )
            af_total = (1 + len(af)) if af else 0  # the length byte plus its contents
            room = 184 - af_total
            if remaining < room:  # pad the packet with adaptation-field stuffing
                stuff = room - remaining
                if af_total == 0:
                    if stuff == 1:
                        af_total = 1  # a bare zero-length adaptation field
                    else:
                        af = bytearray([0x00]) + bytearray(b"\xff" * (stuff - 2))
                        af_total = stuff
                else:
                    af += b"\xff" * stuff
                    af_total += stuff
                room = remaining
            cc = self._cc.get(pid, 0)
            self._cc[pid] = (cc + 1) & 0x0F
            out += bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, (0x30 if af_total else 0x10) | cc])
            if af_total:
                out += bytes([af_total - 1]) + af
            out += payload[pos : pos + room]
            pos += room
            first = False
        return bytes(out)
