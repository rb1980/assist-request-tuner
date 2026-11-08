import types
import copy
import json
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from app.collector import Collector
from app.parse import parse_run, now
from app.patterns import normalize
from app import analysis
from app.main import create_app
from app.config import Settings
from app.ha import HA
from conftest import fixture


def alias_draft(**extra):
    return dict(type='entity_alias', title='Reading glow', payload={'entity_id': 'light.test_lamp', 'add': ['reading glow']}, tests={'must_match': [{'sentence': 'turn on reading glow', 'intent': 'HassTurnOn', 'source': 'builtin'}]}, **extra)


def store(db, text='dim the test lamp to thirty percent', local=False, source='voice', matcher=None, pattern='dim lamp', tool=None):
    row = parse_run(fixture('run_text_llm_tool' if not local else 'run_voice'))
    row.update(pipeline_run_id=str(db.rows('SELECT count(*) AS n FROM requests')[0]['n']), pipeline_id='house', pipeline_name='House',
               started_at=now(), transcript=text, normalized=normalize(text), pattern=pattern, excluded=False,
               handled_by='local_intent' if local else 'llm', topic='home', matcher=matcher, source=source)
    if tool:
        row['tools'] = [tool]
    db.store_run(row)
    return db.rows('SELECT id FROM requests ORDER BY id DESC')[0]['id']


async def test_forbidden_message_types(stack):
    _, _, ha, _, _ = stack
    for kind in ['conversation/process', 'assist_pipeline/run', 'call_service']:
        with pytest.raises(ValueError, match='forbidden'):
            await ha.call(kind, text='do something')
    assert ha.ws is None


async def test_collector_import_dedupe_and_batch(stack, fake_ha):
    settings, db, ha, _, _ = stack
    collector = Collector(db, ha, settings)
    await collector.poll()
    rows = db.rows('SELECT * FROM requests')
    assert len(rows) == 2
    assert {r['handled_by'] for r in rows} == {'local_intent', 'llm'}
    assert all(r['raw_events'] for r in rows)
    assert len([c for c in fake_ha.calls if c['type'].endswith('/debug')]) == 1
    before = len([c for c in fake_ha.calls if c['type'].endswith('pipeline_debug/get')])
    await collector.poll()
    assert db.rows('SELECT count(*) AS n FROM requests')[0]['n'] == 2
    assert len([c for c in fake_ha.calls if c['type'].endswith('pipeline_debug/get')]) == before


async def test_in_progress_refetch(stack, fake_ha):
    settings, db, ha, _, _ = stack
    row = fixture('run_voice')
    for event in row['events']:
        event['timestamp'] = now()
    complete = copy.deepcopy(row)
    row['events'] = row['events'][:-1]
    fake_ha.runs = {'active': row}
    collector = Collector(db, ha, settings)
    await collector.poll()
    assert db.rows('SELECT status FROM requests')[0]['status'] == 'in_progress'
    fake_ha.runs['active'] = complete
    await collector.poll()
    assert db.rows('SELECT status FROM requests')[0]['status'] == 'complete'


async def test_ignore_window_and_overrides(stack, fake_ha):
    settings, db, ha, _, service = stack
    fake_ha.runs = {'recent': fixture('run_voice')}
    for e in fake_ha.runs['recent']['events']:
        e['timestamp'] = now()
    service.ignore(30, 'eval')
    # Window covers collection timestamps generated after it starts.
    for e in fake_ha.runs['recent']['events']:
        e['timestamp'] = now()
    await Collector(db, ha, settings).poll()
    row = db.rows('SELECT * FROM requests')[0]
    assert row['excluded']
    assert analysis.stats(db)['requests'] == 0
    service.ignore(stop=True)
    assert service.ignore() == []
    ident = store(db)
    service.set_pattern('dim lamp', {'topic_override': 'general'})
    assert not analysis.candidates(db, ha)
    service.set_topic(ident, 'home')
    assert analysis.candidates(db, ha)[0]['count'] == 1


async def test_apply_and_exact_rollback(stack, fake_ha):
    _, db, _, changes, _ = stack
    before = copy.deepcopy(fake_ha.entities['light.test_lamp']['aliases'])
    draft = (await changes.create(alias_draft()))
    result = await changes.apply(draft['id'])
    assert result['status'] == 'applied'
    assert result['test_result']['passed']
    assert result['before'] == [None]
    assert fake_ha.entities['light.test_lamp']['aliases'] == [None, 'reading glow']
    result = await changes.rollback(draft['id'])
    assert result['status'] == 'rolled_back'
    assert fake_ha.entities['light.test_lamp']['aliases'] == before
    assert {r['action'] for r in result['log']} >= {'draft', 'test', 'applied', 'rolled_back'}


async def test_failed_test_auto_rollback(stack, fake_ha):
    _, _, _, changes, _ = stack
    draft = alias_draft()
    draft['tests']['must_match'][0]['sentence'] = 'never matches'
    result = await changes.apply((await changes.create(draft))['id'])
    assert result['status'] == 'failed'
    assert fake_ha.entities['light.test_lamp']['aliases'] == [None]
    assert 'never matches' in result['test_result']['failures']
    assert any(l['action'] == 'auto_rollback' for l in result['log'])


async def test_negative_test_auto_rollback(stack, fake_ha):
    _, _, _, changes, _ = stack
    draft = alias_draft()
    draft['tests']['must_not_match'] = ['turn on reading glow']
    result = await changes.apply((await changes.create(draft))['id'])
    assert result['status'] == 'failed'
    assert fake_ha.entities['light.test_lamp']['aliases'] == [None]


async def test_rollback_order(stack):
    _, _, _, changes, _ = stack
    one = await changes.apply((await changes.create(alias_draft()))['id'])
    two_draft = alias_draft()
    two_draft['payload']['add'] = ['second name']
    two = await changes.apply((await changes.create(two_draft))['id'])
    with pytest.raises(ValueError, match='latest'):
        await changes.rollback(one['id'])
    await changes.rollback(two['id'])
    await changes.rollback(one['id'])


async def test_replay_gain_loss_and_confirmation(stack, fake_ha):
    _, db, _, changes, _ = stack
    store(db, text=fake_ha.alias_sentence)
    store(db, text='old local', local=True, matcher=fixture('matcher_debug')['results'][1])
    # Local just before the change, unmatched after it: a real regression.
    fake_ha.matches['old local'] = fixture('matcher_debug')['results'][1]
    fake_ha.matches_after_alias['old local'] = None
    draft = (await changes.create(alias_draft()))
    with pytest.raises(ValueError, match='Apply first'):
        await changes.replay(draft['id'])
    result = await changes.apply(draft['id'])
    assert result['status'] == 'failed'
    report = result['replay_result']
    assert report['newly_local'][0]['sentence'] == fake_ha.alias_sentence
    assert report['lost'][0]['sentence'] == 'old local'
    assert report['coverage_before'] == .5
    assert report['coverage_after'] == .5
    assert fake_ha.entities['light.test_lamp']['aliases'] == [None]
    result = await changes.apply((await changes.create(alias_draft()))['id'], confirm_regressions=True)
    assert result['status'] == 'applied'
    assert result['replay_result']['lost']


async def test_area_and_exposure_roundtrip(stack, fake_ha):
    _, _, _, changes, _ = stack
    for kind, payload in [('area_alias', {'area_id': 'living_room', 'add': ['lounge']}), ('expose_entity', {'entity_id': 'light.test_lamp', 'expose': True})]:
        change = (await changes.create(dict(type=kind, title=kind, payload=payload)))
        result = await changes.apply(change['id'])
        assert result['status'] == 'applied'
        await changes.rollback(change['id'])
    assert fake_ha.areas[0]['aliases'] == []
    assert fake_ha.exposed['light.test_lamp']['conversation'] is False


async def test_automation_roundtrip_and_sentence_only_edit(stack, fake_ha):
    _, _, _, changes, _ = stack
    original = fixture('automation_config_voice')['config']
    aid = original['id']
    draft = (await changes.create(dict(type='trigger_sentences', title='add phrase', payload={'automation_id': aid, 'add': ['how warm is it']})))
    result = await changes.apply(draft['id'])
    assert result['status'] == 'applied'
    assert fake_ha.automations[aid]['actions'] == original['actions']
    assert all('how warm is it' in t['command'] for t in fake_ha.automations[aid]['triggers'])
    await changes.rollback(draft['id'])
    assert fake_ha.automations[aid] == original
    new = dict(type='trigger_automation', title='new', payload={'automation_id': 'ar_tuner_example', 'config': {'triggers': [{'trigger': 'conversation', 'command': ['hello home']}], 'actions': [{'set_conversation_response': 'Hello'}]}})
    result = await changes.apply((await changes.create(new))['id'])
    assert result['status'] == 'applied'
    assert fake_ha.automations['ar_tuner_example']['description'] == 'Managed by Assist Request Tuner'
    await changes.rollback(result['id'])
    assert 'ar_tuner_example' not in fake_ha.automations


async def test_device_actions_gate_and_id_prefix(stack):
    _, _, _, changes, _ = stack
    draft = dict(type='trigger_automation', title='action', payload={'automation_id': 'ar_tuner_action', 'config': {'triggers': [{'trigger': 'conversation', 'command': ['hello']}], 'actions': [{'action': 'light.turn_on'}]}})
    with pytest.raises(ValueError, match='allow_device_actions'):
        await changes.apply((await changes.create(draft))['id'])
    draft['allow_device_actions'] = True
    assert (await changes.apply((await changes.create(draft))['id']))['status'] == 'applied'
    draft['payload']['automation_id'] = 'not_prefixed'
    with pytest.raises(ValueError, match='ar_tuner_'):
        await changes.apply((await changes.create(draft))['id'])


async def test_candidate_ranking_and_suggestions(stack):
    _, db, ha, _, service = stack
    ha.entities = [{'name': 'Test Lamp', 'entity_id': 'light.test_lamp', 'aliases': [], 'exposed': True}]
    tool = {'name': 'assist__script__home_readings', 'args': {}, 'result': None}
    for _ in range(3):
        store(db, text='how warm', pattern='how warm', tool=tool)
    near = fixture('matcher_debug')['results'][2]
    near['unmatched_slots'] = {'name': 'test lam'}
    store(db, pattern='dim lamp', matcher=near)
    rows = analysis.candidates(db, ha)
    assert rows[0]['count'] == 3
    assert rows[0]['suggestions'][0]['skeleton']['actions'][0]['set_conversation_response']
    assert rows[1]['suggestions'][0]['targets'][0]['entity_id'] == 'light.test_lamp'
    assert any(h['kind'].startswith('command:') for h in rows[1]['suggestions'])
    service.set_pattern('how warm', {'ignored': True})
    assert analysis.candidates(db, ha)[0]['pattern'] == 'dim lamp'


async def test_api_ui_auth_and_mcp(fake_ha, tmp_path):
    settings = Settings(ha_url=fake_ha.url, ha_token='fake-placeholder', token='test-placeholder', db_path=str(tmp_path / 'api.sqlite'), reload_timeout=0.01)
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            assert (await client.get('/healthz')).status_code == 200
            assert (await client.get('/metrics')).status_code == 200
            for path in ['/api/v1/status', '/api/v1/requests', '/mcp', '/mcp/']:
                assert (await client.get(path)).status_code == 401
            headers = {'Authorization': 'Bearer test-placeholder'}
            await app.state.collector.poll()
            for path in ['/api/v1/status', '/api/v1/requests', '/api/v1/stats', '/api/v1/candidates', '/api/v1/intents', '/api/v1/entities', '/api/v1/areas', '/api/v1/changes', '/api/v1/ignore', '/api/v1/export']:
                response = await client.get(path, headers=headers)
                assert response.status_code == 200, (path, response.text)
            match = await client.post('/api/v1/match', headers=headers, json={'sentences': ["Turn on Sam's office."]})
            assert match.json()['results'][0] == fixture('matcher_debug')['results'][1]
            assert (await client.post('/login', data={'token': 'test-placeholder'})).status_code == 200
            assert (await client.get('/api/v1/status')).status_code == 401
            for path in ['/', '/requests', '/requests/1', '/candidates', '/intents', '/changes', '/changes/new', '/docs']:
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text)
            change = (await client.post('/api/v1/changes', headers=headers, json=alias_draft())).json()
            assert (await client.get('/changes/' + str(change['id']))).status_code == 200
            assert (await client.post('/ui/ignore', headers={'Origin': 'http://evil.invalid'}, data={'minutes': 30})).status_code == 400
            mh = dict(headers, Accept='application/json, text/event-stream')
            response = await client.post('/mcp/', headers=mh, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
            assert response.status_code == 200, response.text
            response = await client.post('/mcp/', headers=mh, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            tools = response.json()['result']['tools']
            assert len(tools) == 28
            assert {t['name'] for t in tools} >= {'list_candidates', 'test_sentences', 'apply_change'}
            response = await client.post('/mcp/', headers=mh, json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'test_sentences', 'arguments': {'sentences': ["Turn on Sam's office."]}}})
            assert not response.json()['result'].get('isError'), response.text
            assert 'HassTurnOn' in response.text


async def test_missing_token_refuses_start(tmp_path):
    app = create_app(Settings(token='', db_path=str(tmp_path / 'unused')))
    with pytest.raises(RuntimeError, match='AR_TUNER_TOKEN'):
        async with app.router.lifespan_context(app):
            pass


async def test_unconfigured_stays_up(tmp_path):
    settings = Settings(token='placeholder', ha_token='', db_path=str(tmp_path / 'unconfigured'))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        await app.state.collector.poll()
        assert app.state.service.status()['collector'] == 'not configured'
        assert app.state.service.ha.ws is None


async def test_matcher_failure_retry_after_durable_capture(stack, fake_ha, monkeypatch):
    settings, db, ha, _, _ = stack
    collector = Collector(db, ha, settings)
    original = ha.match
    async def fail(sentences):
        raise RuntimeError('temporary matcher outage')
    monkeypatch.setattr(ha, 'match', fail)
    with pytest.raises(RuntimeError):
        await collector.poll()
    assert len(db.rows('SELECT * FROM requests')) == 2
    monkeypatch.setattr(ha, 'match', original)
    await collector.poll()
    assert db.rows('SELECT count(*) AS n FROM requests WHERE matcher IS NULL')[0]['n'] == 0
    assert {r['handled_by'] for r in db.rows('SELECT * FROM requests')} == {'local_intent', 'llm'}


async def test_ten_minute_timeout_retention_and_ignore_pipeline(stack, fake_ha, monkeypatch):
    settings, db, ha, _, _ = stack
    # A host up for less than a day (a fresh CI runner): only the collector's clock, not asyncio's.
    monkeypatch.setattr('app.collector.time', types.SimpleNamespace(monotonic=lambda: 100.0))
    settings.retention = 1
    fake_ha.runs = {'old': fixture('run_voice')}
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    for e in fake_ha.runs['old']['events']:
        e['timestamp'] = old_time
    fake_ha.runs['old']['events'] = fake_ha.runs['old']['events'][:1]
    collector = Collector(db, ha, settings)
    await collector.poll()
    await collector.poll()
    assert db.rows('SELECT status,handled_by FROM requests')[0] == {'status': 'complete', 'handled_by': 'error'}
    db.execute('UPDATE requests SET started_at=?', ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),))
    collector.retention_at = 0
    await collector.poll()
    assert not db.rows('SELECT * FROM requests')
    settings.ignore_pipelines = ['House']
    await collector.poll()
    assert not db.rows('SELECT * FROM requests')


async def test_replay_detects_changed_intent_and_batches(stack, fake_ha):
    _, db, _, changes, _ = stack
    for i in range(51):
        store(db, text='local ' + str(i), local=True, matcher=fixture('matcher_debug')['results'][1])
        fake_ha.matches['local ' + str(i)] = fixture('matcher_debug')['results'][1]
        fake_ha.matches_after_alias['local ' + str(i)] = fixture('matcher_debug')['results'][0]
    result = await changes.apply((await changes.create(alias_draft()))['id'], confirm_regressions=True)
    assert len(result['replay_result']['lost']) == 51
    calls = [c for c in fake_ha.calls if c['type'] == 'conversation/agent/homeassistant/debug']
    assert [len(c['sentences']) for c in calls][-2:] == [50, 1]


async def test_stats_voice_exclusion_and_medians(stack):
    _, db, _, _, _ = stack
    store(db, local=True)
    store(db, local=False)
    store(db, local=False, source='text')
    excluded = store(db, local=False)
    db.execute('UPDATE requests SET excluded=1 WHERE id=?', (excluded,))
    report = analysis.stats(db)
    assert report['requests'] == 2
    assert report['home_coverage'] == .5
    assert report['median_local_s'] == pytest.approx(4.114182)
    assert report['median_llm_s'] == pytest.approx(4.059519)
    assert report['per_week']


async def test_raw_events_disabled_and_registry_fixture(stack, fake_ha):
    settings, db, ha, _, _ = stack
    settings.store_raw = False
    await Collector(db, ha, settings).poll()
    assert all(r['raw_events'] is None for r in db.rows('SELECT * FROM requests'))
    lamp = next(e for e in ha.entities if e['entity_id'] == 'light.test_lamp')
    assert lamp['name'] == 'Test Lamp'
    assert lamp['aliases'] == [None]
    assert not lamp['exposed']


async def test_ha_reconnect_and_backoff(stack, fake_ha):
    _, _, ha, _, _ = stack
    await ha.call('assist_pipeline/pipeline/list')
    await ha.ws.close()
    from app.ha import HAError
    with pytest.raises(HAError):
        await ha.call('assist_pipeline/pipeline/list')
    assert ha.ws is None
    assert ha.backoff == 2
    ha.retry_at = 0
    assert (await ha.call('assist_pipeline/pipeline/list'))['pipelines'] == fake_ha.pipelines
    assert ha.backoff == 1


async def test_api_mutations_and_mcp_shared_functions(fake_ha, tmp_path):
    settings = Settings(ha_url=fake_ha.url, ha_token='fake-placeholder', token='test-placeholder', db_path=str(tmp_path / 'writes.sqlite'), reload_timeout=.01)
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', headers={'Authorization': 'Bearer test-placeholder'}) as client:
            r = await client.post('/api/v1/changes', json=alias_draft())
            assert r.status_code == 201
            ident = r.json()['id']
            assert (await client.patch(f'/api/v1/changes/{ident}', json={'title': 'Renamed'})).json()['title'] == 'Renamed'
            assert (await client.post(f'/api/v1/changes/{ident}/apply', json={})).json()['status'] == 'applied'
            assert (await client.delete(f'/api/v1/changes/{ident}')).status_code == 400
            assert (await client.post(f'/api/v1/changes/{ident}/replay', json={'days': 30})).status_code == 200
            assert (await client.post(f'/api/v1/changes/{ident}/rollback', json={})).json()['status'] == 'rolled_back'
            assert (await client.post('/api/v1/ignore', json={'minutes': 30, 'reason': 'eval'})).json()
            assert (await client.delete('/api/v1/ignore')).json() == []
            assert (await client.get('/api/v1/requests/999')).status_code == 404
            assert (await client.post('/api/v1/changes', json={'type': 'invalid'})).status_code == 422
            await app.state.collector.poll()
            assert (await client.patch('/api/v1/requests/1', json={'topic_override': 'general'})).json()['topic_override'] == 'general'
            assert (await client.patch('/api/v1/patterns/hello', json={'ignored': True})).json()['ignored']
            mh = {'Accept': 'application/json, text/event-stream'}
            for tool, arguments in [('list_candidates', {}), ('list_requests', {'limit': 1}), ('get_status', {}), ('find_entities', {'q': 'test lamp'})]:
                response = await client.post('/mcp/', headers=mh, json={'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': tool, 'arguments': arguments}})
                assert not response.json()['result'].get('isError'), response.text


async def test_rest_refuses_unconfigured_without_connection(tmp_path):
    ha = HA(Settings(ha_token='', ha_url='http://192.0.2.1:8123'))
    from app.ha import HAError
    try:
        with pytest.raises(HAError, match='not configured'):
            await ha.rest('GET')
    finally:
        await ha.close()


async def test_restore_failure_keeps_backup_for_manual_retry(stack, fake_ha, monkeypatch):
    _, _, _, changes, _ = stack
    original = changes.write
    async def fail_restore(change, state):
        if state == [None]:
            raise RuntimeError('fake outage during restore')
        await original(change, state)
    monkeypatch.setattr(changes, 'write', fail_restore)
    draft = alias_draft()
    draft['tests']['must_match'][0]['sentence'] = 'missing'
    result = await changes.apply((await changes.create(draft))['id'])
    assert result['status'] == 'failed'
    assert result['before'] == [None]
    assert 'restore failed' in result['error']
    monkeypatch.setattr(changes, 'write', original)
    await changes.rollback(result['id'])
    assert fake_ha.entities['light.test_lamp']['aliases'] == [None]


async def test_request_override_survives_collector_rewrite(stack, fake_ha):
    settings, db, ha, _, service = stack
    collector = Collector(db, ha, settings)
    await collector.poll()
    row = db.rows('SELECT * FROM requests')[0]
    service.set_topic(row['id'], 'general')
    row['topic_override'] = None
    db.store_run(row)
    assert service.get_request(row['id'])['topic_override'] == 'general'


async def test_refresh_uses_one_full_entity_list_and_preserves_fallbacks(stack, fake_ha):
    _, _, ha, _, _ = stack
    for i in range(1200):
        fake_ha.entities[f'sensor.bulk_{i}'] = dict(entity_id=f'sensor.bulk_{i}', original_name=f'Sensor {i}', aliases=[])
    fake_ha.devices.append(dict(id='fallback', name='Device name', name_by_user='User device name', area_id='kitchen'))
    fake_ha.entities['light.fallback'] = dict(entity_id='light.fallback', name=None, original_name=None, aliases=['glow'], device_id='fallback', area_id=None)
    fake_ha.entities['light.named'] = dict(entity_id='light.named', name='User entity name', original_name='Original entity name', device_id='fallback', area_id='bedroom', aliases=[])
    fake_ha.exposed['light.fallback'] = {'conversation': True}
    await ha.refresh()
    kinds = [c['type'] for c in fake_ha.calls]
    assert len(kinds) == 4
    assert kinds.count('config/entity_registry/list') == 1
    assert 'config/entity_registry/get' not in kinds
    assert 'config/entity_registry/list_for_display' not in kinds
    entities = {e['entity_id']: e for e in ha.entities}
    assert entities['light.fallback']['name'] == 'User device name'
    assert entities['light.fallback']['area'] == 'kitchen'
    assert entities['light.fallback']['aliases'] == ['glow']
    assert entities['light.fallback']['exposed'] is True
    assert entities['light.named']['name'] == 'User entity name'
    assert entities['light.named']['area'] == 'bedroom'
    assert entities['light.test_lamp']['name'] == 'Test Lamp'
    await ha.refresh()
    assert len(fake_ha.calls) == 4
    fake_ha.devices[-1]['name_by_user'] = None
    await ha.refresh(force=True)
    assert next(e for e in ha.entities if e['entity_id'] == 'light.fallback')['name'] == 'Device name'
    assert len(fake_ha.calls) == 8


@pytest.mark.parametrize('exclude_device', [False, True])
async def test_test_named_pipelines_and_devices_require_explicit_exclusion(stack, fake_ha, exclude_device):
    settings, db, ha, _, _ = stack
    voice = fixture('run_voice')
    device_id = next(e['data']['device_id'] for e in voice['events'] if e['type'] == 'intent-start')
    fake_ha.devices.append(dict(id=device_id, name='Test microphone'))
    fake_ha.pipelines[0]['name'] = 'Local + AI (test)'
    fake_ha.runs = {'first': voice}
    settings.exclude_devices = [device_id] if exclude_device else []
    collector = Collector(db, ha, settings)
    await collector.poll()
    # Exercise capture again with the device registry already cached.
    fake_ha.runs['second'] = copy.deepcopy(voice)
    await collector.poll()
    rows = db.rows('SELECT * FROM requests')
    assert len(rows) == 2
    assert all(r['excluded'] is exclude_device for r in rows)
    assert all(r['device_name'] == 'Test microphone' for r in rows)


@pytest.mark.parametrize('ignored', ['house', 'Local + AI (test)'])
async def test_explicit_pipeline_ignore_by_id_or_name(stack, fake_ha, ignored):
    settings, db, ha, _, _ = stack
    fake_ha.pipelines[0]['name'] = 'Local + AI (test)'
    settings.ignore_pipelines = [ignored]
    await Collector(db, ha, settings).poll()
    assert not db.rows('SELECT * FROM requests')
    assert not any(c['type'] == 'assist_pipeline/pipeline_debug/list' for c in fake_ha.calls)


async def test_both_collector_pattern_passes_ignore_hidden_entities(stack, fake_ha, monkeypatch):
    settings, db, ha, _, _ = stack
    fake_ha.entities['sensor.temperature'] = dict(entity_id='sensor.temperature', original_name='Temperature', aliases=['warmth'])
    fake_ha.entities['sensor.outside'] = dict(entity_id='sensor.outside', original_name='Outside', aliases=[])
    fake_ha.exposed['light.test_lamp'] = {'conversation': True}
    row = fixture('run_text_llm_tool')
    for event in row['events']:
        if event['type'] == 'intent-start':
            event['data']['intent_input'] = 'temperature outside test lamp'
    fake_ha.runs = {'pattern': row}
    await ha.refresh()
    match = ha.match
    async def inspect_first_pass(sentences):
        assert db.rows('SELECT pattern FROM requests')[0]['pattern'] == 'temperature {area} {name}'
        return await match(sentences)
    monkeypatch.setattr(ha, 'match', inspect_first_pass)
    await Collector(db, ha, settings).poll()
    assert db.rows('SELECT pattern FROM requests')[0]['pattern'] == 'temperature {area} {name}'


async def test_collector_backlog_batches_and_retains_success_before_failure(stack, fake_ha, monkeypatch):
    settings, db, ha, _, _ = stack
    fake_ha.runs = {}
    for i in range(102):
        ident = store(db, text=f'backlog {i}')
        db.execute('UPDATE requests SET matcher=NULL WHERE id=?', (ident,))
        fake_ha.matches[f'backlog {i}'] = dict(match=True, source='trigger', sentence_template=str(i))
    original = ha.match
    sizes = []
    async def fail_second_batch(sentences):
        sizes.append(len(sentences))
        if len(sizes) == 2:
            raise RuntimeError('temporary matcher outage')
        return await original(sentences)
    monkeypatch.setattr(ha, 'match', fail_second_batch)
    collector = Collector(db, ha, settings)
    with pytest.raises(RuntimeError):
        await collector.poll()
    assert sizes == [50, 50]
    assert db.rows('SELECT count(*) AS n FROM requests WHERE matcher IS NULL')[0]['n'] == 52
    monkeypatch.setattr(ha, 'match', original)
    await collector.poll()
    calls = [c for c in fake_ha.calls if c['type'] == 'conversation/agent/homeassistant/debug']
    assert [len(c['sentences']) for c in calls] == [50, 50, 2]
    for row in db.rows('SELECT * FROM requests'):
        assert row['matcher']['sentence_template'] == row['transcript'].split()[-1]


async def test_non_ar_tuner_automation_replacement_rejected_at_both_layers(stack, fake_ha):
    _, _, _, changes, _ = stack
    original = fixture('automation_config_voice')['config']
    draft = dict(type='trigger_automation', title='Replace existing', payload={'automation_id': original['id'], 'config': copy.deepcopy(original)})
    with pytest.raises(ValueError, match='ar_tuner_'):
        (await changes.create(draft))
    # Also protect old persisted drafts that bypass current model validation.
    with pytest.raises(ValueError, match='ar_tuner_'):
        changes.desired(draft, original)
    assert fake_ha.automations[original['id']] == original
    allowed = (await changes.create(dict(type='trigger_sentences', title='Edit phrases', payload={'automation_id': original['id'], 'add': ['new phrasing']})))
    assert (await changes.apply(allowed['id']))['status'] == 'applied'
    assert fake_ha.automations[original['id']]['actions'] == original['actions']


async def test_managed_automation_replacement_always_sets_description(stack, fake_ha):
    _, _, _, changes, _ = stack
    original = fixture('automation_config_voice')['config']
    original['id'] = 'ar_tuner_existing'
    fake_ha.automations['ar_tuner_existing'] = copy.deepcopy(original)
    replacement = copy.deepcopy(original)
    replacement['description'] = 'Overridden description'
    change = (await changes.create(dict(type='trigger_automation', title='Replace managed', payload={'automation_id': 'ar_tuner_existing', 'config': replacement})))
    applied = await changes.apply(change['id'])
    assert applied['status'] == 'applied'
    assert applied['after']['description'] == 'Managed by Assist Request Tuner'
    await changes.rollback(change['id'])
    assert fake_ha.automations['ar_tuner_existing'] == original


async def test_ha_rejection_error_and_warning_include_redacted_details(stack, fake_ha, caplog):
    from app.ha import HAError
    import logging
    settings, _, ha, _, _ = stack
    fake_ha.errors['config/entity_registry/list'] = {'code': 'unauthorized', 'message': f'Admin required {settings.ha_token} {settings.token}'}
    with caplog.at_level(logging.WARNING, logger='ar_tuner'):
        with pytest.raises(HAError) as exc:
            await ha.refresh()
    assert 'unauthorized' in str(exc.value)
    assert 'Admin required' in str(exc.value)
    assert 'unauthorized' in caplog.text and 'Admin required' in caplog.text
    assert settings.ha_token not in caplog.text + str(exc.value)
    assert settings.token not in caplog.text + str(exc.value)
    assert '[redacted]' in caplog.text


async def test_collector_logs_startup_failure_and_one_successful_import(stack, fake_ha, monkeypatch, caplog):
    import asyncio
    import logging
    settings, db, ha, _, _ = stack
    settings.poll_interval = 3600
    collector = Collector(db, ha, settings)
    original = ha.match
    async def fail(sentences):
        raise RuntimeError(f'matcher unavailable {settings.ha_token}')
    monkeypatch.setattr(ha, 'match', fail)
    with caplog.at_level(logging.INFO, logger='ar_tuner'):
        task = asyncio.create_task(collector.run())
        try:
            for _ in range(100):
                if db.meta('collector_status') == 'HA unavailable; retrying':
                    break
                await asyncio.sleep(.01)
            assert db.meta('collector_status') == 'HA unavailable; retrying'
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert 'Initial import complete' not in caplog.text
        monkeypatch.setattr(ha, 'match', original)
        await collector.poll()
        await collector.poll()
    assert f'HA URL={fake_ha.url}' in caplog.text
    assert 'Pipelines watched: House (house)' in caplog.text
    assert 'Collector poll failed: RuntimeError: matcher unavailable' in caplog.text
    assert caplog.text.count('Initial import complete: 2 runs') == 1
    assert settings.ha_token not in caplog.text


async def test_rest_rejection_logs_redacted_error_details(stack, fake_ha, monkeypatch, caplog):
    import logging
    from app.ha import HAError
    settings, _, ha, _, _ = stack
    async def rejected_request(*args, **kwargs):
        return httpx.Response(403, json={'error': {'code': 'unauthorized', 'message': f'Admin required {settings.ha_token}'}})
    monkeypatch.setattr(ha.http, 'request', rejected_request)
    with caplog.at_level(logging.WARNING, logger='ar_tuner'):
        with pytest.raises(HAError) as exc:
            await ha.rest('GET')
    assert 'HTTP 403' in str(exc.value)
    assert 'unauthorized' in str(exc.value)
    assert 'HAError' in caplog.text
    assert 'Admin required' in caplog.text
    assert settings.ha_token not in caplog.text + str(exc.value)


async def test_replay_ignores_requests_already_local_before_the_change(stack, fake_ha):
    _, db, _, changes, _ = stack
    store(db, text=fake_ha.alias_sentence)
    # Captured as LLM, but an earlier change already made it local: not credited to this change.
    store(db, text='already local now')
    fake_ha.matches['already local now'] = fixture('matcher_debug')['results'][1]
    result = await changes.apply((await changes.create(alias_draft()))['id'])
    assert [r['sentence'] for r in result['replay_result']['newly_local']] == [fake_ha.alias_sentence]


async def test_collector_notices_a_pipeline_created_after_start(stack, fake_ha, caplog):
    import logging
    settings, db, ha, _, _ = stack
    collector = Collector(db, ha, settings)
    with caplog.at_level(logging.INFO, logger='ar_tuner'):
        await collector.poll()
        await collector.poll()   # same pipelines: logged once
        fake_ha.pipelines.append({'id': 'llm', 'name': 'LLM'})
        collector.pipeline_at -= 31   # half a minute later
        await collector.poll()
    assert caplog.text.count('Pipelines watched: House (house)\n') == 1
    assert 'Pipelines watched: House (house), LLM (llm)' in caplog.text
    assert [p['id'] for p in collector.pipelines] == ['house', 'llm']
