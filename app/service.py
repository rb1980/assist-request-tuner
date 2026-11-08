from datetime import datetime, timedelta, timezone
from . import analysis, __version__
from .parse import now
from .explain import near_miss_text, alias_suggestions
from .diagnose import Diagnose


class Service:
    def __init__(self, db, ha, changes):
        self.db, self.ha, self.changes = db, ha, changes
        self.diagnose = Diagnose(self)

    def status(self):
        return dict(version=__version__, collector=self.db.meta('collector_status') or 'starting',
                    ha_connected=self.ha.ws is not None, last_poll=self.db.meta('last_poll'), writer=self.changes.sentences.writer.configuration(),
                    counts={'requests': self.db.rows('SELECT count(*) AS n FROM requests')[0]['n']})

    def get_request(self, ident):
        rows = self.db.rows('SELECT * FROM requests WHERE id=?', (ident,))
        if not rows:
            raise KeyError('Request not found')
        row = rows[0]
        row['events_summary'] = [dict(type=e['type'], timestamp=e['timestamp']) for e in row.get('raw_events') or []]
        row['near_miss_text'] = near_miss_text(row['matcher'])
        row['suggestions'] = alias_suggestions(row['matcher'], self.ha.entities, self.ha.areas)
        return row

    def set_topic(self, ident, topic):
        self.get_request(ident)
        if topic not in {None, 'home', 'general', 'unknown'}:
            raise ValueError('Invalid topic')
        self.db.execute('UPDATE requests SET topic_override=? WHERE id=?', (topic, ident))
        return self.get_request(ident)

    def set_pattern(self, pattern, patch):
        if set(patch) - {'ignored', 'topic_override', 'note'}:
            raise ValueError('Unknown pattern fields')
        if patch.get('topic_override') not in {None, 'home', 'general', 'unknown'}:
            raise ValueError('Invalid topic')
        self.db.save('patterns', dict(patch, pattern=pattern), 'pattern')
        return self.db.rows('SELECT * FROM patterns WHERE pattern=?', (pattern,))[0]

    def ignore(self, minutes=None, reason='', stop=False):
        if minutes is not None and not 0 < minutes <= 1440:
            raise ValueError('minutes must be between 0 and 1440')
        if stop or minutes is not None:
            self.db.execute('UPDATE ignore_windows SET end=? WHERE end>?', (now(), now()))
        if minutes is not None:
            self.db.save('ignore_windows', dict(start=now(), end=(datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(), reason=reason))
        return self.db.rows('SELECT * FROM ignore_windows WHERE start<=? AND end>?', (now(), now()))

    async def match(self, sentences):
        if not 1 <= len(sentences) <= 2000 or any(not isinstance(s, str) or not s.strip() or len(s) > 4000 for s in sentences):
            raise ValueError('Provide 1–2000 nonempty sentences, at most 4000 characters each')
        results = []
        for i in range(0, len(sentences), 50):
            results.extend(await self.ha.match(sentences[i:i + 50]))
        return dict(results=results, summary=[('Matched ' + (r.get('intent', {}).get('name') or r.get('source', 'sentence'))) if r and r.get('match') else ('Near miss: ' + str(r.get('unmatched_slots', {}))) if r else 'No match' for r in results])

    async def entities(self, q='', domain='', exposed=None):
        await self.ha.refresh()
        return [e for e in self.ha.entities if (not domain or e['domain'] == domain) and (exposed is None or e['exposed'] == exposed) and q.lower() in (e['entity_id'] + ' ' + e['name'] + ' ' + ' '.join(a for a in e['aliases'] if a)).lower()]

    async def intents(self):
        counts = {}
        for row in analysis.requests(self.db, limit=-1):
            match = row['matcher'] or {}
            if row['handled_by'] == 'local_intent' and match.get('intent'):
                name = match['intent']['name']
                counts[name] = counts.get(name, 0) + 1
        return dict(automations=await self.ha.automations(), builtin=counts)
