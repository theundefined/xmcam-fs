from xmcamfs.cli import build_parser


def test_defaults():
    args = build_parser().parse_args(["192.0.2.10", "mnt"])
    assert args.refresh == 60.0
    assert args.cache_max_size == 100 * 1024**2
    assert args.background is False


def test_new_options():
    args = build_parser().parse_args(["192.0.2.10", "mnt", "--refresh", "0", "--cache-max-size", "2G", "-b"])
    assert args.refresh == 0
    assert args.cache_max_size == 2 * 1024**3
    assert args.background is True
