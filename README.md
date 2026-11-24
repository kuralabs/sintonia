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

1. Scan for channels and create a configuration:

   ```sh
   dvbv5-scan -o channels.conf /usr/share/dvb/isdb-t/br-Brazil
   docker run --rm -v "$PWD:/work" -w /work ghcr.io/kuralabs/sintonia \
       import channels.conf > config.toml
   ```

   Review `config.toml`: channel numbers are guessed from the service names
   (or the RF channel), and the list order is the Up/Down order. See
   [examples/costa-rica.toml](examples/costa-rica.toml) for every option.

2. Start it:

   ```sh
   docker run -d --name sintonia --restart unless-stopped \
       --gpus all \
       --device /dev/dvb \
       --group-add "$(getent group video | cut -d: -f3)" \
       --network host \
       -v /srv/sintonia/config.toml:/config/config.toml:ro \
       ghcr.io/kuralabs/sintonia:latest
   ```

   `--network host` lets WebRTC reach clients directly. Without it, publish
   `8080/tcp` and `8189/udp` and list the host's LAN address in
   `mediamtx.webrtc_hosts`.

3. Check the set-up at any time with `docker exec sintonia sintonia check`.
   The server runs the same checks at start-up and refuses to start, with
   a hint, if the tuners, ffmpeg, the GPU or MediaMTX are not usable.

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

The tuner selector forces a tuner, which retunes it even if someone else
is watching another multiplex on it (they are moved to another tuner).
**Automatic** picks a tuner already on the right multiplex, or a free one.

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
