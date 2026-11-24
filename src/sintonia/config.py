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
Configuration loading and validation.

The configuration is a TOML file. It is validated and normalised (defaults
filled in) with a ferberus schema, then exposed as an objns ``Namespace``.
"""

import tomllib
from copy import deepcopy
from pathlib import Path

from ferberus import Validator
from objns import Namespace

HWACCELS = ['nvidia', 'vaapi', 'qsv', 'none']
OUTPUTS = ['1080p60', '1080p30', '720p60']
H264_PROFILES = ['high', 'main', 'baseline']

BITRATE = r'^[0-9]+(\.[0-9]+)?[kKmM]?$'

CHANNEL_SCHEMA = {
    'number': {'type': 'string', 'required': True, 'empty': False},
    'name': {'type': 'string', 'required': True, 'empty': False},
    'frequency': {'type': 'integer', 'required': True, 'min': 1},
    'service_id': {
        'type': 'integer', 'required': True, 'min': 1, 'max': 65535,
    },
    'bandwidth': {'type': 'integer', 'default': 6000000, 'min': 1},
    'audio': {'type': 'integer', 'default': 0, 'min': 0},
}

SCHEMA = {
    'server': {
        'type': 'dict',
        'default': {},
        'schema': {
            'host': {'type': 'string', 'default': '0.0.0.0'},
            'port': {
                'type': 'integer', 'default': 8080, 'min': 1, 'max': 65535,
            },
        },
    },
    'tuners': {
        'type': 'dict',
        'default': {},
        'schema': {
            # Empty means every /dev/dvb/adapterN found at startup.
            'adapters': {
                'type': 'list',
                'default': [],
                'schema': {'type': 'integer', 'min': 0},
            },
            'lock_timeout': {
                'type': 'number', 'default': 5.0, 'min': 0.5,
            },
            # Keep the channels next to the current one live on spare
            # tuners, so that Up/Down does not wait for a retune.
            'prewarm': {'type': 'boolean', 'default': True},
        },
    },
    'transcode': {
        'type': 'dict',
        'default': {},
        'schema': {
            'ffmpeg': {
                'type': 'string',
                'default': '/usr/lib/jellyfin-ffmpeg/ffmpeg',
            },
            'hwaccel': {
                'type': 'string', 'default': 'nvidia', 'allowed': HWACCELS,
            },
            'gpu': {'type': 'integer', 'default': 0, 'min': 0},
            'vaapi_device': {
                'type': 'string', 'default': '/dev/dri/renderD128',
            },
            'output': {
                'type': 'string', 'default': '1080p60', 'allowed': OUTPUTS,
            },
            'video_bitrate': {
                'type': 'string', 'default': '8M', 'regex': BITRATE,
            },
            'h264_profile': {
                'type': 'string', 'default': 'high',
                'allowed': H264_PROFILES,
            },
            'audio_bitrate': {
                'type': 'string', 'default': '128k', 'regex': BITRATE,
            },
            'audio_normalize': {'type': 'boolean', 'default': True},
        },
    },
    'mediamtx': {
        'type': 'dict',
        'default': {},
        'schema': {
            'binary': {'type': 'string', 'default': 'mediamtx'},
            'rtsp_port': {'type': 'integer', 'default': 8554},
            'api_port': {'type': 'integer', 'default': 9997},
            'webrtc_port': {'type': 'integer', 'default': 8889},
            'webrtc_udp_port': {'type': 'integer', 'default': 8189},
            # Extra IPs or hostnames advertised as ICE candidates, for when
            # the container cannot see the address clients reach it at.
            'webrtc_hosts': {
                'type': 'list', 'default': [],
                'schema': {'type': 'string'},
            },
        },
    },
    'streams': {
        'type': 'dict',
        'default': {},
        'schema': {
            # Seconds without viewers before a stream is stopped.
            'idle_timeout': {'type': 'number', 'default': 30, 'min': 1},
        },
    },
    'channels': {
        'type': 'list',
        'required': True,
        'minlength': 1,
        'schema': {'type': 'dict', 'schema': CHANNEL_SCHEMA},
    },
}


class ConfigError(Exception):
    """
    The configuration is missing, unreadable or invalid.
    """


def _format_errors(errors, prefix=''):
    """
    Flatten ferberus' nested error tree into readable lines.

    :param dict errors: ``Validator.errors``.
    :param str prefix: Dotted path of the parent field.

    :return: One ``path: message`` line per error.
    :rtype: list
    """
    lines = []
    for field, messages in errors.items():
        path = f'{prefix}.{field}' if prefix else str(field)
        for message in messages:
            if isinstance(message, dict):
                lines.extend(_format_errors(message, path))
            else:
                lines.append(f'{path}: {message}')
    return lines


def validate(document):
    """
    Validate and normalise a configuration document.

    :param dict document: Parsed TOML.

    :return: The configuration, with defaults filled in.
    :rtype: objns.Namespace

    :raises ConfigError: If the document does not match the schema.
    """
    validator = Validator(SCHEMA)
    if not validator.validate(deepcopy(document)):
        raise ConfigError(
            'invalid configuration:\n  '
            + '\n  '.join(_format_errors(validator.errors))
        )
    normalized = validator.document

    numbers = [channel['number'] for channel in normalized['channels']]
    duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicated:
        raise ConfigError(
            f'duplicated channel numbers: {", ".join(duplicated)}'
        )

    adapters = normalized['tuners']['adapters']
    if len(set(adapters)) != len(adapters):
        raise ConfigError('tuners.adapters lists an adapter twice')

    channels = normalized.pop('channels')
    config = Namespace(normalized)
    # objns converts mappings but not lists of mappings.
    config.channels = [Namespace(channel) for channel in channels]
    return config


def load(path):
    """
    Load, validate and normalise a configuration file.

    :param path: Path to the TOML file.
    :type path: str or pathlib.Path

    :return: The configuration, with defaults filled in.
    :rtype: objns.Namespace

    :raises ConfigError: If the file cannot be read or is invalid.
    """
    path = Path(path)
    try:
        with path.open('rb') as fd:
            document = tomllib.load(fd)
    except OSError as e:
        raise ConfigError(f'cannot read {path}: {e.strerror}') from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{path} is not valid TOML: {e}') from e
    return validate(document)
