import asyncio
import copy
import hashlib
import ipaddress
import json
import socket
from pathlib import Path
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from jinja2 import Environment, StrictUndefined
from app.config import Settings
from app.db import DB
from app.ha import HA
from app.changes import Changes
from app.service import Service

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    def check(address):
        if isinstance(address, tuple):
            try:
                valid = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                valid = False
            if not valid:
                raise AssertionError('Tests may connect only to loopback fake servers')
    def connect(sock, address):
        check(address)
        return original(sock, address)
    def connect_ex(sock, address):
        check(address)
        return original_ex(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)


@pytest.fixture(scope='session', autouse=True)
def unchanged_fixtures():
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURES.glob('*.json')}
    yield
    assert hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURES.glob('*.json')}


class FakeHA:
    def __init__(self):
        self.app = FastAPI()
        self.calls = []
        self.errors = {}
        self.entities = {}
        for item in fixture('entity_registry_list_for_display')['entities']:
            self.entities[item['ei']] = dict(entity_id=item['ei'], original_name=item.get('en', item['ei']), aliases=[], device_id=item.get('di'), area_id=None)
        self.entities['light.test_lamp'] = fixture('entity_registry_get_test_lamp')
        self.entities['climate.thermostat'] = dict(entity_id='climate.thermostat', original_name='Thermostat', aliases=[])
        self.areas = fixture('area_registry_list')
        self.devices = fixture('device_registry_list')['devices']
        self.exposed = fixture('expose_entity_list')['exposed_entities']
        voice = fixture('automation_config_voice')
        self.automations = {voice['state_attributes']['id']: voice['config']}
        self.pipelines = [{'id': 'house', 'name': 'House'}]
        listing = fixture('pipeline_debug_list')['pipeline_runs']
        self.runs = {listing[0]['pipeline_run_id']: fixture('run_voice'), listing[1]['pipeline_run_id']: fixture('run_text_llm_tool')}
        self.matches = {"Turn on Sam's office.": fixture('matcher_debug')['results'][1]}
        self.alias_sentence = 'turn on reading glow'
        self.matches_after_alias = {}
        self.app.websocket('/api/websocket')(self.websocket)
        self.reloads = 0
        self.automation_reloads = 0
        self.template_calls = []
        self.app.post('/api/services/automation/reload')(self.reload_automations)
        self.app.post('/api/template')(self.template)
        self.asked = []
        self.app.post('/api/conversation/process')(self.converse)
        self.app.post('/api/services/conversation/reload')(self.reload)
        self.app.get('/api/states')(self.states)
        self.app.get('/api/')(self.api_root)
        self.app.get('/api/config')(lambda: {'version': '2026.11.0', 'location_name': 'Test Home'})
        self.app.api_route('/api/config/automation/config/{ident}', methods=['GET', 'POST', 'DELETE'])(self.automation)

    def match(self, sentence):
        if sentence == self.alias_sentence and 'reading glow' in self.entities['light.test_lamp']['aliases']:
            return fixture('matcher_debug')['results'][1]
        if sentence in self.matches_after_alias and 'reading glow' in self.entities['light.test_lamp']['aliases']:
            return copy.deepcopy(self.matches_after_alias[sentence])
        return copy.deepcopy(self.matches.get(sentence))

    async def websocket(self, ws: WebSocket):
        await ws.accept()
        await ws.send_json({'type': 'auth_required'})
        auth = await ws.receive_json()
        assert auth['type'] == 'auth'
        self.auth_attempts = getattr(self, 'auth_attempts', 0) + 1
        if getattr(self, 'reject_token', False):
            await ws.send_json({'type': 'auth_invalid', 'message': 'Invalid access token or password'})
            await ws.close()
            return
        await ws.send_json({'type': 'auth_ok'})
        try:
            while True:
                message = await ws.receive_json()
                self.calls.append(message)
                if message['type'] in self.errors:
                    await ws.send_json({'id': message['id'], 'type': 'result', 'success': False, 'error': self.errors[message['type']]})
                    continue
                result = self.dispatch(message)
                await ws.send_json({'id': message['id'], 'type': 'result', 'success': True, 'result': result})
        except Exception as exc:
            from starlette.websockets import WebSocketDisconnect
            if not isinstance(exc, WebSocketDisconnect):
                raise

    def dispatch(self, m):
        kind = m['type']
        if kind == 'assist_pipeline/pipeline/list':
            return {'pipelines': self.pipelines, 'preferred_pipeline': 'house'}
        if kind == 'assist_pipeline/pipeline_debug/list':
            return {'pipeline_runs': [{'pipeline_run_id': k, 'timestamp': v['events'][0]['timestamp']} for k, v in self.runs.items()]}
        if kind == 'assist_pipeline/pipeline_debug/get':
            return self.runs[m['pipeline_run_id']]
        if kind == 'conversation/agent/homeassistant/debug':
            return {'results': [self.match(s) for s in m['sentences']]}
        if kind == 'config/entity_registry/list_for_display':
            return {'entities': [{'ei': k} for k in self.entities]}
        if kind == 'config/entity_registry/list':
            return list(self.entities.values())
        if kind == 'config/entity_registry/get':
            return self.entities[m['entity_id']]
        if kind == 'config/entity_registry/update':
            self.entities[m['entity_id']]['aliases'] = m['aliases']
            return self.entities[m['entity_id']]
        if kind == 'config/area_registry/list':
            return self.areas
        if kind == 'config/area_registry/update':
            area = next(a for a in self.areas if a['area_id'] == m['area_id'])
            area['aliases'] = m['aliases']
            return area
        if kind == 'config/device_registry/list':
            return self.devices
        if kind == 'homeassistant/expose_entity/list':
            return {'exposed_entities': self.exposed}
        if kind == 'homeassistant/expose_entity':
            for eid in m['entity_ids']:
                self.exposed.setdefault(eid, {})['conversation'] = m['should_expose']
            return None
        raise AssertionError('Unexpected HA command: ' + kind)

    async def reload_automations(self):
        self.automation_reloads += 1
        return []

    async def template(self, request: Request):
        body = await request.json()
        self.template_calls.append(body)
        env = Environment(undefined=StrictUndefined)
        env.globals.update(states=lambda entity: 'heat', state_attr=lambda entity, key: {
            'temperature': 72, 'current_temperature': 70, 'hvac_action': 'heating'}.get(key))
        try:
            return PlainTextResponse(env.from_string(body['template']).render(**body['variables']))
        except Exception:
            raise HTTPException(400, 'Template render failed')

    async def reload(self):
        self.reloads += 1
        return []

    async def converse(self, request: Request):
        body = await request.json()
        self.asked.append(body)
        return {'response': {'speech': {'plain': {'speech': 'Answer to ' + body['text']}}}}

    async def api_root(self):
        if getattr(self, 'reject_token', False):
            return JSONResponse({'message': 'Unauthorized'}, status_code=401)
        return {'message': 'API running.'}

    async def states(self):
        return [{'entity_id': 'automation.' + key, 'state': 'on', 'attributes': {'id': key}} for key in self.automations]

    async def automation(self, ident: str, request: Request):
        if request.method == 'GET':
            if ident not in self.automations:
                raise HTTPException(404)
            return self.automations[ident]
        if request.method == 'POST':
            self.automations[ident] = await request.json()
        else:
            self.automations.pop(ident, None)
        return {'result': 'ok'}


@pytest_asyncio.fixture
async def fake_ha():
    fake = FakeHA()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    config = uvicorn.Config(fake.app, log_level='error', lifespan='off', ws='websockets-sansio')
    # uvicorn 0.34 supports the standard websockets implementation.
    config.ws = 'websockets'
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    fake.url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    yield fake
    server.should_exit = True
    await task
    sock.close()


@pytest_asyncio.fixture
async def stack(fake_ha, tmp_path):
    settings = Settings(ha_url=fake_ha.url, ha_token='fake-ha-placeholder', token='test-placeholder', db_path=str(tmp_path / 'db.sqlite'), reload_timeout=0.01)
    db = DB(settings.db_path)
    ha = HA(settings)
    changes = Changes(db, ha, settings)
    service = Service(db, ha, changes)
    yield settings, db, ha, changes, service
    await service.diagnose.close()
    await ha.close()
    db.conn.close()


class FakeOpenAI:
    def __init__(self):
        self.app = FastAPI()
        self.scripts = []
        self.calls = []
        self.user_agents = []
        self.auth = []
        self.delay = 0
        self.active = 0
        self.max_active = 0
        self.unsupported = set()   # fields this fake provider rejects, Cerebras-style
        self.models = [{'id': 'some/model'}, {'id': 'other/model'}]
        self.app.post('/v1/chat/completions')(self.complete)
        self.app.get('/v1/models')(lambda: {'object': 'list', 'data': self.models})

    async def complete(self, request: Request):
        payload = await request.json()
        for name in sorted(self.unsupported):
            if name in payload:
                self.calls.append(payload)
                return JSONResponse({'message': f"{name}: property '{name}' is unsupported", 'type': 'invalid_request_error'}, status_code=400)
        self.calls.append(payload)
        self.user_agents.append(request.headers.get('user-agent'))
        self.auth.append(request.headers.get('authorization'))
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.scripts:
                return self.scripts.pop(0)
            return {'choices': [{'message': {'role': 'assistant', 'content': 'Still thinking.'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
        finally:
            self.active -= 1


@pytest_asyncio.fixture
async def fake_openai():
    fake = FakeOpenAI()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(fake.app, log_level='error', lifespan='off'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(.01)
    fake.url = f'http://127.0.0.1:{sock.getsockname()[1]}/v1'
    yield fake
    server.should_exit = True
    await task
    sock.close()


@pytest_asyncio.fixture
async def fake_sftp(tmp_path):
    import asyncssh
    from types import SimpleNamespace
    root = tmp_path / 'sftp'
    root.mkdir()
    (root / 'homeassistant').mkdir()
    host_key = asyncssh.generate_private_key('ssh-ed25519')
    client_key = asyncssh.generate_private_key('ssh-ed25519')
    key_file = tmp_path / 'test_client_key'
    key_file.write_bytes(client_key.export_private_key())
    key_file.chmod(0o600)
    operations = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def public_key_auth_supported(self):
            return True

        def validate_public_key(self, username, key):
            return username == 'root' and key == client_key.convert_to_public()

    class SFTP(asyncssh.SFTPServer):
        def open(self, path, pflags, attrs):
            operations.append(('open', path.decode(), pflags))
            return super().open(path, pflags, attrs)

        def mkdir(self, path, attrs):
            operations.append(('mkdir', path.decode()))
            return super().mkdir(path, attrs)

        def posix_rename(self, oldpath, newpath):
            operations.append(('rename', oldpath.decode(), newpath.decode()))
            return super().posix_rename(oldpath, newpath)

        def remove(self, path):
            operations.append(('remove', path.decode()))
            return super().remove(path)

    server = await asyncssh.listen('127.0.0.1', 0, server_factory=Server, server_host_keys=[host_key],
                                  sftp_factory=lambda chan: SFTP(chan, chroot=str(root).encode()))
    fake = SimpleNamespace(root=root, key_file=key_file, host_key=host_key, port=server.get_port(), operations=operations,
                           path=root / 'homeassistant/custom_sentences/en/ar_tuner.yaml', server=server)
    yield fake
    server.close()
    await server.wait_closed()
    key_file.unlink(missing_ok=True)
