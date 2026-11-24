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
HTTP interface: the UI, a small JSON API, live status over Server-Sent
Events, and a proxy for WHEP signalling so the browser only ever talks to
this origin.
"""

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import httpx2 as httpx
from ferberus import Validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .mediamtx import MediaMTX
from .streams import Manager, StreamError, path_name

log = logging.getLogger(__name__)

UI = Path(__file__).parent / 'data' / 'ui'
SSE_PING = 15

WATCH_SCHEMA = {
    'channel': {'type': 'string', 'required': True, 'empty': False},
    'tuner': {
        'default': 'auto',
        'anyof': [
            {'type': 'string', 'allowed': ['auto']},
            {'type': 'integer', 'min': 0},
        ],
    },
}


def error(message, status):
    """
    :param str message: What went wrong, for the UI.
    :param int status: HTTP status code.

    :return: A JSON error response.
    :rtype: starlette.responses.JSONResponse
    """
    return JSONResponse({'error': message}, status_code=status)


async def config_endpoint(request):
    state = request.app.state
    return JSONResponse({
        'version': __version__,
        'channels': [
            {'number': c.number, 'name': c.name}
            for c in state.config.channels
        ],
        'tuners': [t.adapter for t in state.manager.tuners],
    })


async def state_endpoint(request):
    return JSONResponse(request.app.state.manager.snapshot())


async def events_endpoint(request):
    manager = request.app.state.manager
    queue = manager.subscribe()

    async def stream():
        try:
            yield _sse('state', manager.snapshot())
            while True:
                try:
                    event, data = await asyncio.wait_for(
                        queue.get(), SSE_PING,
                    )
                except TimeoutError:
                    yield ': ping\n\n'
                    continue
                yield _sse(event, data)
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'},
    )


def _sse(event, data):
    """
    :param str event: Event name.
    :param data: JSON-serialisable payload.

    :return: One Server-Sent Events message.
    :rtype: str
    """
    return f'event: {event}\ndata: {json.dumps(data)}\n\n'


async def watch_endpoint(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return error('request body must be JSON', 400)
    validator = Validator(WATCH_SCHEMA)
    if not isinstance(body, dict) or not validator.validate(body):
        return error(f'invalid request: {validator.errors}', 400)
    body = validator.document
    try:
        path = await request.app.state.manager.watch(
            body['channel'], body['tuner'],
        )
    except StreamError as e:
        return error(str(e), e.status)
    return JSONResponse({'path': path, 'whep': f'/whep/{path}'})


def _known_path(request):
    """
    :return: The requested stream path if it belongs to a configured
     channel, else ``None`` (the proxy only forwards known paths).
    :rtype: str or None
    """
    path = request.path_params['path']
    return path if path in request.app.state.paths else None


async def whep_offer_endpoint(request):
    path = _known_path(request)
    if path is None:
        return error('unknown stream', 404)
    state = request.app.state
    try:
        upstream = await state.http.post(
            state.mediamtx.whep_url(path),
            content=await request.body(),
            headers={'Content-Type': 'application/sdp'},
        )
    except httpx.HTTPError as e:
        return error(f'media server unavailable: {e}', 502)

    headers = {}
    location = upstream.headers.get('location', '')
    prefix = f'/{path}/whep/'
    if location.startswith(prefix):
        session = location[len(prefix):]
        headers['Location'] = f'/whep/{path}/{session}'
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get('content-type'),
        headers=headers,
    )


async def whep_session_endpoint(request):
    path = _known_path(request)
    if path is None:
        return error('unknown stream', 404)
    state = request.app.state
    session = request.path_params['session']
    url = f'{state.mediamtx.whep_url(path)}/{session}'
    try:
        upstream = await state.http.request(
            request.method, url,
            content=await request.body(),
            headers={
                key: value for key, value in request.headers.items()
                if key.lower() in ('content-type', 'if-match')
            },
        )
    except httpx.HTTPError as e:
        return error(f'media server unavailable: {e}', 502)
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get('content-type'),
    )


async def health_endpoint(request):
    if not request.app.state.mediamtx.running:
        return error('media server is not running', 503)
    return JSONResponse({'status': 'ok', 'version': __version__})


def create_app(config, adapters, mediamtx=None, manager=None):
    """
    Build the application.

    :param config: The whole configuration.
    :type config: objns.Namespace
    :param list adapters: Adapter numbers to use.
    :param mediamtx: Media server (tests pass a fake).
    :param manager: Stream manager (tests pass a fake).

    :return: The ASGI application.
    :rtype: starlette.applications.Starlette
    """
    mediamtx = mediamtx or MediaMTX(config.mediamtx)
    manager = manager or Manager(config, mediamtx, adapters)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.config = config
        app.state.mediamtx = mediamtx
        app.state.manager = manager
        app.state.paths = {path_name(c.number) for c in config.channels}
        app.state.http = httpx.AsyncClient(timeout=10)
        await mediamtx.start()
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()
            await mediamtx.stop()
            await app.state.http.aclose()

    routes = [
        Route('/healthz', health_endpoint),
        Route('/api/config', config_endpoint),
        Route('/api/state', state_endpoint),
        Route('/api/events', events_endpoint),
        Route('/api/watch', watch_endpoint, methods=['POST']),
        Route('/whep/{path}', whep_offer_endpoint, methods=['POST']),
        Route('/whep/{path}/{session}', whep_session_endpoint,
              methods=['PATCH', 'DELETE']),
        Mount('/', StaticFiles(directory=UI, html=True), name='ui'),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
