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

from sintonia.config import ConfigError, load, validate

CHANNEL = {
    'number': '6.1',
    'name': 'Canal 6 HD',
    'frequency': 521142857,
    'service_id': 19232,
}


def test_defaults():
    config = validate({'channels': [CHANNEL]})

    assert config.server.port == 8080
    assert config.tuners.adapters == []
    assert config.tuners.prewarm is True
    assert config.transcode.hwaccel == 'nvidia'
    assert config.transcode.output == '1080p60'
    assert config.transcode.audio_normalize is True
    assert config.channels[0].name == 'Canal 6 HD'
    assert config.channels[0].bandwidth == 6000000
    assert config.channels[0].audio == 0


def test_does_not_modify_input():
    document = {'channels': [dict(CHANNEL)]}
    validate(document)
    assert document == {'channels': [CHANNEL]}


def test_requires_channels():
    with pytest.raises(ConfigError, match='channels: required field'):
        validate({})


def test_reports_nested_errors():
    bad = dict(CHANNEL, service_id='x')
    with pytest.raises(ConfigError) as info:
        validate({
            'transcode': {'hwaccel': 'cuda'},
            'channels': [bad],
        })
    message = str(info.value)
    assert 'transcode.hwaccel: unallowed value cuda' in message
    assert 'channels.0.service_id: must be of integer type' in message


def test_rejects_unknown_keys():
    with pytest.raises(ConfigError, match='server.colour: unknown field'):
        validate({'server': {'colour': 'red'}, 'channels': [CHANNEL]})


def test_rejects_duplicated_numbers():
    with pytest.raises(ConfigError, match='duplicated channel numbers: 6.1'):
        validate({'channels': [CHANNEL, dict(CHANNEL, name='Other')]})


def test_rejects_duplicated_adapters():
    with pytest.raises(ConfigError, match='adapter twice'):
        validate({'tuners': {'adapters': [0, 0]}, 'channels': [CHANNEL]})


def test_bitrate_format():
    with pytest.raises(ConfigError, match='video_bitrate'):
        validate({
            'transcode': {'video_bitrate': 'fast'},
            'channels': [CHANNEL],
        })


def test_load(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text(
        '[transcode]\n'
        'gpu = 1\n'
        '\n'
        '[[channels]]\n'
        "number = '6.1'\n"
        "name = 'Canal 6 HD'\n"
        'frequency = 521142857\n'
        'service_id = 19232\n'
    )
    config = load(path)
    assert config.transcode.gpu == 1
    assert config.channels[0].service_id == 19232


def test_load_missing_file(tmp_path):
    with pytest.raises(ConfigError, match='cannot read'):
        load(tmp_path / 'nope.toml')


def test_load_bad_toml(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[server\n')
    with pytest.raises(ConfigError, match='not valid TOML'):
        load(path)
