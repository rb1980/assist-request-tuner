import asyncio
import json
import logging
import re
import time
import httpx
from urllib.parse import urlsplit, urlunsplit
from websockets.asyncio.client import connect
from . import names

logger = logging.getLogger(names.LOGGER + ".ha")

ALLOWED = {
    'assist_pipeline/pipeline/list', 'assist_pipeline/pipeline_debug/list', 'assist_pipeline/pipeline_debug/get',
    'conversation/agent/homeassistant/debug', 'config/entity_registry/list_for_display',
    'config/entity_registry/list', 'config/entity_registry/get', 'config/entity_registry/update', 'config/area_registry/list',
    'config/area_registry/update', 'config/device_registry/list', 'homeassistant/expose_entity/list',
    'homeassistant/expose_entity',
}


def safe_message(settings, message):
    """Remove configured credentials from diagnostics before logging or raising."""
    text = str(message)
    for token in sorted(filter(None, (settings.ha_token, settings.token, settings.openrouter_api_key, getattr(settings, 'ai_ha_key', ''))), key=len, reverse=True):
        text = text.replace(token, '[redacted]')
    return text


def safe_url(settings):
    url = urlsplit(settings.ha_url)
    return safe_message(settings, urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path, '', '')))


class HAError(RuntimeError):
    pass


AUTH_RETRY_S = 300  # HA counts every rejected token as a failed login and can ban the IP, so retry slowly.


def rejected(settings):
    if settings.supervised:
        return "Home Assistant rejected the App's Supervisor token. Restart the Assist Request Tuner App."
    return ("Home Assistant rejected the access token Request Tuner uses: it was deleted, or belongs to another HA. Create a new long-lived "
            "access token (your HA profile, Security tab) and paste it on Request Tuner's Settings page.")


class HA:
    def __init__(self, settings):
        self.settings = settings
        self.ws = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.retry_at = 0
        self.backoff = 1
        self.last_error = None   # why the last connection failed, shown instead of a bare 'backoff'
        self.auth_failed = False
        self.entities, self.areas, self.devices = [], [], []
        self.cache_at = None   # None = never loaded; monotonic() starts near 0 after a reboot, so 0 isn't 'long ago'
        self.http = httpx.AsyncClient(base_url=settings.ha_url, headers={'Authorization': f'Bearer {settings.ha_token}'}, timeout=15, trust_env=False)

    async def reconfigure(self):
        """Use the (changed) URL and token: drop the connection and any wait, and connect again on the next call."""
        async with self.lock:
            if self.ws:
                await self.ws.close()
                self.ws = None
            await self.http.aclose()
            self.http = httpx.AsyncClient(base_url=self.settings.ha_url, headers={'Authorization': f'Bearer {self.settings.ha_token}'},
                                          timeout=15, trust_env=False)
            self.retry_at, self.backoff, self.auth_failed, self.last_error = 0, 1, False, None
            self.cache_at = None

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None
        await self.http.aclose()

    async def call(self, message_type, **fields):
        if message_type not in ALLOWED:
            raise ValueError('HA message type is forbidden')
        async with self.lock:
            if not self.settings.ha_token:
                raise HAError('HA is not configured')
            try:
                async with asyncio.timeout(15):
                    if self.ws is None:
                        if time.monotonic() < self.retry_at:
                            raise HAError(self.last_error or 'Home Assistant is unreachable; retrying shortly')
                        url = self.settings.websocket_url()
                        self.ws = await connect(url, proxy=None, max_size=16 * 1024 * 1024)
                        if json.loads(await self.ws.recv()).get('type') != 'auth_required':
                            raise HAError('Unexpected HA handshake')
                        await self.ws.send(json.dumps({'type': 'auth', 'access_token': self.settings.ha_token}))
                        if json.loads(await self.ws.recv()).get('type') != 'auth_ok':
                            self.auth_failed = True
                            raise HAError(rejected(self.settings))
                        self.auth_failed, self.last_error = False, None
                    self.sequence += 1
                    await self.ws.send(json.dumps(dict(fields, id=self.sequence, type=message_type)))
                    while True:
                        result = json.loads(await self.ws.recv())
                        if result.get('id') == self.sequence:
                            break
                    self.backoff = 1
                    if not result.get('success'):
                        error = result.get('error') or {}
                        raise HAError(safe_message(self.settings, f"HA rejected {message_type}: {error.get('code', 'unknown')}: {error.get('message', 'No error message')}"))
                    return result.get('result')
            except HAError as exc:
                if self.ws is None and time.monotonic() < self.retry_at:
                    raise   # still waiting to retry: no new attempt was made, so don't log or extend the wait
                await self.failed(message_type, str(exc))
            except Exception as exc:
                await self.failed(message_type, safe_message(self.settings, f'Home Assistant connection failed: {type(exc).__name__}: {exc}'))

    async def failed(self, what, detail):
        logger.warning('HA request %s failed: %s', what, detail)
        if self.ws:
            await self.ws.close()
            self.ws = None
        self.last_error = detail
        if self.auth_failed:
            self.retry_at = time.monotonic() + AUTH_RETRY_S
        else:
            self.retry_at = time.monotonic() + self.backoff
            self.backoff = min(self.backoff * 2, 60)
        raise HAError(detail) from None

    async def match(self, sentences):
        if not sentences:
            return []
        results = (await self.call('conversation/agent/homeassistant/debug', sentences=sentences, language='en'))['results']
        if len(results) != len(sentences):
            raise HAError('Incomplete matcher response')
        return results

    async def reload_sentences(self):
        return await self.rest('POST', service='conversation.reload')

    async def render_template(self, reply, entity):
        return await self.rest('POST', config={'template': reply, 'variables': {'entity': entity}}, template=True)

    async def ask(self, text):
        # HA's own agent only (never the LLM); callers must first check that nothing matching can change a device.
        result = await self.rest('POST', config={'text': text, 'language': 'en', 'agent_id': 'conversation.home_assistant'}, conversation=True)
        return ((result or {}).get('response', {}).get('speech', {}).get('plain', {}) or {}).get('speech')

    async def rest(self, method, automation_id=None, config=None, *, service=None, template=False, conversation=False):
        if not self.settings.ha_token:
            raise HAError('HA is not configured')
        if conversation:
            if method != 'POST' or automation_id is not None or service is not None or not isinstance(config, dict) or set(config) != {'text', 'language', 'agent_id'} or config['agent_id'] != 'conversation.home_assistant':
                raise ValueError('Invalid conversation operation')
            path = '/api/conversation/process'
        elif template:
            if method != 'POST' or automation_id is not None or service is not None or not isinstance(config, dict) or set(config) != {'template', 'variables'}:
                raise ValueError('Invalid template operation')
            path = '/api/template'
        elif service is not None:
            if service not in {'conversation.reload', 'automation.reload'} or method != 'POST' or automation_id is not None or config is not None:
                raise ValueError('HA service operation is forbidden')
            path = '/api/services/' + service.replace('.', '/')
        elif automation_id is None:
            if method != 'GET':
                raise ValueError('Only state reads allowed')
            path = '/api/states'
        else:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', automation_id) or method not in {'GET', 'POST', 'DELETE'}:
                raise ValueError('Invalid automation operation')
            path = '/api/config/automation/config/' + automation_id
        if self.auth_failed and time.monotonic() < self.retry_at:
            raise HAError(rejected(self.settings))
        try:
            response = await self.http.request(method, path, json=config) if config is not None else await self.http.request(method, path)
        except httpx.HTTPError as exc:
            detail = safe_message(self.settings, f'{method} {path}: {type(exc).__name__}: {exc}')
            logger.warning('HA REST request failed: %s', detail)
            raise HAError(detail) from None
        if response.status_code == 401:
            self.auth_failed, self.last_error = True, rejected(self.settings)
            self.retry_at = time.monotonic() + AUTH_RETRY_S
            logger.warning('HA REST %s %s: %s', method, path, self.last_error)
            raise HAError(self.last_error)
        if response.status_code == 404 and method == 'GET':
            return None
        if response.is_error:
            detail = safe_message(self.settings, f'HA REST rejected {method} {path}: HTTP {response.status_code}: {response.text}')
            logger.warning('HAError: %s', detail)
            raise HAError(detail)
        return response.text if template else response.json() if response.content else None

    async def refresh(self, force=False):
        if not force and self.cache_at is not None and time.monotonic() - self.cache_at < 300:
            return
        areas = await self.call('config/area_registry/list')
        devices = await self.call('config/device_registry/list')
        registry = await self.call('config/entity_registry/list')
        exposed = (await self.call('homeassistant/expose_entity/list'))['exposed_entities']
        entities = []
        device_map = {d['id']: d for d in devices}
        for full in registry:
            eid = full['entity_id']
            device = device_map.get(full.get('device_id'), {})
            entities.append(dict(entity_id=eid, name=full.get('name') or full.get('original_name') or device.get('name_by_user') or device.get('name') or eid,
                                 aliases=full.get('aliases', []), area=full.get('area_id') or device.get('area_id'),
                                 domain=eid.split('.')[0], exposed=exposed.get(eid, {}).get('conversation', False)))
        self.entities, self.areas, self.devices = entities, areas, devices
        self.cache_at = time.monotonic()

    async def automations(self):
        result = []
        for state in await self.rest('GET') or []:
            aid = state.get('attributes', {}).get('id')
            if state['entity_id'].startswith('automation.') and aid:
                config = await self.rest('GET', aid)
                triggers = (config or {}).get('triggers', (config or {}).get('trigger', []))
                triggers = [triggers] if isinstance(triggers, dict) else triggers
                sentences = []
                for t in triggers:
                    if t.get('trigger', t.get('platform')) == 'conversation':
                        commands = t.get('command', [])
                        sentences.extend([commands] if isinstance(commands, str) else commands)
                from .blueprint import instance
                inputs = instance(config)
                if inputs is not None:
                    sentences = inputs.get('sentences', [])
                if sentences:
                    result.append(dict(id=aid, alias=config.get('alias'), enabled=state['state'] == 'on', sentences=sentences,
                        **({'blueprint': 'voice_reply', 'label': 'blueprint: voice_reply', 'entity': inputs.get('entity')} if inputs is not None else {})))
        return result
