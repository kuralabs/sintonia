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
Linux DVB API (v5) access: tune a frontend and read its transport stream.

Only what Sintonía needs is implemented, with ctypes mirrors of the
structures in ``<linux/dvb/frontend.h>`` and ``<linux/dvb/dmx.h>``. The
sizes and ioctl numbers are checked against the kernel headers in the test
suite.
"""

import ctypes
import errno
import fcntl
import os
from pathlib import Path

DVB_ROOT = Path('/dev/dvb')

# Property commands (frontend.h).
DTV_TUNE = 1
DTV_CLEAR = 2
DTV_FREQUENCY = 3
DTV_BANDWIDTH_HZ = 5
DTV_DELIVERY_SYSTEM = 17
DTV_STAT_SIGNAL_STRENGTH = 62
DTV_STAT_CNR = 63

SYS_ISDBT = 8

# enum fe_status.
FE_HAS_SIGNAL = 0x01
FE_HAS_CARRIER = 0x02
FE_HAS_VITERBI = 0x04
FE_HAS_SYNC = 0x08
FE_HAS_LOCK = 0x10

# enum fecap_scale_params.
FE_SCALE_NOT_AVAILABLE = 0
FE_SCALE_DECIBEL = 1
FE_SCALE_RELATIVE = 2

# Demux (dmx.h).
DMX_IN_FRONTEND = 0
DMX_OUT_TS_TAP = 2
DMX_PES_OTHER = 20
DMX_IMMEDIATE_START = 4
# A PES filter on this PID passes the whole transport stream.
PID_ALL = 0x2000

TS_PACKET = 188
# Large kernel-side buffer: the reader may stall briefly under load.
DVR_BUFFER_SIZE = 16 * 1024 * 1024
READ_SIZE = TS_PACKET * 1024


# The kernel structs below are __attribute__((packed)). ctypes has always
# laid out _pack_ structs the MSVC way; Python 3.14 wants _layout_ = 'ms' to
# say so explicitly (and earlier versions ignore it).
class DtvStats(ctypes.Structure):
    """
    ``struct dtv_stats``.
    """
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [
        ('scale', ctypes.c_uint8),
        ('value', ctypes.c_int64),
    ]


class DtvFeStats(ctypes.Structure):
    """
    ``struct dtv_fe_stats``.
    """
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [
        ('len', ctypes.c_uint8),
        ('stat', DtvStats * 4),
    ]


class DtvPropertyValue(ctypes.Union):
    """
    The ``u`` union of ``struct dtv_property``.
    """
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [
        ('data', ctypes.c_uint32),
        ('st', DtvFeStats),
        ('raw', ctypes.c_uint8 * 56),
    ]


class DtvProperty(ctypes.Structure):
    """
    ``struct dtv_property``.
    """
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [
        ('cmd', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 3),
        ('u', DtvPropertyValue),
        ('result', ctypes.c_int),
    ]


class DtvProperties(ctypes.Structure):
    """
    ``struct dtv_properties``.
    """
    _fields_ = [
        ('num', ctypes.c_uint32),
        ('props', ctypes.POINTER(DtvProperty)),
    ]


class DmxPesFilterParams(ctypes.Structure):
    """
    ``struct dmx_pes_filter_params``.
    """
    _fields_ = [
        ('pid', ctypes.c_uint16),
        ('input', ctypes.c_int),
        ('output', ctypes.c_int),
        ('pes_type', ctypes.c_int),
        ('flags', ctypes.c_uint32),
    ]


def _ioc(direction, number, size):
    """
    Build an ioctl request number of the ``'o'`` (DVB) type.

    :param int direction: 0 none, 1 write, 2 read.
    :param int number: Command number.
    :param int size: Size of the argument structure.

    :return: The request number, as ``_IOC()`` builds it.
    :rtype: int
    """
    return (direction << 30) | (size << 16) | (ord('o') << 8) | number


FE_READ_STATUS = _ioc(2, 69, 4)
FE_SET_PROPERTY = _ioc(1, 82, ctypes.sizeof(DtvProperties))
FE_GET_PROPERTY = _ioc(2, 83, ctypes.sizeof(DtvProperties))
DMX_START = _ioc(0, 41, 0)
DMX_STOP = _ioc(0, 42, 0)
DMX_SET_PES_FILTER = _ioc(1, 44, ctypes.sizeof(DmxPesFilterParams))
DMX_SET_BUFFER_SIZE = _ioc(0, 45, 0)


def find_adapters():
    """
    List the DVB adapters that have a frontend, demux and DVR device.

    :return: Sorted adapter numbers.
    :rtype: list
    """
    adapters = []
    for path in DVB_ROOT.glob('adapter*'):
        suffix = path.name.removeprefix('adapter')
        if not suffix.isdigit():
            continue
        if all((path / dev).exists()
               for dev in ('frontend0', 'demux0', 'dvr0')):
            adapters.append(int(suffix))
    return sorted(adapters)


def _properties(commands):
    """
    Build a ``struct dtv_properties`` for a list of commands.

    :param list commands: ``(cmd, data)`` tuples.

    :return: The structure and the array it points to (keep both alive).
    :rtype: tuple
    """
    array = (DtvProperty * len(commands))()
    for index, (cmd, data) in enumerate(commands):
        array[index].cmd = cmd
        array[index].u.data = data
    return DtvProperties(len(commands), array), array


class Frontend:
    """
    A DVB frontend (tuner and demodulator), ``/dev/dvb/adapterN/frontend0``.

    The methods are blocking but short: the kernel tunes asynchronously and
    status reads are a few I2C transfers.

    :param int adapter: Adapter number.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self.path = DVB_ROOT / f'adapter{adapter}' / 'frontend0'
        self._fd = None

    def open(self):
        """
        Open the frontend for tuning.

        :raises OSError: If the device cannot be opened (for example, it is
         in use by another program).
        """
        if self._fd is None:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        """
        Close the frontend; the kernel powers it down after a while.
        """
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def tune(self, frequency, bandwidth, delivery_system=SYS_ISDBT):
        """
        Start tuning to a multiplex. Returns immediately; poll ``status()``
        for the lock.

        :param int frequency: Centre frequency in Hz.
        :param int bandwidth: Channel bandwidth in Hz.
        :param int delivery_system: ``SYS_*`` constant.

        :raises OSError: If the driver rejects the parameters.
        """
        props, _array = _properties([
            (DTV_CLEAR, 0),
            (DTV_DELIVERY_SYSTEM, delivery_system),
            (DTV_FREQUENCY, frequency),
            (DTV_BANDWIDTH_HZ, bandwidth),
            (DTV_TUNE, 0),
        ])
        fcntl.ioctl(self._fd, FE_SET_PROPERTY, props)

    def status(self):
        """
        Read the frontend status flags.

        :return: A bitmask of ``FE_HAS_*`` flags.
        :rtype: int
        """
        status = ctypes.c_uint32()
        fcntl.ioctl(self._fd, FE_READ_STATUS, status)
        return status.value

    def stats(self):
        """
        Read signal strength and carrier-to-noise ratio, when available.

        :return: ``signal_dbm`` and ``cnr_db`` (``None`` when the driver
         does not report a value in decibels).
        :rtype: dict
        """
        props, array = _properties([
            (DTV_STAT_SIGNAL_STRENGTH, 0),
            (DTV_STAT_CNR, 0),
        ])
        fcntl.ioctl(self._fd, FE_GET_PROPERTY, props)
        values = []
        for prop in array:
            value = None
            for stat in prop.u.st.stat[:min(prop.u.st.len, 4)]:
                if stat.scale == FE_SCALE_DECIBEL:
                    # Kernel decibel stats are in units of 0.001 dB.
                    value = round(stat.value / 1000, 1)
                    break
            values.append(value)
        return {'signal_dbm': values[0], 'cnr_db': values[1]}


class TransportStream:
    """
    The whole transport stream of a tuned frontend, read from
    ``/dev/dvb/adapterN/dvr0`` through a pass-all demux filter.

    :param int adapter: Adapter number.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        base = DVB_ROOT / f'adapter{adapter}'
        self._demux_path = base / 'demux0'
        self._dvr_path = base / 'dvr0'
        self._demux = None
        self._dvr = None

    def open(self):
        """
        Open the DVR device and start a demux filter that passes all PIDs.

        :raises OSError: If the devices cannot be opened or configured.
        """
        self._dvr = os.open(self._dvr_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._dvr, DMX_SET_BUFFER_SIZE, DVR_BUFFER_SIZE)
            self._demux = os.open(self._demux_path,
                                  os.O_RDWR | os.O_NONBLOCK)
            params = DmxPesFilterParams(
                pid=PID_ALL,
                input=DMX_IN_FRONTEND,
                output=DMX_OUT_TS_TAP,
                pes_type=DMX_PES_OTHER,
                flags=DMX_IMMEDIATE_START,
            )
            fcntl.ioctl(self._demux, DMX_SET_PES_FILTER, params)
        except OSError:
            self.close()
            raise

    def fileno(self):
        """
        :return: The DVR file descriptor, for ``loop.add_reader()``.
        :rtype: int
        """
        return self._dvr

    def read(self):
        """
        Read what is available without blocking.

        :return: Transport stream bytes; empty when nothing is pending.
        :rtype: bytes

        :raises OSError: On errors other than "try again" or a reported
         buffer overflow (data was lost, but reading can continue).
        """
        try:
            return os.read(self._dvr, READ_SIZE)
        except BlockingIOError:
            return b''
        except OSError as e:
            if e.errno == errno.EOVERFLOW:
                return b''
            raise

    def flush(self):
        """
        Discard buffered data, for example from the previous multiplex.
        """
        if self._demux is not None:
            fcntl.ioctl(self._demux, DMX_STOP)
        while self.read():
            pass
        if self._demux is not None:
            fcntl.ioctl(self._demux, DMX_START)

    def close(self):
        """
        Stop the filter and close both devices.
        """
        for name in ('_demux', '_dvr'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
