# Sintonía

[![ci](https://github.com/kuralabs/sintonia/actions/workflows/ci.yml/badge.svg)](https://github.com/kuralabs/sintonia/actions/workflows/ci.yml)
[![image](https://github.com/kuralabs/sintonia/actions/workflows/image.yml/badge.svg)](https://github.com/kuralabs/sintonia/actions/workflows/image.yml)

Simple, watch-TV-focused web app for live over-the-air TV. One container,
given your DVB tuners and a GPU, serves a page that plays live channels in
the browser over WebRTC, with hardware transcoding.

Open `http://<host>:8080`: the last channel you watched starts playing, the
pane on the right lists the channels, and Up/Down changes channel.

- **Live, low latency**: WebRTC (WHEP) through MediaMTX, under a second
  behind the broadcast.
- **Quick channel changes**: the channels next to the one you are watching
  are kept live on spare tuners, so Up/Down takes about a second.
- **Hardware transcoding** with jellyfin-ffmpeg: interlaced 1080i becomes
  smooth 1080p60, and badly levelled channels are evened out.
- **Phones**: swipe up/down to change channel, landscape fullscreen, screen
  kept on, installable to the home screen.

Built and tested with ISDB-Tb (Costa Rica) on a TBS6814 quad tuner and an
NVIDIA RTX 3060. See [docs/plan.md](docs/plan.md) for the design and
[docs/spike.md](docs/spike.md) for the measurements behind it.

## Requirements

- Linux with working DVB tuners (`/dev/dvb/adapterN`).
- A GPU for transcoding: NVIDIA (NVENC), or Intel/AMD via VAAPI or QSV.
  CPU transcoding works but is heavy.
- Docker; for NVIDIA, the NVIDIA Container Toolkit.
- Clients: Chrome on desktop or Android.

## Run

1. Scan for channels and create a configuration (about two minutes with
   four tuners; stop Sintonía first if it is running, the scan needs the
   tuners):

   ```sh
   docker run --rm --device /dev/dvb \
       --group-add "$(getent group video | cut -d: -f3)" \
       ghcr.io/kuralabs/sintonia scan > config.toml
   ```

   Channels are numbered as on a TV, and each one is annotated with its
   RF channel and signal quality; channels marked weak may stutter. The
   list order is the Up/Down order. Add the other sections from
   [examples/costa-rica.toml](examples/costa-rica.toml) as needed, such as
   `[transcode]` to choose the GPU. `scan --help` lists the options, such as
   `--channels 14-51` to scan fewer channels.

   Alternatively, convert an existing `dvbv5-scan` channel file with
   `sintonia import channels.conf`.

2. Start it, from the directory holding `config.toml`:

   ```sh
   docker run -d --name sintonia --restart unless-stopped \
       --gpus all \
       --device /dev/dvb \
       --group-add "$(getent group video | cut -d: -f3)" \
       --network host \
       -v "$PWD/config.toml:/config/config.toml:ro" \
       ghcr.io/kuralabs/sintonia:latest
   ```

   `--network host` lets WebRTC reach clients directly. Without it, publish
   `8080/tcp` and `8189/udp` and list the host's LAN address in
   `mediamtx.webrtc_hosts`. With it, Sintonía's ports are the host's: if
   something else already uses 8080, set `server.port`. The container runs
   as an unprivileged user, so `config.toml` must be readable by others.

3. Check the set-up at any time with `docker exec sintonia sintonia check`.
   The server runs the same checks at start-up and refuses to start, with
   a hint, if the tuners, ffmpeg, the GPU or MediaMTX are not usable.

[examples/sintonia.sh](examples/sintonia.sh) does all this: put it next to
`config.toml` and run `./sintonia.sh` to pull the image and (re)start the
server, waiting until it is healthy, or `./sintonia.sh scan` to scan into
`config.scan.toml` while the server is paused.

## Deploy behind NGINX

Serve Sintonía over HTTPS: phones only keep the screen on while watching,
and only offer to install Sintonía to the home screen, on HTTPS pages.
[examples/nginx.conf](examples/nginx.conf) is a complete site:

- Bind Sintonía to the loopback interface, so only NGINX reaches it:
  `server.host = '127.0.0.1'`, and the port NGINX proxies to.
- NGINX carries the page, the API, the state updates (Server-Sent Events,
  unbuffered) and the WebRTC signalling. The video does not pass through
  it: browsers receive it from Sintonía directly over UDP port 8189, which
  must be reachable from the clients.
- Sintonía has no login, and every viewer can tie up a tuner. Keep it on
  your LAN or VPN, or restrict it with NGINX's `allow` and `deny`.

The certificate can come from Let's Encrypt even when the name only
resolves on your LAN (for example, a rewrite in your LAN's DNS server):
use the DNS challenge, which needs no inbound connection.

### NVIDIA GPUs

Pass **all** GPUs (`--gpus all`) and choose one with `transcode.gpu`:
when a container is given a single GPU other than GPU 0
(`--gpus '"device=1"'`), NVENC fails with "unsupported device". Giving one
GPU to Jellyfin and another to Sintonía works this way: pin Jellyfin to
GPU 0 (`--gpus '"device=0"'` works) and set `gpu = 1` here.

## Use

| Action | Desktop | Phone |
|---|---|---|
| Previous / next channel | Up / Down | Swipe down / up on the video |
| Fullscreen | F, double-click, or the corner button | Corner button |
| Show / hide the channel list | P, or the top-right button | Top-right button |
| Mute | M | |
| Signal details | I, or click the signal row | Tap the signal row |

Tuners are chosen automatically: a tuner already on the right multiplex,
or a free one, or one carrying only pre-warmed channels. The bottom of the
channel list shows the signal quality of the channel being watched; open
it for every tuner's signal, C/N and share of corrupt packets.

## Development

```sh
pip install -e '.[test,lint]'
ruff check .
pytest
```

The tests need no tuners or GPU. Code style: ruff (lint only), lines up
to 79 characters, single quotes, Sphinx-style docstrings.

## License

Copyright (C) 2026 KuraLabs S.R.L.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
