import re
from .config import PREFIXES


def clean(text):
    return ' '.join(re.sub(r'[^\w\s]', '', text.lower()).split())


def normalize(text, prefixes=None):
    text = clean(text)
    prefixes = sorted((clean(p) for p in (prefixes if prefixes is not None else PREFIXES.split(','))), key=len, reverse=True)
    while True:
        match = next((p for p in prefixes if text == p or text.startswith(p + ' ')), None)
        if not match:
            return text
        text = text[len(match):].strip()


def vocabulary(entities, areas):
    words = {}
    exposed = [entity for entity in entities if entity.get('exposed') is True]
    for rows, slot in ((areas, '{area}'), (exposed, '{name}')):
        for row in rows:
            for name in [row.get('name'), *row.get('aliases', [])]:
                if isinstance(name, str) and clean(name):
                    words[clean(name)] = slot
    return words


def pattern(text, entities=(), areas=(), prefixes=None):
    text = normalize(text, prefixes)
    words = vocabulary(entities, areas)
    if words:
        expression = r'(?<!\w)(?:' + '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r')(?!\w)'
        text = re.sub(expression, lambda m: words[m[0]], text)
    return re.sub(r'\b\d+\b', '{number}', text)
