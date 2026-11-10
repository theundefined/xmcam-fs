"""Offline tests for xmcamfs.tree (pure inode tree, no FUSE, no network)."""

from datetime import datetime

from xmcam.models import Recording
from xmcamfs.tree import ROOT_INODE, FsTree

MB49 = 49 * 1024 * 1024


def make_rec(filename, begin, end, length=MB49):
    return Recording(filename=filename, begin_time=begin, end_time=end,
                     length_bytes=length)


def sample_recordings():
    return [
        make_rec("a.dav", datetime(2026, 11, 10, 8, 0, 0),
                 datetime(2026, 11, 10, 8, 5, 0)),
        make_rec("b.dav", datetime(2026, 11, 10, 9, 0, 0),
                 datetime(2026, 11, 10, 9, 5, 0)),
        make_rec("c.dav", datetime(2026, 11, 11, 7, 30, 0),
                 datetime(2026, 11, 11, 7, 45, 0)),
    ]


def child_names(tree, inode):
    return [n.name for n in tree.list_children(inode)]


def find_file(tree, date_name, view, base):
    date = tree.lookup(ROOT_INODE, date_name)
    view_node = tree.lookup(date.inode, view)
    return tree.lookup(view_node.inode, base)


def test_root_and_date_layout():
    tree = FsTree.from_recordings(sample_recordings())

    assert child_names(tree, ROOT_INODE) == ["2026-11-10", "2026-11-11"]
    for date_name in ("2026-11-10", "2026-11-11"):
        date = tree.lookup(ROOT_INODE, date_name)
        assert date.kind == "date" and date.is_dir
        assert child_names(tree, date.inode) == ["mp4", "raw"]

    busy = tree.lookup(ROOT_INODE, "2026-11-10")
    mp4 = tree.lookup(busy.inode, "mp4")
    assert mp4.kind == "view" and mp4.is_dir
    assert child_names(tree, mp4.inode) == [
        "08.00.00-08.05.00.mp4",
        "09.00.00-09.05.00.mp4",
    ]


def test_file_node_fields():
    tree = FsTree.from_recordings(sample_recordings())
    node = find_file(tree, "2026-11-10", "mp4", "08.00.00-08.05.00.mp4")
    assert node is not None
    assert node.kind == "file"
    assert node.is_dir is False
    assert node.view == "mp4"
    assert node.size == MB49
    assert node.recording is not None
    assert node.recording.filename == "a.dav"
    assert node.mtime == datetime(2026, 11, 10, 8, 5, 0).timestamp()

    raw = find_file(tree, "2026-11-10", "raw", "08.00.00-08.05.00.xmraw")
    assert raw is not None
    assert raw.view == "raw"
    assert raw.size == MB49
    assert raw.is_dir is False


def test_lookup_and_get():
    tree = FsTree.from_recordings(sample_recordings())
    date = tree.lookup(ROOT_INODE, "2026-11-11")
    assert date is not None
    assert tree.get(date.inode) is date
    assert tree.lookup(ROOT_INODE, "1999-01-01") is None
    assert tree.lookup(date.inode, "missing") is None
    assert tree.get(999999) is None
    # Lookup under a file node is not a directory, so it returns None.
    file_node = find_file(tree, "2026-11-11", "raw", "07.30.00-07.45.00.xmraw")
    assert tree.lookup(file_node.inode, "anything") is None
    assert tree.list_children(file_node.inode) == []
    assert tree.list_children(999999) == []


def test_duplicate_basenames_are_disambiguated():
    recs = [
        make_rec("first.dav", datetime(2026, 11, 10, 8, 0, 0),
                 datetime(2026, 11, 10, 8, 5, 0)),
        make_rec("second.dav", datetime(2026, 11, 10, 8, 0, 0),
                 datetime(2026, 11, 10, 8, 5, 0)),
    ]
    tree = FsTree.from_recordings(recs)
    date = tree.lookup(ROOT_INODE, "2026-11-10")
    for view, ext in (("mp4", ".mp4"), ("raw", ".xmraw")):
        view_node = tree.lookup(date.inode, view)
        assert child_names(tree, view_node.inode) == [
            "08.00.00-08.05.00" + ext,
            "08.00.00-08.05.00_2" + ext,
        ]
    # Ordering tiebreak is the original filename: "first" keeps the base name.
    first = find_file(tree, "2026-11-10", "mp4", "08.00.00-08.05.00.mp4")
    assert first.recording.filename == "first.dav"
    second = find_file(tree, "2026-11-10", "mp4", "08.00.00-08.05.00_2.mp4")
    assert second.recording.filename == "second.dav"


def test_missing_end_time_uses_begin_time():
    rec = make_rec("x.dav", datetime(2026, 11, 10, 10, 0, 0), None)
    tree = FsTree.from_recordings([rec])
    node = find_file(tree, "2026-11-10", "mp4", "10.00.00-10.00.00.mp4")
    assert node is not None
    assert node.mtime == datetime(2026, 11, 10, 10, 0, 0).timestamp()


def test_recording_without_begin_time_is_skipped():
    recs = [
        make_rec("no_begin.dav", None, datetime(2026, 11, 10, 8, 5, 0)),
        make_rec("ok.dav", datetime(2026, 11, 10, 8, 0, 0),
                 datetime(2026, 11, 10, 8, 5, 0)),
    ]
    tree = FsTree.from_recordings(recs)
    date = tree.lookup(ROOT_INODE, "2026-11-10")
    mp4 = tree.lookup(date.inode, "mp4")
    assert child_names(tree, mp4.inode) == ["08.00.00-08.05.00.mp4"]

    only_none = FsTree.from_recordings(
        [make_rec("gone.dav", None, None)])
    assert child_names(only_none, ROOT_INODE) == []


def test_inodes_are_sequential_in_tree_order():
    tree = FsTree.from_recordings(sample_recordings())
    assert ROOT_INODE == 1
    date = tree.lookup(ROOT_INODE, "2026-11-10")
    assert date.inode == 2
    mp4 = tree.lookup(date.inode, "mp4")
    assert mp4.inode == 3
    assert tree.lookup(mp4.inode, "08.00.00-08.05.00.mp4").inode == 4
    assert tree.lookup(mp4.inode, "09.00.00-09.05.00.mp4").inode == 5


def test_empty_tree():
    tree = FsTree.from_recordings([])
    assert tree.get(ROOT_INODE) is not None
    assert tree.list_children(ROOT_INODE) == []
