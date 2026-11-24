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
Turn a ``dvbv5-scan`` channel file into Sintonía ``[[channels]]`` entries.
"""

import re

# ISDB-T service_id bits 4-3 give the service type; 0b11 is a partial
# reception (one-seg) service.
ONESEG_MASK = 0x18
ONESEG_NAME = re.compile(r'one[ -]?seg|1[ -]?seg', re.IGNORECASE)

DOTTED_NUMBER = re.compile(r'\b(\d+)\.(\d+)\b')
LEADING_NUMBER = re.compile(r'^\s*(\d+)\b')
NAMED_NUMBER = re.compile(r'\b(?:canal|ch\.?|channel)\s*(\d+)\b',
                          re.IGNORECASE)


def parse(text):
    """
    Parse a dvbv5 channel file.

    :param str text: File contents.

    :return: One dict per entry, with ``name`` plus the file's keys.
    :rtype: list
    """
    entries = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = {'name': line[1:-1].strip()}
            entries.append(current)
        elif current is not None and '=' in line:
            key, value = (part.strip() for part in line.split('=', 1))
            current[key] = value
    return entries


def is_oneseg(entry):
    """
    :param dict entry: A parsed entry.

    :return: Whether it is a one-seg (low resolution, mobile) service.
    :rtype: bool
    """
    service_id = int(entry.get('SERVICE_ID', 0))
    return (service_id & ONESEG_MASK == ONESEG_MASK
            or bool(ONESEG_NAME.search(entry['name'])))


def _major(name):
    """
    The channel number a service name suggests.

    :param str name: Service name.

    :return: ``(major, minor)``, minor being ``None`` when only the major
     number appears, or ``None`` when the name has no number.
    :rtype: tuple or None
    """
    if match := DOTTED_NUMBER.search(name):
        return int(match[1]), match[2]
    for pattern in (LEADING_NUMBER, NAMED_NUMBER):
        if match := pattern.search(name):
            return int(match[1]), None
    return None


def rf_channel(frequency):
    """
    The 6 MHz (Americas/Japan ISDB-T) channel number of a frequency.

    :param int frequency: Centre frequency in Hz.

    :return: The VHF-high (7-13) or UHF (14-69) channel, or ``None``.
    :rtype: int or None
    """
    for first, base, last in ((7, 177142857, 13), (14, 473142857, 69)):
        channel = first + round((frequency - base) / 6e6)
        centre = base + (channel - first) * 6e6
        if first <= channel <= last and abs(frequency - centre) < 1e6:
            return channel
    return None


def to_channels(entries, include_oneseg=False):
    """
    Convert parsed entries into channel configurations.

    Channel numbers come from the service names (``'15-UCR HD'`` becomes
    ``15.1``); otherwise from the RF channel (629.142857 MHz is UHF 40, so
    ``40.1``); otherwise the next free whole number.

    :param list entries: Output of ``parse()``.
    :param bool include_oneseg: Keep one-seg services.

    :return: Channel dicts in ``[[channels]]`` format, sorted by number
     (Up/Down follows this order).
    :rtype: list
    """
    services = []
    seen = set()
    for entry in entries:
        if 'SERVICE_ID' not in entry or 'FREQUENCY' not in entry:
            continue
        if not include_oneseg and is_oneseg(entry):
            continue
        # The same service twice, e.g. when scan files are concatenated.
        key = (int(entry['FREQUENCY']), int(entry['SERVICE_ID']))
        if key in seen:
            continue
        seen.add(key)
        services.append(entry)
    used = set()
    numbered = []
    for entry in services:
        suggestion = _major(entry['name'])
        if suggestion is None:
            channel = rf_channel(int(entry['FREQUENCY']))
            suggestion = (channel, None) if channel else None
        number = None
        if suggestion is not None:
            major, minor = suggestion
            if minor is not None and f'{major}.{minor}' not in used:
                number = f'{major}.{minor}'
            else:
                minor = 1
                while f'{major}.{minor}' in used:
                    minor += 1
                number = f'{major}.{minor}'
            used.add(number)
        numbered.append([entry, number])

    free = 1
    for item in numbered:
        if item[1] is None:
            while str(free) in used:
                free += 1
            item[1] = str(free)
            used.add(item[1])

    channels = []
    for entry, number in numbered:
        channel = {
            'number': number,
            'name': entry['name'],
            'frequency': int(entry['FREQUENCY']),
            'service_id': int(entry['SERVICE_ID']),
        }
        bandwidth = int(entry.get('BANDWIDTH_HZ', 6000000))
        if bandwidth != 6000000:
            channel['bandwidth'] = bandwidth
        channels.append(channel)
    return sorted(channels, key=lambda c: _number_key(c['number']))


def _number_key(number):
    """
    :param str number: Channel number such as ``'38.02'``.

    :return: A key that sorts channel numbers numerically.
    :rtype: tuple
    """
    return tuple(int(part) for part in number.split('.'))


def _toml_string(value):
    """
    :param str value: Any string.

    :return: A TOML string: literal (single quotes) when possible.
    :rtype: str
    """
    if "'" not in value and '\n' not in value:
        return f"'{value}'"
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return '"' + escaped.replace('\n', '\\n') + '"'


IMPORT_HEADER = (
    'Generated by "sintonia import". Review the channel numbers: they are',
    'guessed from the service names.',
)


def to_toml(channels, header=IMPORT_HEADER):
    """
    Render channels as TOML ``[[channels]]`` tables.

    :param list channels: Output of ``to_channels()``. An optional
     ``comment`` key is written as a comment above its table.
    :param tuple header: Lines of the leading comment.

    :return: TOML text.
    :rtype: str
    """
    lines = [f'# {line}' if line else '#' for line in header]
    for channel in channels:
        lines.append('')
        if channel.get('comment'):
            lines.append(f'# {channel["comment"]}')
        lines.append('[[channels]]')
        for key, value in channel.items():
            if key == 'comment':
                continue
            if isinstance(value, str):
                value = _toml_string(value)
            lines.append(f'{key} = {value}')
    return '\n'.join(lines) + '\n'
