import difflib
from .patterns import clean

SLOT_WORDS = {'name': 'device name', 'area': 'area', 'brightness': 'brightness', 'color': 'color',
              'position': 'position', 'temperature': 'temperature', 'floor': 'floor'}


def near_miss_text(matcher):
    if not matcher:
        return 'No built-in sentence is close.'
    if matcher.get('match'):
        return 'This phrasing now matches the local ' + matcher.get('source', 'intent') + ' layer.'
    template = matcher.get('sentence_template', 'unknown template')
    intent = matcher.get('intent', {}).get('name', 'unknown intent')
    prefix = f'Closest built-in sentence: {template} ({intent}).'
    reasons = []
    for slot, text in matcher.get('unmatched_slots', {}).items():
        value = str(text).strip()
        if slot == 'name':
            reasons.append(f"'{value}' isn't the device name of an exposed entity")
        elif slot == 'area':
            reasons.append(f"'{value}' isn't a known area name or alias")
        else:
            reasons.append(f"'{value}' isn't a recognized {SLOT_WORDS.get(slot, slot.replace('_', ' '))}")
    return prefix + (' It failed because ' + '; '.join(reasons) + '.' if reasons else ' The matcher could not resolve this sentence.')


def alias_suggestions(matcher, entities, areas):
    hints = []
    for slot, text in (matcher or {}).get('unmatched_slots', {}).items():
        if slot not in {'name', 'area'} or not isinstance(text, str):
            continue
        normalized = clean(text)
        words = [w for w in normalized.split() if w not in {'the', 'a', 'an', 'light', 'lights', 'lamp', 'lamps'}]
        if not 1 <= len(words) <= 4:
            continue
        phrase = ' '.join(words)
        exposed = [e for e in entities if e.get('exposed') is True]
        pools = [('area', areas)] if slot == 'area' else [('name', exposed)]
        if slot == 'name' and normalized.endswith(' lights'):
            pools.insert(0, ('area', areas))
        ranked = []
        for kind, targets in pools:
            for target in targets:
                names = [target.get('name'), *target.get('aliases', [])]
                score = max((max(difflib.SequenceMatcher(None, phrase, clean(n)).ratio(),
                                 difflib.SequenceMatcher(None, phrase.replace(' ', ''), clean(n).replace(' ', '')).ratio())
                             for n in names if isinstance(n, str) and n), default=0)
                if score >= .6:
                    ranked.append((score, kind, target))
        ranked.sort(key=lambda item: (-item[0], item[2]['name']))
        grouped = {}
        for _, kind, target in ranked[:3]:
            grouped.setdefault(kind, []).append(target)
        for kind, targets in grouped.items():
            hints.append(dict(kind='possible alias', slot=kind, text=phrase, targets=targets))
    return hints
