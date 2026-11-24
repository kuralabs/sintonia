# Phase 0 spike: results

2026-11-24. TBS6814 (ISDB-Tb, Costa Rica off-air), RTX 3060 (GPU 1),
jellyfin-ffmpeg 8.1.3, MediaMTX v1.21.3, headless Chromium.

Pipeline, run by hand: tuner → ffmpeg (NVDEC → `yadif_cuda` send_field →
`h264_nvenc`; audio → `aresample` → `dynaudnorm` → stereo → Opus) → RTSP
into MediaMTX → WHEP → `<video>`.

## Playback (Chrome desktop, headless)

| Measure | Result |
|---|---|
| Video | H.264 1920x1080, 59 fps, 0 packets lost |
| Audio | Opus 48 kHz, 0 packets lost |
| Page load to first frame | 988 ms (encoder GOP 60) |
| Jitter buffer | 86 ms video, 202 ms audio |

MediaMTX advertises `profile-level-id=42e01f` (Constrained Baseline) in the
SDP while the stream is High profile; Chrome desktop decodes it fine.
**Android is not tested yet**; the H.264 profile is a config option in case
a phone's decoder objects.

## Where channel-change time goes

| Step | Time | Notes |
|---|---|---|
| Frontend lock, polled with `FE_READ_STATUS` | 1.7-1.9 s | hardware/driver bound |
| ... as reported by `dvbv5-zap` | ~2.8 s | it reports status once a second |
| Demod settling after lock | ~1-1.5 s | packets carry TEI (seen in driver tests) |
| ffmpeg probe (`-analyzeduration 500K -probesize 1M`) | ~0.9 s | audio still detected, incl. 5.1 LATM |
| CUDA + NVENC init | 0.2 s | |
| `dynaudnorm=f=150:g=15` lookahead | **+1.7 s** | delays the whole output |
| `dynaudnorm=f=100:g=5` lookahead | +0.2 s | |
| Browser: WHEP + wait for keyframe | ≤ 1 s | GOP-bound; GOP 30 halves the wait |

Measured end to end (tuner command to MediaMTX path ready, before the
fixes below): 5.9-7.5 s across multiplexes, ~2.6 s within one multiplex.

## Audio levelling (EBU R128 integrated loudness, LUFS)

| Service | Raw | `f=150:g=15` | `f=100:g=5` |
|---|---|---|---|
| UCR | -15.8 | -12.9 | -11.2 |
| Canal 3 | -19.5 | -10.5 | -9.6 |
| Canal 6 | -14.4 | -10.8 | -10.0 |
| Telefides | -25.0 | -11.3 | -10.2 |
| **Spread** | **10.6 LU** | 2.4 LU | **1.6 LU** |

`f=100:g=5` levels better and starts 1.5 s sooner: it is the default.

## Decisions taken from the spike

1. **Tune from Python with the DVB ioctls** (`FE_SET_PROPERTY`,
   `FE_READ_STATUS`, `DTV_STAT_*`) instead of `dvbv5-zap`: saves ~1 s per
   cross-multiplex change, gives lock/signal/C/N directly, and Python reads
   `dvrN` itself for the fan-out anyway. `dvb-tools` is then only needed to
   produce scan files for `sintonia import`, not at runtime.
2. **Audio levelling**: `dynaudnorm=f=100:g=5` (default on).
3. **Encoder GOP 30** (0.5 s at 60 fps) to halve the browser's keyframe
   wait; bitrate is not a concern on a LAN.
4. **Probe**: `-analyzeduration 500K -probesize 1M`.
5. **Pre-tuning neighbours moves into the tuner-pool phase**: a cross-
   multiplex change has a hardware floor of ~3.5-4 s (lock + settling +
   keyframe). Keeping the channels above and below the current one live on
   spare tuners turns Up/Down into a WHEP reconnect (~0.5-1 s). Pre-tuned
   streams yield to real viewers.
6. Disable MediaMTX's MoQ listener (on by default in v1.21).

Expected after 1-4: ~1.5-2 s within a multiplex, ~4 s across multiplexes,
~0.5-1 s for Up/Down onto a pre-tuned neighbour.
