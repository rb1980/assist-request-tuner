import copy
import json
import httpx
import pytest
from app.collector import Collector
from app.db import DB
from app.diagnose import FIX_EXAMPLES, SYSTEM_PROMPT, MODEL_TOOLS
from app.ha import HAError
from app.main import create_app
from app.working import candidates, changed, ensure_working
from test_integration import store
from test_v2 import configure, response, submission


def fix(kind, change):
    return dict(title='Proposed fix', explanation='Improve local matching.', kind=kind, change=change, risk='low')


def tool(intent='HassLightSet', **args):
    return dict(name='assist__light__' + intent, args=args, result=None)


async def test_rejected_submission_corrects_shapes_and_stages_once(stack, fake_openai):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    good = copy.deepcopy(FIX_EXAMPLES['area_alias'])
    good['payload']['area_id'] = 'play_room'
    bad = copy.deepcopy(good)
    bad['payload'] = {'area_id': 'play_room', 'aliases': ['playroom']}
    invalid_trigger = copy.deepcopy(FIX_EXAMPLES['blueprint_instance'])
    invalid_trigger['payload'] = invalid_trigger['payload']['inputs']
    fake_openai.scripts = [response(('submit_diagnosis', submission([
        fix('area_alias', bad), fix('blueprint_instance', invalid_trigger), fix('area_alias', good)]))),
        response(('submit_diagnosis', submission([fix('area_alias', good)])))]
    original = agent.validate_submission
    async def validate(arguments):
        assert not db.rows('SELECT * FROM changes')
        return await original(arguments)
    agent.validate_submission = validate
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done'
    assert len(db.rows('SELECT * FROM changes')) == 1
    feedback = json.loads(fake_openai.calls[1]['messages'][-1]['content'])
    assert feedback['accepted'] is False and len(feedback['errors']) == 2
    assert 'Provide add or remove' in feedback['errors'][0]['error']
    assert 'add' in feedback['errors'][0]['correct_shape']['payload']
    assert set(feedback['errors'][1]['correct_shape']['payload']) == {'automation_id', 'alias', 'blueprint', 'inputs'}
    assert len(result['steps']) == 2


async def test_second_rejection_keeps_valid_and_errors_no_duplicates(stack, fake_openai):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    good = FIX_EXAMPLES['area_alias']
    bad = copy.deepcopy(FIX_EXAMPLES['blueprint_instance'])
    bad['tests']['must_match'] = [{'sentence': 'not its trigger'}]
    bad['payload']['inputs']['entity'] = 'climate.thermostat'
    args = submission([fix('area_alias', good), fix('blueprint_instance', bad)])
    fake_openai.scripts = [response(('submit_diagnosis', args))] * 2
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done'
    assert len(fake_openai.calls) == 2 and len(db.rows('SELECT * FROM changes')) == 1
    assert 'pre-check failed' in result['result']['fixes'][1]['validation_error']
    feedback = json.loads(fake_openai.calls[1]['messages'][-1]['content'])
    assert 'pre-check failed' in feedback['errors'][0]['error']


async def test_rejection_consumes_step_budget_without_staging(stack, fake_openai):
    settings, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    settings.diagnose_max_steps = 1
    fake_openai.scripts = [response(('submit_diagnosis', submission([fix('area_alias', {})])))]
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    assert agent.get(job['id'])['status'] == 'failed'
    assert 'maximum steps' in agent.get(job['id'])['error']
    assert not db.rows('SELECT * FROM changes')


async def test_template_reference_recovers_from_handwritten_config(stack, fake_ha, fake_openai):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    fake_ha.entities['light.test_lamp']['area_id'] = 'play_room'
    fake_ha.exposed['light.test_lamp'] = {'conversation': True}
    fake_openai.scripts = [response(('submit_diagnosis', submission([
        fix('template', FIX_EXAMPLES['blueprint_instance'])]))),
        response(('submit_diagnosis', submission([
        fix('template', {'template_id': 'area_light_color', 'params': {'sentences': ['set the play room lights to blue']}})])))]
    fake_openai.scripts.append(copy.deepcopy(fake_openai.scripts[-1]))
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done'
    draft = db.rows('SELECT * FROM changes')[0]
    assert draft['precheck']['passed']
    assert draft['type'] == 'custom_sentence'
    assert draft['payload']['slots'] == {'domain': 'light'}
    assert not draft['allow_device_actions']
    feedback = json.loads(fake_openai.calls[1]['messages'][-1]['content'])
    assert feedback['errors'][0]['correct_shape']['template_id'] == 'area_light_color'
    assert not any(c['type'].endswith('/update') for c in fake_ha.calls)


@pytest.mark.parametrize('intent,args,expected', [
    ('HassLightSet', {'area': 'Play Room', 'name': 'Sofa Lamp', 'color': 'blue'},
     ['set the lights in the play room to blue', 'set the play room to blue', 'turn the play room blue', 'set the sofa lamp to blue']),
    ('HassLightSet', {'area': 'Office', 'brightness': 30}, ['set the lights in the office to 30 percent', 'set the office to 30 percent']),
    ('HassLightSet', {'name': 'Desk Lamp', 'brightness': 0}, ['set the desk lamp to 0 percent']),
    ('HassTurnOn', {'area': 'Office', 'name': 'Desk Lamp'}, ['turn on the office lights', 'turn on the lights in the office', 'turn on the desk lamp']),
    ('HassTurnOff', {'area': 'Office', 'name': 'Desk Lamp'}, ['turn off the office lights', 'turn off the lights in the office', 'turn off the desk lamp']),
    ('HassLightSet', {'name': 'Desk Lamp', 'color': 'red', 'brightness': 30}, []),
    ('HassGetState', {'name': 'Desk Lamp'}, []),
    ('HassLightSet', {'name': 'Desk Lamp', 'brightness': 500}, []),
])
def test_canonical_intent_table(intent, args, expected):
    call = tool(intent, **args)
    assert candidates({'handled_by': 'llm', 'tools': [call, call]}) == expected
    assert candidates({'handled_by': 'local_intent', 'tools': [call]}) == []


async def test_rule_phrasings_without_model_stored_ui_and_context(stack, fake_ha, fake_openai):
    settings, db, ha, _, service = stack
    fake_ha.runs = {}
    ident = store(db, tool=tool(name='Sofa Lamp', area='Play Room', color='blue'))
    good = 'set the lights in the play room to blue'
    fake_ha.matches[good] = {'match': True, 'source': 'builtin', 'intent': {'name': 'HassLightSet'}}
    fake_ha.matches['turn the play room blue'] = {'match': True, 'source': 'trigger'}
    collector = Collector(db, ha, settings)
    await collector.poll()
    assert service.get_request(ident)['working_phrasings'] == [good]
    assert not settings.openrouter_api_key
    calls = len(fake_ha.calls)
    await collector.poll()
    assert len(fake_ha.calls) == calls + 1  # Only the pipeline listing, no repeat matcher work.
    agent = configure(stack, fake_openai)
    fake_openai.scripts = [response(('submit_diagnosis', submission()))]
    job = agent.enqueue(request_id=ident)
    await agent.run_job(job['id'])
    assert agent.get(job['id'])['result']['working_phrasings'] == [good]
    context = json.loads(fake_openai.calls[0]['messages'][1]['content'])
    from app.diagnose import mark_untrusted_data
    assert context['requests']['working_phrasings'] == mark_untrusted_data([good])
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            await client.post('/login', data={'token': settings.token})
            page = await client.get(f'/requests/{ident}')
            assert 'Say this instead, it works now:' in page.text and good in page.text


async def test_working_phrasings_retry_failure_and_batching(stack, monkeypatch):
    _, db, ha, _, service = stack
    ident = store(db)
    db.save('requests', {'id': ident, 'tools': [tool('HassTurnOn', name=f'lamp {i}') for i in range(105)]})
    calls = []
    async def unavailable(sentences):
        raise HAError('offline')
    monkeypatch.setattr(ha, 'match', unavailable)
    with pytest.raises(HAError):
        await ensure_working(db, ha, service.get_request(ident))
    assert service.get_request(ident)['working_phrasings'] is None
    async def match(sentences):
        calls.append(sentences)
        return [{'match': True, 'source': 'builtin'} for s in sentences]
    monkeypatch.setattr(ha, 'match', match)
    assert len(await ensure_working(db, ha, service.get_request(ident))) == 105
    assert [len(c) for c in calls] == [50, 50, 5]


async def test_each_context_request_has_own_changed_targets_and_compact_data(stack):
    _, db, ha, _, service = stack
    ids = [store(db, tool=tool(name=name, area='Play Room', color='blue'))
           for name in ['Sofa Lamp', 'Desk Lamp', 'Floor Lamp']]
    context = await service.diagnose.context({'target_type': 'request', 'target': str(ids[0])})
    assert context['requests']['changed'] == [{'intent': 'HassLightSet', 'target': {'name': 'Sofa Lamp', 'area': 'Play Room'}, 'data': {'color': 'blue'}}]
    assert all(len(r['changed']) == 1 for r in context['similar_requests']['requests'])
    assert {r['changed'][0]['target']['name'] for r in context['similar_requests']['requests']} == {'Sofa Lamp', 'Desk Lamp', 'Floor Lamp'}
    assert 'own tool calls' in SYSTEM_PROMPT and 'never by calls from other requests' in SYSTEM_PROMPT
    assert 'change_schema' not in context
    assert all(set(e) == {'entity_id', 'name', 'aliases', 'area_id'} for e in context['exposed_entities'])
    assert len(context['exposed_entities']) == len([e for e in ha.entities if e['exposed']])
    assert all(set(t) == {'id', 'title', 'params', 'sentences'} for t in context['phrasing_templates'])
    assert await service.diagnose.read_tool('list_phrasing_templates', {}, set()) == context['phrasing_templates']
    entities = await service.diagnose.read_tool('find_entities', {}, set())
    assert all(set(e) == {'entity_id', 'name', 'aliases', 'area_id'} for e in entities)
    assert len(json.dumps(context['phrasing_templates'])) < 2000
    assert 'ChangeDraft' not in json.dumps(MODEL_TOOLS)
    for example in FIX_EXAMPLES.values():
        assert json.dumps(example) in SYSTEM_PROMPT


def test_working_phrasing_migration_preserves_data(tmp_path):
    path = str(tmp_path / 'old.db')
    db = DB(path)
    ident = store(db)
    db.execute('ALTER TABLE requests DROP COLUMN working_phrasings')
    db.conn.close()
    db = DB(path)
    assert db.meta('schema_version') == '5'
    assert db.rows('SELECT * FROM requests WHERE id=?', (ident,))[0]['working_phrasings'] is None
    db.save('requests', {'id': ident, 'working_phrasings': ['works']})
    db.conn.close()
    db = DB(path)
    assert db.rows('SELECT * FROM requests WHERE id=?', (ident,))[0]['working_phrasings'] == ['works']
    db.conn.close()


@pytest.mark.parametrize('effort', ['low', 'default'])
async def test_user_reasoning_setting_preserved(stack, fake_openai, effort, monkeypatch):
    settings, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    settings.diagnose_reasoning = effort
    fake_openai.scripts = [response(('submit_diagnosis', submission()))]
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    assert agent.get(job['id'])['status'] == 'done'
    assert fake_openai.calls[0].get('reasoning') == ({'effort': 'low'} if effort == 'low' else None)


async def test_false_wake_burst_is_shown_to_the_ai(stack, fake_openai):
    # Music picked up turn after turn on one device: the AI sees the neighbours and a burst, and is told not to fix lyrics.
    from datetime import datetime, timedelta, timezone
    settings, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    start = datetime(2026, 11, 7, 1, 3, 10, tzinfo=timezone.utc)
    ids = []
    for i, text in enumerate(['But more hope and then', 'The unfinished', 'Spend a night', 'And love you all the same']):
        ident = store(db, text=text, pattern=f'lyric {i}')
        with db.conn:
            db.conn.execute('UPDATE requests SET started_at=?, device_id=?, conversation_id=? WHERE id=?',
                            ((start + timedelta(seconds=12 * i)).isoformat(), 'kitchen-speaker', 'conv-1', ident))
        ids.append(ident)
    lone = store(db, text='turn the volume down', pattern='volume')
    with db.conn:
        db.conn.execute('UPDATE requests SET started_at=?, device_id=?, conversation_id=? WHERE id=?',
                        ((start - timedelta(minutes=30)).isoformat(), 'kitchen-speaker', 'conv-0', lone))
    burst = agent.neighbours(db.rows('SELECT * FROM requests WHERE id=?', (ids[2],))[0])
    assert burst['burst']['requests'] == 4 and [n['seconds'] for n in burst['nearby']] == [-24, -12, 12]
    assert 'Spend a night' not in [n['transcript'] for n in burst['nearby']]
    alone = agent.neighbours(db.rows('SELECT * FROM requests WHERE id=?', (lone,))[0])
    assert alone == {'nearby': [], 'burst': None}
    assert 'false wake' in SYSTEM_PROMPT and 'wake word sensitivity' in SYSTEM_PROMPT and 'never make phrases from music' in SYSTEM_PROMPT
    fake_openai.scripts = [response(('submit_diagnosis', submission()))]
    job = agent.enqueue(request_id=ids[2])
    await agent.run_job(job['id'])
    context = json.loads(fake_openai.calls[0]['messages'][1]['content'])
    assert context['requests']['burst']['requests'] == 4 and len(context['requests']['nearby']) == 3
