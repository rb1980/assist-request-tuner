import re
from .patterns import clean, vocabulary


def classify(row, matcher, settings, entities=(), areas=()):
    local = row.get('processed_locally')
    if row.get('error') or (row.get('response_type') == 'error' and local is not False):
        handled = 'error'
    elif local:
        handled = 'local_trigger' if (matcher or {}).get('source') == 'trigger' else 'local_intent'
    elif local is False:
        handled = 'llm'
    else:
        handled = 'error' if row['status'] == 'complete' else 'unknown'
    names = [t['name'] for t in row.get('tools', [])]
    words = list(vocabulary(entities, areas)) + settings.keywords
    text = clean(row.get('transcript', ''))
    if local or any(n.startswith(('assist__', 'homeassistant__')) for n in names):
        topic = 'home'
    elif names and all(n.endswith(('web_search', 'world_time')) for n in names):
        topic = 'general'
    elif any(re.search(r'(?<!\w)' + re.escape(clean(w)) + r'(?!\w)', text) for w in words if clean(w)):
        topic = 'home'
    else:
        topic = 'general' if text else 'unknown'
    return dict(handled_by=handled, topic=topic)
