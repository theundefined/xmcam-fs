"""FsTree.merge: incremental updates keep inodes/names stable and never duplicate a growing segment."""

from datetime import datetime

from xmcam.models import Recording
from xmcamfs.tree import ROOT_INODE, FsTree, slot_key


def rec(begin, end, token="71db", event="R", length=1000, day=11):
    name = f"/idea0/2026-11-{day:02d}/001/{begin}-{end}[{event}][@{token}][0].h264"
    h, m, s = (int(x) for x in begin.split("."))
    eh, em, es = (int(x) for x in end.split("."))
    return Recording(
        filename=name,
        begin_time=datetime(2026, 11, day, h, m, s),
        end_time=datetime(2026, 11, day, eh, em, es),
        length_bytes=length,
        event=event,
    )


def names(tree, date, view):
    d = tree.lookup(ROOT_INODE, date)
    v = tree.lookup(d.inode, view)
    return [n.name for n in tree.list_children(v.inode)]


def test_slot_key_ignores_growing_end_time():
    assert slot_key(rec("19.41.55", "19.44.10")) == slot_key(rec("19.41.55", "19.44.20"))
    assert slot_key(rec("19.41.55", "19.44.10")) != slot_key(rec("19.41.55", "19.44.10", token="71dc"))


def test_merge_adds_new_files_keeping_inodes():
    tree = FsTree.from_recordings([rec("08.00.00", "08.05.00", token="a1")])
    first = tree.lookup(tree.lookup(tree.lookup(ROOT_INODE, "2026-11-11").inode, "mp4").inode, "08.00.00-08.05.00.mp4")

    result = tree.merge([rec("08.00.00", "08.05.00", token="a1"), rec("08.05.00", "08.10.00", token="a2")])

    assert len(result.added) == 3  # mp4 + raw + ts of the new segment
    assert not result.updated
    assert names(tree, "2026-11-11", "mp4") == ["08.00.00-08.05.00.mp4", "08.05.00-08.10.00.mp4"]
    assert names(tree, "2026-11-11", "raw") == ["08.00.00-08.05.00.xmraw", "08.05.00-08.10.00.xmraw"]
    again = tree.lookup(tree.lookup(tree.lookup(ROOT_INODE, "2026-11-11").inode, "mp4").inode, "08.00.00-08.05.00.mp4")
    assert again.inode == first.inode


def test_merge_new_date_creates_directories():
    tree = FsTree.from_recordings([rec("23.55.00", "23.59.59", token="a1", day=11)])
    tree.merge([rec("00.00.00", "00.05.00", token="b1", day=12)])
    assert [n.name for n in tree.list_children(ROOT_INODE)] == ["2026-11-11", "2026-11-12"]
    assert names(tree, "2026-11-12", "mp4") == ["00.00.00-00.05.00.mp4"]


def test_merge_same_slot_updates_in_place_without_duplicate():
    tree = FsTree.from_recordings([rec("19.41.55", "19.44.10", length=100)])
    node = tree.lookup(tree.lookup(tree.lookup(ROOT_INODE, "2026-11-11").inode, "raw").inode, "19.41.55-19.44.10.xmraw")

    result = tree.merge([rec("19.41.55", "19.44.20", length=150)])

    assert not result.added
    assert len(result.updated) == 3
    old = result.updated[0][1]
    assert old.end_time == datetime(2026, 11, 11, 19, 44, 10)
    assert names(tree, "2026-11-11", "raw") == ["19.41.55-19.44.10.xmraw"]  # name (and inode) stay
    assert node.size == 150 and node.recording.end_time == datetime(2026, 11, 11, 19, 44, 20)


def test_merge_unchanged_is_a_noop():
    r = rec("08.00.00", "08.05.00")
    tree = FsTree.from_recordings([r])
    result = tree.merge([r])
    assert not result


def test_merge_name_collisions_stay_stable():
    a = rec("08.00.00", "08.05.00", token="a1")
    b = rec("08.00.00", "08.05.00", token="a2")
    tree = FsTree.from_recordings([a])
    tree.merge([a, b])
    assert names(tree, "2026-11-11", "mp4") == ["08.00.00-08.05.00.mp4", "08.00.00-08.05.00_2.mp4"]


def test_remove_slots_drops_files_and_empty_directories():
    a, b = rec("08.00.00", "08.05.00", token="a1", day=10), rec("08.00.00", "08.05.00", token="b1", day=11)
    c = rec("08.05.00", "08.10.00", token="b2", day=11)
    tree = FsTree.from_recordings([a, b, c])
    day10 = tree.lookup(ROOT_INODE, "2026-11-10")
    inodes_before = len(tree._nodes)

    removed = tree.remove_slots([slot_key(a)])

    assert [n.name for n in tree.list_children(ROOT_INODE)] == ["2026-11-11"]  # emptied date dir is gone
    assert tree.get(day10.inode) is None
    names_removed = [name for _, name, _ in removed]
    assert names_removed[-1] == "2026-11-10" and "08.00.00-08.05.00.mp4" in names_removed  # children first
    assert len(tree._nodes) == inodes_before - 1 - 3 - 3  # date dir, 3 views, 3 files
    assert names(tree, "2026-11-11", "mp4") == ["08.00.00-08.05.00.mp4", "08.05.00-08.10.00.mp4"]

    tree.remove_slots([slot_key(b)])
    assert names(tree, "2026-11-11", "mp4") == ["08.05.00-08.10.00.mp4"]  # date dir stays while it has files
    assert tree.remove_slots([slot_key(a)]) == []  # unknown slot: no-op
