"""The Settings page: which settings can be edited in the UI, saving them, and applying them without a restart."""
import json
import os
from dataclasses import fields, replace
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import httpx
from . import config, ha_llm, llm
from .config import Settings

# (env name, label, kind, help). Kinds: text, url, secret, int, float, bool, list, choice:<a>|<b>.
SECTIONS = [
    ('Home Assistant', 'ha', [
        ('HA_URL', 'Home Assistant URL', 'url', 'For example http://homeassistant.local:8123 or http://192.168.1.10:8123.'),
        ('HA_TOKEN', 'Access token', 'secret', "A long-lived access token from an admin user's profile (Security tab)."),
    ]),
    ('Writing to the HA config folder', 'writer', [
        ('HA_CONFIG_ACCESS', 'Access', 'choice:sftp|local|none',
         'sftp: through the Advanced SSH & Web Terminal App. local: the folder is mounted at /homeassistant. none: those fixes stay drafts.'),
        ('HA_SSH_HOST', 'SSH host', 'text', 'Leave empty to use the host from the Home Assistant URL.'),
        ('HA_SSH_PORT', 'SSH port', 'int', ''),
        ('HA_SSH_USER', 'SSH user', 'text', ''),
        ('HA_SSH_HOST_KEY', 'Pinned SSH host key', 'text',
         'Optional, e.g. "ssh-ed25519 AAAA…". Empty: Request Tuner trusts the key it first sees at each address.'),
    ]),
    ('AI analysis', 'ai', [
        ('AI_SOURCE', 'AI server', 'choice:ha|custom',
         "Home Assistant: use the server and model of a voice agent you already set up there with the Local OpenAI LLM "
         "integration (HACS); change them in Home Assistant. Custom: any API, below."),
        ('AI_HA_SERVER', 'Agent in Home Assistant', 'haserver', ''),
        ('LLM_BASE_URL', 'API URL', 'url', 'Any OpenAI-compatible chat API with tool calling, for example https://openrouter.ai/api/v1, '
         'https://api.openai.com/v1, https://api.cerebras.ai/v1 or a local http://host:11434/v1.'),
        ('LLM_API_KEY', 'API key', 'secret', ''),
        ('DIAGNOSE_MODEL', 'Model', 'text', 'Press Test to list the models this API offers, then choose one. Test checks it calls tools.'),
        ('DIAGNOSE_REASONING', 'Reasoning effort', 'choice:medium|low|high|default', '"default" sends no reasoning setting. Ignored by models without one.'),
        ('DIAGNOSE_PRICE_IN', 'Price per million input tokens (USD)', 'float', 'For the daily budget. Filled in by Test when the API publishes prices.'),
        ('DIAGNOSE_PRICE_OUT', 'Price per million output tokens (USD)', 'float', ''),
        ('DIAGNOSE_DAILY_USD', 'Daily budget (USD)', 'float', 'Estimated spend per day, after which AI analysis pauses. 0 means no limit (for example a local model).'),
        ('NIGHTLY_DIAGNOSE', 'Analyse the top five requests every night', 'bool', ''),
    ]),
    ('Collecting requests', 'collector', [
        ('IGNORE_PIPELINES', 'Pipelines to ignore', 'list', 'Names or IDs, comma-separated (for example a test pipeline).'),
        ('EXCLUDE_DEVICE_IDS', 'Devices to exclude', 'list', 'Device IDs, comma-separated.'),
        ('RETENTION_DAYS', 'Keep requests for (days)', 'int', ''),
        ('TZ', 'Time zone', 'text', 'For example America/Los_Angeles or Europe/Berlin.'),
    ]),
]
# With AI server "Home Assistant", these come from HA (its agent's model; no reasoning setting, prices or budget).
FROM_HA = ('LLM_BASE_URL', 'LLM_API_KEY', 'DIAGNOSE_MODEL', 'DIAGNOSE_REASONING', 'DIAGNOSE_PRICE_IN', 'DIAGNOSE_PRICE_OUT',
           'DIAGNOSE_DAILY_USD')
HA_SHOWS = {'DIAGNOSE_REASONING': 'default', 'DIAGNOSE_PRICE_IN': '0', 'DIAGNOSE_PRICE_OUT': '0', 'DIAGNOSE_DAILY_USD': '0'}
# Words for choice values, where the value itself isn't clear.
CHOICE_LABELS = {'AI_SOURCE': {'ha': "Home Assistant's Local OpenAI LLM", 'custom': 'Custom'}}
FIELDS = {name: dict(label=label, kind=kind, help=help, section=key) for _, key, rows in SECTIONS for name, label, kind, help in rows}
SOURCE_WORDS = {'saved': 'Saved here', 'env': 'Environment variable', 'options': "App's Configuration tab",
                'file': 'Config file', 'default': 'Default'}


def current(settings):
    """Values and sources for the form. Secrets are reported as set or not, never returned."""
    values = {'HA_URL': settings.ha_url, 'HA_CONFIG_ACCESS': settings.ha_config_access, 'HA_SSH_HOST': config.text('HA_SSH_HOST'),
              'HA_SSH_PORT': settings.ha_ssh_port, 'HA_SSH_USER': settings.ha_ssh_user, 'HA_SSH_HOST_KEY': settings.ha_ssh_host_key,
              'AI_SOURCE': settings.ai_source, 'AI_HA_SERVER': settings.ai_ha_server, 'LLM_BASE_URL': settings.llm_base_url, 'DIAGNOSE_MODEL': settings.diagnose_model,
              'DIAGNOSE_REASONING': settings.diagnose_reasoning, 'DIAGNOSE_DAILY_USD': settings.diagnose_daily_usd,
              'DIAGNOSE_PRICE_IN': settings.diagnose_price_in, 'DIAGNOSE_PRICE_OUT': settings.diagnose_price_out,
              'NIGHTLY_DIAGNOSE': settings.nightly_diagnose, 'IGNORE_PIPELINES': ', '.join(settings.ignore_pipelines),
              'EXCLUDE_DEVICE_IDS': ', '.join(settings.exclude_devices), 'RETENTION_DAYS': settings.retention, 'TZ': settings.timezone}
    secrets = {'HA_TOKEN': bool(settings.ha_token), 'LLM_API_KEY': bool(settings.openrouter_api_key)}
    return {name: dict(field, name=name, value=values.get(name, ''), is_set=secrets.get(name), source=SOURCE_WORDS[config.source(name)])
            for name, field in FIELDS.items()}


def parse(form, supervised=False):
    """The form's changes as {name: value or None}. None removes the saved value, so the file/env/default applies again."""
    changes, errors = {}, []
    for name, field in FIELDS.items():
        if supervised and field['section'] in ('ha', 'writer'):
            continue   # the Supervisor provides these in an App
        if form.get('AI_SOURCE') == 'ha' and name in FROM_HA:
            continue   # greyed out (so not sent): Home Assistant provides them; keep the Custom values for later
        kind = field['kind']
        if kind == 'secret':
            value = (form.get(name) or '').strip()
            if form.get(name + '__clear'):
                changes[name] = None
            elif value:
                changes[name] = value
            continue   # empty: keep the current secret
        if kind == 'bool':
            changes[name] = 'true' if form.get(name) else 'false'
            continue
        value = (form.get(name) or '').strip()
        if not value:
            changes[name] = None
            continue
        try:
            if kind == 'url':
                parts = urlsplit(value)
                if parts.scheme not in ('http', 'https') or not parts.netloc:
                    raise ValueError('must start with http:// or https://')
                value = value.rstrip('/')
            elif kind == 'int':
                if int(value) < (0 if name == 'RETENTION_DAYS' else 1) or int(value) > 65535 * 100:
                    raise ValueError('is out of range')
                value = str(int(value))
            elif kind == 'float':
                if float(value) < 0:
                    raise ValueError('must not be negative')
                value = str(float(value))
            elif kind == 'haserver':
                value = value[:100]   # a config entry id from HA's list; checked against HA when used
            elif kind.startswith('choice:'):
                if value not in kind[7:].split('|'):
                    raise ValueError('is not one of the choices')
            elif kind == 'list':
                value = ', '.join(x.strip() for x in value.split(',') if x.strip())
            elif name == 'TZ':
                ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            errors.append(f"{field['label']}: {'is not a valid time zone' if isinstance(exc, ZoneInfoNotFoundError) or name == 'TZ' else exc}")
            continue
        changes[name] = value
    return changes, errors


def shown(settings, name):
    """A field's current value as the form shows it, for spotting fields the user didn't touch."""
    value = current(settings)[name]['value']
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if FIELDS[name]['kind'] == 'float':
        return str(float(value))
    return '' if value is None else str(value)


def changed_only(changes, settings):
    """Drop fields that still show the value from a lower source, so a save pins only what the user changed."""
    settings = Settings(token=settings.token, db_path=settings.db_path)   # as the sources say now
    return {name: value for name, value in changes.items()
            if config.source(name) == 'saved' or FIELDS[name]['kind'] == 'secret' or (value or '') != shown(settings, name)}


def save(changes):
    """Merge changes into the saved-settings file (mode 600, replaced atomically)."""
    path = Path(config.saved_path())
    data = dict(config.saved())
    for name, value in changes.items():
        if name not in FIELDS:
            raise ValueError('Unknown setting')
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    temp = path.with_name(path.name + '.tmp')
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
    os.replace(temp, path)


# Saved AI setups: named copies of the AI settings, to switch between providers and models. Kept next to the saved
# settings (mode 600), because they include API keys.
PRESET_FIELDS = ('AI_SOURCE', 'AI_HA_SERVER', 'LLM_BASE_URL', 'LLM_API_KEY', 'DIAGNOSE_MODEL', 'DIAGNOSE_REASONING', 'DIAGNOSE_PRICE_IN', 'DIAGNOSE_PRICE_OUT',
                 'DIAGNOSE_DAILY_USD')


def presets_path():
    return Path(config.saved_path()).with_name('ai_setups.json')


def presets():
    try:
        data = json.loads(presets_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_presets(data):
    path = presets_path()
    temp = path.with_name(path.name + '.tmp')
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
    os.replace(temp, path)


def preset_values(trial):
    return {'AI_SOURCE': trial.ai_source, 'AI_HA_SERVER': trial.ai_ha_server or '', 'LLM_BASE_URL': trial.llm_base_url.rstrip('/'), 'LLM_API_KEY': trial.openrouter_api_key or '',
            'DIAGNOSE_MODEL': trial.diagnose_model, 'DIAGNOSE_REASONING': trial.diagnose_reasoning,
            'DIAGNOSE_PRICE_IN': str(float(trial.diagnose_price_in)), 'DIAGNOSE_PRICE_OUT': str(float(trial.diagnose_price_out)),
            'DIAGNOSE_DAILY_USD': str(float(trial.diagnose_daily_usd))}


def preset_name(name):
    name = ' '.join((name or '').split())
    if not 1 <= len(name) <= 60:
        raise ValueError('Give the AI setup a name of 1 to 60 characters')
    return name


def save_preset(name, trial):
    data = presets()
    data[preset_name(name)] = preset_values(trial)
    _write_presets(data)


def delete_preset(name):
    data = presets()
    if data.pop(name, None) is None:
        raise KeyError('No saved AI setup with that name')
    _write_presets(data)


def use_preset(name, settings):
    """Make a saved setup the current AI settings (saved on this page, so they win over the other sources)."""
    values = presets().get(name)
    if values is None:
        raise KeyError('No saved AI setup with that name')
    save(changed_only({k: (v or None) for k, v in values.items() if k in PRESET_FIELDS}, settings))


def presets_view(settings):
    """Saved setups for the page, without their keys, and which one matches the current settings."""
    now = preset_values(settings)
    return [dict(name=name, model=values.get('DIAGNOSE_MODEL', ''),
                 host='Home Assistant' if values.get('AI_SOURCE') == 'ha' else urlsplit(values.get('LLM_BASE_URL', '')).netloc,
                 active=all(values.get(k, '' if k in ('AI_SOURCE', 'AI_HA_SERVER') else None) == now[k] or
                            (k == 'AI_SOURCE' and values.get(k) is None and now[k] == 'custom') for k in PRESET_FIELDS))
            for name, values in sorted(presets().items(), key=lambda item: item[0].lower())]


async def check_ha(url, token):
    """Try Home Assistant with the given URL and token. Returns (ok, message)."""
    if not token:
        return False, 'No access token is set.'
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.get(url.rstrip('/') + '/api/', headers={'Authorization': f'Bearer {token}'})
    except httpx.HTTPError as exc:
        return False, f"Couldn't reach Home Assistant at {url} ({type(exc).__name__})."
    if response.status_code == 401:
        return False, 'Home Assistant rejected the access token.'
    if response.is_error:
        return False, f'Home Assistant answered HTTP {response.status_code}.'
    return True, 'Connected to Home Assistant.'


async def apply(settings, service):
    """Reload settings from every source into the shared object, then reconnect what depends on them."""
    fresh = Settings()
    for item in fields(Settings):
        if item.name not in ('db_path', 'token'):   # the database and login token only change on restart
            setattr(settings, item.name, getattr(fresh, item.name))
    await service.ha.reconfigure()
    service.changes.reconfigure_writer()
    await ha_llm.resolve(settings, service.changes.sentences.writer, force=True)


# Setting name -> Settings attribute, for testing values typed in the form before they are saved.
ATTRS = {'HA_URL': 'ha_url', 'HA_TOKEN': 'ha_token', 'HA_CONFIG_ACCESS': 'ha_config_access', 'HA_SSH_HOST': 'ha_ssh_host',
         'HA_SSH_PORT': 'ha_ssh_port', 'HA_SSH_USER': 'ha_ssh_user', 'HA_SSH_HOST_KEY': 'ha_ssh_host_key', 'LLM_BASE_URL': 'llm_base_url', 'AI_SOURCE': 'ai_source', 'AI_HA_SERVER': 'ai_ha_server',
         'LLM_API_KEY': 'openrouter_api_key', 'DIAGNOSE_MODEL': 'diagnose_model', 'DIAGNOSE_REASONING': 'diagnose_reasoning',
         'DIAGNOSE_DAILY_USD': 'diagnose_daily_usd', 'DIAGNOSE_PRICE_IN': 'diagnose_price_in', 'DIAGNOSE_PRICE_OUT': 'diagnose_price_out', 'NIGHTLY_DIAGNOSE': 'nightly_diagnose', 'IGNORE_PIPELINES': 'ignore_pipelines',
         'EXCLUDE_DEVICE_IDS': 'exclude_devices', 'RETENTION_DAYS': 'retention', 'TZ': 'timezone'}


def candidate(form, settings):
    """A copy of the settings with the form's values, or (None, errors) if they don't parse."""
    changes, errors = parse(form, settings.supervised)
    if errors:
        return None, errors
    trial = replace(settings)
    for name, value in changes.items():
        if value is None:
            if name == 'HA_SSH_HOST':
                trial.ha_ssh_host = urlsplit(trial.ha_url).hostname or trial.ha_ssh_host
            continue   # emptied: keep the value in effect
        kind = FIELDS[name]['kind']
        setattr(trial, ATTRS[name], int(value) if kind == 'int' else float(value) if kind == 'float' else value == 'true' if kind == 'bool'
                else [x.strip() for x in value.split(',') if x.strip()] if kind == 'list' else value)
    if changes.get('HA_URL') and not changes.get('HA_SSH_HOST'):
        trial.ha_ssh_host = urlsplit(trial.ha_url).hostname or trial.ha_ssh_host
    return trial, []


def short(text, limit=200):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit] + '…'


async def test_ha(trial, service):
    ok, message = await check_ha(trial.ha_url, trial.ha_token)
    if not ok:
        return ok, message
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            info = (await client.get(trial.ha_url.rstrip('/') + '/api/config', headers={'Authorization': f'Bearer {trial.ha_token}'})).json()
        return True, f"Connected to Home Assistant {info.get('version', '')} ({info.get('location_name', 'unnamed')})."
    except (httpx.HTTPError, ValueError):
        return True, message


async def test_writer(trial, service):
    from .local_writer import make_writer
    if trial.ha_config_access == 'none':
        return True, 'Access is "none": custom sentences and spoken replies will be drafts only. Nothing to test.'
    writer = make_writer(service.db, trial)
    state = writer.configuration()
    if not state['configured']:
        return False, state['state']
    try:
        text = await writer.read()
    except Exception as exc:   # HAError, ValueError, OSError: report, never raise
        return False, short(exc)
    where = 'over SFTP' if trial.ha_config_access == 'sftp' else 'in the mounted folder'
    if text is None:
        return True, f"Connected {where}. Request Tuner's sentence file doesn't exist yet; it is created by the first fix."
    return True, f"Connected {where} and read Request Tuner's sentence file ({len(text.encode())} bytes)."


async def test_ai(trial, service):
    """Custom: lists the provider's models, then checks the chosen one with a real tool-calling request. Home Assistant:
    checks the server and model HA's agent uses, as they are. What the provider accepts is remembered for this URL and
    model (see llm.py)."""
    from .diagnose import MODEL_TOOLS
    if trial.ai_source == 'ha':
        await ha_llm.resolve(trial, service.changes.sentences.writer, force=True)
        if off := llm.unavailable(trial):
            return dict(ok=False, message=off)
        client = llm.Client(trial, service.db)
        using = f'Using “{trial.ai_ha_title}” from Home Assistant: {trial.ai_ha_model} at {trial.ai_ha_url}.'
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
                ok, notes = await client.probe(http, MODEL_TOOLS)
        except httpx.HTTPError as exc:
            return dict(ok=False, message=using + f" Couldn't reach it ({type(exc).__name__}).")
        return dict(ok=ok, message=' '.join([using] + notes))
    if off := llm.unavailable(trial):
        return dict(ok=False, message=off)
    client = llm.Client(trial, service.db)
    models, listed = [], ''
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
            try:
                models = await client.models(http)
                usable = [m for m in models if m['tools'] is not False]
                hidden = len(models) - len(usable)
                models = usable
                listed = f"{len(models)} model{'s' * (len(models) != 1)} available" + (f' ({hidden} without tool calling not shown)' if hidden else '') + '.'
            except (httpx.HTTPError, ValueError) as exc:
                listed = "This API doesn't list its models" + (f' ({short(exc)})' if isinstance(exc, ValueError) else '') + '; type the model name.'
            if not trial.diagnose_model:
                return dict(ok=bool(models), message=listed + ' Choose a model, then press Test again.', models=models)
            if models and trial.diagnose_model not in {m['id'] for m in models}:
                return dict(ok=False, message=listed + f' “{trial.diagnose_model}” isn\'t one of them: choose one from the list.', models=models)
            ok, notes = await client.probe(http, MODEL_TOOLS)
    except httpx.HTTPError as exc:
        return dict(ok=False, message=f"Couldn't reach {client.base} ({type(exc).__name__}).", models=models)
    chosen = next((m for m in models if m['id'] == trial.diagnose_model), {})
    return dict(ok=ok, message=' '.join(([listed] if listed else []) + notes), models=models,
                price_in=chosen.get('price_in'), price_out=chosen.get('price_out'))


async def test_collector(trial, service):
    notes, ok = [f'Time zone {trial.timezone} is valid.'], True
    try:
        pipelines = await service.ha.call('assist_pipeline/pipeline/list')
        await service.ha.refresh()
    except Exception as exc:
        return False, notes[0] + " Couldn't check pipelines and devices: " + short(exc)
    known = {p['id'] for p in pipelines['pipelines']} | {p['name'] for p in pipelines['pipelines']}
    missing = [p for p in trial.ignore_pipelines if p not in known]
    watched = [p['name'] for p in pipelines['pipelines'] if p['id'] not in trial.ignore_pipelines and p['name'] not in trial.ignore_pipelines]
    if missing:
        ok = False
        notes.append('Not found in Home Assistant: ' + ', '.join(f'“{p}”' for p in missing) + '.')
    devices = {d['id'] for d in service.ha.devices}
    unknown = [d for d in trial.exclude_devices if d not in devices]
    if unknown:
        ok = False
        notes.append('Unknown device IDs: ' + ', '.join(unknown) + '.')
    notes.append(f"Watching {len(watched)} pipeline{'s' * (len(watched) != 1)}: " + ', '.join(watched) + '.')
    return ok, ' '.join(notes)


TESTS = {'ha': test_ha, 'writer': test_writer, 'ai': test_ai, 'collector': test_collector}


async def test(section, form, settings, service):
    if section not in TESTS:
        raise ValueError('Unknown section')
    trial, errors = candidate(form, settings)
    if errors:
        return dict(ok=False, message='Fix these first: ' + '; '.join(errors))
    result = await TESTS[section](trial, service)
    if isinstance(result, dict):
        return result
    ok, message = result
    return dict(ok=ok, message=message)
