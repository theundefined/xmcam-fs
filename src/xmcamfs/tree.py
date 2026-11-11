"""Pure, read-only in-memory inode tree for the xmcam-fs FUSE layer.

This module has no FUSE, network or pyfuse3 dependency. It turns a flat list
of :class:`xmcam.models.Recording` objects into a tree of :class:`Node`
objects that the FUSE layer can serve directly.

Layout::

    /                             (root, inode ROOT_INODE)
      <YYYY-MM-DD>/               one directory per distinct begin date
        mp4/                      view directory
          HH.MM.SS-HH.MM.SS.mp4    a regular/continuous recording
          HH.MM.SS-HH.MM.SS.M.mp4  a motion-triggered recording (".M" marker)
        raw/                      view directory
          HH.MM.SS-HH.MM.SS.xmraw
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from xmcam.models import Recording

ROOT_INODE = 1

VIEW_MP4 = "mp4"
VIEW_RAW = "raw"
VIEWS = (VIEW_MP4, VIEW_RAW)

_EXTENSIONS = {VIEW_MP4: ".mp4", VIEW_RAW: ".xmraw"}


@dataclass
class Node:
    inode: int
    name: str  # basename (ascii-safe)
    kind: str  # "root" | "date" | "view" | "file"
    parent: int
    is_dir: bool
    children: dict = field(default_factory=dict)  # name -> inode (dirs only)
    recording: Optional[Recording] = None  # file nodes only
    view: Optional[str] = None  # "mp4" | "raw"
    size: int = 0  # bytes (file nodes)
    mtime: float = 0.0  # epoch seconds


_TOKEN_RE = re.compile(r"\[@([0-9A-Za-z]+)\]")


def slot_key(rec: Recording) -> tuple:
    """Identity of a recording that stays stable while the camera is still writing it.

    The camera's ``FileName`` embeds the segment's *end* time, so it changes while a
    segment grows (``19.41.55-19.44.10[R][@71db]`` -> ``...-19.44.20[R][@71db]``).
    The begin time plus the ``[@token]`` do not change, so they identify the
    segment. Names without a token (older firmware, tests) fall back to the filename.
    """
    match = _TOKEN_RE.search(rec.filename or "")
    ident = match.group(1) if match else rec.filename
    return (rec.begin_time, ident, rec.channel)


def _base_name(rec: Recording) -> str:
    """Return ``HH.MM.SS-HH.MM.SS`` for a recording (without extension).

    Non-regular events are tagged so they stand out in a directory listing:
    a motion clip becomes ``HH.MM.SS-HH.MM.SS.M`` (and e.g. ``.A`` for alarm),
    while regular/continuous recordings are left unmarked.
    """
    end = rec.end_time if rec.end_time is not None else rec.begin_time
    base = f"{rec.begin_time:%H.%M.%S}-{end:%H.%M.%S}"
    event = getattr(rec, "event", "")
    if event and event != "R":
        base += f".{event}"
    return base


@dataclass
class MergeResult:
    """What :meth:`FsTree.merge` changed."""

    added: List[Node] = field(default_factory=list)  # new file nodes
    updated: List[Tuple[Node, Optional[Recording]]] = field(default_factory=list)  # (node, previous recording)

    def __bool__(self) -> bool:
        return bool(self.added or self.updated)


class FsTree:
    """Inode tree built from recordings. Read-only for users; grows via :meth:`merge`."""

    def __init__(self) -> None:
        root = Node(inode=ROOT_INODE, name="", kind="root", parent=ROOT_INODE, is_dir=True)
        self._nodes: Dict[int, Node] = {ROOT_INODE: root}
        self._next_inode = ROOT_INODE + 1
        # slot_key -> {view: file node}; lets merge() update a segment in place.
        self._slots: Dict[tuple, Dict[str, Node]] = {}

    @classmethod
    def from_recordings(cls, recordings: Iterable[Recording]) -> "FsTree":
        """Build the tree. Recordings without ``begin_time`` are skipped."""
        tree = cls()
        tree.merge(recordings)
        return tree

    # ----- construction helpers -------------------------------------------

    def _alloc_inode(self) -> int:
        inode = self._next_inode
        self._next_inode += 1
        return inode

    def _new_dir(self, parent: Node, name: str, kind: str, view: Optional[str] = None) -> Node:
        node = Node(inode=self._alloc_inode(), name=name, kind=kind, parent=parent.inode, is_dir=True, view=view)
        self._nodes[node.inode] = node
        parent.children[name] = node.inode
        return node

    @staticmethod
    def _file_mtime(rec: Recording) -> float:
        # mtime uses end_time (falling back to begin_time). Note: naive
        # datetimes are interpreted as local time by .timestamp().
        when = rec.end_time if rec.end_time is not None else rec.begin_time
        return when.timestamp()

    def _new_file(self, parent: Node, name: str, rec: Recording, view: str) -> Node:
        node = Node(
            inode=self._alloc_inode(),
            name=name,
            kind="file",
            parent=parent.inode,
            is_dir=False,
            recording=rec,
            view=view,
            # For "raw" this is exact. For "mp4" it is an UPPER BOUND: the
            # real MP4 is slightly smaller than the container, and the true
            # size is only known after conversion. Keep this in mind when
            # the FUSE layer reports st_size.
            size=rec.length_bytes,
            mtime=self._file_mtime(rec),
        )
        self._nodes[node.inode] = node
        parent.children[name] = node.inode
        return node

    # ----- incremental update ---------------------------------------------

    def merge(self, recordings: Iterable[Recording]) -> "MergeResult":
        """Add new recordings and update known ones in place.

        Existing nodes keep their inode and file name (so open handles and kernel
        caches stay valid); a known segment (same :func:`slot_key`) only gets its
        recording, size and mtime refreshed. Unknown segments become new files,
        creating the date directory when needed. Recordings without
        ``begin_time`` are skipped. Nothing is ever removed.
        """
        result = MergeResult()
        fresh: Dict[str, List[Recording]] = {}
        for rec in sorted(
            (r for r in recordings if r.begin_time is not None), key=lambda r: (r.begin_time, r.filename)
        ):
            slot = self._slots.get(slot_key(rec))
            if slot is not None:
                self._update_slot(slot, rec, result)
            else:
                fresh.setdefault(rec.begin_time.strftime("%Y-%m-%d"), []).append(rec)
        for date_name in sorted(fresh):  # ISO dates sort chronologically as strings
            self._add_recordings(date_name, fresh[date_name], result)
        return result

    def _update_slot(self, slot: Dict[str, Node], rec: Recording, result: "MergeResult") -> None:
        for node in slot.values():
            old = node.recording
            if (
                old is not None
                and old.filename == rec.filename
                and old.end_time == rec.end_time
                and old.length_bytes == rec.length_bytes
            ):
                continue
            node.recording = rec
            node.size = rec.length_bytes
            node.mtime = self._file_mtime(rec)
            result.updated.append((node, old))

    def _add_recordings(self, date_name: str, recs: List[Recording], result: "MergeResult") -> None:
        root = self._nodes[ROOT_INODE]
        date_id = root.children.get(date_name)
        date_node = self._nodes[date_id] if date_id is not None else self._new_dir(root, date_name, "date")
        mp4_id = date_node.children.get(VIEW_MP4)
        used = set(self._nodes[mp4_id].children) if mp4_id is not None else set()  # views share basenames
        names: List[str] = []
        for rec in recs:
            base = _base_name(rec)
            name, n = base, 1
            while name + _EXTENSIONS[VIEW_MP4] in used:
                n += 1
                name = f"{base}_{n}"
            used.add(name + _EXTENSIONS[VIEW_MP4])
            names.append(name)
        slots = [{} for _ in recs]  # type: List[Dict[str, Node]]
        for view in VIEWS:
            view_id = date_node.children.get(view)
            view_node = (
                self._nodes[view_id] if view_id is not None else self._new_dir(date_node, view, "view", view=view)
            )
            for rec, name, slot in zip(recs, names, slots):
                slot[view] = self._new_file(view_node, name + _EXTENSIONS[view], rec, view)
                result.added.append(slot[view])
        for rec, slot in zip(recs, slots):
            self._slots[slot_key(rec)] = slot

    # ----- removal ----------------------------------------------------------------

    def slot_keys(self) -> List[tuple]:
        """Keys of all segments currently in the tree."""
        return list(self._slots)

    def remove_slots(self, keys: Iterable[tuple]) -> List[Tuple[int, str, int]]:
        """Remove segments (both views); drop view/date directories left empty.

        Returns ``(parent_inode, name, inode)`` for every removed entry, children
        before their parents, so the FUSE layer can invalidate kernel caches.
        Inodes are never reused. Already open file handles are unaffected.
        """
        removed: List[Tuple[int, str, int]] = []
        for key in keys:
            slot = self._slots.pop(key, None)
            for node in (slot or {}).values():
                parent = self._nodes.get(node.parent)
                if parent is not None and parent.children.get(node.name) == node.inode:
                    del parent.children[node.name]
                    removed.append((parent.inode, node.name, node.inode))
                self._nodes.pop(node.inode, None)
        root = self._nodes[ROOT_INODE]
        for date_name, date_id in list(root.children.items()):
            date = self._nodes[date_id]
            if any(self._nodes[view_id].children for view_id in date.children.values()):
                continue
            for view_name, view_id in list(date.children.items()):
                removed.append((date_id, view_name, view_id))
                del self._nodes[view_id]
            del root.children[date_name]
            del self._nodes[date_id]
            removed.append((ROOT_INODE, date_name, date_id))
        return removed

    # ----- public read API ------------------------------------------------

    def get(self, inode: int) -> Optional[Node]:
        """Return the node for ``inode`` or ``None``."""
        return self._nodes.get(inode)

    def lookup(self, parent_inode: int, name: str) -> Optional[Node]:
        """Return the child ``name`` of directory ``parent_inode``, or ``None``."""
        parent = self._nodes.get(parent_inode)
        if parent is None or not parent.is_dir:
            return None
        child = parent.children.get(name)
        if child is None:
            return None
        return self._nodes.get(child)

    def list_children(self, inode: int) -> List[Node]:
        """Return child nodes of a directory, sorted by name.

        Returns an empty list for files and unknown inodes. Sorting by name
        gives dates ascending, ``mp4`` before ``raw``, and files by name.
        """
        node = self._nodes.get(inode)
        if node is None or not node.is_dir:
            return []
        return [self._nodes[child] for _, child in sorted(node.children.items())]
