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
    │   ├── 08.00.00-08.05.00.mp4      # playable MP4, produced on first open
    │   └── 08.05.00-08.09.17.mp4
    └── raw/
        ├── 08.00.00-08.05.00.xmraw    # original Xiongmai container
        └── 08.05.00-08.09.17.xmraw
```

Each recording appears twice:

- **`mp4/`** — a playable MP4 (HEVC/H.264, stream-copied, no re-encode). The
  first time you open one, the whole segment is downloaded and converted, then
  cached — so the first read is slow (~2 min for a 5-minute segment on these
  cameras) and later reads are instant.
- **`raw/`** — the original `.xmraw` container, exact size, no conversion cost.

The filesystem is **read-only**.

## Why a filesystem?

Because then every tool you already have just works: `cp`, `rsync`, `mpv
mnt/2026-11-10/mp4/08.00.00-08.05.00.mp4`, a file manager, a backup script.
No bespoke download command needed.

## Requirements

- Linux with FUSE 3 (`/dev/fuse`, `fusermount3`).
- **`libfuse3-dev`** and `pkg-config` are needed to *build* `pyfuse3` (its only
  non-Python dependency). On Debian/Ubuntu: `sudo apt install libfuse3-dev pkg-config`.
- `ffmpeg` for the `mp4/` view (the `raw/` view works without it).

## Install

```bash
pipx install xmcam-fs        # or: pip install xmcam-fs
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
