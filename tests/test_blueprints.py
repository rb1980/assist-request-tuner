import copy
import json
from pathlib import Path
import httpx
import pytest
from app.blueprint import BUNDLED, PATH
from app.changes import ChangeDraft
from app.diagnose import FIX_EXAMPLES, SYSTEM_PROMPT
from app.ha import HAError
from app.main import create_app
from app.phrasing import render_draft, THERMOSTAT_REPLY
from test_custom_sentences import setup_writer
from test_integration import store
from test_v2 import configure, response, submission


def draft():
    value = copy.deepcopy(FIX_EXAMPLES['blueprint_instance'])
    value['payload']['inputs']['entity'] = 'climate.thermostat'
    return value


def setup(stack, fake_sftp):
    setup_writer(stack, fake_sftp)
    parent = fake_sftp.root / 'homeassistant/blueprints/automation'
    parent.mkdir(parents=True)
    return parent / 'ar_tuner/voice_reply.yaml'


async def test_sync_absent_identical_differs_and_confinement(stack, fake_sftp, fake_ha, caplog):
    _, db, _, changes, _ = stack
    remote = setup(stack, fake_sftp)
    blueprints = changes.blueprints
    assert (await blueprints.listing())['status'] == 'absent'
    assert (await blueprints.ensure_blueprint())['status'] == 'in sync'
    assert remote.read_bytes() == BUNDLED.encode() and fake_ha.automation_reloads == 1
    count = len([op for op in fake_sftp.operations if op[0] == 'rename'])
    await blueprints.ensure_blueprint()
    assert len([op for op in fake_sftp.operations if op[0] == 'rename']) == count
    assert fake_ha.automation_reloads == 1
    previous = '# blueprint version 0\nold: content\n'
    remote.write_text(previous)
    assert (await blueprints.listing())['remote_version'] == 0
    assert (await blueprints.listing())['status'] == 'differs'
    await blueprints.ensure_blueprint()
    assert db.meta('blueprint_previous') == previous and 'Replacing differing' in caplog.text
    assert fake_ha.automation_reloads == 2 and remote.read_text() == BUNDLED
    managed = '/homeassistant/blueprints/automation/ar_tuner/voice_reply.yaml'
    for op in fake_sftp.operations:
        assert op[1] in ({str(Path(managed).parent)} if op[0] == 'mkdir' else {managed, managed + '.tmp'})
        if op[0] == 'rename':
            assert op[2] == managed
    for name in ['../other', '/homeassistant/automations.yaml', 'voice_reply.yaml']:
        with pytest.raises(ValueError, match='Unknown managed'):
            await blueprints.writer.write('bad', name)


@pytest.mark.parametrize('component', ['blueprints', 'blueprints/automation', 'blueprints/automation/ar_tuner', 'blueprints/automation/ar_tuner/voice_reply.yaml', 'blueprints/automation/ar_tuner/voice_reply.yaml.tmp'])
async def test_blueprint_symlinks_refused(stack, fake_sftp, component):
    setup_writer(stack, fake_sftp)
    target = fake_sftp.root / 'outside'
    target.mkdir()
    path = fake_sftp.root / 'homeassistant' / component
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    with pytest.raises(ValueError, match='symlinks'):
        await stack[3].blueprints.ensure_blueprint()
    assert not list(target.iterdir())


async def test_blueprint_parent_must_exist_and_reload_retries(stack, fake_sftp, monkeypatch):
    setup_writer(stack, fake_sftp)
    _, _, ha, changes, _ = stack
    with pytest.raises(HAError):
        await changes.blueprints.ensure_blueprint()
    assert not (fake_sftp.root / 'homeassistant/blueprints').exists()
    (fake_sftp.root / 'homeassistant/blueprints/automation').mkdir(parents=True)
    original = ha.rest
    async def fail(*args, **kwargs):
        raise HAError('reload failed')
    monkeypatch.setattr(ha, 'rest', fail)
    with pytest.raises(HAError):
        await changes.blueprints.ensure_blueprint()
    monkeypatch.setattr(ha, 'rest', original)
    assert (await changes.blueprints.ensure_blueprint())['status'] == 'in sync'
    assert changes.db.meta('blueprint_reload_pending') == '0'


@pytest.mark.parametrize('field,value', [
    ('sentences', ['what is {name}']), ('sentences', ['<rule> status']), ('sentences', ['bad [syntax']),
    ('sentences', []), ('sentences', ['x'] * 21), ('sentences', ['x' * 201]), ('sentences', ['  ']),
    ('reply', ''), ('reply', 'x' * 1001), ('unavailable_reply', '{{ bad }}'),
    ('unavailable_reply', '{% bad %}'), ('unavailable_reply', 'x' * 301)])
def test_invalid_inputs(field, value):
    data = draft()
    data['payload']['inputs'][field] = value
    with pytest.raises(ValueError):
        ChangeDraft.model_validate(data)


@pytest.mark.parametrize('field,value', [('automation_id', 'ar_tuner_bad-id'), ('automation_id', 'other'),
    ('alias', ''), ('alias', 'x' * 101), ('blueprint', 'unknown')])
def test_invalid_payload(field, value):
    data = draft()
    data['payload'][field] = value
    with pytest.raises(ValueError):
        ChangeDraft.model_validate(data)


async def test_live_validation_preview_update_and_defaults(stack, fake_ha):
    _, _, _, changes, _ = stack
    data = draft()
    data.pop('tests')
    created = await changes.create(data)
    assert created['reply_preview'] == 'Set to 72 degrees.'
    assert created['precheck']['passed'] and created['tests']['must_match'][0]['source'] == 'trigger'
    assert not created['changes_devices']
    assert fake_ha.template_calls[-1] == {'template': data['payload']['inputs']['reply'], 'variables': {'entity': 'climate.thermostat'}}
    data['payload']['inputs']['reply'] = '{{ undefined_variable }}'
    with pytest.raises(HAError, match='Template render failed'):
        await changes.update(created['id'], {'payload': data['payload']})
    assert changes.get(created['id'])['reply_preview'] == 'Set to 72 degrees.'
    data['payload']['inputs']['entity'] = 'light.test_lamp'
    with pytest.raises(ValueError, match='exposed'):
        await changes.create(data)
    data['payload']['inputs']['entity'] = 'sensor.nonexistent'
    with pytest.raises(ValueError, match='exposed'):
        await changes.create(data)


@pytest.mark.parametrize('existing,fail', [(False, False), (True, False), (False, True)])
async def test_apply_exact_config_inventory_and_rollback(stack, fake_sftp, fake_ha, existing, fail):
    _, _, ha, changes, _ = stack
    remote = setup(stack, fake_sftp)
    data = draft()
    aid = data['payload']['automation_id']
    before = {'id': aid, 'alias': 'Old config', 'triggers': [], 'actions': []}
    if existing:
        fake_ha.automations[aid] = copy.deepcopy(before)
    if not fail:
        fake_ha.matches['what is the thermostat set to'] = {'match': True, 'source': 'trigger'}
    created = await changes.create(data)
    expected = dict(id=aid, alias='Voice: Thermostat', description='Managed by Assist Request Tuner',
                    use_blueprint={'path': PATH, 'input': data['payload']['inputs']})
    assert changes.desired(created, None) == expected
    applied = await changes.apply(created['id'])
    assert remote.read_text() == BUNDLED
    if fail:
        assert applied['status'] == 'failed' and aid not in fake_ha.automations
        return
    assert applied['status'] == 'applied' and applied['test_result']['passed']
    assert fake_ha.automations[aid] == expected
    item = next(a for a in await ha.automations() if a['id'] == aid)
    assert item['label'] == 'blueprint: voice_reply' and item['entity'] == 'climate.thermostat'
    remote.write_text('# blueprint version 0\n')
    await changes.rollback(created['id'])
    assert remote.read_text() == BUNDLED and fake_ha.automation_reloads == 2
    assert fake_ha.automations.get(aid) == (before if existing else None)


async def test_edit_instance_sentences_and_capture(stack, fake_ha):
    _, db, _, changes, _ = stack
    data = draft()
    config = changes.desired(data, None)
    aid = data['payload']['automation_id']
    fake_ha.automations[aid] = config
    store(db, text='how is the thermostat', local=True, matcher={'source': 'builtin', 'intent': {'name': 'HassGetState'}})
    change = dict(type='trigger_sentences', title='Extend', payload={'automation_id': aid, 'add': ['how is the thermostat'],
                  'remove': ['what is the thermostat set to']}, tests={'must_match': [{'sentence': 'how is the thermostat'}]})
    created = await changes.create(change)
    after = changes.desired(created, config)
    assert after['use_blueprint']['input']['sentences'] == ['how is the thermostat']
    assert after['use_blueprint']['input']['reply'] == config['use_blueprint']['input']['reply'] and 'triggers' not in after
    assert created['precheck']['passed'] and created['capture_preview']['steals_local'] == 1
    change['payload']['add'] = ['how is {name}']
    with pytest.raises(ValueError, match='wildcards'):
        await changes.create(change)


async def test_recipes_and_diagnose_reject_then_correct(stack, fake_openai):
    _, db, ha, changes, _ = stack
    await ha.refresh()
    recipe = render_draft('thermostat_status', {'entity_id': 'climate.thermostat'}, ha.entities, ha.areas)
    assert recipe['payload']['inputs']['reply'] == THERMOSTAT_REPLY
    created = await changes.create(recipe)
    assert created['precheck']['passed'] and '72' in created['reply_preview'] and '70' in created['reply_preview']
    generic = render_draft('entity_reply', {'entity_id': 'climate.thermostat', 'commands': ['how is the heating'],
                           'reply': '{{ states(entity) }}', 'sentences': ['how is the heating']}, ha.entities, ha.areas)
    assert (await changes.create(generic))['reply_preview'] == 'heat'
    agent = configure(stack, fake_openai)
    bad = dict(type='trigger_automation', title='Old style', payload={'automation_id': 'ar_tuner_old', 'config': {
        'triggers': [{'trigger': 'conversation', 'command': ['what is the thermostat set to']}], 'actions': [{'set_conversation_response': 'OK'}]}})
    def proposed(kind, change):
        return submission([dict(title='Reply', explanation='Answer one question', kind=kind, change=change, risk='low')])
    fake_openai.scripts = [response(('submit_diagnosis', proposed('trigger_automation', bad))),
                          response(('submit_diagnosis', proposed('blueprint_instance', draft())))]
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    result = agent.get(job['id'])
    assert result['status'] == 'done'
    feedback = json.loads(fake_openai.calls[1]['messages'][-1]['content'])
    assert feedback['errors'] and 'trigger_automation' in feedback['errors'][0]['error']
    staged = changes.get(result['result']['fixes'][0]['change_id'])
    assert staged['type'] == 'blueprint_instance' and staged['reply_preview']
    assert 'provide no fix' in SYSTEM_PROMPT and 'Never submit trigger_automation' in SYSTEM_PROMPT


async def test_interfaces(stack, fake_sftp):
    settings, _, _, _, _ = stack
    setup(stack, fake_sftp)
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Authorization': 'Bearer ' + settings.token}) as client:
            assert (await client.get('/api/v1/blueprints')).json()['status'] == 'absent'
            assert (await client.post('/api/v1/blueprints/sync')).json()['status'] == 'in sync'
            body = {'entity': 'climate.thermostat', 'reply': '{{ states(entity) }}'}
            assert (await client.post('/api/v1/blueprints/preview_reply', json=body)).json()['reply_preview'] == 'heat'
            await client.post('/login', data={'token': settings.token})
            assert 'in sync' in (await client.get('/settings')).text
            assert 'Preview reply' in (await client.get('/templates')).text
            created = (await client.post('/api/v1/changes', json=draft())).json()
            page = (await client.get('/changes/' + str(created['id']))).text
            assert 'Reply preview' in page and 'Set to 72 degrees.' in page
            preview = await client.post('/ui/blueprints/preview_reply', data={'entity_id': body['entity'], 'reply': body['reply']})
            assert preview.json()['reply_preview'] == 'heat'
            for name, args in [('list_blueprints', {}), ('preview_reply', body)]:
                result = await client.post('/mcp/', headers={'Accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
                assert not result.json()['result'].get('isError'), result.text


def test_blueprint_bundled_unchanged_in_docker_context():
    assert Path('app/blueprints/voice_reply.yaml').read_bytes() == BUNDLED.encode()
    assert 'COPY app ./app' in Path('Dockerfile').read_text()
    assert 'blueprints' not in Path('.dockerignore').read_text()
