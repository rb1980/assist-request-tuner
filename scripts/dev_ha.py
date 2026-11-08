"""Dev Home Assistant helpers for scripts/dev-ha.sh (run with the dev venv: .venv/bin/python scripts/dev_ha.py ...).

  onboard                    create the dev owner if HA isn't onboarded yet (login saved to .data/dev-ha-login.txt)
  assistant <api_url> <model> an LLM voice assistant like a real home: the Local OpenAI LLM integration on any
                             OpenAI-compatible API, demo lights in rooms, and a "Dev Assist" pipeline that tries
                             Home Assistant's own intents first
  say "<text>" ...           send text through the preferred pipeline, like a satellite after speech-to-text

Talks to the dev Home Assistant through the loopback proxy at http://127.0.0.1:8125; DEV_HA_URL overrides this address.
"""
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import httpx
import websockets

NAME = os.environ.get('DEV_HA_NAME', 'ar-tuner-ha')
BASE = os.environ.get('DEV_HA_URL', 'http://127.0.0.1:8125')
LOGIN = Path(__file__).resolve().parent.parent / '.data' / 'dev-ha-login.txt'
CONFIG = '/mnt/supervisor/homeassistant'       # HA's config folder, inside the dev container
# The Local OpenAI LLM conversation integration (HACS), pinned so the setup is repeatable.
LOCAL_OPENAI = 'https://github.com/skye-harris/hass_local_openai_llm/releases/download/1.15.0/local_openai.zip'
PROMPT = """You are a voice assistant for a home. Reply in one or two short spoken sentences, plain text only.
A reply that ends with a question makes the device listen again, so never end with a question unless you must ask which room or which device.
Call a tool for every device action, right away, and never say something is done until the tool returned success.
For a question about a device's current state, call GetLiveContext and answer from it.
"""
ROOMS = {'light.bed_light': 'Bedroom', 'light.ceiling_lights': 'Living Room', 'light.kitchen_lights': 'Kitchen',
         'light.living_room_rgbww_lights': 'Living Room', 'light.office_rgbw_lights': 'Office',
         'light.entrance_color_white_lights': 'Entrance'}


def in_container(*command, **kw):
    return subprocess.run(['docker', 'exec', NAME, *command], check=True, text=True, capture_output=True, **kw).stdout


class DevHA:
    async def __aenter__(self):
        self.http = httpx.AsyncClient(base_url=BASE, timeout=120)
        login = dict(line.split(': ', 1) for line in LOGIN.read_text().splitlines())
        token = (await self.http.post('/auth/token', data=dict(grant_type='refresh_token', refresh_token=login['refresh_token'],
                                                               client_id=BASE + '/'))).json()['access_token']
        self.http.headers['Authorization'] = 'Bearer ' + token
        self.ws = await websockets.connect(BASE.replace('http', 'ws', 1) + '/api/websocket', max_size=None)
        await self.ws.recv()
        await self.ws.send(json.dumps({'type': 'auth', 'access_token': token}))
        await self.ws.recv()
        self.n = 0
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()
        await self.http.aclose()

    async def call(self, **message):
        self.n += 1
        await self.ws.send(json.dumps({'id': self.n, **message}))
        while True:
            reply = json.loads(await self.ws.recv())
            if reply.get('id') == self.n and reply.get('type') == 'result':
                if not reply['success']:
                    raise RuntimeError(f"{message['type']}: {reply['error']}")
                return reply['result']

    async def flow(self, path, handler, *steps):
        # Run a config flow, sending one dict per form step; returns the final result.
        result = (await self.http.post(path, json={'handler': handler})).json()
        for data in steps:
            if result.get('type') != 'form':
                break
            result = (await self.http.post(f"{path}/{result['flow_id']}", json=data)).json()
        if result.get('type') != 'create_entry':
            raise RuntimeError(f'{handler}: {result}')
        return result


async def onboard():
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        for _ in range(60):   # just after (re)starting the Supervisor, HA may still be starting
            try:
                steps = (await http.get('/api/onboarding')).json()
                break
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(3)
        else:
            raise RuntimeError('Home Assistant did not answer')
        if next(s for s in steps if s['step'] == 'user')['done']:
            print('Already onboarded' + ('' if LOGIN.exists() else f' (by hand: no saved login in {LOGIN})'))
            return
        client = BASE + '/'
        password = secrets.token_urlsafe(12)
        code = (await http.post('/api/onboarding/users', json=dict(client_id=client, name='Dev Owner', username='dev',
                                                                    password=password, language='en'))).json()['auth_code']
        token = (await http.post('/auth/token', data=dict(grant_type='authorization_code', code=code, client_id=client))).json()
        http.headers['Authorization'] = 'Bearer ' + token['access_token']
        for step, body in (('core_config', {}), ('analytics', {}), ('integration', dict(client_id=client, redirect_uri=client))):
            (await http.post(f'/api/onboarding/{step}', json=body)).raise_for_status()
        LOGIN.parent.mkdir(exist_ok=True)
        LOGIN.touch(mode=0o600)
        LOGIN.write_text(f'url: {BASE}\nusername: dev\npassword: {password}\nrefresh_token: {token["refresh_token"]}\n')
        print(f'Onboarded: user "dev", password in {LOGIN}')


async def assistant(api_url, model):
    # 1. The integration and demo devices (files in HA's config folder; HA restarts once to load them).
    if not _exists(f'{CONFIG}/custom_components/local_openai/manifest.json'):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'local_openai.zip'
            urllib.request.urlretrieve(LOCAL_OPENAI, archive)
            zipfile.ZipFile(archive).extractall(Path(tmp) / 'local_openai')
            in_container('mkdir', '-p', f'{CONFIG}/custom_components')
            subprocess.run(['docker', 'cp', f'{tmp}/local_openai', f'{NAME}:{CONFIG}/custom_components/'], check=True)
        restart = True
    else:
        restart = False
    if 'demo:' not in in_container('cat', f'{CONFIG}/configuration.yaml'):
        in_container('sh', '-c', f'printf "\\n# Dev: demo devices (lights, media players, climate...) to talk to.\\ndemo:\\n" >> {CONFIG}/configuration.yaml')
        restart = True
    if restart:
        print('Restarting Home Assistant to load the integration and demo devices')
        in_container('docker', 'exec', 'hassio_cli', 'ha', 'core', 'restart')
        await _wait_for_core()
    async with DevHA() as ha:
        # 2. Rooms for the demo lights (on their devices: the entities have no names of their own).
        areas = {a['name']: a['area_id'] for a in await ha.call(type='config/area_registry/list')}
        for room in set(ROOMS.values()) - set(areas):
            areas[room] = (await ha.call(type='config/area_registry/create', name=room))['area_id']
        entities = {e['entity_id']: e for e in await ha.call(type='config/entity_registry/list')}
        for entity, room in ROOMS.items():
            if entity in entities:
                await ha.call(type='config/device_registry/update', device_id=entities[entity]['device_id'], area_id=areas[room])
        # 3. The LLM server and its conversation agent.
        entries = (await ha.http.get('/api/config/config_entries/entry', params={'domain': 'local_openai'})).json()
        entry = next((e for e in entries if e['title'] == 'Dev LLM'), None)
        if entry is None:
            entry = (await ha.flow('/api/config/config_entries/flow', 'local_openai',
                                   dict(server_name='Dev LLM', base_url=api_url, server_type='generic')))['result']
        before = set(_agents((await ha.http.get('/api/states')).json()))
        subentries = await ha.call(type='config_entries/subentries/list', entry_id=entry['entry_id'])
        created = not any(model in s.get('title', '') for s in subentries)
        if created:
            await ha.flow('/api/config/config_entries/subentries/flow', [entry['entry_id'], 'conversation'],
                          dict(model=model, prompt=PROMPT, llm_hass_api=['assist'], parallel_tool_calls=True, strip_emojis=True,
                               always_continue_conversation=False, temperature=0.2, chat_template_opts={}, request_body_opts={}))
        for _ in range(30):
            agents = _agents((await ha.http.get('/api/states')).json())
            # A new agent's entity appears a moment after its subentry; an existing one is found by entry and model.
            ours = [a for a in agents if _slug(entry['title']) in a and _slug(model) in a]
            agent = next((a for a in agents if a not in before), None) if created else next(iter(ours), None)
            if agent:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f'No conversation agent appeared for {model}')
        # 4. The pipeline: Home Assistant's own intents first, then the LLM (as in a real LLM home).
        pipelines = (await ha.call(type='assist_pipeline/pipeline/list'))['pipelines']
        existing = next((p for p in pipelines if p['name'] == 'Dev Assist'), None)
        body = dict(name='Dev Assist', language='en', conversation_engine=agent, conversation_language='en', prefer_local_intents=True,
                    stt_engine=None, stt_language=None, tts_engine=None, tts_language=None, tts_voice=None,
                    wake_word_entity=None, wake_word_id=None)
        if existing:
            pipeline = await ha.call(type='assist_pipeline/pipeline/update', pipeline_id=existing['id'], **body)
        else:
            pipeline = await ha.call(type='assist_pipeline/pipeline/create', **body)
        await ha.call(type='assist_pipeline/pipeline/set_preferred', pipeline_id=pipeline['id'])
        print(f'Dev Assist (preferred pipeline): local intents first, then {agent} ({model} at {api_url})')


async def say(phrases):
    async with DevHA() as ha:
        for text in phrases:
            started = time.monotonic()
            ha.n += 1
            await ha.ws.send(json.dumps({'id': ha.n, 'type': 'assist_pipeline/run', 'start_stage': 'intent', 'end_stage': 'intent',
                                         'input': {'text': text}}))
            outcome = '?'
            while True:
                message = json.loads(await ha.ws.recv())
                if message.get('id') != ha.n or message.get('type') != 'event':
                    continue
                event = message['event']
                if event['type'] == 'intent-end':
                    speech = event['data']['intent_output']['response']['speech'].get('plain', {}).get('speech', '')
                    outcome = f"{'HA ' if event['data'].get('processed_locally') else 'LLM'} {speech!r}"
                elif event['type'] == 'error':
                    outcome = f"error {event['data']}"
                if event['type'] in ('run-end', 'error'):
                    break
            print(f'{time.monotonic() - started:5.1f}s  {text!r:45} {outcome}')


def _exists(path):
    return subprocess.run(['docker', 'exec', NAME, 'test', '-e', path]).returncode == 0


def _agents(states):
    return [s['entity_id'] for s in states if s['entity_id'].startswith('conversation.') and s['entity_id'] != 'conversation.home_assistant']


def _slug(model):
    return ''.join(c if c.isalnum() else '_' for c in model.lower())


async def _wait_for_core():
    async with httpx.AsyncClient(base_url=BASE, timeout=5) as http:
        await asyncio.sleep(10)
        for _ in range(60):
            try:
                if (await http.get('/manifest.json')).status_code == 200:
                    await asyncio.sleep(5)   # integrations finish loading just after the frontend answers
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(3)
    raise RuntimeError('Home Assistant did not come back after the restart')


if __name__ == '__main__':
    command, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ('', [])
    if command == 'onboard' and not args:
        asyncio.run(onboard())
    elif command == 'assistant' and len(args) == 2:
        asyncio.run(assistant(*args))
    elif command == 'say' and args:
        asyncio.run(say(args))
    else:
        sys.exit(__doc__)
