import asyncio
import copy
import re
import time
from typing import Literal
from hassil import parse_sentence
from hassil.sample import sample_sentence
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import names
from .analysis import requests, cutoff
from .parse import now
from .ha import HAError
from .local_writer import make_writer
from .precheck import check_template
from . import sentences
from .blueprint import Blueprints, Payload, Inputs, PATH, instance
from .sentence_store import SentenceStore
from .phrasing import managed_description


class MatchTest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sentence: str = Field(min_length=1)
    source: str | None = None
    intent: str | None = None


class Tests(BaseModel):
    model_config = ConfigDict(extra='forbid')
    must_match: list[MatchTest] = Field(default_factory=list)
    must_not_match: list[str] = Field(default_factory=list)
    must_not_match_scope: Literal['any', 'change'] = 'any'


def triggers(config):
    value = config.get('triggers', config.get('trigger', []))
    return [value] if isinstance(value, dict) else value


def changes_devices(actions, readonly=()):
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        return True
    for action in actions:
        if not isinstance(action, dict):
            return True
        keys = set(action) - {'alias', 'enabled', 'continue_on_error'}
        if keys <= {'variables', 'set_conversation_response', 'stop', 'response_variable', 'error'} and keys & {'variables', 'set_conversation_response', 'stop'}:
            continue
        if 'choose' in action and keys <= {'choose', 'default'}:
            if not isinstance(action['choose'], list) or any(not isinstance(b, dict) for b in action['choose']):
                return True
            if any(changes_devices(branch.get('sequence', []), readonly) for branch in action['choose']) or changes_devices(action.get('default', []), readonly):
                return True
            continue
        if 'if' in action and keys <= {'if', 'then', 'else'}:
            if changes_devices(action.get('then', []), readonly) or changes_devices(action.get('else', []), readonly):
                return True
            continue
        service = action.get('action', action.get('service'))
        if isinstance(service, str) and service in readonly and keys <= {'action', 'service', 'data', 'target', 'response_variable'}:
            continue
        return True
    return False


class ChangeDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['entity_alias', 'area_alias', 'trigger_automation', 'trigger_sentences', 'expose_entity', 'custom_sentence', 'blueprint_instance']
    title: str = Field(min_length=1, max_length=200)
    payload: dict
    tests: Tests = Field(default_factory=Tests)
    allow_device_actions: bool = False

    @model_validator(mode='after')
    def validate_payload(self):
        p = self.payload
        if self.tests.must_not_match_scope == 'change' and not (self.type.startswith('trigger_') or self.type == 'blueprint_instance'):
            raise ValueError('Change-scoped negative tests require a trigger change')
        if self.type == 'blueprint_instance':
            self.payload = Payload.model_validate(p).model_dump(exclude_none=True)
            if not self.tests.must_match:
                self.tests.must_match = [MatchTest(sentence=next(iter(sample_sentence(parse_sentence(s)))).strip(), source='trigger')
                                         for s in self.payload['inputs']['sentences']]
            for test in self.tests.must_match:
                test.source = test.source or 'trigger'
            return self
        if self.type == 'custom_sentence':
            sentences.validate_payload(p)
            return self
        target = 'area_id' if self.type == 'area_alias' else 'automation_id' if self.type.startswith('trigger_') else 'entity_id'
        value = p.get(target)
        regex = r'[a-z_]+\.[a-z0-9_]+' if target == 'entity_id' else r'[A-Za-z0-9_-]+'
        if not isinstance(value, str) or not re.fullmatch(regex, value):
            raise ValueError('Invalid target id')
        allowed = {target}
        if self.type in {'entity_alias', 'area_alias', 'trigger_sentences'}:
            allowed |= {'add', 'remove'}
            for key in ('add', 'remove'):
                if not isinstance(p.get(key, []), list) or any(not isinstance(s, str) or not s.strip() for s in p.get(key, [])):
                    raise ValueError('add/remove must contain nonempty strings')
            if not p.get('add') and not p.get('remove'):
                raise ValueError('Provide add or remove')
        elif self.type == 'expose_entity':
            allowed.add('expose')
            if type(p.get('expose')) is not bool:
                raise ValueError('expose must be boolean')
        else:
            if not names.managed_automation(value):
                raise ValueError(f'trigger_automation ids must start with {names.AUTOMATION_PREFIX}; use trigger_sentences for other existing automations')
            allowed.add('config')
            config = p.get('config')
            if not isinstance(config, dict) or not any(t.get('trigger', t.get('platform')) == 'conversation' and t.get('command') for t in triggers(config)):
                raise ValueError('A conversation trigger with commands is required')
            if config.get('id', value) != value:
                raise ValueError('Config id must match automation_id')
        if set(p) - allowed:
            raise ValueError('Unknown payload fields')
        return self


def target(change):
    p = change['payload']
    if change['type'] == 'custom_sentence':
        return 'custom_sentence', 'sentence file'
    kind = 'automation' if (change['type'].startswith('trigger_') or change['type'] == 'blueprint_instance') else 'area' if change['type'] == 'area_alias' else 'entity'
    return kind, p.get('automation_id', p.get('area_id', p.get('entity_id')))


def same_outcome(before, after):
    """Both matched the same intent with the same slots and target devices, through HA's built-in or a custom
    sentence. Such a request still does exactly the same thing. A sentence trigger taking over is not the same: it runs
    an automation instead of the intent."""
    before, after = before or {}, after or {}
    return (matched(before) and matched(after) and {before.get('source'), after.get('source')} <= {'builtin', 'custom'}
            and (before.get('intent') or {}).get('name') == (after.get('intent') or {}).get('name')
            and before.get('slots') == after.get('slots') and sorted(before.get('targets') or {}) == sorted(after.get('targets') or {}))


def matched(result, test=None):
    result = result or {}
    return bool(result.get('match') and (not test or ((not test.get('source') or result.get('source') == test['source']) and (not test.get('intent') or result.get('intent', {}).get('name') == test['intent']))))


class Changes:
    def __init__(self, db, ha, settings):
        self.db, self.ha, self.settings = db, ha, settings
        self.lock = asyncio.Lock()
        self.sentences = SentenceStore(db, ha, settings)
        self.blueprints = Blueprints(db, ha, self.sentences.writer)

    def reconfigure_writer(self):
        # After a settings change: the access mode or SSH details may differ.
        self.sentences.writer = self.blueprints.writer = make_writer(self.db, self.settings)

    def get(self, change_id):
        rows = self.db.rows('SELECT * FROM changes WHERE id=?', (change_id,))
        if not rows:
            raise KeyError('Change not found')
        row = rows[0]
        row['changes_devices'] = row['type'] == 'trigger_automation' and changes_devices(row['payload']['config'].get('actions', row['payload']['config'].get('action', [])), self.settings.readonly_services)
        if row['type'] == 'custom_sentence' and row['status'] == 'draft':
            row['rendered_before'] = sentences.render(self.sentences.blocks())
            row['rendered_after'] = sentences.render(sentences.desired(self.sentences.blocks(), row['payload']))
        row['log'] = self.db.rows('SELECT * FROM changes_log WHERE change_id=? ORDER BY id', (change_id,))
        return row

    async def draft_commands(self, draft):
        if draft['type'] == 'blueprint_instance':
            return draft['payload']['inputs']['sentences']
        config = draft['payload'].get('config')
        if draft['type'] == 'trigger_sentences':
            config = self.desired(draft, await self.snapshot(draft))
        if instance(config) is not None:
            return instance(config)['sentences']
        commands = []
        for trigger in triggers(config):
            if trigger.get('trigger', trigger.get('platform')) == 'conversation':
                value = trigger.get('command', [])
                commands.extend([value] if isinstance(value, str) else value)
        return commands

    async def capture_preview(self, draft):
        if draft['type'] == 'custom_sentence':
            return await self.custom_preview(draft)
        if not (draft['type'].startswith('trigger_') or draft['type'] == 'blueprint_instance'):
            return None
        commands = await self.draft_commands(draft)
        warnings = [dict(sentence=s, message='wildcards capture any words; prefer literal alternatives')
                    for s in commands if re.search(r'(?<!\\)\{[^{}]+\}', s)]
        rows = self.db.rows("SELECT * FROM requests WHERE started_at>=? AND excluded=0 AND transcript!='' ORDER BY started_at DESC, id DESC", (cutoff(90),))
        distinct = {}
        for row in rows:
            distinct.setdefault(row['transcript'], row)
        rows = list(distinct.values())
        captures, errors = [], []
        for start in range(0, len(rows), 200):
            batch = rows[start:start + 200]
            result = check_template(commands, [r['transcript'] for r in batch])
            if result['error']:
                errors.append(result['error'])
            for row, match in zip(batch, result['results']):
                if not match['match']:
                    continue
                current = row['matcher'] or {}
                local = row['handled_by'] in {'local_intent', 'local_trigger'}
                # HA often omits trigger identity. Treat unknown ownership as a warning.
                own = current.get('source') == 'trigger' and current.get('automation_id') == draft['payload']['automation_id']
                captures.append(dict(transcript=row['transcript'], handled_by=row['handled_by'],
                    source=current.get('source') if local else None,
                    intent=(current.get('intent') or {}).get('name') if local else None,
                    steals_local=local and not own, takes_from_llm=row['handled_by'] == 'llm'))
        return dict(days=90, checked=len(rows), would_capture=captures, warnings=warnings,
                    steals_local=sum(r['steals_local'] for r in captures),
                    takes_from_llm=sum(r['takes_from_llm'] for r in captures), errors=list(dict.fromkeys(errors)))

    async def precheck(self, draft):
        if draft['type'] == 'custom_sentence':
            try:
                compiled = await self.custom_engine(draft)
                positive = draft['tests']['must_match']
                negative = draft['tests']['must_not_match']
                results = sentences.match(compiled, [t['sentence'] for t in positive])
                negatives = sentences.match(compiled, negative)
                count = sum(matched(r, t) for r, t in zip(results, positive))
                return dict(applicable=True, passed=count == len(positive) and not any(r['match'] for r in negatives), matched=count,
                            total=len(positive), results=results, negative_results=negatives, error=None)
            except (ValueError, KeyError) as exc:
                return dict(applicable=True, passed=False, matched=0, total=len(draft['tests']['must_match']), results=[], negative_results=[], error=str(exc))
        if not (draft['type'].startswith('trigger_') or draft['type'] == 'blueprint_instance'):
            return dict(applicable=False, passed=None, matched=0, total=0, results=[], error=None)
        commands = await self.draft_commands(draft)
        result = check_template(commands, [t['sentence'] for t in draft['tests']['must_match']])
        negative = check_template(commands, draft['tests']['must_not_match'])
        result['negative_results'] = negative['results']
        result['passed'] = result['passed'] and negative['error'] is None and not any(r['match'] for r in negative['results'])
        result['error'] = result['error'] or negative['error']
        return dict(result, applicable=True)

    async def custom_engine(self, draft):
        await self.ha.refresh()
        blocks = sentences.desired(self.sentences.blocks(), draft['payload'])
        return sentences.engine(blocks, self.ha.entities, self.ha.areas)

    async def custom_preview(self, draft):
        preview = dict(days=90, checked=0, would_capture=[], warnings=[], steals_local=0, takes_from_llm=0, errors=[])
        try:
            compiled = await self.custom_engine(draft)
            # Only report what this change adds: transcripts the current blocks already match the same way don't count.
            baseline = sentences.engine(self.sentences.blocks(), self.ha.entities, self.ha.areas)
            rows = self.db.rows("SELECT * FROM requests WHERE started_at>=? AND excluded=0 AND transcript!='' ORDER BY started_at DESC,id DESC", (cutoff(90),))
            distinct = {}
            for row in rows:
                distinct.setdefault(row['transcript'], row)
            preview['checked'] = len(distinct)
            for row in distinct.values():
                result = sentences.match(compiled, [row['transcript']])[0]
                if not result['match'] or result['source'] != 'custom':
                    continue
                base = sentences.match(baseline, [row['transcript']])[0]
                if base['match'] and base['source'] == 'custom' and base['intent'] == result['intent']:
                    continue
                current = row['matcher'] or {}
                local = row['handled_by'] in {'local_intent', 'local_trigger'}
                steal = local and (current.get('source') == 'trigger' or (current.get('intent') or {}).get('name') != result['intent']['name'])
                item = dict(transcript=row['transcript'], handled_by=row['handled_by'], source=current.get('source') if local else None,
                            intent=(current.get('intent') or {}).get('name') if local else None, matcher=result,
                            steals_local=steal, takes_from_llm=row['handled_by'] == 'llm')
                preview['would_capture'].append(item)
                preview['steals_local'] += steal
                preview['takes_from_llm'] += item['takes_from_llm']
        except (ValueError, KeyError) as exc:
            preview['errors'].append(str(exc))
        return preview

    async def validate_live(self, draft):
        if draft['type'] == 'blueprint_instance':
            inputs = draft['payload']['inputs']
            return await self.blueprints.preview_reply(inputs['entity'], inputs['reply'])
        if draft['type'] == 'trigger_sentences':
            self.desired(draft, await self.snapshot(draft))
        return None

    async def create(self, draft, actor='api', diagnosis_id=None, source='manual'):
        draft = ChangeDraft.model_validate(draft).model_dump()
        reply_preview = await self.validate_live(draft)
        precheck = await self.precheck(draft)
        ident = self.db.save('changes', dict(draft, status='draft', created_at=now(), updated_at=now(),
                                             reply_preview=reply_preview, precheck=precheck, capture_preview=await self.capture_preview(draft), diagnosis_id=diagnosis_id, source=source))
        self.db.log(ident, 'draft', actor, {})
        return self.get(ident)

    async def update(self, ident, patch, actor='api'):
        old = self.get(ident)
        if old['status'] != 'draft':
            raise ValueError('Only drafts may be edited')
        base = {k: old[k] for k in ChangeDraft.model_fields}
        base.update(patch)
        draft = ChangeDraft.model_validate(base).model_dump()
        reply_preview = await self.validate_live(draft)
        precheck = await self.precheck(draft)
        self.db.save('changes', dict(draft, id=ident, updated_at=now(), reply_preview=reply_preview, precheck=precheck, capture_preview=await self.capture_preview(draft)))
        self.db.log(ident, 'update', actor, patch)
        return self.get(ident)

    async def snapshot(self, change):
        p, kind = change['payload'], change['type']
        if kind == 'custom_sentence':
            return await self.sentences.snapshot()
        if kind == 'entity_alias':
            return (await self.ha.call('config/entity_registry/get', entity_id=p['entity_id']))['aliases']
        if kind == 'area_alias':
            area = next((a for a in await self.ha.call('config/area_registry/list') if a['area_id'] == p['area_id']), None)
            if area is None:
                raise ValueError('Area not found')
            return area.get('aliases', [])
        if kind == 'expose_entity':
            exposed = await self.ha.call('homeassistant/expose_entity/list')
            return exposed['exposed_entities'].get(p['entity_id'], {}).get('conversation', False)
        return await self.ha.rest('GET', p['automation_id'])

    def desired(self, change, before):
        p, kind = change['payload'], change['type']
        if kind == 'custom_sentence':
            return sentences.render(sentences.desired(self.sentences.blocks(), p, change.get('id')))
        def edit(values):
            return list(dict.fromkeys([v for v in values if v not in p.get('remove', [])] + p.get('add', [])))
        if kind.endswith('_alias'):
            return edit(before)
        if kind == 'expose_entity':
            return p['expose']
        if kind == 'blueprint_instance':
            alias = p['alias'] if p['alias'].startswith('Voice: ') else 'Voice: ' + p['alias']
            return dict(id=p['automation_id'], alias=alias, description=names.MANAGED_BY,
                        use_blueprint={'path': PATH, 'input': copy.deepcopy(p['inputs'])})
        if kind == 'trigger_automation':
            after = copy.deepcopy(p['config'])
            if not names.managed_automation(p['automation_id']):
                raise ValueError(f'trigger_automation ids must start with {names.AUTOMATION_PREFIX}')
            after['description'] = managed_description(after.get('description'))
            after['id'] = p['automation_id']
            return after
        if before is None:
            raise ValueError('Automation does not exist')
        after = copy.deepcopy(before)
        inputs = instance(after)
        if inputs is not None:
            inputs['sentences'] = edit(inputs.get('sentences', []))
            Inputs.model_validate(inputs)
            return after
        found = False
        for trigger in triggers(after):
            if trigger.get('trigger', trigger.get('platform')) == 'conversation':
                found = True
                values = trigger.get('command', [])
                trigger['command'] = edit([values] if isinstance(values, str) else values)
        if not found:
            raise ValueError('Automation has no conversation trigger')
        return after

    async def write(self, change, state):
        p, kind = change['payload'], change['type']
        if kind == 'custom_sentence':
            stored = self.get(change['id'])
            blocks = stored['sentence_before'] if state == stored['before'] else stored['sentence_after']
            await self.sentences.write(state, blocks)
        elif kind.endswith('_alias'):
            registry = 'entity' if kind == 'entity_alias' else 'area'
            await self.ha.call(f'config/{registry}_registry/update', **{registry + '_id': p[registry + '_id'], 'aliases': state})
        elif kind == 'expose_entity':
            await self.ha.call('homeassistant/expose_entity', entity_ids=[p['entity_id']], assistants=['conversation'], should_expose=state)
        else:
            if kind == 'blueprint_instance':
                await self.blueprints.ensure_blueprint()
            await self.ha.rest('DELETE' if state is None else 'POST', p['automation_id'], state)
        self.ha.cache_at = None

    async def test(self, change):
        tests = change['tests']
        positive = tests.get('must_match', [])
        negative = tests.get('must_not_match', [])
        sentences = [t['sentence'] for t in positive] + negative
        results = await self.ha.match(sentences or ['ar tuner reload probe'])
        if not sentences:
            results = []
        failures = [t['sentence'] for t, r in zip(positive, results) if not matched(r, t)]
        commands = []
        if tests.get('must_not_match_scope') == 'change':
            config = change.get('after') or self.desired(change, await self.snapshot(change))
            if instance(config) is not None:
                commands.extend(instance(config)['sentences'])
            for trigger in triggers(config):
                if trigger.get('trigger', trigger.get('platform')) == 'conversation':
                    values = trigger.get('command', [])
                    commands.extend([values] if isinstance(values, str) else values)
        for sentence, result in zip(negative, results[len(positive):]):
            if not matched(result):
                continue
            if tests.get('must_not_match_scope') != 'change' or (result.get('source') == 'trigger' and result.get('sentence_template') in commands):
                failures.append(sentence)
        return dict(passed=not failures, failures=failures, results=results)

    async def replay_matches(self, days=30):
        rows = requests(self.db, since=cutoff(days), limit=-1)
        sentences = list(dict.fromkeys(r['transcript'] for r in rows if r['transcript']))[:2000]
        matches = []
        for i in range(0, len(sentences), 50):
            matches.extend(await self.ha.match(sentences[i:i + 50]))
        return rows, sentences, dict(zip(sentences, matches))

    async def replay(self, ident, days=30, baseline=None):
        change = self.get(ident)
        if change['status'] == 'draft':
            raise ValueError('Apply first: HA cannot preview a draft')
        rows, sentences, mapping = await self.replay_matches(days)
        new, lost = {}, {}
        before_count = after_count = home_count = 0
        for row in rows:
            if row['transcript'] not in mapping:
                continue
            result = mapping[row['transcript']]
            local = row['handled_by'].startswith('local_')
            old = row['matcher'] or {}
            if baseline is not None and row['transcript'] in baseline:
                # Compare with HA just before this change, not with how the request was handled when captured.
                old = baseline[row['transcript']] or {}
                local = matched(old)
            if row['handled_by'] == 'llm' and matched(result) and not (baseline is not None and local):
                new[row['transcript']] = dict(sentence=row['transcript'], matcher=result)
            if local and not same_outcome(old, result) and (not matched(result) or (result or {}).get('source') != old.get('source') or (result or {}).get('intent') != old.get('intent')):
                lost[row['transcript']] = dict(sentence=row['transcript'], before=old, after=result)
            if row['source'] == 'voice' and (local or (row['handled_by'] == 'llm' and row['effective_topic'] == 'home')):
                home_count += 1
                before_count += local
                after_count += matched(result)
        report = dict(newly_local=list(new.values()), lost=list(lost.values()), sentences=len(sentences),
                      truncated=len({r['transcript'] for r in rows}) > 2000,
                      coverage_before=before_count / home_count if home_count else 0,
                      coverage_after=after_count / home_count if home_count else 0)
        self.db.save('changes', dict(id=ident, replay_result=report, updated_at=now()))
        return report

    async def apply(self, ident, confirm_regressions=False, actor='api'):
        async with self.lock:
            change = self.get(ident)
            if change['status'] != 'draft':
                raise ValueError('Only drafts may be applied')
            if change['changes_devices'] and not change['allow_device_actions']:
                raise ValueError('Changes devices: allow_device_actions is required')
            precheck = change.get('precheck') or {}
            if precheck.get('applicable') and precheck.get('passed') is False:
                raise ValueError('Offline pre-check failed: fix the sentences or tests in the draft before applying')
            before = await self.snapshot(change)
            after = self.desired(change, before)
            try:
                baseline = (await self.replay_matches())[2]
            except HAError:
                baseline = None
            if change['type'] == 'custom_sentence':
                self.db.save('changes', dict(id=ident, sentence_before=self.sentences.blocks(),
                    sentence_after=sentences.desired(self.sentences.blocks(), change['payload'], ident)))
            self.db.save('changes', dict(id=ident, before=before, after=after, status='applying', updated_at=now()))
            self.db.log(ident, 'apply_started', actor, {})
            try:
                await self.write(change, after)
                deadline = time.monotonic() + self.settings.reload_timeout
                while True:
                    try:
                        test = await self.test(dict(change, after=after))
                    except HAError:
                        test = dict(passed=False, failures=['Matcher unavailable during reload'], results=[])
                    if test['passed'] or time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0.2)
                self.db.save('changes', dict(id=ident, test_result=test))
                self.db.log(ident, 'test', actor, test)
                if not test['passed']:
                    raise ValueError('Change tests failed')
                self.db.save('changes', dict(id=ident, status='applied', updated_at=now()))
                replay = await self.replay(ident, baseline=baseline)
                self.db.log(ident, 'replay', actor, replay)
                if replay['lost'] and not confirm_regressions:
                    raise ValueError('Regressions found; restored backup. Review lost matches and create a confirmed draft to keep them.')
                self.db.log(ident, 'applied', actor, {})
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) else 'HA operation failed'
                try:
                    await self.write(change, before)
                    self.db.log(ident, 'auto_rollback', actor, {'restored': True})
                except Exception:
                    reason += '; backup restore failed: retry rollback'
                    self.db.log(ident, 'auto_rollback', actor, {'restored': False})
                self.db.save('changes', dict(id=ident, status='failed', error=reason, updated_at=now()))
                self.db.log(ident, 'failed', actor, {'error': reason})
            return self.get(ident)

    async def rollback(self, ident, force=False, actor='api'):
        async with self.lock:
            change = self.get(ident)
            if change['status'] not in {'applied', 'failed', 'applying'} or not any(l['action'] == 'apply_started' for l in change['log']):
                raise ValueError('Change has no applied backup')
            later = self.db.rows("SELECT * FROM changes WHERE status IN ('applied','applying') AND id!=?", (ident,))
            applied_at = next(l['at'] for l in change['log'] if l['action'] == 'apply_started')
            if not force and any(target(c) == target(change) and any(l['at'] > applied_at for l in self.db.rows("SELECT at FROM changes_log WHERE change_id=? AND action='apply_started'", (c['id'],))) for c in later):
                raise ValueError('Roll back the latest applied change on this target first')
            await self.write(change, change['before'])
            self.db.save('changes', dict(id=ident, status='rolled_back', updated_at=now()))
            self.db.log(ident, 'rolled_back', actor, {'force': force})
            return self.get(ident)
