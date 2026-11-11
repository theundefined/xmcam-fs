# xmcam-fs

**Mount a Xiongmai (XM) / iCSee / XMeye IP camera as a filesystem** and browse
its recordings as ordinary files — like `sshfs`, but for your camera's SD
card/disk. Built on [`xmcam`](https://github.com/theundefined/xmcam-py) and
[`pyfuse3`](https://github.com/libfuse/pyfuse3). Everything stays on the local
network: **no cloud, no internet.**

```
mnt/
└── 2026-11-10/
    ├── mp4/
    │   ├── 08.00.00-08.05.00.mp4        # continuous recording
    │   └── 08.09.17-08.09.34.M.mp4      # motion clip (".M" marker)
    └── raw/
        ├── 08.00.00-08.05.00.xmraw      # original Xiongmai container
        └── 08.09.17-08.09.34.M.xmraw
```

Each recording appears twice:

- **`mp4/`** — a playable MP4 (HEVC/H.264, stream-copied, no re-encode). The
  first time you open one, the whole segment is downloaded and converted, then
  cached — so the first read is slow (~2 min for a 5-minute segment on these
  cameras) and later reads are instant.
- **`raw/`** — the original `.xmraw` container, exact size, no conversion cost.

Motion-triggered clips are tagged with a **`.M`** marker in the file name, so
motion stands out in a plain `ls` (and `ls *.M.mp4` lists just those).

The filesystem is **read-only**.

## Why a filesystem?

Because then every tool you already have just works: `cp`, `rsync`, `mpv
mnt/2026-11-10/mp4/08.00.00-08.05.00.mp4`, a file manager, a backup script.
No bespoke download command needed.

## Requirements

`xmcam-fs` depends on `pyfuse3`, a C extension with **no prebuilt wheels**, so a
few system packages must be present *before* you install:

- Linux with FUSE 3 (`/dev/fuse`, `fusermount3`).
- **`libfuse3-dev`** + `pkg-config` to build `pyfuse3`, and `fuse3` to mount.
- A C toolchain and Python headers (`build-essential`, `python3-dev`) if not
  already present.
- `ffmpeg` for the `mp4/` view (the `raw/` view works without it).

On Debian/Ubuntu:

```bash
sudo apt install -y libfuse3-dev pkg-config fuse3 build-essential python3-dev
```

## Install

```bash
pipx install xmcam-fs        # or: pip install xmcam-fs
```

### Troubleshooting

**`Package fuse3 was not found in the pkg-config search path` / `failed-wheel-build-for-install`**
— `pyfuse3` can't find the libfuse3 development headers. Install them and retry:

```bash
sudo apt install -y libfuse3-dev pkg-config   # Debian/Ubuntu
# Fedora: sudo dnf install fuse3-devel pkgconf-pkg-config
# Arch:   sudo pacman -S fuse3 pkgconf
pipx install xmcam-fs
```

## Usage

```bash
# Mount the camera (credentials auto-recovered from anonymous ONVIF if omitted)
xmcam-fs 192.0.2.10 ./mnt

# Only index the last 3 days, foreground (Ctrl-C to unmount)
xmcam-fs 192.0.2.10 ./mnt --days 3 --foreground

# With explicit credentials and the smaller sub-stream
XMCAM_USER=... XMCAM_PASS=... xmcam-fs 192.0.2.10 ./mnt --stream Sub

# Unmount
fusermount3 -u ./mnt
```

### Monitoring access and downloads

Opening a file triggers a download (segments are slow), so the mount tracks
activity. Read the virtual **`.status`** file at the mount root to see what is
being accessed and the progress of in-flight downloads, with **ETA**:

```bash
cat ./mnt/.status
# or watch it live:
watch -n1 cat ./mnt/.status
```

```
xmcam-fs status

Active downloads:
  12.00.00-12.00.26.M.mp4  [mp4]   46%  2.1MB/4.6MB  543.7KB/s  ETA 00:04

Recent access:
  13:46:57  12.00.00-12.00.26.M.mp4  [mp4]  fetch

Completed this session: 1
```

When run in a terminal, add **`--progress`** to also print a live progress line
with ETA to stderr as each segment downloads.

Options: `--days N` (how many past days to index, default 7), `--stream Main|Sub`,
`--channel N`, `--cache-dir PATH` (default under `$XDG_CACHE_HOME`),
`--user/--password`, `--foreground`, `--debug`.

## How it works

On mount, `xmcam-fs` recovers the camera account (anonymous ONVIF), lists the
recordings for the chosen window (DVRIP `OPFileQuery`) and builds a read-only
tree. `ls` is cheap — it never downloads. Opening a file triggers a one-off
download (DVRIP `OPPlayBack`) in a worker thread; for the `mp4/` view the
Xiongmai container is demuxed and remuxed to MP4. Results are cached on disk, so
re-opening is instant.

> The recording list is a snapshot taken at mount time. Remount to pick up
> newer footage. (Live refresh is planned.)

## Security & ethics

`xmcam-fs` can recover camera credentials and pull footage over the LAN because
the firmware exposes them to anonymous ONVIF. **Use it only on cameras you own
or are authorised to administer.** No cloud is ever contacted and no credentials
are written to disk (they are recovered per-run and kept in memory). The on-disk
cache under `--cache-dir` holds decoded footage — put it somewhere appropriate
and clear it when done.

## License

MIT © theundefined
