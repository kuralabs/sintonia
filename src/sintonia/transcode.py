# Copyright (C) 2026 KuraLabs S.R.L.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
ffmpeg command lines: one service of a multiplex (read from stdin) to
browser-friendly H.264 + Opus, published to MediaMTX over RTSP.

Broadcast video is interlaced, so it is always deinterlaced; audio is
AAC-LATM, which browsers cannot play, so it is always re-encoded. The
settings come from the measurements in ``docs/spike.md``.
"""

# (width, height, frames per second, field-rate deinterlacing)
OUTPUTS = {
    '1080p60': (1920, 1080, 60, True),
    '1080p30': (1920, 1080, 30, False),
    '720p60': (1280, 720, 60, True),
}

# Short probe: enough to find the streams, including LATM audio, while
# keeping channel changes fast.
PROBE = ['-analyzeduration', '500K', '-probesize', '1M']
# Longer probe, for weak multiplexes where the first packets after a tune
# are corrupt and dropped (used for retries and pre-warming).
PROBE_THOROUGH = ['-analyzeduration', '3M', '-probesize', '8M']

# Fast-reacting loudness levelling; a longer window adds its lookahead to
# every channel change.
NORMALIZE = 'dynaudnorm=f=100:g=5'

CPU_PRESET = 'veryfast'


def _maxrate(bitrate):
    """
    Peak bitrate for a target bitrate, as an ffmpeg value.

    :param str bitrate: Target such as ``'8M'`` or ``'800k'``.

    :return: 1.25 times the target, in kbit/s (``'10000k'``).
    :rtype: str
    """
    units = {'k': 1, 'm': 1000}
    suffix = bitrate[-1].lower()
    scale = units.get(suffix)
    number = float(bitrate[:-1] if scale else bitrate)
    kbits = number * (scale or 0.001)
    return f'{round(kbits * 1.25)}k'


def _video(transcode):
    """
    Input options, filter chain and encoder options for the video.

    :param transcode: The ``transcode`` configuration section.
    :type transcode: objns.Namespace

    :return: ``(input options, filter, encoder options)``.
    :rtype: tuple
    """
    width, height, fps, field_rate = OUTPUTS[transcode.output]
    scale = (width, height) != (1920, 1080)
    gop = str(fps // 2)
    profile = transcode.h264_profile
    rate = [
        '-b:v', transcode.video_bitrate,
        '-maxrate', _maxrate(transcode.video_bitrate),
        '-bufsize', _maxrate(transcode.video_bitrate),
    ]
    # No B-frames (WebRTC), and a short GOP so new viewers start quickly.
    common = ['-g', gop, '-bf', '0']

    hwaccel = transcode.hwaccel
    if hwaccel == 'nvidia':
        gpu = str(transcode.gpu)
        mode = 'send_field' if field_rate else 'send_frame'
        vf = f'yadif_cuda=mode={mode}:parity=auto:deint=interlaced'
        if scale:
            vf += f',scale_cuda={width}:{height}'
        return (
            ['-hwaccel', 'cuda', '-hwaccel_device', gpu,
             '-hwaccel_output_format', 'cuda'],
            vf,
            ['-c:v', 'h264_nvenc', '-gpu', gpu, '-preset', 'p4',
             '-tune', 'll', '-profile:v', profile, '-forced-idr', '1',
             *rate, *common],
        )

    if hwaccel == 'vaapi':
        rate_mode = 'field' if field_rate else 'frame'
        vf = f'deinterlace_vaapi=mode=motion_adaptive:rate={rate_mode}'
        if scale:
            vf += f',scale_vaapi=w={width}:h={height}'
        return (
            ['-hwaccel', 'vaapi', '-hwaccel_device', transcode.vaapi_device,
             '-hwaccel_output_format', 'vaapi'],
            vf,
            ['-c:v', 'h264_vaapi', '-profile:v', profile, *rate, *common],
        )

    if hwaccel == 'qsv':
        rate_mode = 'field' if field_rate else 'frame'
        vf = f'vpp_qsv=deinterlace=advanced:rate={rate_mode}'
        if scale:
            vf += f':w={width}:h={height}'
        return (
            ['-hwaccel', 'qsv', '-qsv_device', transcode.vaapi_device,
             '-hwaccel_output_format', 'qsv'],
            vf,
            ['-c:v', 'h264_qsv', '-profile:v', profile, '-low_power', '0',
             *rate, *common],
        )

    # CPU fallback.
    mode = 'send_field' if field_rate else 'send_frame'
    vf = f'bwdif=mode={mode}:parity=auto:deint=interlaced'
    if scale:
        vf += f',scale={width}:{height}'
    return (
        [],
        vf,
        ['-c:v', 'libx264', '-preset', CPU_PRESET, '-tune', 'zerolatency',
         '-profile:v', profile, *rate, *common],
    )


def command(transcode, service_id, audio, url, thorough=False):
    """
    Build the ffmpeg command for one service.

    :param transcode: The ``transcode`` configuration section.
    :type transcode: objns.Namespace
    :param int service_id: MPEG-TS program number of the service.
    :param int audio: Index of the audio track within the service.
    :param str url: RTSP URL to publish to.
    :param bool thorough: Probe the input longer (slower to start, but
     works on weak signals).

    :return: The command, as an argument list.
    :rtype: list
    """
    hw_input, vf, video_encoder = _video(transcode)
    af = 'aresample=async=1'
    if transcode.audio_normalize:
        af += f',{NORMALIZE}'
    return [
        transcode.ffmpeg,
        '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'warning',
        # Drop packets the demodulator flagged as corrupt (they arrive for
        # about a second after every tune).
        '-fflags', '+discardcorrupt+genpts',
        *(PROBE_THOROUGH if thorough else PROBE),
        *hw_input,
        '-f', 'mpegts', '-i', 'pipe:0',
        '-map', f'0:p:{service_id}:v:0',
        '-map', f'0:p:{service_id}:a:{audio}',
        '-vf', vf,
        *video_encoder,
        '-af', af,
        '-ac', '2', '-ar', '48000',
        '-c:a', 'libopus', '-b:a', transcode.audio_bitrate,
        '-f', 'rtsp', '-rtsp_transport', 'tcp', url,
    ]
