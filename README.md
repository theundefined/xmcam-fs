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
    ├── raw/
    │   ├── 08.00.00-08.05.00.xmraw      # original Xiongmai container
    │   └── 08.09.17-08.09.34.M.xmraw
    └── ts/
        ├── 08.00.00-08.05.00.ts         # MPEG-TS: plays while it is still downloading
        └── 08.09.17-08.09.34.M.ts
```

Each recording appears three times:

- **`mp4/`** — a playable MP4 (HEVC/H.264, stream-copied, no re-encode). The
  first time you open one, the whole segment is downloaded and converted, then
  cached — so the first read is slow (~2 min for a 5-minute segment on these
  cameras) and later reads are instant.
- **`raw/`** — the original `.xmraw` container, exact size, no conversion cost.
- **`ts/`** — MPEG-TS that **starts playing within a few seconds** (see
  [Streaming playback](#streaming-playback)).

`mp4/` and `ts/` carry the camera's audio (G.711 A-law, encoded to AAC).

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

# Detach and keep running in the background (returns once the mount is up)
xmcam-fs 192.0.2.10 ./mnt --background

# Unmount (also stops a background instance)
fusermount3 -u ./mnt
```

### Choosing views

All three views are mounted by default. Mount only what you need with `--views`
(e.g. `--views ts` for a player-only mount, `--views raw,mp4` for backups):

```bash
xmcam-fs 192.0.2.10 ./mnt --views ts
```

### Streaming playback

Opening a file in `mp4/` downloads and converts the whole segment first (an MP4
keeps its index at the end of the file), so a player waits minutes. Files in
**`ts/`** are produced while they download: `open` returns after a couple of seconds
and the bytes are served as they appear. The camera sends 3–8× faster than real
time, so playback does not run dry.

```bash
mpv   ./mnt/2026-11-10/ts/08.00.00-08.05.00.ts
mplayer ./mnt/2026-11-10/ts/08.00.00-08.05.00.ts
```

- While a segment is still being produced its file is **not seekable** (players treat
  it like a pipe): you can watch from the start but not jump ahead. Once the segment
  is complete it is cached, and every later open is a normal, fully seekable file.
- The camera has one playback channel, so only one segment downloads at a time.
- `--ts-on-close cancel` (default): when the last reader closes a file whose download
  is still running (after a 15 s grace period, since players often reopen a file),
  the download is cancelled and the partial output discarded, which frees the
  camera. `--ts-on-close finish` keeps downloading so the whole segment ends up in
  the cache.
- Picture and sound stay in sync: the camera's frame rate is not constant (it varies
  around the nominal 12 fps by about 1% between segments, which would drift by seconds
  over ten minutes), so every video frame is time-stamped from the audio clock.

### Live updates

The mount follows the camera: every `--refresh` seconds (default 60; `0` = never)
new recordings are added to the tree, so `ls` shows footage as it is recorded —
no remount needed. The segment the camera is *currently writing* is held back
until it is finished (otherwise a half-written copy would be cached), so a new
file appears when its segment closes — at the latest about one refresh after a
5-minute segment ends, sooner when motion or a new segment closes the previous one.

The camera often drops queries while a download is running, so refreshes are
skipped during downloads and run right after they finish.

### Background mode

`--background` (`-b`) detaches after the mount is up; errors during start-up
(wrong credentials, FUSE problems) still show in your terminal. The daemon logs to
`--log-file` (default `~/.local/state/xmcam-fs/<camera-ip>.log`). Stop it with
`fusermount3 -u ./mnt`.

### Cache size limit

Downloaded and converted segments are cached on disk. `--cache-max-size` caps
that cache (default **500M**; `2G`, `500M`, … or `0` for unlimited): when it is
exceeded the least recently used segments are deleted and simply fetched again
on next access. The limit is soft — the segment just produced and anything used
in the last 30 s are never evicted, so one segment larger than the limit still
works. A 5-minute segment is roughly 45 MB, so 500M holds about a dozen; raise the
limit to keep more.

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
  13:46:57  12.00.00-12.00.26.M.mp4  [mp4]  fetch  4.4MB  543.7KB/s
  13:44:02  11.55.00-12.00.00.mp4  [mp4]  hit  45.1MB

Completed this session: 1
```

Each access shows the size of the file and, for a download, the download speed
(conversion time excluded).

When run in a terminal, add **`--progress`** to also print a live progress line
with ETA to stderr as each segment downloads.

Options: `--days N` (how many past days to index, default 7), `--stream Main|Sub`,
`--channel N`, `--cache-dir PATH` (default under `$XDG_CACHE_HOME`),
`--views LIST` (default `mp4,raw,ts`), `--ts-on-close cancel|finish`,
`--cache-max-size SIZE` (default 500M), `--refresh SECONDS` (default 60),
`-b/--background`, `--log-file PATH`, `--user/--password`, `--foreground`, `--debug`.

## How it works

On mount, `xmcam-fs` recovers the camera account (anonymous ONVIF), lists the
recordings for the chosen window (DVRIP `OPFileQuery`) and builds a read-only
tree. `ls` is cheap — it never downloads. Opening a file triggers a one-off
download (DVRIP `OPPlayBack`) in a worker thread; for the `mp4/` view the
Xiongmai container is demuxed and remuxed to MP4. Results are cached on disk, so
re-opening is instant.

The recording list is refreshed in the background (see *Live updates*), so a
long-running mount keeps up with the camera. The tree mirrors the last `--days`
days: about every 15 minutes the whole window is listed again and recordings the
camera has overwritten (or that slid out of the window) are removed — after being
missing from two consecutive listings, so a glitchy reply cannot make files vanish.
Files you already have open keep working, and cached copies stay in the cache until
evicted by the size limit.

## Security & ethics

`xmcam-fs` can recover camera credentials and pull footage over the LAN because
the firmware exposes them to anonymous ONVIF. **Use it only on cameras you own
or are authorised to administer.** No cloud is ever contacted and no credentials
are written to disk (they are recovered per-run and kept in memory). The on-disk
cache under `--cache-dir` holds decoded footage — put it somewhere appropriate
and clear it when done.

## License

MIT © theundefined
