"""Keep the mounted tree in sync with the camera while it is recording.

The camera lists the segment it is *currently writing* like any other, but its
end time and size keep growing and its ``FileName`` changes with them. Serving it
would freeze a truncated copy in the cache, so a segment is only published once it
is **settled**: either a newer segment exists, or its end time and size did not
change between two consecutive refreshes.

A refresh never makes a filesystem call wait: the camera query runs in a worker
thread, results are merged into the tree on the trio thread, and a failed query
(the camera often drops queries while a download is running) just skips the tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

import trio

from xmcam.models import Recording

from .tree import FsTree, MergeResult, slot_key

log = logging.getLogger("xmcamfs")

#: Query window overlap: re-ask for this much before the newest settled segment.
OVERLAP = timedelta(minutes=15)
#: The camera clock may run slightly ahead of ours; look a little into the future.
FUTURE_SLACK = timedelta(minutes=2)
#: Delay between a finished download and the refresh it triggers (camera needs to settle).
POKE_DELAY = 2.0
#: After a failed refresh, retry sooner than the regular interval.
RETRY_INTERVAL = 10.0
#: First refresh after mount: soon, so the newest segment (held back until it is
#: seen unchanged twice) appears quickly on an idle camera.
FIRST_INTERVAL = 15.0

#: How often the whole ``--days`` window is re-listed to notice recordings the camera overwrote.
FULL_EVERY = timedelta(minutes=15)
#: A recording must be missing from this many consecutive full listings before it is removed
#: (a truncated or glitchy reply must not make files flap).
MISSING_LIMIT = 2

#: ``fetch(start, end)`` -> recordings in that window; raises on any failure.
Fetch = Callable[[datetime, datetime], List[Recording]]


class Refresher:
    def __init__(
        self,
        tree: FsTree,
        fetch: Fetch,
        cache=None,
        interval: float = 60.0,
        days: int = 7,
        clock: Callable[[], datetime] = datetime.now,
        full_every: timedelta = FULL_EVERY,
        on_removed: Optional[Callable[[list], Awaitable[None]]] = None,
    ) -> None:
        self.tree = tree
        self.fetch = fetch
        self.cache = cache  # optional SegmentCache: stale files are dropped, busy state is honoured
        self.interval = interval
        self.days = days
        self._clock = clock
        self.full_every = full_every
        self.on_removed = on_removed  # async callback(list of (parent_inode, name, inode)) after pruning
        self._last_full = clock()  # the mount-time listing counts as a full one
        self._missing: Dict[tuple, int] = {}  # slot -> consecutive full listings without it
        self._pending: Dict[tuple, tuple] = {}  # slot -> (end_time, length) seen at the last refresh
        self._last_end: Optional[datetime] = None  # end of the newest settled segment
        self._wake = trio.Event()
        self._failing = False

    # -- pure logic -------------------------------------------------------------
    def settle(self, recs: Iterable[Recording]) -> List[Recording]:
        """Return the recordings that are safe to publish; remember the rest.

        All but the newest segment are finished by definition. The newest one is
        still being written unless it looked exactly the same last time.
        """
        ordered = sorted((r for r in recs if r.begin_time is not None), key=lambda r: (r.begin_time, r.filename))
        if not ordered:
            return []
        *older, newest = ordered
        settled = list(older)
        key = slot_key(newest)
        state = (newest.end_time, newest.length_bytes)
        if self._pending.get(key) == state:
            settled.append(newest)
            self._pending.pop(key, None)
        else:
            self._pending = {key: state}  # only the newest can be in flight
        return settled

    def apply(self, recs: Iterable[Recording]) -> MergeResult:
        """Settle *recs* and merge them into the tree (call on the trio thread)."""
        settled = self.settle(recs)
        result = self.tree.merge(settled)
        for node, old in result.updated:
            if self.cache is not None and old is not None:
                self._drop_stale(node, old)
        for rec in settled:
            edge = rec.end_time or rec.begin_time
            if edge is not None and (self._last_end is None or edge > self._last_end):
                self._last_end = edge
        return result

    def _drop_stale(self, node, old: Recording) -> None:
        """A known segment changed on the camera: any cached copy is outdated."""
        try:
            path = self.cache.path_for(old, node.view or "mp4")
            if path.exists():
                path.unlink()
                log.info("dropped outdated cache file %s", path.name)
        except Exception:  # noqa: BLE001 - best-effort
            pass

    def reconcile(self, recs: Iterable[Recording]) -> list:
        """Drop recordings that a full listing no longer reports; returns removed entries.

        The tree mirrors the last ``days`` days of the camera: footage the camera
        overwrote, or that slid out of the window, disappears. A recording must be
        absent from :data:`MISSING_LIMIT` consecutive full listings, and an empty
        listing never removes anything.
        """
        present = {slot_key(r) for r in recs if r.begin_time is not None}
        if not present:
            return []
        stale = []
        known = self.tree.slot_keys()
        for key in known:
            if key in present:
                self._missing.pop(key, None)
                continue
            self._missing[key] = self._missing.get(key, 0) + 1
            if self._missing[key] >= MISSING_LIMIT:
                stale.append(key)
        self._missing = {k: v for k, v in self._missing.items() if k in set(known) and k not in stale}
        if not stale:
            return []
        removed = self.tree.remove_slots(stale)
        log.info("removed %d recording(s) no longer on the camera", len(stale))
        return removed

    def window(self, full: bool = False) -> tuple:
        now = self._clock()
        floor = now - timedelta(days=self.days)
        start = floor if (full or self._last_end is None) else max(floor, self._last_end - OVERLAP)
        return start, now + FUTURE_SLACK

    # -- async driver -------------------------------------------------------------
    def poke(self) -> None:
        """Ask for a refresh soon (e.g. after a download finished). Trio thread only."""
        self._wake.set()

    async def tick(self) -> bool:
        """One refresh. Returns False if skipped or failed."""
        if self.cache is not None and self.cache.is_busy():
            return False  # the camera drops queries during a download; poke() will follow
        now = self._clock()
        full = now - self._last_full >= self.full_every
        start, end = self.window(full=full)
        try:
            recs = await trio.to_thread.run_sync(self.fetch, start, end)
        except Exception as exc:  # noqa: BLE001 - camera unreachable, busy, ...
            level = logging.WARNING if not self._failing else logging.DEBUG
            log.log(level, "refresh failed: %s", exc)
            self._failing = True
            return False
        if self._failing:
            log.info("refresh recovered")
            self._failing = False
        result = self.apply(recs)
        if result.added:
            log.info("refresh: %d new file(s)", len(result.added))
        if full:
            self._last_full = now
            removed = self.reconcile(recs)
            if removed and self.on_removed is not None:
                try:
                    await self.on_removed(removed)
                except Exception as exc:  # noqa: BLE001 - kernel cache invalidation is best-effort
                    log.debug("invalidation failed: %s", exc)
        return True

    async def run(self) -> None:
        """Refresh forever (cancel to stop)."""
        wait = min(self.interval, FIRST_INTERVAL)
        while True:
            woken = False
            with trio.move_on_after(wait):
                await self._wake.wait()
                woken = True
            if woken:
                self._wake = trio.Event()
                await trio.sleep(POKE_DELAY)
            ok = await self.tick()
            wait = self.interval if ok or self.interval < RETRY_INTERVAL else RETRY_INTERVAL
