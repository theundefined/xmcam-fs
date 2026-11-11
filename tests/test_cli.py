from xmcamfs.cli import build_parser


def test_defaults():
    args = build_parser().parse_args(["192.0.2.10", "mnt"])
    assert args.refresh == 60.0
    assert args.cache_max_size == 500 * 1024**2
    assert args.background is False


def test_new_options():
    args = build_parser().parse_args(["192.0.2.10", "mnt", "--refresh", "0", "--cache-max-size", "2G", "-b"])
    assert args.refresh == 0
    assert args.cache_max_size == 2 * 1024**3
    assert args.background is True


def test_views_and_ts_options():
    import pytest

    args = build_parser().parse_args(["192.0.2.10", "mnt"])
    assert args.views == ("mp4", "raw", "ts") and args.ts_on_close == "cancel"
    args = build_parser().parse_args(["192.0.2.10", "mnt", "--views", "ts,raw", "--ts-on-close", "finish"])
    assert args.views == ("raw", "ts") and args.ts_on_close == "finish"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["192.0.2.10", "mnt", "--views", "mp3"])
