"""Managed English sentence blocks and offline matching with HA's vocabulary."""
import copy
import hashlib
import json
import re
from functools import lru_cache
import yaml
from hassil import Intents, recognize
from home_assistant_intents import get_intents
from .parse import now
from . import names

HEADER = names.HEADER


@lru_cache(maxsize=1)
def english():
    return get_intents('en')


def validate_payload(payload):
    if set(payload) - {'intent', 'add', 'remove', 'slots', 'requires_context', 'block_id'}:
        raise ValueError('Unknown custom_sentence payload fields')
    if payload.get('intent') not in english()['intents']:
        raise ValueError('Select a known English built-in intent')
    for key in ('add', 'remove'):
        value = payload.get(key, [])
        if not isinstance(value, list) or any(not isinstance(s, str) or not s.strip() or len(s) > 4000 for s in value):
            raise ValueError('add/remove must contain nonempty sentences up to 4000 characters')
    if not payload.get('add') and not payload.get('remove'):
        raise ValueError('Provide add or remove')
    for key in ('slots', 'requires_context'):
        if not isinstance(payload.get(key, {}), dict):
            raise ValueError(key + ' must be an object')
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('Sentence payload must contain JSON-compatible values') from None
    if 'block_id' in payload and (type(payload['block_id']) is not int or payload['block_id'] <= 0):
        raise ValueError('block_id must be a positive integer')


def render(blocks):
    intents = {}
    for block in sorted(blocks, key=lambda b: (b['intent'], b['id'])):
        data = {k: copy.deepcopy(block[k]) for k in ('sentences', 'slots', 'requires_context') if block.get(k)}
        if block.get('comment'):
            data['metadata'] = {names.COMMENT_KEY: block['comment']}
        intents.setdefault(block['intent'], {'data': []})['data'].append(data)
    return HEADER + yaml.safe_dump({'language': 'en', 'intents': intents}, sort_keys=False, allow_unicode=True)


def parse(text):
    if text is None:
        return []
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError('Invalid custom sentence YAML') from None
    if not isinstance(document, dict) or document.get('language') != 'en' or set(document) - {'language', 'intents'} or not isinstance(document.get('intents'), dict):
        raise ValueError('Expected English intents YAML; unsupported file fields cannot be imported')
    blocks = []
    for intent, definition in document['intents'].items():
        if not isinstance(definition, dict) or set(definition) != {'data'} or not isinstance(definition['data'], list):
            raise ValueError('Expected intent data blocks')
        for data in definition['data']:
            if not isinstance(data, dict) or set(data) - {'sentences', 'slots', 'requires_context', 'metadata'}:
                raise ValueError('Unsupported sentence block fields')
            metadata = data.get('metadata', {})
            if not isinstance(metadata, dict) or set(metadata) - {names.COMMENT_KEY}:
                raise ValueError('Unsupported sentence metadata')
            validate_payload(dict(intent=intent, add=data.get('sentences'), slots=data.get('slots', {}), requires_context=data.get('requires_context', {})))
            blocks.append(dict(id=len(blocks) + 1, intent=intent, sentences=data['sentences'], slots=data.get('slots', {}),
                               requires_context=data.get('requires_context', {}), comment=metadata.get(names.COMMENT_KEY, ''), created_at=now(), change_id=None))
    # Parse every custom sentence now, even without captured examples.
    engine(blocks, [], [])
    return blocks


def desired(blocks, payload, change_id=None):
    result = copy.deepcopy(blocks)
    ident = payload.get('block_id')
    chosen = next((b for b in result if b['id'] == ident), None) if ident else None
    if ident and (not chosen or chosen['intent'] != payload['intent']):
        raise ValueError('block_id does not belong to the selected intent')
    for block in result:
        if block['intent'] == payload['intent'] and (ident is None or block['id'] == ident):
            block['sentences'] = [s for s in block['sentences'] if s not in payload.get('remove', [])]
    if payload.get('add'):
        if chosen is None:
            chosen = dict(id=max((b['id'] for b in result), default=0) + 1, intent=payload['intent'], sentences=[], slots={}, requires_context={}, comment='', created_at=now(), change_id=change_id)
            result.append(chosen)
        chosen['sentences'] = list(dict.fromkeys(chosen['sentences'] + payload['add']))
        for key in ('slots', 'requires_context'):
            if key in payload:
                chosen[key] = copy.deepcopy(payload[key])
        chosen['change_id'] = change_id
    return [b for b in result if b['sentences']]


def engine(blocks, entities, areas):
    data = copy.deepcopy(english())
    area_values = []
    for area in areas:
        for name in [area['name'], *area.get('aliases', [])]:
            if name:
                area_values.append({'in': name, 'out': area['name'], 'context': {'area': area['area_id']}})
    name_values = []
    for entity in entities:
        if entity.get('exposed') is not True:
            continue
        for name in [entity['name'], *entity.get('aliases', [])]:
            if name:
                name_values.append({'in': name, 'out': entity['name'], 'context': {'domain': entity['domain'], 'name': entity['entity_id'], 'area': entity.get('area')}})
    data['lists'].update(area={'values': area_values}, name={'values': name_values}, floor={'values': []})
    for block in reversed(blocks):
        item = {k: copy.deepcopy(block.get(k, {})) for k in ('sentences', 'slots', 'requires_context')}
        item['metadata'] = {names.SOURCE_KEY: 'custom'}
        data['intents'][block['intent']]['data'].insert(0, item)
    compiled = Intents.from_dict(data)
    # Validate references as well as syntax; unknown lists must not become wildcards.
    rules = data.get('expansion_rules', {})
    for block in blocks:
        for sentence in block['sentences']:
            missing = set(re.findall(r'\{([^}:]+)(?::[^}]+)?\}', sentence)) - set(data['lists'])
            unknown = set(re.findall(r'<([^>]+)>', sentence)) - set(rules)
            if missing or unknown:
                raise ValueError('Unknown sentence lists/rules: ' + ', '.join(sorted(missing | unknown)))
    for intent in compiled.intents.values():
        for item in intent.data:
            _ = item.sentences
    return compiled


def match(compiled, sentences):
    results = []
    for sentence in sentences:
        result = recognize(sentence, compiled, language='en')
        results.append(dict(sentence=sentence, match=result is not None,
            intent={'name': result.intent.name} if result else None,
            source=('custom' if (result.intent_metadata or {}).get(names.SOURCE_KEY) == 'custom' else 'builtin') if result else None,
            slots={k: v.value for k, v in result.entities.items()} if result else {}))
    return results


def fingerprint(text):
    return hashlib.sha256(('absent' if text is None else 'present:' + text).encode()).hexdigest()
