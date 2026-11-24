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
MediaMTX: configuration, process supervision and control API.

ffmpeg publishes each channel to MediaMTX over RTSP on localhost, and
browsers read it over WebRTC (WHEP). The WHEP signalling port is bound to
localhost too: browsers reach it through Sintonía's proxy, so only the
WebRTC media port (UDP) is exposed.
"""

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import httpx2 as httpx

log = logging.getLogger(__name__)

LOCALHOST = '127.0.0.1'


def render_config(mediamtx):
    """
    Render a MediaMTX configuration file.

    YAML is a superset of JSON, so the configuration is written as JSON:
    no YAML dependency and no quoting surprises.

    :param mediamtx: The ``mediamtx`` configuration section.
    :type mediamtx: objns.Namespace

    :return: The configuration file contents.
    :rtype: str
    """
    local = {'user': 'any', 'pass': '', 'ips': [LOCALHOST, '::1']}
    config = {
        'logLevel': 'warn',
        'logDestinations': ['stdout'],
        'authMethod': 'internal',
        'authInternalUsers': [
            # Anyone may read: viewers come through the WHEP proxy.
            {'user': 'any', 'pass': '', 'ips': [],
             'permissions': [{'action': 'read'}]},
            # Only local processes may publish or use the API.
            dict(local, permissions=[
                {'action': 'publish'}, {'action': 'api'},
            ]),
        ],
        'api': True,
        'apiAddress': f'{LOCALHOST}:{mediamtx.api_port}',
        'metrics': False,
        'pprof': False,
        'playback': False,
        'rtsp': True,
        'rtspAddress': f'{LOCALHOST}:{mediamtx.rtsp_port}',
        'rtspTransports': ['tcp'],
        'rtmp': False,
        'hls': False,
        'srt': False,
        'moq': False,
        'webrtc': True,
        'webrtcAddress': f'{LOCALHOST}:{mediamtx.webrtc_port}',
        'webrtcAllowOrigins': ['*'],
        'webrtcLocalUDPAddress': f':{mediamtx.webrtc_udp_port}',
        'webrtcAdditionalHosts': list(mediamtx.webrtc_hosts),
        'paths': {'all_others': {}},
    }
    return json.dumps(config, indent=2) + '\n'


class MediaMTXError(Exception):
    """
    MediaMTX failed to start or its API is not answering.
    """


class MediaMTX:
    """
    A supervised MediaMTX process.

    :param mediamtx: The ``mediamtx`` configuration section.
    :type mediamtx: objns.Namespace
    """

    def __init__(self, mediamtx):
        self.config = mediamtx
        self.api_url = f'http://{LOCALHOST}:{mediamtx.api_port}'
        self.whep_base = f'http://{LOCALHOST}:{mediamtx.webrtc_port}'
        self._process = None
        self._tmpdir = None
        self._client = None
        self._logger = None

    def rtsp_url(self, path):
        """
        :param str path: Stream path name.

        :return: The URL ffmpeg publishes to.
        :rtype: str
        """
        return f'rtsp://{LOCALHOST}:{self.config.rtsp_port}/{path}'

    def whep_url(self, path):
        """
        :param str path: Stream path name.

        :return: MediaMTX's WHEP endpoint for the path.
        :rtype: str
        """
        return f'{self.whep_base}/{path}/whep'

    async def start(self, timeout=10):
        """
        Write the configuration, start MediaMTX and wait for its API.

        :param float timeout: Seconds to wait for the API.

        :raises MediaMTXError: If the process exits or never answers.
        """
        self._tmpdir = tempfile.TemporaryDirectory(prefix='sintonia-')
        config_path = Path(self._tmpdir.name) / 'mediamtx.yml'
        config_path.write_text(render_config(self.config))

        self._process = await asyncio.create_subprocess_exec(
            self.config.binary, str(config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._tmpdir.name,
        )
        self._logger = asyncio.create_task(self._log_output())
        self._client = httpx.AsyncClient(timeout=5)

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._process.returncode is not None:
                raise MediaMTXError(
                    f'mediamtx exited with {self._process.returncode}'
                )
            try:
                response = await self._client.get(
                    f'{self.api_url}/v3/paths/list'
                )
                if response.status_code == 200:
                    log.info('mediamtx ready')
                    return
            except httpx.TransportError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise MediaMTXError('mediamtx API did not come up')
            await asyncio.sleep(0.1)

    async def _log_output(self):
        """
        Forward MediaMTX's output to our log.
        """
        while line := await self._process.stdout.readline():
            log.info('mediamtx: %s', line.decode(errors='replace').rstrip())

    @property
    def running(self):
        """
        :return: Whether the process is alive.
        :rtype: bool
        """
        return self._process is not None and self._process.returncode is None

    async def paths(self):
        """
        Read the state of every path.

        :return: Path name to ``{'ready': bool, 'readers': int}``.
        :rtype: dict

        :raises MediaMTXError: If the API does not answer.
        """
        try:
            response = await self._client.get(
                f'{self.api_url}/v3/paths/list', params={'itemsPerPage': 1000}
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise MediaMTXError(f'mediamtx API: {e}') from e
        return {
            item['name']: {
                'ready': bool(item.get('ready')),
                'readers': len(item.get('readers') or []),
            }
            for item in response.json().get('items', [])
        }

    async def wait_ready(self, path, timeout):
        """
        Wait until a path has a publisher with its tracks.

        :param str path: Stream path name.
        :param float timeout: Seconds to wait.

        :return: Whether the path became ready in time.
        :rtype: bool
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await self._client.get(
                    f'{self.api_url}/v3/paths/get/{path}'
                )
                if response.status_code == 200 and response.json().get(
                    'ready'
                ):
                    return True
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.05)
        return False

    async def stop(self):
        """
        Stop MediaMTX and clean up.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self.running:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 5)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._logger is not None:
            await self._logger
            self._logger = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
