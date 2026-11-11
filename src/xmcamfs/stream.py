"""Progressive MPEG-TS rendering of a recording (the ``ts`` view).

The ``mp4`` view can only be handed out once the whole segment is downloaded and
converted (an MP4 keeps its index at the end of the file). MPEG-TS has no such
index, so a :class:`TsJob` feeds the downloaded Xiongmai frames through an
incremental demuxer into ``ffmpeg -c copy -f mpegts`` and appends the output to a
growing file; readers get the bytes as soon as they exist and wait for the rest.
The camera sends faster than a player consumes (measured 3-8x real time), so
playback can start a few seconds after ``open``.

Audio (G.711 A-law) is encoded to AAC, since MPEG-TS players do not agree on A-law.
Video frames get timestamps from the audio clock (see :mod:`xmcamfs.tsmux`), so picture
and sound stay in sync although the camera's frame rate is not constant.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import trio

from xmcam.exceptions import XMCamError
from xmcam.media import XmDemuxer, codec_from_es, have_ffmpeg
from xmcam.playback import iter_recording

from .tsmux import FrameClock, TsVideoWriter

log = logging.getLogger("xmcamfs")

#: Video bytes buffered before ffmpeg is started (the codec and the audio format must be known).
PROBE_BYTES = 64 * 1024
#: With ``on_close="cancel"``: how long a download keeps going after its last reader closed
#: (players often open, close and reopen a file while probing it).
CLOSE_GRACE = 15.0
#: A reader that waits this long without any new output gets EIO.
READ_STALL = 90.0
#: How long ``open`` waits for the first bytes (a download of another segment may hold the camera).
START_TIMEOUT = 600.0


class _Cancelled(Exception):
    pass


class _Mux:
    """An ``ffmpeg`` process turning raw video (and audio) elementary streams into MPEG-TS."""

    def __init__(self, job: "TsJob", audio_rate: Optional[int]):
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-probesize",
            "500000",
            "-analyzeduration",
            "1000000",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
        ]
        self._audio_w: Optional[int] = None
        pass_fds: tuple = ()
        audio_r = None
        if audio_rate:
            audio_r, self._audio_w = os.pipe()
            cmd += ["-f", "alaw", "-ar", str(audio_rate), "-ac", "1", "-i", f"pipe:{audio_r}"]
            pass_fds = (audio_r,)
        cmd += ["-c:v", "copy"]
        if audio_rate:
            cmd += ["-c:a", "aac", "-aac_coder", "fast", "-b:a", "32k"]
        cmd += ["-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1"]
        self.job = job
        self.stderr_tail = b""
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds
        )
        if audio_r is not None:
            os.close(audio_r)  # the child has its own copy
        self._video_q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._audio_q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._threads = [
            threading.Thread(target=self._pump_video, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
            threading.Thread(target=self._pump_output, daemon=True),
        ]
        if audio_rate:
            self._threads.append(threading.Thread(target=self._pump_audio, daemon=True))
        for t in self._threads:
            t.start()

    # -- feeding (producer thread) ---------------------------------------------
    def write(self, video: bytes, audio: bytes) -> None:
        if video:
            self._video_q.put(video)
        if audio and self._audio_w is not None:
            self._audio_q.put(audio)

    def finish(self) -> int:
        """No more input: close the pipes, wait for ffmpeg and for the output to be copied."""
        self._video_q.put(None)
        self._audio_q.put(None)
        rc = self.proc.wait()
        for t in self._threads:
            t.join(timeout=10)
        return rc

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        self._video_q.put(None)
        self._audio_q.put(None)
        self.proc.wait()
        for t in self._threads:
            t.join(timeout=5)

    # -- pipe pumps (threads) --------------------------------------------------------
    def _pump_video(self) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        try:
            while True:
                item = self._video_q.get()
                if item is None:
                    break
                stdin.write(item)
                stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass

    def _pump_audio(self) -> None:
        w = self._audio_w
        assert w is not None
        try:
            while True:
                item = self._audio_q.get()
                if item is None:
                    break
                view = memoryview(item)
                while view:
                    view = view[os.write(w, view) :]
        except OSError:
            pass
        finally:
            try:
                os.close(w)
            except OSError:
                pass

    def _pump_stderr(self) -> None:
        stderr = self.proc.stderr
        assert stderr is not None
        try:
            for line in stderr:
                self.stderr_tail = (self.stderr_tail + line)[-2000:]
        except (OSError, ValueError):
            pass

    def _pump_output(self) -> None:
        stdout = self.proc.stdout
        assert stdout is not None
        fd = stdout.fileno()
        with open(self.job.part, "wb") as out:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out.write(chunk)
                out.flush()
                self.job.written += len(chunk)
                self.job._progress = time.monotonic()


class TsJob:
    """One segment being rendered to ``<cache>/ts/<stem>.ts``; shared by all readers."""

    def __init__(self, cache, rec, display: str, dest: Path, on_close: str = "cancel", grace: float = CLOSE_GRACE):
        self.cache = cache
        self.rec = rec
        self.display = display
        self.dest = dest
        self.part = dest.with_name(dest.name + ".part")
        self.on_close = on_close
        self.grace = grace
        self.written = 0  # bytes of TS output available in ``part``
        self.done = False  # finished (successfully or not); see ``error``
        self.error: Optional[str] = None
        self.cancelled = False
        self.callbacks: List[Callable[["TsJob"], None]] = []  # called from the producer thread when done
        self._readers = 0
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._progress = time.monotonic()
        self._thread = threading.Thread(target=self._run, name=f"ts-{dest.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # -- readers (trio thread) ----------------------------------------------------------
    def acquire(self) -> None:
        with self._lock:
            self._readers += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def release(self) -> None:
        with self._lock:
            self._readers -= 1
            if self._readers <= 0 and not self.done and self.on_close == "cancel":
                self._timer = threading.Timer(self.grace, self._cancel_if_idle)
                self._timer.daemon = True
                self._timer.start()

    def _cancel_if_idle(self) -> None:
        with self._lock:
            if self._readers <= 0 and not self.done:
                log.info("no reader left, cancelling %s", self.display)
                self.cancelled = True

    def open_fd(self) -> int:
        try:
            return os.open(self.part, os.O_RDONLY)
        except FileNotFoundError:  # finished (renamed) in the meantime
            return os.open(self.dest, os.O_RDONLY)

    async def wait_started(self, timeout: float = START_TIMEOUT) -> None:
        """Wait until the first bytes exist; raises ``OSError`` if the job failed."""
        deadline = time.monotonic() + timeout
        while self.written <= 0 and not self.done:
            if time.monotonic() > deadline:
                raise OSError("timed out waiting for the camera")
            await trio.sleep(0.05)
        if self.written <= 0:
            raise OSError(self.error or "no data")

    async def read(self, fd: int, off: int, size: int) -> bytes:
        """Bytes ``[off, off+size)``; waits for the output to get there, empty at the end."""
        need = off + size
        while self.written < need and not self.done:
            if time.monotonic() - self._progress > READ_STALL:
                raise OSError("stalled")
            await trio.sleep(0.02)
        if self.written <= off:
            if self.error and self.error != "cancelled" and self.written <= off:
                raise OSError(self.error)
            return b""
        return os.pread(fd, min(size, self.written - off), off)

    # -- producer thread ---------------------------------------------------------------------
    def _run(self) -> None:
        cache = self.cache
        try:
            with cache._download_lock:  # one playback channel per camera
                if self.cancelled:
                    raise _Cancelled()
                self._produce()
                os.replace(self.part, self.dest)
        except _Cancelled:
            self.error = "cancelled"
        except Exception as exc:  # noqa: BLE001 - reported to readers as EIO
            log.error("ts %s failed: %s", self.display, exc)
            self.error = str(exc) or exc.__class__.__name__
        finally:
            if self.error:
                try:
                    os.remove(self.part)
                except OSError:
                    pass
            with cache._jobs_lock:
                if cache._jobs.get(self.dest) is self:
                    del cache._jobs[self.dest]
            self.done = True
            if self._timer is not None:
                self._timer.cancel()
            if not self.error:
                try:
                    cache.evict(keep=self.dest)
                except Exception:  # noqa: BLE001 - best-effort
                    pass
            for cb in self.callbacks:
                try:
                    cb(self)
                except Exception:  # noqa: BLE001
                    pass

    def _produce(self) -> None:
        cache, rec = self.cache, self.rec
        if not have_ffmpeg():
            raise XMCamError("ffmpeg not found; cannot produce the TS view")
        tracker = cache.tracker
        key = str(self.dest)
        if tracker is not None:
            tracker.start(key, self.display, "ts", rec.length_bytes)
        ok = False
        mux: Optional[_Mux] = None
        cam = cache._client()
        gen = iter_recording(cam, rec, channel=cache.channel, stream=cache.stream)
        try:
            dem = XmDemuxer()
            writer: Optional[TsVideoWriter] = None
            clock: Optional[FrameClock] = None
            pre_ts, pre_a = bytearray(), bytearray()  # output held back until ffmpeg is started
            received = 0

            def convert(events) -> tuple:
                """XM frames -> (video TS bytes, audio bytes), with timestamps from the audio clock."""
                nonlocal writer, clock
                ts, au = bytearray(), bytearray()
                rate = dem.audio_rate if dem.audio_codec == "alaw" else None
                for kind, payload, key, before in events:
                    if kind == "A":
                        au += payload
                        continue
                    if writer is None:
                        writer = TsVideoWriter(dem.media_codec or codec_from_es(payload) or "h265")
                        clock = FrameClock(dem.fps or 15)
                        ts += writer.tables()
                    elif key:
                        ts += writer.tables()
                    assert clock is not None
                    seconds = clock.next(before / float(rate) if rate else None)
                    ts += writer.frame(payload, seconds, key)
                return bytes(ts), bytes(au)

            for chunk in gen:
                if self.cancelled:
                    raise _Cancelled()
                received += len(chunk)
                if tracker is not None:
                    tracker.update(key, received)
                ts, audio = convert(dem.feed_events(chunk))
                if mux is None:
                    pre_ts += ts
                    pre_a += audio
                    if len(pre_ts) >= PROBE_BYTES:
                        mux = self._start_mux(dem, pre_ts, pre_a)
                        pre_ts, pre_a = bytearray(), bytearray()
                else:
                    mux.write(ts, audio)
            ts, audio = convert(dem.finish_events())
            if mux is None:
                pre_ts += ts
                pre_a += audio
                if writer is None:
                    raise XMCamError("no video in the recording")
                mux = self._start_mux(dem, pre_ts, pre_a)
            else:
                mux.write(ts, audio)
            rc = mux.finish()
            if rc != 0 or self.written <= 0:
                tail = mux.stderr_tail.decode("utf-8", "replace").strip()
                raise XMCamError(f"ffmpeg failed (rc={rc}): {tail}")
            ok = True
        except BaseException:
            if mux is not None:
                mux.kill()
            raise
        finally:
            try:
                gen.close()  # sends "Stop" to the camera
            except Exception:  # noqa: BLE001
                pass
            cam.close()
            if tracker is not None:
                size = self.written if ok else None
                tracker.finish(key, ok=ok, size=size, cancelled=self.cancelled)

    def _start_mux(self, dem: XmDemuxer, pre_ts: bytearray, pre_a: bytearray) -> _Mux:
        audio_rate = dem.audio_rate if (dem.audio_codec == "alaw" and pre_a) else None
        mux = _Mux(self, audio_rate)
        mux.write(bytes(pre_ts), bytes(pre_a))
        return mux
