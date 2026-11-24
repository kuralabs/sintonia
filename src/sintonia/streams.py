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
Tuners, streams and the policy that ties them together.

A tuner carries one multiplex at a time and fans its transport stream out
to one ffmpeg per watched channel of that multiplex. A stream is one
channel being transcoded and published to MediaMTX. Streams nobody watches
are stopped after a while; the channels next to a watched one are kept live
("pre-warmed") on spare tuners so that Up/Down is quick.
"""

import asyncio
import json
import logging
import re
import time

from . import dvb
from .transcode import command

log = logging.getLogger(__name__)

# Drop data for an ffmpeg that is this far behind instead of buffering
# without limit (about 6 s of a 14 Mbit/s multiplex).
FEED_LIMIT = 10 * 1024 * 1024
# How long to wait for a new stream to reach MediaMTX.
READY_TIMEOUT = 15
STATUS_INTERVAL = 1.0
# A stream whose ffmpeg dies this soon after starting is not restarted.
CRASH_WINDOW = 5.0


class StreamError(Exception):
    """
    A channel could not be started.

    :param str message: Human-readable reason, shown in the UI.
    :param int status: HTTP status code for the API.
    """

    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def path_name(number):
    """
    MediaMTX path for a channel number.

    :param str number: Channel number, such as ``'6.1'``.

    :return: A URL-safe path name, such as ``'ch-6-1'``.
    :rtype: str
    """
    slug = re.sub(r'[^a-z0-9]+', '-', number.lower()).strip('-')
    return f'ch-{slug}'


def choose_tuner(tuners, frequency, requested='auto'):
    """
    Pick the tuner for a channel.

    In order of preference: the requested tuner; a tuner already on the
    multiplex; an idle tuner; a tuner only carrying pre-warmed streams; the
    tuner whose streams were viewed least recently.

    :param list tuners: ``Tuner`` objects.
    :param int frequency: Multiplex of the channel.
    :param requested: ``'auto'`` or an adapter number.

    :return: The tuner and whether it has to be retuned.
    :rtype: tuple

    :raises StreamError: If the requested tuner does not exist.
    """
    if requested != 'auto':
        for tuner in tuners:
            if tuner.adapter == requested:
                return tuner, tuner.frequency != frequency
        raise StreamError(f'there is no tuner {requested}', 404)

    for tuner in tuners:
        if tuner.frequency == frequency and tuner.state in (
            'tuning', 'locked',
        ):
            return tuner, False
    for tuner in tuners:
        if not tuner.streams:
            return tuner, True
    for tuner in tuners:
        if all(stream.prewarm for stream in tuner.streams):
            return tuner, True
    return min(tuners, key=lambda t: t.last_viewed()), True


def neighbours(channels, number):
    """
    The channels before and after a channel, wrapping around.

    :param list channels: Channels in list order.
    :param str number: Channel number.

    :return: Channel numbers (empty for a single channel).
    :rtype: list
    """
    numbers = [channel.number for channel in channels]
    if len(numbers) < 2 or number not in numbers:
        return []
    index = numbers.index(number)
    around = {
        numbers[(index - 1) % len(numbers)],
        numbers[(index + 1) % len(numbers)],
    }
    around.discard(number)
    return sorted(around, key=numbers.index)


class Tuner:
    """
    One DVB adapter: a frontend and the transport stream it delivers.

    :param int adapter: Adapter number.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self.frequency = None
        self.state = 'idle'
        self.stats = {'signal_dbm': None, 'cnr_db': None}
        self.streams = set()
        self._frontend = dvb.Frontend(adapter)
        self._ts = None

    def last_viewed(self):
        """
        :return: Most recent time any of its streams had a viewer.
        :rtype: float
        """
        return max((s.last_viewed for s in self.streams), default=0.0)

    async def tune(self, frequency, bandwidth, timeout):
        """
        Tune to a multiplex and start reading its transport stream.

        :param int frequency: Centre frequency in Hz.
        :param int bandwidth: Bandwidth in Hz.
        :param float timeout: Seconds to wait for the lock.

        :raises StreamError: If the frontend does not lock in time.
        """
        self._stop_reading()
        self.frequency = frequency
        self.state = 'tuning'
        self.stats = {'signal_dbm': None, 'cnr_db': None}
        try:
            self._frontend.open()
            self._frontend.tune(frequency, bandwidth)
        except OSError as e:
            self.state = 'error'
            raise StreamError(f'tuner {self.adapter}: {e.strerror}') from e

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = await asyncio.to_thread(self._frontend.status)
            if status & dvb.FE_HAS_LOCK:
                break
            if loop.time() > deadline:
                self.state = 'nolock'
                raise StreamError(
                    f'no signal on {frequency / 1e6:.3f} MHz '
                    f'(tuner {self.adapter})'
                )
            await asyncio.sleep(0.02)

        self.state = 'locked'
        self._ts = dvb.TransportStream(self.adapter)
        self._ts.open()
        loop.add_reader(self._ts.fileno(), self._fan_out)
        log.info('tuner %d locked on %d Hz', self.adapter, frequency)

    def _fan_out(self):
        """
        Read what the DVR device has and feed it to every stream.
        """
        try:
            data = self._ts.read()
        except OSError as e:
            log.warning('tuner %d read error: %s', self.adapter, e)
            return
        if data:
            for stream in self.streams:
                stream.feed(data)

    async def refresh(self):
        """
        Update lock state and signal statistics.
        """
        if self.state not in ('locked', 'nolock'):
            return
        try:
            status = await asyncio.to_thread(self._frontend.status)
            self.stats = await asyncio.to_thread(self._frontend.stats)
        except OSError as e:
            log.warning('tuner %d status: %s', self.adapter, e)
            return
        self.state = 'locked' if status & dvb.FE_HAS_LOCK else 'nolock'

    def _stop_reading(self):
        if self._ts is not None:
            asyncio.get_running_loop().remove_reader(self._ts.fileno())
            self._ts.close()
            self._ts = None

    def release(self):
        """
        Stop reading and close the frontend.
        """
        self._stop_reading()
        self._frontend.close()
        self.frequency = None
        self.state = 'idle'
        self.stats = {'signal_dbm': None, 'cnr_db': None}

    def snapshot(self):
        """
        :return: JSON-serialisable state for the UI.
        :rtype: dict
        """
        return {
            'adapter': self.adapter,
            'state': self.state,
            'frequency': self.frequency,
            'signal_dbm': self.stats['signal_dbm'],
            'cnr_db': self.stats['cnr_db'],
            'channels': sorted(s.channel.number for s in self.streams),
        }


class Stream:
    """
    One channel transcoded by ffmpeg and published to MediaMTX.

    :param channel: The channel configuration.
    :type channel: objns.Namespace
    :param Tuner tuner: The tuner carrying its multiplex.
    :param bool prewarm: Started ahead of a viewer.
    """

    def __init__(self, channel, tuner, prewarm=False):
        self.channel = channel
        self.tuner = tuner
        self.prewarm = prewarm
        self.path = path_name(channel.number)
        self.state = 'starting'
        self.viewers = 0
        now = time.monotonic()
        self.started = now
        self.last_viewed = now
        self.dropped = 0
        self.last_error = None
        self._process = None
        self._stderr = None

    async def start(self, transcode, url):
        """
        Start ffmpeg and attach to the tuner.

        :param transcode: The ``transcode`` configuration section.
        :type transcode: objns.Namespace
        :param str url: RTSP URL to publish to.

        :raises StreamError: If ffmpeg cannot be started.
        """
        argv = command(
            transcode, self.channel.service_id, self.channel.audio, url,
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise StreamError(f'cannot run {argv[0]}: {e.strerror}') from e
        self._stderr = asyncio.create_task(self._log_stderr())
        self.started = time.monotonic()
        self.tuner.streams.add(self)

    async def _log_stderr(self):
        while line := await self._process.stderr.readline():
            self.last_error = line.decode(errors='replace').rstrip()
            log.warning('ffmpeg %s: %s', self.channel.number,
                        self.last_error)

    @property
    def alive(self):
        """
        :return: Whether ffmpeg is running.
        :rtype: bool
        """
        return self._process is not None and self._process.returncode is None

    def feed(self, data):
        """
        Pass transport stream data to ffmpeg, dropping it if ffmpeg is too
        far behind.

        :param bytes data: Transport stream bytes.
        """
        if not self.alive:
            return
        transport = self._process.stdin.transport
        if transport.is_closing():
            return
        if transport.get_write_buffer_size() > FEED_LIMIT:
            self.dropped += len(data)
            return
        self._process.stdin.write(data)

    async def stop(self):
        """
        Detach from the tuner and stop ffmpeg.
        """
        self.tuner.streams.discard(self)
        self.state = 'stopped'
        if self._process is None:
            return
        if self.alive:
            self._process.stdin.close()
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 3)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._stderr is not None:
            await self._stderr

    def snapshot(self):
        """
        :return: JSON-serialisable state for the UI.
        :rtype: dict
        """
        return {
            'number': self.channel.number,
            'path': self.path,
            'tuner': self.tuner.adapter,
            'state': self.state,
            'prewarm': self.prewarm,
            'viewers': self.viewers,
        }


class Manager:
    """
    Owns the tuners and streams, and keeps them matched to what viewers
    are watching.

    :param config: The whole configuration.
    :type config: objns.Namespace
    :param mediamtx: The running MediaMTX.
    :type mediamtx: sintonia.mediamtx.MediaMTX
    :param list adapters: Adapter numbers to use.
    """

    def __init__(self, config, mediamtx, adapters):
        self.config = config
        self.mediamtx = mediamtx
        self.channels = {c.number: c for c in config.channels}
        self.tuners = [Tuner(adapter) for adapter in adapters]
        self.streams = {}
        self._lock = asyncio.Lock()
        self._listeners = set()
        self._last_state = None
        self._monitor = None

    # Events ---------------------------------------------------------------

    def subscribe(self):
        """
        :return: A queue that receives ``(event, data)`` tuples.
        :rtype: asyncio.Queue
        """
        queue = asyncio.Queue(maxsize=100)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue):
        """
        :param asyncio.Queue queue: A queue from ``subscribe()``.
        """
        self._listeners.discard(queue)

    def _emit(self, event, data):
        for queue in list(self._listeners):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                # A stuck client: drop it rather than block everyone.
                self._listeners.discard(queue)

    def snapshot(self):
        """
        :return: JSON-serialisable state of tuners and streams.
        :rtype: dict
        """
        return {
            'tuners': [tuner.snapshot() for tuner in self.tuners],
            'streams': [
                stream.snapshot() for stream in self.streams.values()
            ],
        }

    def _publish_state(self, force=False):
        state = self.snapshot()
        encoded = json.dumps(state, sort_keys=True)
        if force or encoded != self._last_state:
            self._last_state = encoded
            self._emit('state', state)

    # Lifecycle ------------------------------------------------------------

    async def start(self):
        """
        Start the background monitor.
        """
        self._monitor = asyncio.create_task(self._run_monitor())

    async def stop(self):
        """
        Stop every stream and release every tuner.
        """
        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for stream in list(self.streams.values()):
                await stream.stop()
            self.streams.clear()
            for tuner in self.tuners:
                tuner.release()

    # Watching -------------------------------------------------------------

    async def watch(self, number, tuner='auto'):
        """
        Make a channel available to a viewer.

        :param str number: Channel number.
        :param tuner: ``'auto'`` or an adapter number.

        :return: The MediaMTX path of the channel.
        :rtype: str

        :raises StreamError: If the channel cannot be started.
        """
        if number not in self.channels:
            raise StreamError(f'unknown channel {number}', 404)
        async with self._lock:
            stream = await self._ensure(number, tuner, prewarm=False)
        self._publish_state()
        await self._wait_live(stream)
        return stream.path

    async def _wait_live(self, stream):
        """
        Wait until MediaMTX has the stream, failing early if ffmpeg dies.

        :param Stream stream: A started stream.

        :raises StreamError: If ffmpeg exits or the stream is not ready in
         time.
        """
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await self.mediamtx.wait_ready(stream.path, 0.25):
                if stream.state == 'starting':
                    stream.state = 'live'
                    self._publish_state()
                return
            if not stream.alive:
                reason = stream.last_error or 'ffmpeg exited'
                raise StreamError(
                    f'channel {stream.channel.number} failed: {reason}'
                )
        raise StreamError(
            f'channel {stream.channel.number} did not start in time'
        )

    async def _ensure(self, number, requested, prewarm):
        """
        Get a stream for a channel running, retuning a tuner if needed.
        Must be called with the lock held.
        """
        channel = self.channels[number]
        stream = self.streams.get(number)
        if stream is not None:
            wrong_tuner = (
                requested != 'auto' and stream.tuner.adapter != requested
            )
            if stream.alive and not wrong_tuner:
                if not prewarm:
                    stream.prewarm = False
                    stream.last_viewed = time.monotonic()
                return stream
            await self._stop_stream(stream)

        tuner, retune = choose_tuner(
            self.tuners, channel.frequency, requested,
        )
        if retune:
            for other in list(tuner.streams):
                if not other.prewarm:
                    self._emit('notice', {
                        'channel': other.channel.number,
                        'message': (
                            f'Tuner {tuner.adapter} was taken for channel '
                            f'{number}'
                        ),
                    })
                await self._stop_stream(other)
            await tuner.tune(
                channel.frequency, channel.bandwidth,
                self.config.tuners.lock_timeout,
            )

        stream = Stream(channel, tuner, prewarm=prewarm)
        await stream.start(
            self.config.transcode, self.mediamtx.rtsp_url(stream.path),
        )
        self.streams[number] = stream
        log.info(
            'started %s%s on tuner %d', number,
            ' (pre-warm)' if prewarm else '', tuner.adapter,
        )
        return stream

    async def _stop_stream(self, stream):
        """
        Stop a stream, and release its tuner if nothing else uses it.
        Must be called with the lock held.
        """
        await stream.stop()
        if self.streams.get(stream.channel.number) is stream:
            del self.streams[stream.channel.number]
        if not stream.tuner.streams:
            stream.tuner.release()
        log.info('stopped %s', stream.channel.number)

    # Monitor --------------------------------------------------------------

    async def _run_monitor(self):
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception('monitor tick failed')
            await asyncio.sleep(STATUS_INTERVAL)

    async def _tick(self):
        """
        Update viewers and tuner stats, stop idle or dead streams, and keep
        the neighbours of watched channels pre-warmed.
        """
        paths = await self.mediamtx.paths()
        now = time.monotonic()
        idle_timeout = self.config.streams.idle_timeout

        async with self._lock:
            for tuner in self.tuners:
                await tuner.refresh()

            for stream in list(self.streams.values()):
                info = paths.get(stream.path, {})
                stream.viewers = info.get('readers', 0)
                if stream.viewers:
                    stream.last_viewed = now
                    stream.prewarm = False
                if info.get('ready') and stream.state == 'starting':
                    stream.state = 'live'

                if not stream.alive:
                    log.warning('ffmpeg for %s exited',
                                stream.channel.number)
                    quick = now - stream.started < CRASH_WINDOW
                    await self._stop_stream(stream)
                    if not quick and not stream.prewarm:
                        await self._restart(stream)
                    continue
                if (not stream.prewarm
                        and now - stream.last_viewed > idle_timeout):
                    await self._stop_stream(stream)

            if self.config.tuners.prewarm:
                await self._prewarm()
        self._publish_state()

    async def _restart(self, stream):
        try:
            await self._ensure(stream.channel.number, 'auto', prewarm=False)
        except StreamError as e:
            log.warning('restart of %s failed: %s',
                        stream.channel.number, e)

    async def _prewarm(self):
        """
        Keep the neighbours of watched channels live, using only spare
        capacity: a tuner already on the right multiplex, or an idle one.
        Must be called with the lock held.
        """
        watched = [
            s.channel.number for s in self.streams.values() if not s.prewarm
        ]
        wanted = []
        for number in watched:
            for neighbour in neighbours(self.config.channels, number):
                if neighbour not in watched and neighbour not in wanted:
                    wanted.append(neighbour)

        for stream in list(self.streams.values()):
            if stream.prewarm and stream.channel.number not in wanted:
                await self._stop_stream(stream)

        for number in wanted:
            if number in self.streams:
                continue
            channel = self.channels[number]
            spare = any(
                (t.frequency == channel.frequency and t.state == 'locked')
                or not t.streams
                for t in self.tuners
            )
            if not spare:
                continue
            try:
                await self._ensure(number, 'auto', prewarm=True)
            except StreamError as e:
                log.info('pre-warm of %s skipped: %s', number, e)
