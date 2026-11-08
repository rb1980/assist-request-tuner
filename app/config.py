"""Settings: built-in defaults < config file (YAML) < Home Assistant App options < environment variables < saved in the UI.

The config file is AR_TUNER_CONFIG (default /data/config.yaml); see config.example.yaml for every key. Each setting also
has an environment variable (the names below), and as a Home Assistant App the options page writes /data/options.json
using the same names in lower case. Values saved on the Settings page (AR_TUNER_SETTINGS, default /data/settings.json,
same names) win over everything, so the files only provide the starting values. Inside an App, HA's URL, token and config directory come from the Supervisor.
"""
import json
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
import yaml
from . import names

KEYWORDS = 'light,lamp,fan,thermostat,ac,air,heat,temperature,humidity,door,lock,garage,tv,music,speaker,scene,inside,outside,room'
# Stock openWakeWord wake words (in case speech-to-text keeps them) and polite fillers.
PREFIXES = 'hey jarvis,ok nabu,hey mycroft,alexa,please,can you,could you,would you,i want you to'
SUPERVISOR_URL = 'http://supervisor/core'

# Environment variable -> path in the YAML config file.
KEYS = {
    'HA_URL': 'home_assistant.url', 'HA_TOKEN': 'home_assistant.token', 'HA_WS_URL': 'home_assistant.websocket_url',
    'HA_CONFIG_ACCESS': 'home_assistant.config_access', 'HA_CONFIG_DIR': 'home_assistant.config_dir',
    'HA_SSH_HOST': 'home_assistant.ssh.host', 'HA_SSH_PORT': 'home_assistant.ssh.port', 'HA_SSH_USER': 'home_assistant.ssh.user',
    'HA_SSH_KEY_FILE': 'home_assistant.ssh.key_file', 'HA_SSH_HOST_KEY': 'home_assistant.ssh.host_key',
    'AR_TUNER_TOKEN': 'ar_tuner.token', 'DB_PATH': 'ar_tuner.db_path', 'TZ': 'ar_tuner.timezone',
    'POLL_INTERVAL_S': 'collector.poll_interval_s', 'IGNORE_PIPELINES': 'collector.ignore_pipelines',
    'EXCLUDE_DEVICE_IDS': 'collector.exclude_device_ids', 'HOME_KEYWORDS': 'collector.home_keywords',
    'NORMALIZE_PREFIXES': 'collector.normalize_prefixes', 'RETENTION_DAYS': 'collector.retention_days',
    'STORE_RAW_EVENTS': 'collector.store_raw_events', 'READ_ONLY_SERVICES': 'changes.read_only_services',
    'LLM_BASE_URL': 'diagnose.base_url', 'LLM_API_KEY': 'diagnose.api_key', 'DIAGNOSE_MODEL': 'diagnose.model',
    'DIAGNOSE_PROVIDER': 'diagnose.provider', 'DIAGNOSE_MAX_STEPS': 'diagnose.max_steps', 'DIAGNOSE_TIMEOUT_S': 'diagnose.timeout_s',
    'DIAGNOSE_REASONING': 'diagnose.reasoning', 'DIAGNOSE_DAILY_USD': 'diagnose.daily_usd', 'DIAGNOSE_PRICE_IN': 'diagnose.price_in',
    'DIAGNOSE_PRICE_OUT': 'diagnose.price_out', 'NIGHTLY_DIAGNOSE': 'diagnose.nightly',
    'AI_SOURCE': 'diagnose.source', 'AI_HA_SERVER': 'diagnose.ha_server',
}
ALIASES = {'OPENROUTER_API_KEY': 'LLM_API_KEY'}  # the OpenRouter-only name, also accepted


def _config_value(name):
    return _dig(_file(names.env('CONFIG') or '/data/config.yaml'), KEYS[name])


def _options():
    return _file(names.env('OPTIONS') or '/data/options.json')


@lru_cache(maxsize=8)
def _read(path, mtime):
    if path.endswith('.json'):
        return json.loads(Path(path).read_text() or '{}')
    return yaml.safe_load(Path(path).read_text()) or {}


def _file(path):
    try:
        info = Path(path).stat()
        return _read(path, (info.st_mtime_ns, info.st_size))
    except FileNotFoundError:
        return {}


def _dig(data, dotted):
    for part in dotted.split('.'):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def supervised():
    """Running as a Home Assistant App (the Supervisor provides a token)."""
    return bool(os.getenv('SUPERVISOR_TOKEN'))


def saved_path():
    return names.env('SETTINGS') or '/data/settings.json'


def saved():
    """Settings saved from the web UI (env-style names)."""
    return _file(saved_path())


def source(name):
    """Where a setting's current value comes from: saved, env, options, file or default."""
    if saved().get(name) not in (None, ''):
        return 'saved'
    if any(os.getenv(env) not in (None, '') for env in [name] + [old for old, new in ALIASES.items() if new == name]):
        return 'env'
    options = _options()
    if any(options.get(k) not in (None, '') for k in [name.lower()] + [old.lower() for old, new in ALIASES.items() if new == name]):
        return 'options'
    if _config_value(name) not in (None, ''):
        return 'file'
    return 'default'


def raw(name, default=None):
    """One setting's value from the highest-priority source that has it."""
    if saved().get(name) not in (None, ''):
        return saved()[name]
    for env in [name] + [old for old, new in ALIASES.items() if new == name]:
        if os.getenv(env) not in (None, ''):
            return os.environ[env]
    options = _options()
    for key in [name.lower()] + [old.lower() for old, new in ALIASES.items() if new == name]:
        if options.get(key) not in (None, ''):
            return options[key]
    value = _config_value(name)
    return default if value in (None, '') else value


def text(name, default=''):
    return str(raw(name, default))


def items(name, default=''):
    value = raw(name, default)
    return [str(x).strip() for x in (value if isinstance(value, list) else str(value).split(',')) if str(x).strip()]


def flag(name, default):
    value = raw(name, default)
    return value if isinstance(value, bool) else str(value).lower() in ('true', '1', 'yes', 'on')


def mapping(name, default):
    value = raw(name)
    return value if isinstance(value, dict) else json.loads(value) if value else default


# Kept for callers that read a comma-separated environment variable directly.
def csv(name, default=''):
    return items(name, default)


def ha_url():
    return text('HA_URL', SUPERVISOR_URL if supervised() and not raw('HA_TOKEN') else 'http://homeassistant.local:8123')


def ha_token():
    return text('HA_TOKEN', os.getenv('SUPERVISOR_TOKEN', '') if supervised() else '')


def ssh_host():
    return text('HA_SSH_HOST', urlsplit(ha_url()).hostname or 'homeassistant.local')


def config_access():
    # local: the HA config folder is mounted (App, or Docker on the HA host); sftp: Advanced SSH App; none: drafts only.
    return text('HA_CONFIG_ACCESS', 'local' if supervised() else 'sftp').lower()


def api_token(db_path):
    """AR_TUNER_TOKEN, or inside an App a random token kept next to the database (shown on the Settings page)."""
    value = text('AR_TUNER_TOKEN')
    if value or not supervised():
        return value
    path = Path(db_path).with_name('api_token')
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text().strip()


@dataclass
class Settings:
    ha_url: str = field(default_factory=ha_url)
    ha_token: str = field(default_factory=ha_token)
    ha_ws_url: str = field(default_factory=lambda: text('HA_WS_URL'))
    db_path: str = field(default_factory=lambda: text('DB_PATH', names.DB_FILE))
    token: str = field(default_factory=lambda: api_token(text('DB_PATH', names.DB_FILE)))
    poll_interval: float = field(default_factory=lambda: float(raw('POLL_INTERVAL_S', 5)))
    ignore_pipelines: list = field(default_factory=lambda: items('IGNORE_PIPELINES'))
    exclude_devices: list = field(default_factory=lambda: items('EXCLUDE_DEVICE_IDS'))
    keywords: list = field(default_factory=lambda: items('HOME_KEYWORDS', KEYWORDS))
    prefixes: list = field(default_factory=lambda: items('NORMALIZE_PREFIXES', PREFIXES))
    readonly_services: list = field(default_factory=lambda: items('READ_ONLY_SERVICES'))
    retention: int = field(default_factory=lambda: int(raw('RETENTION_DAYS', 365)))
    store_raw: bool = field(default_factory=lambda: flag('STORE_RAW_EVENTS', True))
    timezone: str = field(default_factory=lambda: text('TZ', 'UTC'))
    reload_timeout: float = 10
    supervised: bool = field(default_factory=supervised)

    # Which API AI analysis uses: 'custom' (the URL and key below) or 'ha' (a Local OpenAI LLM server set up in Home
    # Assistant, with the model of one of its conversation agents, read by ha_llm.py into the ai_ha_* fields at run time;
    # AI_HA_SERVER is "<entry id>/<agent id>" when there are several).
    ai_source: str = field(default_factory=lambda: text('AI_SOURCE', 'custom'))
    ai_ha_server: str = field(default_factory=lambda: text('AI_HA_SERVER'))
    ai_ha_url: str = ''
    ai_ha_key: str = ''
    ai_ha_title: str = ''
    ai_ha_model: str = ''
    ai_ha_problem: str = ''
    llm_base_url: str = field(default_factory=lambda: text('LLM_BASE_URL', 'https://openrouter.ai/api/v1'))
    openrouter_api_key: str = field(default_factory=lambda: text('LLM_API_KEY'))
    diagnose_model: str = field(default_factory=lambda: text('DIAGNOSE_MODEL', 'qwen/qwen3.8-27b'))
    diagnose_provider: dict = field(default_factory=lambda: mapping('DIAGNOSE_PROVIDER', {'zdr': True}))
    diagnose_max_steps: int = field(default_factory=lambda: int(raw('DIAGNOSE_MAX_STEPS', 12)))
    diagnose_timeout: float = field(default_factory=lambda: float(raw('DIAGNOSE_TIMEOUT_S', 300)))
    # Reasoning effort for OpenRouter-style APIs: medium (low skipped its own test results in testing); 'default' sends nothing.
    diagnose_reasoning: str = field(default_factory=lambda: text('DIAGNOSE_REASONING', 'medium'))
    diagnose_daily_usd: float = field(default_factory=lambda: float(raw('DIAGNOSE_DAILY_USD', 1.00)))
    diagnose_price_in: float = field(default_factory=lambda: float(raw('DIAGNOSE_PRICE_IN', 0.20)))
    diagnose_price_out: float = field(default_factory=lambda: float(raw('DIAGNOSE_PRICE_OUT', 2.50)))
    nightly_diagnose: bool = field(default_factory=lambda: flag('NIGHTLY_DIAGNOSE', False))

    ha_config_access: str = field(default_factory=config_access)
    ha_config_dir: str = field(default_factory=lambda: text('HA_CONFIG_DIR', '/homeassistant'))
    ha_ssh_host: str = field(default_factory=ssh_host)
    ha_ssh_port: int = field(default_factory=lambda: int(raw('HA_SSH_PORT', 22)))
    ha_ssh_user: str = field(default_factory=lambda: text('HA_SSH_USER', 'root'))
    ha_ssh_key_file: str = field(default_factory=lambda: text('HA_SSH_KEY_FILE', '/data/ha_ssh_key'))
    ha_ssh_host_key: str = field(default_factory=lambda: text('HA_SSH_HOST_KEY'))

    def websocket_url(self):
        if self.ha_ws_url:
            return self.ha_ws_url
        base = self.ha_url.rstrip('/')
        scheme = 'wss' if base.startswith('https') else 'ws'
        # The Supervisor's Core proxy serves the WebSocket API at /core/websocket, not /core/api/websocket.
        suffix = '/websocket' if base == SUPERVISOR_URL else '/api/websocket'
        return scheme + ':' + base.split(':', 1)[1] + suffix
