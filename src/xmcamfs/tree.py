"""Pure, read-only in-memory inode tree for the xmcam-fs FUSE layer.

This module has no FUSE, network or pyfuse3 dependency. It turns a flat list
of :class:`xmcam.models.Recording` objects into a tree of :class:`Node`
objects that the FUSE layer can serve directly.

Layout::

    /                             (root, inode ROOT_INODE)
      <YYYY-MM-DD>/               one directory per distinct begin date
        mp4/                      view directory
          HH.MM.SS-HH.MM.SS.mp4   one file per recording on that day
        raw/                      view directory
          HH.MM.SS-HH.MM.SS.xmraw
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

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


def _base_name(rec: Recording) -> str:
    """Return ``HH.MM.SS-HH.MM.SS`` for a recording (without extension)."""
    end = rec.end_time if rec.end_time is not None else rec.begin_time
    return f"{rec.begin_time:%H.%M.%S}-{end:%H.%M.%S}"


def _assign_names(recs: List[Recording]) -> List[str]:
    """Return one basename per recording, disambiguating collisions.

    ``recs`` must already be in the desired order. The first occurrence of a
    basename keeps it unchanged; later occurrences get ``_2``, ``_3``, ...
    inserted before the extension (the extension is added by the caller).
    Both view directories use the same basenames, so this is computed once
    per date.
    """
    used = set()
    counts: Dict[str, int] = {}
    names: List[str] = []
    for rec in recs:
        base = _base_name(rec)
        n = counts.get(base, 1)
        name = base
        while name in used:
            n += 1
            name = f"{base}_{n}"
        counts[base] = n
        used.add(name)
        names.append(name)
    return names


class FsTree:
    """Read-only inode tree built from recordings."""

    def __init__(self) -> None:
        root = Node(inode=ROOT_INODE, name="", kind="root", parent=ROOT_INODE, is_dir=True)
        self._nodes: Dict[int, Node] = {ROOT_INODE: root}
        self._next_inode = ROOT_INODE + 1

    @classmethod
    def from_recordings(cls, recordings: Iterable[Recording]) -> "FsTree":
        """Build the tree. Recordings without ``begin_time`` are skipped."""
        tree = cls()
        by_date: Dict[str, List[Recording]] = {}
        for rec in recordings:
            if rec.begin_time is None:
                continue
            date_name = rec.begin_time.strftime("%Y-%m-%d")
            by_date.setdefault(date_name, []).append(rec)
        # ISO dates sort chronologically as strings.
        for date_name in sorted(by_date):
            tree._add_date(date_name, by_date[date_name])
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

    def _new_file(self, parent: Node, name: str, rec: Recording, view: str) -> Node:
        # mtime uses end_time (falling back to begin_time). Note: naive
        # datetimes are interpreted as local time by .timestamp().
        when = rec.end_time if rec.end_time is not None else rec.begin_time
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
            mtime=when.timestamp(),
        )
        self._nodes[node.inode] = node
        parent.children[name] = node.inode
        return node

    def _add_date(self, date_name: str, recs: List[Recording]) -> None:
        root = self._nodes[ROOT_INODE]
        date_node = self._new_dir(root, date_name, "date")
        ordered = sorted(recs, key=lambda r: (r.begin_time, r.filename))
        names = _assign_names(ordered)
        for view in VIEWS:
            view_node = self._new_dir(date_node, view, "view", view=view)
            for rec, base in zip(ordered, names):
                self._new_file(view_node, base + _EXTENSIONS[view], rec, view)

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
