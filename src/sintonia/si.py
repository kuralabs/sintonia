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
Service information from an MPEG transport stream: the tables a scan needs
to list the services of a multiplex and number them like a TV does.

- PAT (ISO/IEC 13818-1): the programs in the multiplex.
- SDT (ETSI EN 300 468, ARIB STD-B10): service names, providers and types.
- NIT (same): the network, and for ISDB-T the remote control key of each
  transport stream, which is the major channel number TVs show.
"""

PACKET_SIZE = 188
SYNC_BYTE = 0x47

PID_PAT = 0x0000
PID_NIT = 0x0010
PID_SDT = 0x0011

TABLE_PAT = 0x00
TABLE_NIT_ACTUAL = 0x40
TABLE_SDT_ACTUAL = 0x42

DESCRIPTOR_NETWORK_NAME = 0x40
DESCRIPTOR_SERVICE = 0x48
# ARIB STD-B10 TS information descriptor.
DESCRIPTOR_TS_INFORMATION = 0xCD

# SDT service types that carry television (EN 300 468 table 87).
TV_SERVICE_TYPES = frozenset({0x01, 0x11, 0x16, 0x19, 0x1F})

# DVB text: a first byte below 0x20 selects the character table.
_CHARSETS = {
    0x01: 'iso8859-5', 0x02: 'iso8859-6', 0x03: 'iso8859-7',
    0x04: 'iso8859-8', 0x05: 'iso8859-9', 0x06: 'iso8859-10',
    0x07: 'iso8859-11', 0x09: 'iso8859-13', 0x0A: 'iso8859-14',
    0x0B: 'iso8859-15', 0x11: 'utf-16-be', 0x15: 'utf-8',
}
_ISO8859_PARTS = {
    1: 'iso8859-1', 2: 'iso8859-2', 3: 'iso8859-3', 4: 'iso8859-4',
    5: 'iso8859-5', 6: 'iso8859-6', 7: 'iso8859-7', 8: 'iso8859-8',
    9: 'iso8859-9', 10: 'iso8859-10', 11: 'iso8859-11', 13: 'iso8859-13',
    14: 'iso8859-14', 15: 'iso8859-15', 16: 'iso8859-16',
}


def _crc_table():
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = (crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc32(data):
    """
    The MPEG-2 CRC-32 of PSI sections.

    :param bytes data: A whole section including its CRC.

    :return: 0 for an intact section.
    :rtype: int
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(crc >> 24) ^ byte]
    return crc


def decode_text(data):
    """
    Decode a DVB/ISDB-Tb text field (service, provider or network name).

    :param bytes data: The raw field.

    :return: The text, stripped, without emphasis control codes.
    :rtype: str
    """
    if not data:
        return ''
    charset = 'iso8859-15'  # SBTVD (ABNT NBR 15603) default
    first = data[0]
    if first == 0x10 and len(data) >= 3:
        charset = _ISO8859_PARTS.get(data[2], charset)
        data = data[3:]
    elif first < 0x20:
        charset = _CHARSETS.get(first, charset)
        data = data[1:]
    if charset.startswith('iso8859'):
        # 0x80-0x9F are control codes (emphasis on/off, line breaks).
        data = bytes(b for b in data if not 0x80 <= b <= 0x9F)
    return data.decode(charset, errors='replace').strip()


class SectionReader:
    """
    Reassemble PSI sections of selected PIDs from transport stream
    packets. Packets flagged with transport errors are skipped, and
    sections failing their CRC are dropped.

    :param pids: The PIDs to collect.
    :type pids: iterable
    """

    def __init__(self, pids):
        self._pending = {pid: None for pid in pids}
        self._continuity = {}
        self._remainder = b''
        self.packets = 0
        self.errors = 0

    def feed(self, data):
        """
        Consume transport stream bytes.

        :param bytes data: Any amount of stream; partial packets are kept
         for the next call.

        :return: ``(pid, section)`` for each complete, intact section.
        :rtype: list
        """
        data = self._remainder + data
        sections = []
        offset = 0
        end = len(data) - PACKET_SIZE
        while offset <= end:
            if data[offset] != SYNC_BYTE:
                # Lost packet alignment: find the next sync byte.
                offset += 1
                continue
            packet = data[offset:offset + PACKET_SIZE]
            offset += PACKET_SIZE
            self.packets += 1
            if packet[1] & 0x80:
                self.errors += 1
                continue
            sections.extend(self._packet(packet))
        self._remainder = data[offset:]
        return sections

    def _packet(self, packet):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid not in self._pending:
            return []
        start = bool(packet[1] & 0x40)
        adaptation = (packet[3] >> 4) & 0x3
        continuity = packet[3] & 0x0F
        if not adaptation & 0x1:
            return []

        previous = self._continuity.get(pid)
        self._continuity[pid] = continuity
        if previous is not None and continuity != (previous + 1) & 0x0F:
            # A packet was lost: the section in progress is incomplete.
            self._pending[pid] = None

        payload = packet[4:]
        if adaptation & 0x2:
            payload = payload[1 + payload[0]:]
        if not payload:
            return []

        found = []
        if start:
            pointer = payload[0]
            before, payload = payload[1:1 + pointer], payload[1 + pointer:]
            if self._pending[pid] is not None:
                found.extend(self._append(pid, before))
            self._pending[pid] = bytearray()
            found.extend(self._append(pid, payload))
        elif self._pending[pid] is not None:
            found.extend(self._append(pid, payload))
        return found

    def _append(self, pid, data):
        """
        Add payload to the section in progress, emitting the sections it
        completes. Several short sections can share one packet.
        """
        found = []
        buffer = self._pending[pid]
        if buffer is None:
            return found
        buffer += data
        while len(buffer) >= 3:
            if buffer[0] == 0xFF:
                # Stuffing: nothing more in this packet.
                self._pending[pid] = None
                return found
            length = 3 + (((buffer[1] & 0x0F) << 8) | buffer[2])
            if len(buffer) < length:
                return found
            section = bytes(buffer[:length])
            del buffer[:length]
            if crc32(section) == 0:
                found.append((pid, section))
        return found


def _descriptors(data):
    """
    :param bytes data: A descriptor loop.

    :return: ``(tag, body)`` pairs.
    :rtype: list
    """
    found = []
    offset = 0
    while offset + 2 <= len(data):
        tag, length = data[offset], data[offset + 1]
        found.append((tag, data[offset + 2:offset + 2 + length]))
        offset += 2 + length
    return found


def _header(section):
    """
    :param bytes section: A long-form section.

    :return: table_id, table_id_extension, version, section_number,
     last_section_number.
    :rtype: tuple
    """
    return (
        section[0],
        (section[3] << 8) | section[4],
        (section[5] >> 1) & 0x1F,
        section[6],
        section[7],
    )


def parse_pat(section):
    """
    :param bytes section: A PAT section.

    :return: ``transport_stream_id`` and the program numbers (service IDs),
     without the network PID entry.
    :rtype: tuple
    """
    tsid = (section[3] << 8) | section[4]
    programs = []
    body = section[8:-4]
    for offset in range(0, len(body) - 3, 4):
        number = (body[offset] << 8) | body[offset + 1]
        if number:
            programs.append(number)
    return tsid, programs


def parse_sdt(section):
    """
    :param bytes section: An SDT (actual transport stream) section.

    :return: ``transport_stream_id``, ``original_network_id`` and
     ``{service_id: {'type', 'provider', 'name'}}``.
    :rtype: tuple
    """
    tsid = (section[3] << 8) | section[4]
    onid = (section[8] << 8) | section[9]
    services = {}
    offset = 11
    end = len(section) - 4
    while offset + 5 <= end:
        sid = (section[offset] << 8) | section[offset + 1]
        loop = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
        info = {'type': None, 'provider': '', 'name': ''}
        for tag, body in _descriptors(
                section[offset + 5:offset + 5 + loop]):
            if tag == DESCRIPTOR_SERVICE and len(body) >= 2:
                info['type'] = body[0]
                provider_end = 2 + body[1]
                info['provider'] = decode_text(body[2:provider_end])
                name_length = body[provider_end]
                info['name'] = decode_text(
                    body[provider_end + 1:provider_end + 1 + name_length])
        services[sid] = info
        offset += 5 + loop
    return tsid, onid, services


def parse_nit(section):
    """
    :param bytes section: An NIT (actual network) section.

    :return: ``network_id``, the network name, and
     ``{transport_stream_id: remote_control_key_id}`` for the transport
     streams that announce one.
    :rtype: tuple
    """
    network_id = (section[3] << 8) | section[4]
    name = ''
    loop = ((section[8] & 0x0F) << 8) | section[9]
    for tag, body in _descriptors(section[10:10 + loop]):
        if tag == DESCRIPTOR_NETWORK_NAME:
            name = decode_text(body)
    offset = 10 + loop
    ts_loop_end = offset + 2 + (
        ((section[offset] & 0x0F) << 8) | section[offset + 1])
    offset += 2
    keys = {}
    while offset + 6 <= min(ts_loop_end, len(section) - 4):
        tsid = (section[offset] << 8) | section[offset + 1]
        length = ((section[offset + 4] & 0x0F) << 8) | section[offset + 5]
        for tag, body in _descriptors(
                section[offset + 6:offset + 6 + length]):
            if tag == DESCRIPTOR_TS_INFORMATION and body:
                keys[tsid] = body[0]
        offset += 6 + length
    return network_id, name, keys


class Multiplex:
    """
    Collects the PAT, SDT and NIT of one multiplex.
    """

    def __init__(self):
        self.reader = SectionReader((PID_PAT, PID_NIT, PID_SDT))
        self._tables = {}
        self.tsid = None
        self.programs = None
        self.onid = None
        self.services = None
        self.network_id = None
        self.network_name = ''
        self.remote_keys = None

    def feed(self, data):
        """
        :param bytes data: Transport stream bytes.

        :return: Whether all three tables are complete.
        :rtype: bool
        """
        for pid, section in self.reader.feed(data):
            self._section(pid, section)
        return self.complete

    @property
    def complete(self):
        return None not in (self.programs, self.services, self.remote_keys)

    @property
    def has_services(self):
        """
        Whether enough is known to list services (PAT and SDT).
        """
        return self.programs is not None and self.services is not None

    def _section(self, pid, section):
        if len(section) < 12 or not section[1] & 0x80:
            return
        table, extension, version, number, last = _header(section)
        if (pid, table) not in ((PID_PAT, TABLE_PAT),
                                (PID_SDT, TABLE_SDT_ACTUAL),
                                (PID_NIT, TABLE_NIT_ACTUAL)):
            return
        key = (table, extension)
        parts = self._tables.get(key)
        if parts is None or parts['version'] != version:
            parts = {'version': version, 'last': last, 'sections': {}}
            self._tables[key] = parts
        parts['sections'][number] = section
        if len(parts['sections']) == parts['last'] + 1:
            self._complete(table, parts['sections'])

    def _complete(self, table, sections):
        ordered = [sections[n] for n in sorted(sections)]
        if table == TABLE_PAT and self.programs is None:
            programs = []
            for section in ordered:
                self.tsid, numbers = parse_pat(section)
                programs.extend(numbers)
            self.programs = programs
        elif table == TABLE_SDT_ACTUAL and self.services is None:
            services = {}
            for section in ordered:
                tsid, self.onid, found = parse_sdt(section)
                services.update(found)
            self.services = services
            # The SDT names the transport stream too, for when the PAT is
            # lost on a weak signal.
            if self.tsid is None:
                self.tsid = tsid
        elif table == TABLE_NIT_ACTUAL and self.remote_keys is None:
            keys = {}
            for section in ordered:
                self.network_id, self.network_name, found = \
                    parse_nit(section)
                keys.update(found)
            self.remote_keys = keys

    @property
    def remote_key(self):
        """
        The remote control key (major channel number) of this transport
        stream, or ``None`` when unknown.
        """
        if self.remote_keys is None or self.tsid is None:
            return None
        if self.tsid in self.remote_keys:
            return self.remote_keys[self.tsid]
        # A NIT describing only this multiplex, under another ID.
        if len(self.remote_keys) == 1:
            return next(iter(self.remote_keys.values()))
        return None
