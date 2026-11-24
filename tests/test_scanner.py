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

from sintonia import scanner
from sintonia.config import validate
from sintonia.scan import to_toml
from sintonia.si import Multiplex

DATA = Path(__file__).parent / 'data' / 'si'


def result(channel, errors=0.0):
    """
    A probe() result built from a capture of the given UHF channel.
    """
    multiplex = Multiplex()
    multiplex.feed((DATA / f'uhf{channel}.ts').read_bytes())
    return {
        'channel': channel, 'frequency': scanner.frequency(channel),
        'locked': True, 'signal_dbm': -70.0, 'cnr_db': 20.0,
        'errors': errors, 'multiplex': multiplex,
    }


def numbers(channels):
    return {c['number']: c['name'] for c in channels}


@pytest.mark.parametrize('channel, frequency', [
    (7, 177142857), (13, 213142857), (14, 473142857), (18, 497142857),
    (69, 803142857),
])
def test_frequency(channel, frequency):
    assert scanner.frequency(channel) == frequency


def test_frequency_out_of_band():
    with pytest.raises(ValueError):
        scanner.frequency(6)


def test_parse_channels():
    assert scanner.parse_channels('18, 14-16,15') == [14, 15, 16, 18]
    assert len(scanner.parse_channels(scanner.DEFAULT_CHANNELS)) == 63


@pytest.mark.parametrize('spec', ['', 'x', '20-14', '14-70', '5'])
def test_parse_channels_invalid(spec):
    with pytest.raises(ValueError):
        scanner.parse_channels(spec)


def test_numbers_like_a_tv():
    channels = scanner.to_channels([result(15), result(22)])
    assert numbers(channels) == {
        '6.1': 'CANAL 6 HD', '6.2': 'Ch 4 HD',
        '15.1': '15-UCR HD', '15.2': '15-UCR SD',
    }
    assert [c['number'] for c in channels] == ['6.1', '6.2', '15.1', '15.2']


def test_oneseg_included_on_request():
    channels = scanner.to_channels([result(22)], include_oneseg=True)
    assert numbers(channels) == {
        '6.1': 'CANAL 6 HD', '6.2': 'Ch 4 HD', '6.3': 'One Seg CH 6',
    }


def test_without_nit_falls_back_to_names_then_rf_channel():
    channels = scanner.to_channels([result(40)])
    assert numbers(channels) == {'40.1': 'Telefides', '40.2': 'Teleuno'}


def test_collision_moves_service_to_number_in_its_name():
    channels = scanner.to_channels([result(50)])
    assert numbers(channels) == {
        '50.1': 'BETHEL CR', '50.2': 'BETHEL CR', '50.31': 'BETHEL 50.31',
    }


def test_repeater_keeps_cleaner_reception():
    weak = result(22, errors=0.4)
    clean = result(22, errors=0.0)
    clean['channel'] = 23
    clean['frequency'] = scanner.frequency(23)
    channels = scanner.to_channels([weak, clean])
    assert len(channels) == 2
    assert {c['frequency'] for c in channels} == {scanner.frequency(23)}


def test_unlocked_and_unreadable_results_are_skipped():
    empty = {**result(15), 'multiplex': Multiplex()}
    unlocked = {**result(22), 'locked': False, 'multiplex': None}
    assert scanner.to_channels([empty, unlocked]) == []


def test_describe():
    assert scanner.describe(result(22)) == (
        'UHF 22, 521.143 MHz, -70.0 dBm, C/N 20.0 dB'
    )
    assert scanner.describe(result(22, errors=0.25)).endswith(
        'weak: 25% corrupt packets'
    )


def test_output_is_a_valid_configuration():
    channels = scanner.to_channels(
        [result(n) for n in (15, 22, 29, 40, 50)]
    )
    text = to_toml(channels, header=('Test.',))
    assert text.startswith('# Test.\n')
    assert '# UHF 22, 521.143 MHz' in text
    parsed = tomllib.loads(text)
    assert 'comment' not in parsed['channels'][0]
    assert numbers(parsed['channels'])['2.2'] == 'Monumental'
    validate(parsed)


def test_scan_spreads_channels_and_survives_a_busy_tuner(monkeypatch):
    def probe(adapter, channel, *args):
        if adapter == 1:
            raise OSError(16, 'Device or resource busy')
        return {'channel': channel, 'adapter': adapter}

    monkeypatch.setattr(scanner, 'probe', probe)
    seen = []
    results = scanner.scan([0, 1], [14, 15, 16], progress=seen.append)
    assert [r['channel'] for r in results] == [14, 15, 16]
    assert {r['adapter'] for r in results} == {0}
    assert len(seen) == 3


def test_scan_without_usable_tuners(monkeypatch):
    def probe(adapter, channel, *args):
        raise OSError(16, 'Device or resource busy')

    monkeypatch.setattr(scanner, 'probe', probe)
    with pytest.raises(scanner.ScanError, match='no usable tuner'):
        scanner.scan([0], [14])
