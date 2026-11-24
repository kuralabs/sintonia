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
Scan for channels with the tuners directly (``sintonia scan``).

Each RF channel is tuned on one of the free tuners, several in parallel;
on a lock the PAT, SDT and NIT are read from the stream. Channels are
numbered as TVs do (ABNT NBR 15603): the remote control key of the
multiplex is the major number, and the service number, the lowest three
bits of the service ID, plus one is the minor number.
"""

import logging
import queue
import re
import select
import threading
import time

from . import dvb
from .scan import (
    DOTTED_NUMBER,
    ONESEG_MASK,
    ONESEG_NAME,
    _major,
    _number_key,
)
from .si import TV_SERVICE_TYPES, Multiplex
from .streams import SETTLE_CLEAN, SETTLE_MAX

log = logging.getLogger(__name__)

BANDWIDTH = 6000000
# (first channel, its centre frequency, last channel); the 1/7 MHz offset
# is the ISDB-T 6 MHz convention.
BANDS = {
    'vhf': (7, 177142857, 13),
    'uhf': (14, 473142857, 69),
}
DEFAULT_CHANNELS = '7-69'

LOCK_TIMEOUT = 5.0
# The NIT may repeat only every 10 s (ARIB TR-B14).
TABLES_TIMEOUT = 12.0
# Seconds to measure the error rate for, once the demodulator settled.
MEASURE = 2.0
# Above this share of corrupt packets, a multiplex is marked weak.
WEAK_ERRORS = 0.01
# ISDB-T one-seg (partial reception) service type.
ONESEG_SERVICE_TYPE = 0xC0

_RANGE = re.compile(r'^\s*(\d+)\s*(?:-\s*(\d+))?\s*$')


class ScanError(Exception):
    """
    The scan cannot run, for example because no tuner is free.
    """


def frequency(channel):
    """
    :param int channel: A 6 MHz VHF-high (7-13) or UHF (14-69) channel.

    :return: Its centre frequency in Hz.
    :rtype: int

    :raises ValueError: For channels outside those bands.
    """
    for first, base, last in BANDS.values():
        if first <= channel <= last:
            return base + (channel - first) * BANDWIDTH
    raise ValueError(f'no ISDB-T channel {channel} (valid: 7-69)')


def band(channel):
    """
    :param int channel: An RF channel.

    :return: ``'VHF 7'`` or ``'UHF 18'``.
    :rtype: str
    """
    return f'{"VHF" if channel < 14 else "UHF"} {channel}'


def parse_channels(spec):
    """
    Parse a channel list such as ``'7-13,14-51'`` or ``'18,22,38'``.

    :param str spec: Comma separated channels and ranges.

    :return: Sorted, unique RF channels.
    :rtype: list

    :raises ValueError: On syntax errors or unknown channels.
    """
    channels = set()
    for part in spec.split(','):
        match = _RANGE.match(part)
        if not match:
            raise ValueError(f'invalid channel or range: {part!r}')
        first = int(match[1])
        last = int(match[2] or first)
        if last < first:
            raise ValueError(f'invalid range: {part!r}')
        for channel in range(first, last + 1):
            frequency(channel)
            channels.add(channel)
    return sorted(channels)


def probe(adapter, channel, lock_timeout=LOCK_TIMEOUT,
          tables_timeout=TABLES_TIMEOUT):
    """
    Tune one RF channel and read its service information.

    :param int adapter: Adapter number; its frontend must be free.
    :param int channel: RF channel.
    :param float lock_timeout: Seconds to wait for a lock.
    :param float tables_timeout: Seconds to wait for the tables.

    :return: ``channel``, ``frequency``, ``locked``, ``signal_dbm``,
     ``cnr_db``, ``errors`` (share of corrupt packets) and ``multiplex``
     (``si.Multiplex``, or ``None`` without a lock).
    :rtype: dict

    :raises OSError: If the tuner cannot be opened or tuned.
    """
    result = {
        'channel': channel, 'frequency': frequency(channel),
        'locked': False, 'signal_dbm': None, 'cnr_db': None,
        'errors': None, 'multiplex': None,
    }
    frontend = dvb.Frontend(adapter)
    frontend.open()
    try:
        frontend.tune(result['frequency'], BANDWIDTH)
        deadline = time.monotonic() + lock_timeout
        while not frontend.status() & dvb.FE_HAS_LOCK:
            if time.monotonic() > deadline:
                return result
            time.sleep(0.1)
        result['locked'] = True

        multiplex = Multiplex()
        reader = multiplex.reader
        stream = dvb.TransportStream(adapter)
        stream.open()
        try:
            # The demodulator delivers corrupt packets for a few seconds
            # after locking. Tables are read meanwhile (their sections are
            # CRC checked), but the error rate is only measured once the
            # stream is clean, or after SETTLE_MAX on a weak signal.
            locked = time.monotonic()
            deadline = locked + tables_timeout
            clean_since = locked
            settled = None
            baseline = (0, 0)
            while time.monotonic() < deadline:
                select.select([stream.fileno()], [], [], 0.2)
                errors = reader.errors
                complete = multiplex.feed(stream.read())
                now = time.monotonic()
                if settled is None:
                    if reader.errors != errors:
                        clean_since = now
                    if (now - clean_since >= SETTLE_CLEAN
                            or now - locked >= SETTLE_MAX):
                        settled = now
                        baseline = (reader.errors, reader.packets)
                elif complete and now - settled >= MEASURE:
                    break
        finally:
            stream.close()
        result.update(frontend.stats())
        packets = reader.packets - baseline[1]
        if settled is not None and packets:
            result['errors'] = (reader.errors - baseline[0]) / packets
        result['multiplex'] = multiplex
        return result
    finally:
        frontend.close()


def scan(adapters, channels, lock_timeout=LOCK_TIMEOUT,
         tables_timeout=TABLES_TIMEOUT, progress=None):
    """
    Probe RF channels, spread over the tuners.

    :param list adapters: Adapter numbers to use.
    :param list channels: RF channels.
    :param float lock_timeout: Seconds to wait for a lock.
    :param float tables_timeout: Seconds to wait for the tables.
    :param progress: Called with each ``probe()`` result as it completes
     (from the tuner threads, one at a time).
    :type progress: callable or None

    :return: ``probe()`` results, by RF channel.
    :rtype: list

    :raises ScanError: If none of the tuners can be used.
    """
    pending = queue.Queue()
    for channel in channels:
        pending.put(channel)
    results = []
    failed = {}
    lock = threading.Lock()

    def work(adapter):
        while True:
            try:
                channel = pending.get_nowait()
            except queue.Empty:
                return
            try:
                result = probe(adapter, channel, lock_timeout,
                               tables_timeout)
            except OSError as e:
                # This tuner is unusable (busy, removed): leave its
                # channel to the others.
                pending.put(channel)
                with lock:
                    failed[adapter] = e
                return
            with lock:
                results.append(result)
                if progress is not None:
                    progress(result)

    threads = [
        threading.Thread(target=work, args=(adapter,), daemon=True)
        for adapter in adapters
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for adapter, error in sorted(failed.items()):
        log.warning('tuner %d not used: %s', adapter, error)
    if not pending.empty():
        raise ScanError(
            'no usable tuner (is "sintonia serve" or another program '
            'using them?): ' + '; '.join(
                f'tuner {a}: {e}' for a, e in sorted(failed.items()))
        )
    return sorted(results, key=lambda r: r['channel'])


def is_oneseg(service_id, service):
    """
    :param int service_id: Service ID.
    :param dict service: SDT information (``type``, ``name``).

    :return: Whether it is a one-seg (low resolution, mobile) service.
    :rtype: bool
    """
    return (service['type'] == ONESEG_SERVICE_TYPE
            or service_id & ONESEG_MASK == ONESEG_MASK
            or bool(ONESEG_NAME.search(service['name'])))


def describe(result):
    """
    One line about a probed multiplex, for progress and the output.

    :param dict result: A ``probe()`` result.

    :return: For example ``'UHF 18, 497.143 MHz, -75.4 dBm, C/N 16.6 dB'``.
    :rtype: str
    """
    parts = [band(result['channel']),
             f'{result["frequency"] / 1e6:.3f} MHz']
    if result['signal_dbm'] is not None:
        parts.append(f'{result["signal_dbm"]} dBm')
    if result['cnr_db'] is not None:
        parts.append(f'C/N {result["cnr_db"]} dB')
    if result['errors'] is not None and result['errors'] > WEAK_ERRORS:
        parts.append(f'weak: {result["errors"]:.0%} corrupt packets')
    return ', '.join(parts)


def to_channels(results, include_oneseg=False):
    """
    Turn scan results into channel configurations, numbered like a TV.

    When a multiplex did not announce its remote control key (its NIT was
    not received), the major number is taken from the service names, or
    else the RF channel, as ``sintonia import`` does.

    :param list results: Output of ``scan()``.
    :param bool include_oneseg: Keep one-seg services.

    :return: Channel dicts in ``[[channels]]`` format with a ``comment``
     describing the multiplex, sorted by number.
    :rtype: list
    """
    candidates = []
    seen = {}
    for result in results:
        multiplex = result['multiplex']
        if multiplex is None or multiplex.services is None:
            continue
        for service_id, service in multiplex.services.items():
            if (multiplex.programs is not None
                    and service_id not in multiplex.programs):
                continue
            # Radio and data services: there is no video to transcode.
            kind = service['type']
            if kind is not None and kind not in TV_SERVICE_TYPES | {
                    ONESEG_SERVICE_TYPE}:
                continue
            if not include_oneseg and is_oneseg(service_id, service):
                continue
            # A service received on two RF channels (a repeater): keep the
            # cleaner reception.
            identity = (multiplex.onid, service_id)
            quality = -(result['errors'] or 0)
            if identity in seen and seen[identity][0] >= quality:
                continue
            seen[identity] = (quality, result, multiplex, service_id,
                              service)

    # Without a remote control key (no NIT received), every service of a
    # multiplex takes the number its main service's name suggests, or the
    # RF channel.
    fallback = {}
    for _, _, multiplex, _, service in sorted(
            seen.values(), key=lambda item: item[3]):
        if multiplex.remote_key is None and id(multiplex) not in fallback:
            suggestion = _major(service['name'])
            if suggestion is not None:
                fallback[id(multiplex)] = suggestion[0]

    for quality, result, multiplex, service_id, service in seen.values():
        major = multiplex.remote_key
        if major is None:
            major = fallback.get(id(multiplex), result['channel'])
        candidates.append({
            'number': (major, (service_id & 0x7) + 1),
            'name': service['name'] or f'Service {service_id}',
            'frequency': result['frequency'],
            'service_id': service_id,
            'comment': describe(result),
            'quality': quality,
        })

    # Two services claiming one number: a service whose name carries
    # another number (BETHEL 50.31) moves to it; otherwise the cleaner
    # service keeps the number and the other takes the next free one.
    def named(candidate):
        match = DOTTED_NUMBER.search(candidate['name'])
        if match is None or int(match[1]) != candidate['number'][0]:
            return None
        number = (int(match[1]), int(match[2]))
        return None if number == candidate['number'] else number

    # Each number goes to one service first, so that a displaced service
    # cannot take the number of another that is still to be placed.
    candidates.sort(key=lambda c: (c['number'], named(c) is not None,
                                   -c['quality'], c['service_id']))
    used = set()
    placed = []
    displaced = []
    for candidate in candidates:
        if candidate['number'] in used:
            displaced.append(candidate)
        else:
            used.add(candidate['number'])
            placed.append(candidate)
    for candidate in displaced:
        major, minor = named(candidate) or candidate['number']
        while (major, minor) in used:
            minor += 1
        candidate['number'] = (major, minor)
        used.add(candidate['number'])
        placed.append(candidate)

    channels = []
    for candidate in placed:
        major, minor = candidate.pop('number')
        candidate.pop('quality')
        channels.append({'number': f'{major}.{minor}', **candidate})
    return sorted(channels, key=lambda c: _number_key(c['number']))
