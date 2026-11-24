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

from pathlib import Path

import pytest

from sintonia.si import (
    PACKET_SIZE,
    Multiplex,
    SectionReader,
    crc32,
    decode_text,
)

DATA = Path(__file__).parent / 'data' / 'si'


def load(name):
    """
    Feed a capture (PAT, NIT and SDT packets of one multiplex, recorded
    in Costa Rica) to a Multiplex.
    """
    multiplex = Multiplex()
    multiplex.feed((DATA / name).read_bytes())
    return multiplex


def section(table_id, extension, body, number=0, last=0):
    """
    Build a long-form PSI section with a valid CRC.
    """
    length = 5 + len(body) + 4
    data = bytes([
        table_id, 0xB0 | (length >> 8), length & 0xFF,
        extension >> 8, extension & 0xFF, 0xC1, number, last,
    ]) + body
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 \
                else crc << 1
            crc &= 0xFFFFFFFF
    return data + crc.to_bytes(4, 'big')


def packets(pid, payload, continuity=0, error=False):
    """
    Split a section into transport stream packets.
    """
    data = bytes([0]) + payload  # pointer_field
    out = b''
    first = True
    while data:
        chunk, data = data[:PACKET_SIZE - 4], data[PACKET_SIZE - 4:]
        chunk += b'\xff' * (PACKET_SIZE - 4 - len(chunk))
        out += bytes([
            0x47,
            (0x80 if error else 0) | (0x40 if first else 0) | pid >> 8,
            pid & 0xFF,
            0x10 | continuity & 0x0F,
        ]) + chunk
        continuity += 1
        first = False
    return out


def test_crc32_check_value():
    assert crc32(b'123456789') == 0x0376E6E7
    pat = section(0x00, 1, bytes([0, 1, 0xE1, 0x00]))
    assert crc32(pat) == 0


@pytest.mark.parametrize('raw, text', [
    (b'CANAL 6 HD', 'CANAL 6 HD'),
    (b'Telef\xedsica', 'Telefísica'),
    (b'\x15Teletica \xc3\xb1', 'Teletica ñ'),
    (b'\x10\x00\x01Ca\xf1a', 'Caña'),
    (b'\x86OPA\x87 ', 'OPA'),
    (b'', ''),
])
def test_decode_text(raw, text):
    assert decode_text(raw) == text


def test_reader_reassembles_across_packets_and_reads():
    body = bytes(range(200)) * 2
    data = packets(0x11, section(0x42, 7, body))
    reader = SectionReader([0x11])
    found = []
    # Reads that split packets anywhere.
    for offset in range(0, len(data), 100):
        found += reader.feed(data[offset:offset + 100])
    assert len(found) == 1
    assert found[0][0] == 0x11
    assert found[0][1][8:-4] == body


def test_reader_drops_errored_and_discontinuous_sections():
    good = section(0x42, 7, bytes(300))
    reader = SectionReader([0x11])
    # Transport error indicator: the packet is skipped.
    assert reader.feed(packets(0x11, good, error=True)) == []
    assert reader.errors == 2
    # A lost packet (continuity jump) discards the section in progress.
    data = packets(0x11, good, continuity=0)
    assert reader.feed(data[:PACKET_SIZE]) == []
    tail = bytearray(data[PACKET_SIZE:])
    tail[3] = 0x10 | 5
    assert reader.feed(bytes(tail)) == []
    # A corrupted section fails its CRC.
    bad = bytearray(good)
    bad[20] ^= 0xFF
    assert reader.feed(packets(0x11, bytes(bad), continuity=6)) == []
    assert len(reader.feed(packets(0x11, good, continuity=8))) == 1


def test_reader_resynchronises():
    data = packets(0x00, section(0x00, 1, bytes([0, 1, 0xE1, 0x00])))
    reader = SectionReader([0x00])
    assert len(reader.feed(b'\x00\x12' + data)) == 1


def test_multiplex_with_nit():
    multiplex = load('uhf22.ts')
    assert multiplex.complete
    assert multiplex.tsid == 601
    assert multiplex.network_name == 'REPRETEL'
    assert multiplex.remote_key == 6
    assert multiplex.programs == [19256, 19232, 19233]
    assert multiplex.services[19232] == {
        'type': 0x01, 'provider': 'REPRETEL', 'name': 'CANAL 6 HD',
    }
    assert multiplex.services[19256]['type'] == 0xC0  # one-seg


def test_multiplex_without_nit():
    multiplex = load('uhf40.ts')
    assert not multiplex.complete
    assert multiplex.has_services
    assert multiplex.remote_key is None
    assert multiplex.services[53921]['name'] == 'Teleuno'


def test_multiplex_without_pat_uses_sdt_transport_stream():
    multiplex = load('uhf29.ts')
    assert multiplex.programs is None
    assert multiplex.tsid == 2902
    assert multiplex.remote_key == 2
    assert len(multiplex.services) == 8
