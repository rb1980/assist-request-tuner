import asyncio
from app import __version__
import copy
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
import pytest
from jinja2 import Environment
from app.changes import changes_devices
from app.db import DB
from app.diagnose import MODEL_TOOLS, DiagnoseInput
from app.explain import alias_suggestions, near_miss_text
from app.main import create_app
from app.config import Settings
from app.phrasing import list_templates, render_draft
from app.precheck import check_template
from conftest import fixture
from test_integration import store

EXPOSED = [dict(entity_id='light.couch', name='Sofa Lamp', aliases=['reading lamp'], area='play_room', domain='light', exposed=True),
           dict(entity_id='climate.house', name='Thermostat', aliases=[], domain='climate', exposed=True)]
AREAS = [dict(area_id='play_room', name='Play Room', aliases=[])]
NEAR = dict(match=False, source='builtin', intent={'name': 'HassLightSet'},
            sentence_template='[<set>] [[the] color of] [<the>] {name} to {color}', unmatched_slots={'name': 'playroom lights '})


def response(*calls, usage=None):
    return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
        {'id': f'call_{i}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        for i, (name, args) in enumerate(calls)]}}], 'usage': usage or {'prompt_tokens': 100, 'completion_tokens': 50}}


def submission(fixes=None, working=None):
    return dict(summary='The area spelling and sentence shape missed locally.',
                why_missed='Playroom needs an area alias. HA has no built-in area-lights-color sentence.',
                working_phrasings=working or [], fixes=fixes or [], notes='Review drafts before applying.')


def configure(stack, fake_openai):
    settings, _, _, _, service = stack
    settings.openrouter_api_key = 'fake-provider-placeholder'
    settings.llm_base_url = fake_openai.url
    return service.diagnose


def test_model_tool_allowlist_has_no_write_tool():
    names = {t['function']['name'] for t in MODEL_TOOLS}
    assert names == {'test_sentences', 'check_template', 'find_entities', 'list_areas', 'list_intents',
                     'similar_requests', 'list_phrasing_templates', 'submit_diagnosis'}
    assert not names & {'apply_change', 'create_change', 'rollback_change', 'update_change', 'call_service', 'conversation/process', 'assist_pipeline/run'}
    submit = next(t['function']['parameters'] for t in MODEL_TOOLS if t['function']['name'] == 'submit_diagnosis')
    assert 'ChangeDraft' not in submit['$defs']
    assert len(submit['$defs']['ProposedFix']['properties']['change']['examples']) == 6


def test_thermostat_recipe_still_matches_literal_sentences():
    draft = render_draft('thermostat_status', {'entity_id': 'climate.house'}, EXPOSED, AREAS)
    config = draft['payload']['inputs']
    assert check_template(config['sentences'], ['what is the thermostat set to', 'is the air conditioning running'])['passed']
    assert draft['type'] == 'blueprint_instance'
    assert all(word in json.dumps(config) for word in ['temperature', 'current_temperature', 'hvac_action'])


def test_alias_rule_and_plain_english_near_miss():
    hints = alias_suggestions(NEAR, EXPOSED, AREAS)
    assert hints[0]['slot'] == 'area'
    assert hints[0]['text'] == 'playroom'
    assert hints[0]['targets'][0]['area_id'] == 'play_room'
    assert 'HassLightSet' in near_miss_text(NEAR)
    assert "'playroom lights'" in near_miss_text(NEAR)
    assert 'exposed entity' in near_miss_text(NEAR)
    assert near_miss_text(None) == 'No built-in sentence is close.'
    near = dict(NEAR, unmatched_slots={'name': 'please turn everything over there blue right now lights'})
    assert not alias_suggestions(near, EXPOSED, AREAS)
    hidden = [dict(EXPOSED[0], exposed=False)]
    assert not alias_suggestions(dict(NEAR, unmatched_slots={'name': 'Sofa Lamp'}), hidden, AREAS)
    assert not alias_suggestions(dict(NEAR, unmatched_slots={'area': 'xyzzy'}), EXPOSED, AREAS)
    both = alias_suggestions(NEAR, [dict(EXPOSED[0], name='Playroom')], AREAS)
    assert {hint['slot'] for hint in both} == {'name', 'area'}
    assert sum(len(hint['targets']) for hint in both) <= 3


def test_hassil_slots_failures_and_validation():
    result = check_template(['set {area} lights to {color}'], ['set playroom lights to blue', 'unrelated sentence'])
    assert result['results'][0]['slots'] == {'area': 'playroom', 'color': 'blue'}
    assert result['matched'] == 1 and not result['passed']
    assert not check_template(['set [the'], ['set the'])['passed']
    assert check_template(['set {color}'], ['set blue'], {'color': ['blue', 'red']})['passed']
    assert not check_template(['set {color}'], ['set green'], {'color': ['blue', 'red']})['passed']
    with pytest.raises(ValueError):
        check_template([], ['hello'])


def test_diagnose_settings_defaults_and_target_validation(monkeypatch):
    for name in ['DIAGNOSE_PROVIDER', 'DIAGNOSE_MODEL', 'DIAGNOSE_MAX_STEPS', 'DIAGNOSE_TIMEOUT_S', 'DIAGNOSE_DAILY_USD', 'NIGHTLY_DIAGNOSE']:
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.diagnose_provider == {'zdr': True}
    assert settings.diagnose_model == 'qwen/qwen3.8-27b'
    assert settings.diagnose_max_steps == 12 and settings.diagnose_timeout == 300 and settings.diagnose_reasoning == 'medium'
    assert settings.diagnose_daily_usd == 1 and not settings.nightly_diagnose
    with pytest.raises(ValueError):
        DiagnoseInput(request_id=1, pattern='both')
    with pytest.raises(ValueError):
        DiagnoseInput()


def test_v1_migration_preserves_existing_changes(tmp_path):
    path = str(tmp_path / 'migration.sqlite')
    db = DB(path)
    db.execute("INSERT INTO changes(id,title,status) VALUES (1,'preserve me','draft')")
    for name in ['diagnosis_id', 'source', 'precheck']:
        db.execute('ALTER TABLE changes DROP COLUMN ' + name)
    db.meta('schema_version', '1')
    db.conn.close()
    db = DB(path)
    assert db.meta('schema_version') == '5'
    row = db.rows('SELECT * FROM changes WHERE id=1')[0]
    assert row['title'] == 'preserve me' and row['source'] == 'manual'
    assert row['precheck'] is None and row['diagnosis_id'] is None
    db.conn.close()


async def test_draft_create_edit_and_existing_commands_are_prechecked(stack):
    _, _, _, changes, _ = stack
    draft = render_draft('thermostat_status', {'entity_id': 'climate.house'}, EXPOSED, AREAS)
    draft['payload']['inputs']['entity'] = 'climate.thermostat'
    row = await changes.create(draft)
    assert row['precheck']['passed']
    edited = await changes.update(row['id'], {'tests': {'must_match': [{'sentence': 'not matching'}]}})
    assert not edited['precheck']['passed']
    aid = fixture('automation_config_voice')['config']['id']
    row = await changes.create(dict(type='trigger_sentences', title='new commands', payload={'automation_id': aid, 'add': ['how is the den']},
        tests={'must_match': [{'sentence': 'how is the den'}, {'sentence': 'what is the temperature in the bedroom'}]}))
    assert row['precheck']['passed'] and row['precheck']['matched'] == 2
    edited = await changes.update(row['id'], {'payload': {'automation_id': aid, 'remove': ['how is the den']}})
    assert not edited['precheck']['passed']


async def test_playroom_diagnosis_end_to_end_creates_only_drafts(stack, fake_ha, fake_openai, monkeypatch):
    settings, db, ha, _, service = stack
    agent = configure(stack, fake_openai)
    fake_ha.entities['light.test_lamp']['area_id'] = 'play_room'
    fake_ha.exposed['light.test_lamp'] = {'conversation': True}
    transcript = 'Set the playroom lights to blue.'
    ident = store(db, text=transcript, pattern='set playroom lights to blue', matcher=NEAR,
                  tool={'name': 'assist__light__HassLightSet', 'args': {'name': 'Sofa Lamp', 'area': 'Play Room', 'color': 'blue'}, 'result': None})
    works = ['set the lights in the play room to blue', 'turn the play room blue']
    for sentence in works:
        fake_ha.matches[sentence] = dict(match=True, source='builtin', intent={'name': 'HassLightSet'})
    alias = dict(type='area_alias', title='Playroom alias', payload={'area_id': 'play_room', 'add': ['playroom']},
                 tests={'must_match': [{'sentence': 'set the lights in playroom to blue'}]})
    template = render_draft('area_light_color', {'sentences': ['set the play room lights to blue']}, EXPOSED, AREAS)
    fixes = [dict(title='Area spelling', explanation='STT writes one word.', kind='area_alias', change=alias, risk='low'),
             dict(title='Area-wide color', explanation='Target all lights in the spoken area.', kind='template', change={'template_id': 'area_light_color', 'params': {'sentences': ['set the play room lights to blue']}}, risk='high'),
             dict(title='Invalid', explanation='Must not be staged.', kind='alias', change={'type': 'unsafe'}, risk='low')]
    fake_openai.scripts = [response(('test_sentences', {'sentences': works + ['fake working phrase']}), ('list_areas', {}),
                                    ('find_entities', {'query': 'lamp'}), ('list_phrasing_templates', {})),
                          response(('check_template', {'templates': ['set [the] {area} lights to {color}'], 'sentences': [transcript]}),
                                   ('similar_requests', {'text_or_pattern': 'set playroom lights to blue'}), ('list_intents', {})),
                          response(('submit_diagnosis', submission(fixes, works + ['fake working phrase']))),
                          response(('submit_diagnosis', submission(fixes, works + ['fake working phrase'])))]
    job = agent.enqueue(request_id=ident)
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done', result
    assert result['result']['working_phrasings'] == works
    drafts = db.rows('SELECT * FROM changes')
    assert len(drafts) == 2
    assert all(d['status'] == 'draft' and d['diagnosis_id'] == job['id'] and d['source'] == 'diagnosis' for d in drafts)
    assert not any(d['allow_device_actions'] for d in drafts)
    assert drafts[0]['precheck']['applicable'] is False  # Aliases need live HA tests after apply.
    assert drafts[1]['precheck']['passed']
    assert 'validation_error' in result['result']['fixes'][2]
    assert all(log['actor'] == 'diagnose' for log in db.rows('SELECT * FROM changes_log'))
    assert not any(c['type'].endswith('/update') or c['type'] == 'homeassistant/expose_entity' for c in fake_ha.calls)
    assert fake_ha.areas[6]['aliases'] == []
    assert len(fake_ha.automations) == 1
    assert result['tokens_in'] == 400 and result['tokens_out'] == 200
    assert result['cost_estimate'] == pytest.approx((400 * .2 + 200 * 2.5) / 1_000_000)
    assert all(call['provider'] == {'zdr': True} and call['model'] == settings.diagnose_model for call in fake_openai.calls)
    assert set(fake_openai.user_agents) == {'ar-tuner/' + __version__}
    assert all(call['tools'] == MODEL_TOOLS for call in fake_openai.calls)
    prompt = json.loads(fake_openai.calls[0]['messages'][1]['content'])
    from app.diagnose import mark_untrusted_data
    assert prompt['requests']['tools'][0]['args']['name'] == mark_untrusted_data('Sofa Lamp')
    assert all(set(e) == {'entity_id', 'name', 'aliases', 'area_id'} for e in prompt['exposed_entities'])
    assert 'untrusted DATA' in fake_openai.calls[0]['messages'][0]['content']
    assert result['steps']


async def test_unoffered_write_tool_is_refused_and_working_phrases_reverified(stack, fake_ha, fake_openai):
    agent = configure(stack, fake_openai)
    _, db, _, _, _ = stack
    ident = store(db, text='need local')
    fake_ha.matches['works once'] = {'match': True, 'source': 'builtin'}
    fake_openai.scripts = [response(('apply_change', {'id': 1}), ('test_sentences', {'sentences': ['works once']})),
                          response(('submit_diagnosis', submission(working=['works once', 'never tested'])))]
    original = fake_ha.match
    counts = {}
    def transient(sentence):
        counts[sentence] = counts.get(sentence, 0) + 1
        return original(sentence) if counts[sentence] == 1 else None
    fake_ha.match = transient
    job = agent.enqueue(request_id=ident)
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done'
    assert result['result']['working_phrasings'] == []
    assert 'Unknown read-only tool' in result['steps'][0]['result']
    assert not db.rows('SELECT * FROM changes')


async def test_disabled_and_budget_refusal(stack, fake_openai):
    settings, db, _, _, service = stack
    ident = store(db)
    with pytest.raises(ValueError, match='AI analysis is off'):
        service.diagnose.enqueue(request_id=ident)
    agent = configure(stack, fake_openai)
    settings.diagnose_daily_usd = .00001
    fake_openai.scripts = [response(('submit_diagnosis', submission()), usage={'prompt_tokens': 1000, 'completion_tokens': 1000})]
    job = agent.enqueue(request_id=ident)
    queued = agent.enqueue(request_id=ident)
    await agent.run_job(job['id'])
    assert agent.budget_used() > settings.diagnose_daily_usd
    with pytest.raises(ValueError, match='budget'):
        agent.enqueue(request_id=ident)
    await agent.run_job(queued['id'])
    assert agent.get(queued['id'])['status'] == 'failed'
    assert len(fake_openai.calls) == 1
    settings.diagnose_daily_usd = 0   # 0 means no limit, e.g. a free local model
    assert agent.enqueue(request_id=ident)['status'] == 'queued'


@pytest.mark.parametrize('mode', ['steps', 'timeout'])
async def test_model_limits_persist_failure_and_usage(stack, fake_openai, mode):
    settings, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    settings.diagnose_max_steps = 2
    if mode == 'timeout':
        settings.diagnose_timeout = .03
        fake_openai.delay = .15
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'failed'
    assert ('maximum steps' if mode == 'steps' else 'timed out') in result['error']
    if mode == 'steps':
        assert len(fake_openai.calls) == 2 and result['tokens_in'] == 20


async def test_queue_runs_one_job_at_a_time_and_provider_override(stack, fake_openai, monkeypatch):
    settings, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    settings.diagnose_provider = {'zdr': True, 'order': ['fake-provider']}
    fake_openai.delay = .02
    fake_openai.scripts = [response(('submit_diagnosis', submission())), response(('submit_diagnosis', submission()))]
    ident = store(db)
    jobs = [agent.enqueue(request_id=ident) for _ in range(2)]
    agent.start()
    for _ in range(200):
        if all(agent.get(j['id'])['status'] == 'done' for j in jobs):
            break
        await asyncio.sleep(.01)
    assert all(agent.get(j['id'])['status'] == 'done' for j in jobs)
    assert fake_openai.max_active == 1
    assert fake_openai.calls[0]['provider'] == settings.diagnose_provider


async def test_invalid_fixes_remain_visible_without_drafts(stack, fake_openai):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    invalid = render_draft('thermostat_status', {'entity_id': 'climate.house'}, EXPOSED, AREAS)
    invalid['tests']['must_match'] = [{'sentence': 'does not match'}]
    no_tests = dict(type='area_alias', title='Missing tests', payload={'area_id': 'play_room', 'add': ['playroom']})
    fake_openai.scripts = [response(('submit_diagnosis', submission([
        dict(title='No match', explanation='Bad template', risk='high', kind='trigger_automation', change=invalid),
        dict(title='No tests', explanation='Bad tests', risk='low', kind='area_alias', change=no_tests)])))]
    fake_openai.scripts *= 2
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done'
    assert all(f['validation_error'] for f in result['result']['fixes'])
    assert not db.rows('SELECT * FROM changes')


async def test_nightly_top_five_once_per_local_day(stack, fake_openai):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    for i in range(7):
        store(db, text=f'home {i}', pattern=f'home {i}')
    await agent.nightly_tick(datetime(2026, 11, 3, 2, 59, tzinfo=ZoneInfo('America/Los_Angeles')))
    assert not agent.list()
    moment = datetime(2026, 11, 3, 3, 0, tzinfo=ZoneInfo('America/Los_Angeles'))
    await agent.nightly_tick(moment)
    assert len(agent.list()) == 5
    await agent.nightly_tick(moment)
    assert len(agent.list()) == 5


async def test_v2_rest_ui_forms_and_mcp(fake_ha, fake_openai, tmp_path):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', openrouter_api_key='provider-placeholder',
                        ha_url=fake_ha.url, llm_base_url=fake_openai.url, db_path=str(tmp_path / 'ui.sqlite'))
    fake_ha.exposed['light.test_lamp'] = {'conversation': True}
    fake_ha.entities['light.test_lamp']['area_id'] = 'play_room'
    fake_openai.scripts = [response(('submit_diagnosis', submission()))]
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        ident = store(app.state.service.db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver', follow_redirects=True) as client:
            for path in ['/api/v1/templates', '/api/v1/diagnoses', '/api/v1/settings']:
                assert (await client.get(path)).status_code == 401
            headers = {'Authorization': 'Bearer ui-placeholder'}
            templates = await client.get('/api/v1/templates', headers=headers)
            assert len(templates.json()) == 4
            checked = await client.post('/api/v1/check-template', headers=headers, json={'templates': ['hello {name}'], 'sentences': ['hello friend']})
            assert checked.json()['results'][0]['slots'] == {'name': 'friend'}
            job = await client.post('/api/v1/diagnose', headers=headers, json={'request_id': ident})
            assert job.status_code == 202
            job_id = job.json()['id']
            for _ in range(100):
                job = (await client.get(f'/api/v1/diagnoses/{job_id}', headers=headers)).json()
                if job['status'] == 'done':
                    break
                await asyncio.sleep(.01)
            assert job['status'] == 'done', job
            assert (await client.get('/api/v1/diagnoses', headers=headers)).json()[0]['id'] == job_id
            created = await client.post('/api/v1/templates/area_light_color/draft', headers=headers, json={'params': {}})
            assert created.status_code == 201
            assert created.json()['precheck']['passed'] and created.json()['source'] == 'template'
            assert not created.json()['allow_device_actions']
            await client.post('/login', data={'token': 'ui-placeholder'})
            for path in ['/settings', '/templates', '/make-local', f'/requests/{ident}', '/candidates', f'/diagnoses/{job_id}', '/changes/' + str(created.json()['id'])]:
                page = await client.get(path)
                assert page.status_code == 200, (path, page.text)
                assert settings.openrouter_api_key not in page.text
                if path == '/templates':
                    assert 'live area, entity and value lists' in page.text
            assert (await client.get(f'/ui/diagnoses/{job_id}')).json()['status'] == 'done'
            alias = await client.post('/ui/make-local/alias', data={'target': 'area_alias:play_room', 'alias': 'playroom', 'sentence': 'turn on lights in playroom'})
            assert alias.status_code == 200 and '/changes/' in str(alias.url)
            aid = fixture('automation_config_voice')['config']['id']
            sentence = await client.post('/ui/make-local/sentences', data={'automation_id': aid, 'commands': 'hello house', 'sentences': 'hello house'})
            assert sentence.status_code == 200 and 'Pre-check passed: 1/1' in sentence.text
            hidden = await client.post('/ui/make-local/alias', data={'target': 'entity_alias:sensor.sun_next_dawn', 'alias': 'sun', 'sentence': 'sun please'})
            assert hidden.status_code == 400
            template_form = await client.post('/ui/templates/area_light_color/draft', data={})
            assert template_form.status_code == 200 and 'Pre-check passed: 1/1' in template_form.text
            mh = dict(headers, Accept='application/json, text/event-stream')
            names = (await client.post('/mcp/', headers=mh, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})).json()
            assert {t['name'] for t in names['result']['tools']} >= {'diagnose_request', 'diagnose_pattern', 'get_diagnosis', 'list_diagnoses', 'check_template', 'list_phrasing_templates', 'create_change_from_template'}
            for name, arguments in [('get_diagnosis', {'id': job_id}), ('list_diagnoses', {}), ('list_phrasing_templates', {}),
                                    ('check_template', {'templates': ['hello {name}'], 'sentences': ['hello friend']}),
                                    ('create_change_from_template', {'template_id': 'area_light_color', 'params': {}})]:
                result = (await client.post('/mcp/', headers=mh, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})).json()
                assert not result['result'].get('isError'), result
            fake_openai.scripts.extend([response(('submit_diagnosis', submission())) for _ in range(3)])
            launched = await client.post('/ui/diagnose', data={'pattern': 'dim lamp'})
            assert launched.status_code == 200 and '/diagnoses/' in str(launched.url)
            for name, arguments in [('diagnose_request', {'request_id': ident}), ('diagnose_pattern', {'pattern': 'dim lamp'})]:
                result = (await client.post('/mcp/', headers=mh, json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})).json()
                assert not result['result'].get('isError'), result
            for _ in range(100):
                if all(j['status'] == 'done' for j in app.state.service.diagnose.list()):
                    break
                await asyncio.sleep(.01)
            assert all(j['status'] == 'done' for j in app.state.service.diagnose.list())


async def test_diagnose_rejects_large_tool_batch_and_redacts_provider_secrets(stack, fake_openai, caplog):
    settings, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    fake_openai.scripts = [response(('test_sentences', {'sentences': ['one'] * 26})),
                          response(('submit_diagnosis', submission()))]
    with caplog.at_level(logging.INFO):
        job = agent.enqueue(request_id=store(db))
        await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done'
    assert 'error' in result['steps'][0]['result']
    assert settings.openrouter_api_key not in caplog.text
    assert 'change_schema' not in caplog.text


async def test_model_cannot_grant_device_authorization(stack, fake_openai, fake_ha):
    _, db, _, changes, _ = stack
    agent = configure(stack, fake_openai)
    fake_ha.entities['light.test_lamp']['area_id'] = 'play_room'
    fake_ha.exposed['light.test_lamp'] = {'conversation': True}
    draft = dict(type='trigger_automation', title='Color', payload={'automation_id': 'ar_tuner_devices', 'config': {'triggers': [{'trigger': 'conversation', 'command': ['paint the lamp']}], 'actions': [{'action': 'light.turn_on'}]}}, tests={'must_match': [{'sentence': 'paint the lamp'}]}, allow_device_actions=True)
    fix = dict(title='Color', explanation='Change area lights', kind='trigger_automation', change=draft, risk='high')
    fake_openai.scripts = [response(('submit_diagnosis', submission([fix])))] * 2
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['result']['fixes'][0]['validation_error']
    assert not db.rows('SELECT * FROM changes')


async def test_template_rejects_hidden_entities_and_unknown_targets(stack):
    _, _, _, _, service = stack
    with pytest.raises(ValueError, match='exposed'):
        await service.diagnose.create_from_template('thermostat_status', {'entity_id': 'light.test_lamp'})
    with pytest.raises(ValueError, match='Unknown template parameters'):
        await service.diagnose.create_from_template('area_light_color', {'area_id': 'not-an-area'})


async def test_restarted_worker_marks_interrupted_job_failed(stack, fake_openai):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    job = agent.enqueue(request_id=store(db))
    db.save('diagnoses', dict(id=job['id'], status='running'))
    agent.start()
    assert agent.get(job['id'])['status'] == 'failed'
    assert 'restart' in agent.get(job['id'])['error']
