import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from .explain import near_miss_text, alias_suggestions


def cutoff(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def requests(db, include_excluded=False, limit=50, offset=0, **filters):
    clauses, args = [], []
    if not include_excluded:
        clauses.append('r.excluded=0')
    for key, column in {'handled_by': 'r.handled_by', 'topic': 'COALESCE(r.topic_override,p.topic_override,r.topic)', 'source': 'r.source', 'device': 'r.device_id', 'pattern': 'r.pattern'}.items():
        if filters.get(key):
            clauses.append(column + '=?')
            args.append(filters[key])
    for key, op in [('since', '>='), ('until', '<=')]:
        if filters.get(key):
            clauses.append('r.started_at' + op + '?')
            args.append(filters[key])
    if filters.get('q'):
        clauses.append('r.transcript LIKE ?')
        args.append('%' + filters['q'] + '%')
    sql = 'SELECT r.*, COALESCE(r.topic_override,p.topic_override,r.topic) AS effective_topic FROM requests r LEFT JOIN patterns p ON p.pattern=r.pattern'
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    return db.rows(sql + ' ORDER BY r.started_at DESC LIMIT ? OFFSET ?', (*args, limit, offset))


def summarize(rows):
    local = [r for r in rows if r['handled_by'].startswith('local_')]
    llm = [r for r in rows if r['handled_by'] == 'llm']
    home = [r for r in llm if r.get('effective_topic', r['topic']) == 'home']
    def med(items):
        values = [r['duration_s'] for r in items if r['duration_s'] is not None]
        return median(values) if values else None
    return dict(requests=len(rows), local=len(local), llm=len(llm), home_llm=len(home),
                local_percent=100 * len(local) / len(rows) if rows else 0,
                llm_percent=100 * len(llm) / len(rows) if rows else 0,
                home_among_llm_percent=100 * len(home) / len(llm) if llm else 0,
                home_coverage=len(local) / (len(local) + len(home)) if local or home else 0,
                median_local_s=med(local), median_llm_s=med(llm))


def stats(db, days=30, source='voice'):
    rows = requests(db, since=cutoff(days), source=source, limit=-1)
    result = summarize(rows)
    for label, key in [('per_day', lambda r: r['started_at'][:10]),
                       ('per_week', lambda r: datetime.fromisoformat(r['started_at']).strftime('%G-W%V')),
                       ('per_device', lambda r: r['device_name'] or 'Typed / unknown')]:
        groups = defaultdict(list)
        for row in rows:
            groups[key(row)].append(row)
        result[label] = {k: summarize(v) for k, v in sorted(groups.items())}
    result['per_handled_by'] = dict(Counter(r['handled_by'] for r in rows))
    result['top_local'] = Counter((r['matcher'] or {}).get('intent', {}).get('name') or (r['matcher'] or {}).get('sentence_template', 'unknown') for r in rows if r['handled_by'].startswith('local_')).most_common(20)
    result['top_tools'] = Counter(t['name'] for r in rows if r['handled_by'] == 'llm' for t in r['tools']).most_common(20)
    return result


def candidates(db, ha, days=30, limit=20):
    ignored = {p['pattern'] for p in db.rows('SELECT * FROM patterns WHERE ignored=1')}
    groups = defaultdict(list)
    for row in requests(db, handled_by='llm', topic='home', since=cutoff(days), limit=-1):
        if row['pattern'] not in ignored:
            groups[row['pattern']].append(row)
    result = []
    for pattern, rows in sorted(groups.items(), key=lambda p: (-len(p[1]), p[0]))[:limit]:
        tools = Counter(json.dumps({'name': t['name'], 'args': t['args']}, sort_keys=True) for r in rows for t in r['tools'])
        misses = Counter(json.dumps({'intent': (r['matcher'] or {}).get('intent'), 'unmatched_slots': (r['matcher'] or {}).get('unmatched_slots', {})}, sort_keys=True) for r in rows)
        examples = list(dict.fromkeys(r['transcript'] for r in rows))[:5]
        hints = []
        near = json.loads(misses.most_common(1)[0][0])
        representative = next(r['matcher'] for r in rows if (r['matcher'] or {}).get('unmatched_slots', {}) == near['unmatched_slots'])
        hints.extend(alias_suggestions(representative, ha.entities, ha.areas))
        all_tools = [t for r in rows for t in r['tools']]
        if all(r['tools'] for r in rows) and all(t['name'].endswith(('home_readings', 'GetLiveContext')) for t in all_tools):
            hints.append(dict(kind='status question: add a sentence-trigger automation with a templated reply', skeleton={
                'alias': 'Voice: new status question', 'triggers': [{'trigger': 'conversation', 'command': examples}],
                'actions': [{'set_conversation_response': 'TODO: replace with a sensor template'}]}))
        if all(len(r['tools']) == 1 for r in rows) and len({(t['name'], json.dumps(t['args'].get('entity_id', t['args'].get('name')), sort_keys=True)) for t in all_tools}) == 1 and all_tools and 'Hass' in all_tools[0]['name']:
            hints.append(dict(kind='command: alias or a sentence trigger for that action'))
        result.append(dict(pattern=pattern, count=len(rows), examples=examples,
                           devices=dict(Counter(r['device_name'] or 'Typed / unknown' for r in rows)),
                           tools=[dict(json.loads(k), count=v) for k, v in tools.most_common(10)],
                           near_miss=near, near_miss_text=near_miss_text(representative), suggestions=hints))
    return result
