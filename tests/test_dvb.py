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

import ctypes

from sintonia import dvb


def test_abi_matches_kernel_headers():
    # Values printed by a C program compiled against the x86_64 Linux uapi
    # headers <linux/dvb/frontend.h> and <linux/dvb/dmx.h>.
    assert ctypes.sizeof(dvb.DtvStats) == 9
    assert ctypes.sizeof(dvb.DtvFeStats) == 37
    assert ctypes.sizeof(dvb.DtvProperty) == 76
    assert dvb.DtvProperty.u.offset == 16
    assert dvb.DtvProperty.result.offset == 72
    assert ctypes.sizeof(dvb.DtvProperties) == 16
    assert ctypes.sizeof(dvb.DmxPesFilterParams) == 20

    assert dvb.FE_SET_PROPERTY == 0x40106F52
    assert dvb.FE_GET_PROPERTY == 0x80106F53
    assert dvb.FE_READ_STATUS == 0x80046F45
    assert dvb.DMX_SET_PES_FILTER == 0x40146F2C
    assert dvb.DMX_SET_BUFFER_SIZE == 0x6F2D
    assert dvb.DMX_STOP == 0x6F2A
    assert dvb.DMX_START == 0x6F29


def test_find_adapters(tmp_path, monkeypatch):
    for name, devices in {
        'adapter0': ['frontend0', 'demux0', 'dvr0'],
        'adapter2': ['frontend0', 'demux0', 'dvr0'],
        'adapter3': ['frontend0'],  # incomplete
        'adapterX': ['frontend0', 'demux0', 'dvr0'],
    }.items():
        (tmp_path / name).mkdir()
        for device in devices:
            (tmp_path / name / device).touch()
    monkeypatch.setattr(dvb, 'DVB_ROOT', tmp_path)
    assert dvb.find_adapters() == [0, 2]


def test_find_adapters_without_dvb(tmp_path, monkeypatch):
    monkeypatch.setattr(dvb, 'DVB_ROOT', tmp_path / 'missing')
    assert dvb.find_adapters() == []
