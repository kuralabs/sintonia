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

import asyncio

import httpx2 as httpx
import pytest
from objns import Namespace
from starlette.testclient import TestClient

from sintonia.app import create_app
from sintonia.config import validate
from sintonia.streams import StreamError

CONFIG = validate({'channels': [
    {'number': '6.1', 'name': 'Canal 6 HD', 'frequency': 521142857,
     'service_id': 19232},
    {'number': '15.1', 'name': '15-UCR HD', 'frequency': 479142857,
     'service_id': 32},
]})


class FakeMediaMTX:
    running = True

    async def start(self):
        pass

    async def stop(self):
        pass

    def whep_url(self, path):
        return f'http://127.0.0.1:8889/{path}/whep'


class FakeManager:
    def __init__(self):
        self.tuners = [Namespace(adapter=0), Namespace(adapter=1)]
        self.watched = []
        self.queue = None

    async def start(self):
        pass

    async def stop(self):
        pass

    def snapshot(self):
        return {'tuners': [], 'streams': []}

    def subscribe(self):
        self.queue = asyncio.Queue()
        return self.queue

    def unsubscribe(self, queue):
        pass

    async def watch(self, number, tuner='auto'):
        if number == '9.9':
            raise StreamError('unknown channel 9.9', 404)
        self.watched.append((number, tuner))
        return f'ch-{number.replace(".", "-")}'


@pytest.fixture
def manager():
    return FakeManager()


@pytest.fixture
def client(manager):
    app = create_app(CONFIG, [0, 1], FakeMediaMTX(), manager)
    with TestClient(app) as client:
        yield client


def test_health(client):
    assert client.get('/healthz').json()['status'] == 'ok'


def test_config(client):
    body = client.get('/api/config').json()
    assert body['channels'] == [
        {'number': '6.1', 'name': 'Canal 6 HD'},
        {'number': '15.1', 'name': '15-UCR HD'},
    ]
    assert body['tuners'] == [0, 1]


def test_ui_is_served(client):
    response = client.get('/')
    assert response.status_code == 200
    assert '<title>Sintonía</title>' in response.text
    assert client.get('/app.js').status_code == 200


def test_watch(client, manager):
    response = client.post('/api/watch', json={'channel': '6.1'})
    assert response.json() == {'path': 'ch-6-1', 'whep': '/whep/ch-6-1'}
    assert manager.watched == [('6.1', 'auto')]

    client.post('/api/watch', json={'channel': '15.1', 'tuner': 1})
    assert manager.watched[-1] == ('15.1', 1)


@pytest.mark.parametrize('body', [
    {},
    {'channel': ''},
    {'channel': '6.1', 'tuner': 'first'},
    {'channel': '6.1', 'tuner': -1},
    ['6.1'],
])
def test_watch_rejects_bad_requests(client, body):
    response = client.post('/api/watch', json=body)
    assert response.status_code == 400
    assert 'invalid request' in response.json()['error']


def test_watch_rejects_non_json(client):
    response = client.post('/api/watch', content=b'{nope')
    assert response.status_code == 400


def test_watch_errors(client):
    response = client.post('/api/watch', json={'channel': '9.9'})
    assert response.status_code == 404
    assert response.json() == {'error': 'unknown channel 9.9'}


def test_whep_proxy(client):
    seen = {}

    def upstream(request):
        seen['url'] = str(request.url)
        seen['body'] = request.content
        if request.method == 'POST':
            return httpx.Response(
                201, content=b'v=0 answer',
                headers={'Content-Type': 'application/sdp',
                         'Location': '/ch-6-1/whep/abc-123'},
            )
        return httpx.Response(200)

    client.app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
    )
    response = client.post(
        '/whep/ch-6-1', content=b'v=0 offer',
        headers={'Content-Type': 'application/sdp'},
    )
    assert response.status_code == 201
    assert response.content == b'v=0 answer'
    assert response.headers['location'] == '/whep/ch-6-1/abc-123'
    assert seen == {'url': 'http://127.0.0.1:8889/ch-6-1/whep',
                    'body': b'v=0 offer'}

    response = client.delete('/whep/ch-6-1/abc-123')
    assert response.status_code == 200
    assert seen['url'] == 'http://127.0.0.1:8889/ch-6-1/whep/abc-123'


def test_whep_proxy_refuses_unknown_paths(client):
    assert client.post('/whep/secret', content=b'x').status_code == 404
    assert client.delete('/whep/secret/abc').status_code == 404
