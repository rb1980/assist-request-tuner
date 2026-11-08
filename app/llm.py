"""Any OpenAI-compatible chat API, with no provider-specific code.

Requests carry a few optional fields that only some providers understand: OpenRouter's privacy routing (`provider`),
two styles of reasoning effort (`reasoning: {effort}` and `reasoning_effort`), and the token limit, which newer OpenAI
models want as `max_completion_tokens`. Providers that don't know a field either ignore it or reject the request
naming it. A rejection teaches Request Tuner: the field is dropped (or renamed), the request is retried, and what was
learned is saved per API URL and model, so later requests go right the first time. The Settings page's Test button
runs the same discovery up front and lists the provider's models.
"""
import asyncio
import json
import re
import time
from . import names
from . import __version__

OPTIONAL = ('provider', 'reasoning', 'reasoning_effort', 'parallel_tool_calls')
PROFILES = 'llm_profiles'


def error_text(response, key, limit=300):
    """The provider's own error message, without the API key."""
    try:
        data = response.json()
        error = data.get('error')
        message = (error.get('message') if isinstance(error, dict) else error) or data.get('message') or data.get('detail')
        if not isinstance(message, str):
            message = json.dumps(message)
    except (ValueError, AttributeError):
        message = None
    message = ' '.join(str(message or response.text or '').split())
    if key:
        message = message.replace(key, '[redacted]')
    return message[:limit] + ('…' if len(message) > limit else '')


REJECTED = re.compile(r"unsupported|not supported|does not support|doesn't support|unrecogni[sz]ed|unknown|extra (inputs|fields)"
                      r"|not permitted|not allowed|additional properties|unexpected", re.I)


def rejects_field(text):
    """Whether an error says a field isn't accepted (as opposed to some other failure that names a field)."""
    return REJECTED.search(text) is not None


def mentions(text, name):
    # Word match, so "reasoning" doesn't match inside "reasoning_effort".
    return re.search(r'(?<![\w])' + re.escape(name) + r'(?![\w])', text) is not None


def wait_for(response, attempt):
    """Seconds to wait before retrying a rate-limited request: the provider's Retry-After, else 5, 10, 20, 40 s."""
    for name in ('retry-after', 'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens'):
        try:
            return min(60.0, max(1.0, float(response.headers[name].rstrip('s'))))
        except (KeyError, ValueError):
            continue
    return 5.0 * 2 ** attempt


def endpoint(settings):
    """(API URL, key) that AI analysis uses: typed in (custom), or a Local OpenAI LLM server from Home Assistant."""
    if settings.ai_source == 'ha':
        return settings.ai_ha_url, settings.ai_ha_key
    return settings.llm_base_url, settings.openrouter_api_key


def effective(settings):
    """Model, reasoning effort, prices and daily budget in use. With Home Assistant's server everything comes from HA:
    its agent's model, no reasoning setting, and no prices or budget (HA has none to give)."""
    if settings.ai_source == 'ha':
        return dict(model=settings.ai_ha_model, reasoning='default', price_in=0.0, price_out=0.0, budget=0.0)
    return dict(model=settings.diagnose_model, reasoning=settings.diagnose_reasoning, price_in=settings.diagnose_price_in,
                price_out=settings.diagnose_price_out, budget=settings.diagnose_daily_usd)


def unavailable(settings):
    """Why AI analysis can't run with these settings, in words, or '' when it can."""
    url, key = endpoint(settings)
    if settings.ai_source == 'ha':
        return settings.ai_ha_problem if not url else ''   # HA's servers may run without a key (LM Studio, llama.cpp)
    if not url:
        return 'No AI API URL is set: add one under Settings → AI analysis.'
    return '' if key else 'No AI API key is set: add one under Settings → AI analysis.'


class Client:
    """Chat completions for one API URL, key and model, remembering what the provider accepts."""

    def __init__(self, settings, db=None, model=None):
        self.settings, self.db = settings, db
        self.model = model or effective(settings)['model']
        url, self.key = endpoint(settings)
        self.base = url.rstrip('/')
        self.headers = {'User-Agent': names.TECH + '/' + __version__}
        if self.key:   # local servers often need none
            self.headers['Authorization'] = 'Bearer ' + self.key

    # Profiles: {"<url>|<model>": {"unsupported": [...], "token_field": "max_tokens", "tools": true, "checked_at": ...}}
    def _profiles(self):
        try:
            return json.loads((self.db.meta(PROFILES) if self.db else None) or '{}')
        except ValueError:
            return {}

    @property
    def profile(self):
        return self._profiles().get(f'{self.base}|{self.model}', {})

    def remember(self, **learned):
        if not self.db:
            return
        profiles = self._profiles()
        profiles[f'{self.base}|{self.model}'] = {**profiles.get(f'{self.base}|{self.model}', {}), **learned}
        self.db.meta(PROFILES, json.dumps(profiles))

    def body(self, messages, tools=None, max_tokens=4096):
        profile = self.profile
        result = {'model': self.model, 'messages': messages, profile.get('token_field', 'max_tokens'): max_tokens}
        if tools:
            result['tools'] = tools
        if self.settings.diagnose_provider:
            result['provider'] = self.settings.diagnose_provider
        effort = effective(self.settings)['reasoning']
        if effort != 'default':
            result['reasoning'] = {'effort': effort}      # OpenRouter's style
            result['reasoning_effort'] = effort           # OpenAI's style; providers keep the one they know
        for name in profile.get('unsupported', []):
            result.pop(name, None)
        return result

    def adapt(self, payload, message):
        """The payload without the field a 400 error names, or None if it names none of ours."""
        if not rejects_field(message):
            return None
        token_field = 'max_completion_tokens' if 'max_completion_tokens' in payload else 'max_tokens'
        if token_field == 'max_tokens' and mentions(message, 'max_tokens') and mentions(message, 'max_completion_tokens'):
            payload = {k: v for k, v in payload.items() if k != 'max_tokens'} | {'max_completion_tokens': payload['max_tokens']}
            self.remember(token_field='max_completion_tokens')
            return payload
        for name in OPTIONAL:
            if name in payload and mentions(message, name):
                self.remember(unsupported=sorted(set(self.profile.get('unsupported', [])) | {name}))
                return {k: v for k, v in payload.items() if k != name}
        if 'reasoning_effort' in payload and 'reasoning' in message.lower():
            # "This model does not support reasoning": drop both styles.
            self.remember(unsupported=sorted(set(self.profile.get('unsupported', [])) | {'reasoning', 'reasoning_effort'}))
            return {k: v for k, v in payload.items() if k not in ('reasoning', 'reasoning_effort')}
        return None

    async def post(self, http, payload, retries=4, sleep=asyncio.sleep):
        """POST a chat completion, dropping fields the provider rejects and waiting out rate limits (HTTP 429)."""
        url = self.base + '/chat/completions'
        attempt = adaptations = 0
        while True:
            response = await http.post(url, headers=self.headers, json=payload)
            if response.status_code in (400, 422) and adaptations < len(OPTIONAL) + 2:
                adapted = self.adapt(payload, error_text(response, None, limit=2000))
                if adapted is not None:
                    payload, adaptations = adapted, adaptations + 1
                    continue
            if response.status_code != 429 or attempt >= retries:
                return response
            await sleep(wait_for(response, attempt))
            attempt += 1

    async def models(self, http):
        """The provider's models: [{id, tools, price_in, price_out}] (price in USD per million tokens, if known)."""
        response = await http.get(self.base + '/models', headers=self.headers)
        if response.is_error:
            raise ValueError(f'HTTP {response.status_code}: ' + error_text(response, self.key))
        data = response.json()
        items = data.get('data', data.get('models', [])) if isinstance(data, dict) else data
        found = []
        for item in items if isinstance(items, list) else []:
            ident = item.get('id') or item.get('name') if isinstance(item, dict) else item
            if not isinstance(ident, str):
                continue
            entry = dict(id=ident, tools=None, price_in=None, price_out=None)
            if isinstance(item, dict):
                supported = item.get('supported_parameters')
                if isinstance(supported, list):
                    entry['tools'] = 'tools' in supported
                pricing = item.get('pricing') or {}
                try:
                    entry['price_in'] = round(float(pricing['prompt']) * 1_000_000, 4)
                    entry['price_out'] = round(float(pricing['completion']) * 1_000_000, 4)
                except (KeyError, TypeError, ValueError):
                    pass
            found.append(entry)
        return sorted(found, key=lambda m: m['id'].lower())

    async def probe(self, http, tools):
        """One real request with Request Tuner's tools and every optional field. Learns what the provider accepts and
        whether the model calls tools. Returns (ok, notes)."""
        messages = [{'role': 'system', 'content': 'You are testing tool calling. Call the list_areas tool now. Do not answer in text.'},
                    {'role': 'user', 'content': 'Call list_areas.'}]
        before = set(self.profile.get('unsupported', []))
        payload = self.body(messages, tools, max_tokens=512)
        response = await self.post(http, payload)
        if response.status_code in (401, 403):
            return False, [f'The provider rejected the API key (HTTP {response.status_code}).']
        if response.status_code == 404:
            return False, [f'HTTP 404: {error_text(response, self.key)}. Check the model name and that the URL ends in /v1 (or the API\'s base path).']
        if response.is_error:
            return False, [f'HTTP {response.status_code}: ' + error_text(response, self.key)]
        try:
            message = response.json()['choices'][0]['message']
        except (ValueError, KeyError, IndexError, TypeError):
            return False, ['The API answered, but not in the OpenAI chat format. Is the URL an OpenAI-compatible endpoint?']
        calls = message.get('tool_calls') or []
        tools_ok = any((c.get('function') or {}).get('name') == 'list_areas' for c in calls)
        profile = self.profile
        self.remember(tools=tools_ok, checked_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        notes = [f'{self.model} answered.']
        notes.append('It calls tools, which AI analysis needs.' if tools_ok else
                     "✗ It didn't call a tool when asked. AI analysis needs a model with tool calling; try another model.")
        dropped = set(profile.get('unsupported', [])) - before
        unsupported = set(profile.get('unsupported', []))
        if effective(self.settings)['reasoning'] != 'default':
            if {'reasoning', 'reasoning_effort'} <= unsupported:
                notes.append('This provider or model takes no reasoning setting, so none is sent.')
            elif dropped & {'reasoning', 'reasoning_effort'}:
                notes.append('Reasoning effort is sent in the form this provider accepts.')
        if self.settings.diagnose_provider and 'provider' in unsupported:
            notes.append('Privacy routing (zero data retention) is an OpenRouter option; this provider doesn\'t take it, so check its own data policy.')
        if profile.get('token_field') == 'max_completion_tokens':
            notes.append('Token limits are sent as max_completion_tokens, as this model requires.')
        return tools_ok, notes
