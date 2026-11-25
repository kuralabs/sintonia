# Sintonía — plan

Sintonía (`sintonia`) is a simple, watch-TV-focused web app for live
over-the-air TV. It runs as one Docker container that is given the DVB
tuners and a GPU. Open `http://host:port` and the last channel you watched
plays on the left, with a collapsible pane on the right holding the
channel list. Up/Down changes channel.

Scope: LAN only, no authentication. Clients: Chrome desktop and Chrome on
Android. Developed on a TBS6814 quad ISDB-Tb card (Costa Rica) with an
NVIDIA RTX 3060, but nothing in the design is specific to that card.

## Measured facts that drive the design

- Every HD channel is **1080i H.264 High** (interlaced, top field first).
  Browsers do not deinterlace, so video must be deinterlaced and re-encoded.
- Audio is **AAC in LATM framing** (some HE-AAC/v2, some 5.1). No browser
  plays LATM and WebRTC carries only Opus, so audio is always transcoded.
- **ffmpeg 8.1 or newer is required**: ffmpeg 8.0.1 (Ubuntu 26.04 stock)
  does not recognise Canal 6's LATM audio; 8.1.2 and jellyfin-ffmpeg 8.1.3
  do.
- GPU pipeline (NVDEC → `yadif_cuda` → NVENC, audio → Opus) runs at
  **~15x real time** for one 1080i stream on an RTX 3060; four streams are
  comfortably within budget.
- **NVENC fails ("unsupported device") when a container is given only an
  NVIDIA GPU other than GPU 0.** Pinning GPU 0 alone works. So a container
  that must use GPU 1 gets `--gpus all` and selects the GPU itself
  (`-hwaccel_device N`, `-gpu N`); verified that the work then lands on GPU
  1 only.
- After lock, the demodulator needs ~1-1.5 s before packets are clean.

## Architecture

```
            ┌──────────── sintonia container (--network host) ─────────────┐
/dev/dvb ──►│ Python tunes each tuner (DVB ioctls), reads its dvrN once    │
            │ and fans the full-mux TS out to one ffmpeg                   │
            │ per watched channel (a dvr device has a single reader)       │
            │ ffmpeg (GPU N): decode → yadif_cuda (1080p60) → h264_nvenc;  │
            │   audio → aresample → dynaudnorm → stereo → Opus             │
            │   ── RTSP 127.0.0.1:8554/<channel> ──► MediaMTX              │
            │                               WebRTC media (UDP) ────────────┼──► browser
            │ Python (Starlette + uvicorn) :8080: UI, API, SSE, WHEP proxy ┼──► (one origin)
            └──────────────────────────────────────────────────────────────┘
```

- **MediaMTX is the WebRTC server.** ffmpeg publishes into it over local
  RTSP; browsers read from it with WHEP (RFC 9725, one HTTP POST of SDP).
  We write no WebRTC code. Python proxies the WHEP POST so the browser only
  talks to one origin; media flows directly from MediaMTX over UDP.
- **Python orchestrates**: it tunes the frontends itself with the DVB
  ioctls (lock and signal stats included; see `docs/spike.md` for why not
  `dvbv5-zap`), and supervises MediaMTX and the per-channel ffmpeg
  processes (asyncio subprocesses, argument lists, never shell strings).
- **Mux sharing**: a tuner carries a whole multiplex; ffmpeg selects the
  channel by service ID. Viewers of different channels on one multiplex
  share a tuner; viewers of the same channel share one ffmpeg.
- **Idle shutdown** uses MediaMTX's control API (readers per path), so no
  client heartbeats: about 30 s without readers stops that channel's
  ffmpeg, and the tuner when nothing else uses it.
- **Tuner policy**: automatic. A tuner already on the right multiplex is
  reused, else a free one is taken, else one carrying only pre-warmed
  channels, else the least recently viewed. The API can still force a
  tuner (`POST /api/watch` with `tuner`), even if it is on another
  multiplex for another viewer (last request wins); that viewer is retuned
  and shown a notice. The UI no longer offers this: it stays for watching.

## Technologies

| Area | Choice |
|---|---|
| Language | Python ≥ 3.12 (covers Ubuntu 24.04 manual installs) |
| Web | Starlette + uvicorn (single process holds the tuner state) |
| Config | TOML (`tomllib`) → ferberus (validate, normalise, defaults) → objns `Namespace` |
| API validation | ferberus schemas for request bodies |
| Live status | Server-Sent Events (`EventSource`) |
| Tuning | DVB ioctls from Python (`FE_SET_PROPERTY`, `FE_READ_STATUS`, `DTV_STAT_*`) |
| Transcoding | jellyfin-ffmpeg 8.1+ (`jellyfin-ffmpeg8` from Jellyfin's apt repo) |
| Delivery | MediaMTX, pinned release, checksum verified |
| Video out | H.264 (NVENC), no B-frames, 0.5 s GOP, ~8 Mbit/s |
| Audio out | Opus 48 kHz stereo 128 kbit/s, `dynaudnorm=f=100:g=5` levelling |
| Frontend | Plain HTML, ES modules and CSS, no build step |
| Tests | pytest, pytest-asyncio (no hardware needed) |

`hwaccel` mirrors Jellyfin: `nvidia` (default), `vaapi`, `qsv`, or `none`
(CPU `bwdif` + `libx264`). A missing GPU fails loudly at startup rather than
silently falling back to the CPU.

## Configuration

```toml
[server]
port = 8080

[tuners]
adapters = [0, 1, 2, 3]

[transcode]
hwaccel = 'nvidia'
gpu = 1                     # device index; see the NVENC note above
output = '1080p60'          # 1080p60 | 1080p30 | 720p60
video_bitrate = '8M'
audio_normalize = true      # Costa Rican channels are badly levelled

[[channels]]
number = '6.1'
name = 'Canal 6 HD'
frequency = 521142857
service_id = 19232
```

`sintonia scan` tunes every RF channel on the free tuners, reads the
PAT, SDT and NIT, and prints `[[channels]]` entries numbered as TVs do
(ABNT NBR 15603: the NIT's remote control key is the major number, the
service number plus one the minor), annotated with signal quality.
`sintonia import channels.conf` converts a `dvbv5-scan` result instead.
One-seg services are skipped unless asked. Scanning is CLI only: the UI
stays for watching.
`examples/costa-rica.toml` ships the services found during development.

## UI

- Video area on the left; collapsible right pane (state remembered) with
  the channel list (number + name, current highlighted) and, at the bottom,
  the signal quality of the channel being watched (bars, from the share of
  corrupt packets and the C/N). Tapping it (or I) shows every tuner's
  channels, signal, C/N and corrupt packets: for diagnosis, hidden by
  default.
- On the video: a translucent fullscreen icon that fades after ~2 s of no
  mouse movement; a TV-style banner (`6.1 Canal 6 HD`) with a spinner while
  tuning; "No signal" when lock is lost.
- Keys: Up/Down channel, F fullscreen, P pane, Esc exit fullscreen.
- Last channel per browser (`localStorage`); first channel on first visit.
- Android: swipe up/down on the video to change channel, landscape lock in
  fullscreen, Screen Wake Lock while playing, muted autoplay until the
  first tap (browser policy), PWA manifest for "Add to Home screen".

## Repository and standards

```
sintonia/
├── pyproject.toml        setuptools; version = {attr = 'sintonia.__version__'}
├── LICENSE               Apache-2.0
├── Dockerfile
├── docs/plan.md
├── examples/costa-rica.toml, nginx.conf, sintonia.sh
├── src/sintonia/
│   ├── __init__.py       __version__
│   ├── __main__.py       serve | check | health | scan | import
│   ├── scanner.py si.py  native channel scan, PSI/SI tables
│   ├── config.py dvb.py tuners.py transcode.py mediamtx.py app.py
│   └── data/ui/          index.html, app.js, whep.js, style.css, manifest, icons
├── tests/
└── .github/              workflows/ci.yml, workflows/image.yml, dependabot.yml
```

- Copyright (C) 2026 KuraLabs S.R.L., Apache License 2.0; short Apache
  header in every source file.
- ruff **lint only** (no formatter): `select = ['E', 'F', 'W', 'B', 'I']`,
  `line-length = 79`.
- Single-quoted strings, except where double quotes make sense (text that
  contains single quotes).
- Docstrings open with `"""` and a newline, Sphinx fields, one blank line
  between description, params, returns and raises:

  ```python
  def tune(adapter, frequency):
      """
      Tune an adapter to a multiplex and keep the transport stream flowing.

      :param int adapter: DVB adapter number, as in /dev/dvb/adapterN.
      :param int frequency: Centre frequency in Hz.

      :return: The running dvbv5-zap process.
      :rtype: asyncio.subprocess.Process

      :raises TuneError: If the frontend does not lock within the timeout.
      """
  ```

- SemVer; release tags `vX.Y.Z`.

## Container and CI

- Image: `ubuntu:26.04`, Jellyfin's apt repo for `jellyfin-ffmpeg8` only,
  Python venv with sintonia, MediaMTX pinned and checksum
  verified, `tini` as init, non-root user, `HEALTHCHECK` on `/healthz`,
  OCI labels (`org.opencontainers.image.source`, `licenses=Apache-2.0`).
  `amd64` only.
- `ci.yml`: ruff check and pytest on every push and pull request.
- `image.yml`: build and push `ghcr.io/kuralabs/sintonia` on `main`
  (`latest`, `sha-…`) and on tags (`X.Y.Z`, `X.Y`), plus a **weekly
  rebuild** to pick up base image and jellyfin-ffmpeg updates.
- Dependabot for Actions, pip and the base image; MediaMTX bumps by hand
  (checksum).

Run (GPU 1 for sintonia; Jellyfin can be pinned to GPU 0 with
`--gpus '"device=0"'`, which works):

```sh
docker run -d --name sintonia --restart unless-stopped \
  --gpus all --device /dev/dvb \
  --group-add "$(getent group video | cut -d: -f3)" \
  --network host -v "$PWD/config.toml:/config/config.toml:ro" \
  ghcr.io/kuralabs/sintonia:latest
```

`examples/sintonia.sh` wraps this (pull, restart, wait for health, and a
`scan` mode that pauses the server). In production Sintonía is bound to
127.0.0.1 behind NGINX with HTTPS (`examples/nginx.conf`): the screen wake
lock and installing to the home screen need a secure origin. Only HTTP
goes through NGINX; WebRTC media reaches clients directly on UDP 8189.

## Phases

| # | Deliverable | Done when |
|---|---|---|
| 0 | Spike: zap → ffmpeg (GPU) → MediaMTX → bare WHEP page, by hand | **Done** except the Android check: see `docs/spike.md` |
| 1 | Skeleton | **Done** |
| 2 | Backend core | **Done** |
| 3 | UI | **Done**; Android to be tried on a real phone |
| 4 | Tuner pool | **Done** |
| 5 | Container + release | **Done** except tagging v0.1.0 |
| Later | — | EIT "now playing"; audio track choice in the UI; logos |

## Measured on hardware (end-to-end, container, headless Chrome)

- First channel after opening the page (cold tune): ~5-6 s.
- Up/Down onto a pre-warmed neighbour: 1.0-2.5 s, weak channels included.
- After a lock the demodulator needs 1-4 s before packets are clean; the
  stream is only handed to ffmpeg once it is (see `streams.Tuner`).

## Open items

- Teletica 7.1 is not received yet; its physical channel is unknown.
- GPU 1 is shared with another GPU workload on the host; NVENC/NVDEC are
  separate engines, but a very large job could exhaust VRAM (~1 GB is
  enough for four streams).
