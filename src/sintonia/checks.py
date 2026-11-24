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
Start-up checks. Each failure says what is wrong and how to fix it, rather
than letting the first viewer find out.
"""

import re
import shutil
import subprocess

from . import dvb

# ffmpeg 8.0 does not recognise the AAC-LATM audio of some ISDB-Tb
# services; 8.1 does (see docs/spike.md).
MIN_FFMPEG = (8, 1)

NVIDIA_HINT = (
    'Run the container with --gpus all and '
    '-e NVIDIA_DRIVER_CAPABILITIES=video,compute,utility, and choose the '
    'GPU with transcode.gpu. Passing a single GPU other than GPU 0 '
    '(--gpus device=1) breaks NVENC.'
)


class CheckError(Exception):
    """
    A start-up check failed.
    """


def _run(argv, timeout=30):
    """
    Run a short command.

    :param list argv: Command and arguments.
    :param float timeout: Seconds before giving up.

    :return: Exit status and combined output.
    :rtype: tuple
    """
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f'{argv[0]}: not found'
    except subprocess.TimeoutExpired:
        return 124, f'{argv[0]}: timed out'
    return result.returncode, (result.stdout + result.stderr).strip()


def ffmpeg_version(output):
    """
    Parse the version out of ``ffmpeg -version``.

    :param str output: The command output.

    :return: ``(major, minor)``, or ``None`` if it cannot be parsed (for
     example a git build).
    :rtype: tuple or None
    """
    match = re.search(r'ffmpeg version n?(\d+)\.(\d+)', output)
    return (int(match[1]), int(match[2])) if match else None


def check_adapters(config):
    """
    :param config: The whole configuration.
    :type config: objns.Namespace

    :return: The adapters to use.
    :rtype: list

    :raises CheckError: If there are none, or configured ones are missing.
    """
    found = dvb.find_adapters()
    wanted = list(config.tuners.adapters) or found
    if not wanted:
        raise CheckError(
            'no DVB adapters found under /dev/dvb. Is the driver loaded? '
            'In Docker, pass --device /dev/dvb.'
        )
    missing = sorted(set(wanted) - set(found))
    if missing:
        raise CheckError(
            'configured adapters not found: '
            + ', '.join(f'/dev/dvb/adapter{n}' for n in missing)
        )
    return wanted


def check_ffmpeg(transcode):
    """
    :param transcode: The ``transcode`` configuration section.
    :type transcode: objns.Namespace

    :raises CheckError: If ffmpeg is missing, too old, or cannot use the
     configured hardware.
    """
    binary = transcode.ffmpeg
    if shutil.which(binary) is None:
        raise CheckError(
            f'ffmpeg not found at {binary}. Install jellyfin-ffmpeg8 or set '
            'transcode.ffmpeg.'
        )
    status, output = _run([binary, '-hide_banner', '-version'])
    version = ffmpeg_version(output)
    if status != 0:
        raise CheckError(f'{binary} -version failed: {output}')
    if version is not None and version < MIN_FFMPEG:
        raise CheckError(
            f'{binary} is version {version[0]}.{version[1]}; '
            f'{MIN_FFMPEG[0]}.{MIN_FFMPEG[1]} or newer is needed to decode '
            'ISDB-Tb audio.'
        )

    test = [binary, '-hide_banner', '-v', 'error']
    source = ['-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=60',
              '-frames:v', '2']
    hwaccel = transcode.hwaccel
    if hwaccel == 'nvidia':
        gpu = str(transcode.gpu)
        test += ['-init_hw_device', f'cuda=hw:{gpu}', '-filter_hw_device',
                 'hw', *source, '-vf', 'hwupload', '-c:v', 'h264_nvenc',
                 '-gpu', gpu]
        hint = NVIDIA_HINT
    elif hwaccel == 'vaapi':
        test += ['-init_hw_device', f'vaapi=hw:{transcode.vaapi_device}',
                 '-filter_hw_device', 'hw', *source,
                 '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi']
        hint = f'Pass --device {transcode.vaapi_device} to the container.'
    elif hwaccel == 'qsv':
        test += ['-init_hw_device', f'qsv=hw:{transcode.vaapi_device}',
                 '-filter_hw_device', 'hw', *source,
                 '-vf', 'format=nv12,hwupload=extra_hw_frames=16',
                 '-c:v', 'h264_qsv']
        hint = f'Pass --device {transcode.vaapi_device} to the container.'
    else:
        test += [*source, '-c:v', 'libx264']
        hint = 'This ffmpeg build lacks libx264.'
    status, output = _run([*test, '-f', 'null', '-'])
    if status != 0:
        raise CheckError(
            f'{hwaccel} H.264 encoding does not work: {output}\n{hint}'
        )


def check_mediamtx(mediamtx):
    """
    :param mediamtx: The ``mediamtx`` configuration section.
    :type mediamtx: objns.Namespace

    :raises CheckError: If the binary is missing.
    """
    if shutil.which(mediamtx.binary) is None:
        raise CheckError(
            f'mediamtx not found ({mediamtx.binary}). Set mediamtx.binary.'
        )


def preflight(config):
    """
    Run every start-up check.

    :param config: The whole configuration.
    :type config: objns.Namespace

    :return: The adapters to use.
    :rtype: list

    :raises CheckError: On the first failing check.
    """
    adapters = check_adapters(config)
    check_ffmpeg(config.transcode)
    check_mediamtx(config.mediamtx)
    return adapters
