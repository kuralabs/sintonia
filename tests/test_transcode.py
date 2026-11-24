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

import pytest

from sintonia.config import validate
from sintonia.transcode import _maxrate, command

CHANNEL = {
    'number': '6.1',
    'name': 'Canal 6 HD',
    'frequency': 521142857,
    'service_id': 19232,
}
URL = 'rtsp://127.0.0.1:8554/ch-6-1'


def build(**transcode):
    config = validate({'transcode': transcode, 'channels': [CHANNEL]})
    return command(config.transcode, 19232, 0, URL)


def option(argv, name):
    return argv[argv.index(name) + 1]


def test_nvidia_default():
    argv = build(gpu=1)

    assert argv[0] == '/usr/lib/jellyfin-ffmpeg/ffmpeg'
    assert option(argv, '-hwaccel') == 'cuda'
    assert option(argv, '-hwaccel_device') == '1'
    assert option(argv, '-gpu') == '1'
    assert option(argv, '-c:v') == 'h264_nvenc'
    assert option(argv, '-vf') == (
        'yadif_cuda=mode=send_field:parity=auto:deint=interlaced'
    )
    assert option(argv, '-g') == '30'
    assert option(argv, '-bf') == '0'
    assert option(argv, '-b:v') == '8M'
    assert option(argv, '-maxrate') == '10000k'
    assert option(argv, '-i') == 'pipe:0'
    assert argv[argv.index('-map') + 1] == '0:p:19232:v:0'
    assert '0:p:19232:a:0' in argv
    assert option(argv, '-af') == 'aresample=async=1,dynaudnorm=f=100:g=5'
    assert option(argv, '-c:a') == 'libopus'
    assert argv[-1] == URL


def test_hwaccel_flags_come_before_input():
    argv = build()
    assert argv.index('-hwaccel') < argv.index('-i')


def test_without_normalize():
    assert option(build(audio_normalize=False), '-af') == 'aresample=async=1'


@pytest.mark.parametrize('output, vf, gop', [
    ('1080p30', 'yadif_cuda=mode=send_frame:parity=auto:deint=interlaced',
     '15'),
    ('720p60', 'yadif_cuda=mode=send_field:parity=auto:deint=interlaced'
               ',scale_cuda=1280:720', '30'),
])
def test_outputs(output, vf, gop):
    argv = build(output=output)
    assert option(argv, '-vf') == vf
    assert option(argv, '-g') == gop


@pytest.mark.parametrize('hwaccel, encoder, vf_start', [
    ('vaapi', 'h264_vaapi', 'deinterlace_vaapi='),
    ('qsv', 'h264_qsv', 'vpp_qsv='),
    ('none', 'libx264', 'bwdif='),
])
def test_other_hwaccels(hwaccel, encoder, vf_start):
    argv = build(hwaccel=hwaccel)
    assert option(argv, '-c:v') == encoder
    assert option(argv, '-vf').startswith(vf_start)
    assert '-gpu' not in argv


def test_cpu_has_no_hwaccel():
    assert '-hwaccel' not in build(hwaccel='none')


def test_probe():
    argv = build()
    assert option(argv, '-analyzeduration') == '500K'
    config = validate({'channels': [CHANNEL]})
    thorough = command(config.transcode, 19232, 0, URL, thorough=True)
    assert option(thorough, '-analyzeduration') == '3M'
    assert option(thorough, '-probesize') == '8M'


def test_audio_track_index():
    config = validate({'channels': [CHANNEL]})
    argv = command(config.transcode, 53921, 2, URL)
    assert '0:p:53921:a:2' in argv


@pytest.mark.parametrize('bitrate, expected', [
    ('8M', '10000k'),
    ('800k', '1000k'),
    ('2.5M', '3125k'),
    ('4000000', '5000k'),
])
def test_maxrate(bitrate, expected):
    assert _maxrate(bitrate) == expected
