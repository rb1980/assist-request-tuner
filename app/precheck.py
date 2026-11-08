import re
from hassil import Intents, recognize


def check_template(templates, sentences, slots=None):
    """Match sentence-trigger templates offline, using wildcard slots by default."""
    if not isinstance(templates, list) or not 1 <= len(templates) <= 100:
        raise ValueError('Provide 1–100 templates')
    if not isinstance(sentences, list) or len(sentences) > 200:
        raise ValueError('Provide at most 200 sentences')
    if any(not isinstance(s, str) or not s.strip() or len(s) > limit for values, limit in [(templates, 64000), (sentences, 4000)] for s in values):
        raise ValueError('Templates must be nonempty strings up to 64000 characters; sentences up to 4000')
    names = {m.split(':')[0] for template in templates for m in re.findall(r'\{([^{}]+)\}', template)}
    lists = {name: {'wildcard': True} for name in names}
    for name, values in (slots or {}).items():
        lists[name] = {'values': values} if isinstance(values, list) else values
    results, error = [], None
    try:
        intents = Intents.from_dict({'language': 'en', 'lists': lists, 'intents': {
            'Preview': {'data': [{'sentences': templates}]}}})
        # Force parsing even when no test sentences were provided.
        for data in intents.intents['Preview'].data:
            _ = data.sentences
        for sentence in sentences:
            result = recognize(sentence, intents)
            results.append(dict(sentence=sentence, match=result is not None,
                                slots={k: v.value for k, v in result.entities.items()} if result else {}))
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    count = sum(r['match'] for r in results)
    return dict(passed=error is None and count == len(sentences), matched=count, total=len(sentences),
                results=results, error=error)
