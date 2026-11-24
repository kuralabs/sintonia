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

import tomllib
from pathlib import Path

import pytest

from sintonia.config import validate
from sintonia.scan import (
    is_oneseg,
    parse,
    rf_channel,
    to_channels,
    to_toml,
)

SAMPLE = (Path(__file__).parent / 'data' / 'dvbv5-scan.conf').read_text()


def test_parse():
    entries = parse(SAMPLE)
    assert [e['name'] for e in entries] == [
        '15-UCR HD', '15-UCR SD', '15-UCR One-Seg',
        'Canal 38 One Seg', '38.02 OPA', '38.02 LIVE One Seg',
    ]
    assert entries[0]['SERVICE_ID'] == '32'
    assert entries[0]['FREQUENCY'] == '479142857'


def test_oneseg_detection():
    by_name = {e['name']: e for e in parse(SAMPLE)}
    assert is_oneseg(by_name['15-UCR One-Seg'])
    assert is_oneseg(by_name['38.02 LIVE One Seg'])
    assert not is_oneseg(by_name['15-UCR HD'])
    assert not is_oneseg(by_name['38.02 OPA'])
    # By service type bits alone.
    assert is_oneseg({'name': 'Mobile', 'SERVICE_ID': '19256'})


def test_to_channels():
    channels = to_channels(parse(SAMPLE))
    assert channels == [
        {'number': '15.1', 'name': '15-UCR HD',
         'frequency': 479142857, 'service_id': 32},
        {'number': '15.2', 'name': '15-UCR SD',
         'frequency': 479142857, 'service_id': 33},
        {'number': '38.02', 'name': '38.02 OPA',
         'frequency': 617142857, 'service_id': 1793},
    ]


def test_to_channels_with_oneseg():
    numbers = [c['number'] for c in to_channels(parse(SAMPLE), True)]
    assert numbers == ['15.1', '15.2', '15.3', '38.1', '38.02', '38.2']


def test_numbers_from_rf_channel():
    entries = [
        {'name': 'Telefides', 'SERVICE_ID': '53920',
         'FREQUENCY': '629142857'},
        {'name': 'CANAL 6 HD', 'SERVICE_ID': '19232',
         'FREQUENCY': '521142857'},
        {'name': 'Teleuno', 'SERVICE_ID': '53921',
         'FREQUENCY': '629142857'},
        {'name': 'BETHEL 50.31', 'SERVICE_ID': '528',
         'FREQUENCY': '689142857'},
        {'name': 'Odd', 'SERVICE_ID': '1', 'FREQUENCY': '100000000'},
    ]
    assert [c['number'] for c in to_channels(entries)] == [
        '1', '6.1', '40.1', '40.2', '50.31',
    ]


@pytest.mark.parametrize('frequency, channel', [
    (177142857, 7),
    (213142857, 13),
    (473142857, 14),
    (629142857, 40),
    (689142857, 50),
    (100000000, None),
    (476000000, None),
])
def test_rf_channel(frequency, channel):
    assert rf_channel(frequency) == channel


def test_toml_round_trip():
    entries = parse(SAMPLE) + [{
        'name': "Bob's TV", 'SERVICE_ID': '10', 'FREQUENCY': '1',
    }]
    text = to_toml(to_channels(entries))
    assert "name = '15-UCR HD'" in text
    assert 'name = "Bob\'s TV"' in text
    config = validate(tomllib.loads(text))
    assert "Bob's TV" in [channel.name for channel in config.channels]


def test_duplicates_are_dropped():
    entries = parse(SAMPLE)
    assert to_channels(entries + entries) == to_channels(entries)
