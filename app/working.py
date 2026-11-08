"""Canonical built-in alternatives derived from captured intent arguments."""


def changed(tools):
    result = []
    for tool in tools or []:
        intent = tool.get('name', '').split('__')[-1]
        args = tool.get('args') or {}
        if not intent.startswith('Hass') or not isinstance(args, dict):
            continue
        result.append(dict(intent=intent, target={k: args[k] for k in ('name', 'area') if k in args},
                           data={k: v for k, v in args.items() if k not in ('name', 'area')}))
    return result


def candidates(row):
    if row.get('handled_by') != 'llm':
        return []
    sentences = []
    for call in changed(row.get('tools')):
        target, data, intent = call['target'], call['data'], call['intent']
        name, area = target.get('name'), target.get('area')
        name = name.strip().lower() if isinstance(name, str) else ''
        area = area.strip().lower() if isinstance(area, str) else ''
        if intent == 'HassLightSet':
            color, brightness = data.get('color'), data.get('brightness')
            # A single-value phrasing must not silently discard another requested setting.
            if color is not None and brightness is not None:
                continue
            value = color if isinstance(color, str) and color.strip() else None
            if brightness is not None and isinstance(brightness, (int, float)) and not isinstance(brightness, bool) and 0 <= brightness <= 100:
                value = f'{brightness:g} percent'
            if value is None:
                continue
            value = value.strip().lower()
            if area:
                sentences += [f'set the lights in the {area} to {value}', f'set the {area} to {value}']
                if color:
                    sentences.append(f'turn the {area} {value}')
            if name:
                sentences.append(f'set the {name} to {value}')
        elif intent in ('HassTurnOn', 'HassTurnOff'):
            action = 'on' if intent == 'HassTurnOn' else 'off'
            if area:
                sentences += [f'turn {action} the {area} lights', f'turn {action} the lights in the {area}']
            if name:
                sentences.append(f'turn {action} the {name}')
    return list(dict.fromkeys(sentences))


async def ensure_working(db, ha, row):
    if row.get('working_phrasings') is not None or row.get('handled_by') != 'llm' or row.get('status') != 'complete':
        return row.get('working_phrasings') or []
    proposed = candidates(row)
    working = []
    for start in range(0, len(proposed), 50):
        batch = proposed[start:start + 50]
        results = await ha.match(batch)
        working.extend(s for s, r in zip(batch, results) if r and r.get('match') and r.get('source') == 'builtin')
    db.save('requests', {'id': row['id'], 'working_phrasings': working})
    row['working_phrasings'] = working
    return working
