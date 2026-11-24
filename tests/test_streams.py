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
from objns import Namespace

from sintonia.streams import (
    StreamError,
    choose_tuner,
    neighbours,
    path_name,
)


class FakeStream:
    def __init__(self, prewarm=False, last_viewed=0.0):
        self.prewarm = prewarm
        self.last_viewed = last_viewed


class FakeTuner:
    def __init__(self, adapter, frequency=None, state='idle', streams=()):
        self.adapter = adapter
        self.frequency = frequency
        self.state = state
        self.streams = set(streams)

    def last_viewed(self):
        return max((s.last_viewed for s in self.streams), default=0.0)


MUX_A = 479142857
MUX_B = 521142857
MUX_C = 629142857


def test_path_name():
    assert path_name('6.1') == 'ch-6-1'
    assert path_name('Canal 7') == 'ch-canal-7'
    assert path_name('  15.1  ') == 'ch-15-1'


def test_reuses_tuner_on_same_multiplex():
    busy = FakeTuner(0, MUX_A, 'locked', [FakeStream()])
    tuners = [busy, FakeTuner(1)]
    assert choose_tuner(tuners, MUX_A) == (busy, False)


def test_ignores_failed_tuner_on_same_multiplex():
    failed = FakeTuner(0, MUX_A, 'nolock', [FakeStream()])
    idle = FakeTuner(1)
    assert choose_tuner([failed, idle], MUX_A) == (idle, True)


def test_prefers_idle_tuner():
    busy = FakeTuner(0, MUX_A, 'locked', [FakeStream()])
    idle = FakeTuner(1)
    assert choose_tuner([busy, idle], MUX_B) == (idle, True)


def test_evicts_prewarm_before_viewers():
    watched = FakeTuner(0, MUX_A, 'locked', [FakeStream()])
    warm = FakeTuner(1, MUX_B, 'locked', [FakeStream(prewarm=True)])
    assert choose_tuner([watched, warm], MUX_C) == (warm, True)


def test_takes_least_recently_viewed_when_full():
    old = FakeTuner(0, MUX_A, 'locked', [FakeStream(last_viewed=10.0)])
    new = FakeTuner(1, MUX_B, 'locked', [FakeStream(last_viewed=20.0)])
    assert choose_tuner([new, old], MUX_C) == (old, True)


def test_requested_tuner():
    on_a = FakeTuner(0, MUX_A, 'locked', [FakeStream()])
    other = FakeTuner(1, MUX_B, 'locked', [FakeStream()])
    assert choose_tuner([on_a, other], MUX_A, 1) == (other, True)
    assert choose_tuner([on_a, other], MUX_A, 0) == (on_a, False)


def test_requested_tuner_must_exist():
    with pytest.raises(StreamError) as info:
        choose_tuner([FakeTuner(0)], MUX_A, 7)
    assert info.value.status == 404


CHANNELS = [Namespace(number=n) for n in ('4.1', '6.1', '7.1', '13.1')]


@pytest.mark.parametrize('number, expected', [
    ('6.1', ['4.1', '7.1']),
    ('4.1', ['6.1', '13.1']),
    ('13.1', ['4.1', '7.1']),
    ('9.9', []),
])
def test_neighbours(number, expected):
    assert neighbours(CHANNELS, number) == expected


def test_neighbours_small_lists():
    assert neighbours(CHANNELS[:1], '4.1') == []
    assert neighbours(CHANNELS[:2], '4.1') == ['6.1']
