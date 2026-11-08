import copy
import json
import os
import stat
import pytest
import asyncssh
import httpx
from hassil import Intents
from app import sentences
from app.changes import ChangeDraft
from app.ha import HAError
from app.main import create_app
from app.phrasing import list_templates, render_draft
from app.sentence_writer import SentenceWriter
from test_integration import store
from test_v2 import configure, response, submission

AREAS = [{'area_id': 'play_room', 'name': 'Play Room', 'aliases': ['playroom']}]
ENTITIES = [dict(entity_id='light.couch', name='Sofa Lamp', aliases=['Reading Lamp'], area='play_room', domain='light', exposed=True),
            dict(entity_id='light.hidden', name='Hidden Ring', aliases=[], area='play_room', domain='light', exposed=False)]
COLOR = {'intent': 'HassLightSet', 'add': ['[<set>] [the] {area} lights [to] {color}', '(turn|make) [the] {area} lights {color}'], 'slots': {'domain': 'light'}}
HANDWRITTEN = 'language: en\nintents:\n  HassLightSet:\n    data:\n      - sentences:\n          - "set [the] {area} lights to {color}"\n        slots:\n          domain: light\n'


def setup_writer(stack, fake):
    settings, _, _, changes, _ = stack
    settings.ha_ssh_host = '127.0.0.1'
    settings.ha_ssh_port = fake.port
    settings.ha_ssh_key_file = str(fake.key_file)
    return changes.sentences


def draft(**payload):
    return dict(type='custom_sentence', title='Area color', payload=payload or copy.deepcopy(COLOR),
                tests={'must_match': [{'sentence': 'set the play room lights to blue', 'source': 'custom', 'intent': 'HassLightSet'}],
                       'must_not_match': ['set the play room lights to a warm white at 50% brightness', 'set the nowhere lights to blue']})


def remote(fake, content):
    fake.path.parent.mkdir(parents=True, exist_ok=True)
    fake.path.write_text(content)


async def test_writer_tofu_atomic_write_path_confinement_and_delete(stack, fake_sftp):
    store = setup_writer(stack, fake_sftp)
    writer = store.writer
    assert await writer.read() is None
    assert writer.configuration()['host_key_fingerprint'] == fake_sftp.host_key.get_fingerprint()
    await writer.write(HANDWRITTEN)
    assert await writer.read() == HANDWRITTEN
    assert not fake_sftp.path.with_suffix('.yaml.tmp').exists()
    assert ('rename', '/homeassistant/custom_sentences/en/ar_tuner.yaml.tmp', '/homeassistant/custom_sentences/en/ar_tuner.yaml') in fake_sftp.operations
    for operation in fake_sftp.operations:
        if operation[0] == 'mkdir':
            assert operation[1] in ('/homeassistant/custom_sentences', '/homeassistant/custom_sentences/en')
        else:
            assert operation[1] in (writer.path, writer.temp)
            if operation[0] == 'rename':
                assert operation[2] == writer.path
    await writer.write(None)
    assert await writer.read() is None
    assert not fake_sftp.path.exists()
    assert writer.configuration()['last_write']


async def test_writer_pinned_key_and_mismatch_refusal(stack, fake_sftp):
    settings, db, _, _, _ = stack
    store = setup_writer(stack, fake_sftp)
    settings.ha_ssh_host_key = fake_sftp.host_key.export_public_key().decode()
    assert await store.writer.read() is None
    settings.ha_ssh_host_key = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    with pytest.raises(HAError, match='not the one Request Tuner saved'):
        await store.writer.write(HANDWRITTEN)
    assert not fake_sftp.path.exists()
    settings.ha_ssh_host_key = ''
    saved = json.loads(db.meta('sentence_host_key'))
    saved['key'] = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    db.meta('sentence_host_key', json.dumps(saved))
    with pytest.raises(HAError, match='Forget saved host key'):
        await store.writer.read()
    # Forgetting the saved key trusts the next one again.
    store.writer.forget_host_key()
    assert await store.writer.read() is None
    assert json.loads(db.meta('sentence_host_key'))['key'] == fake_sftp.host_key.export_public_key().decode().strip()


async def test_writer_trusts_again_when_the_address_changes(stack, fake_sftp):
    settings, db, _, _, _ = stack
    store = setup_writer(stack, fake_sftp)
    # A key saved for another address (e.g. the IP, before switching to homeassistant.local) does not block.
    other = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode().strip()
    db.meta('sentence_host_key', json.dumps({'host': 'old-address.example:22', 'key': other}))
    assert await store.writer.read() is None
    assert json.loads(db.meta('sentence_host_key'))['host'] == f'{settings.ha_ssh_host}:{settings.ha_ssh_port}'


async def test_writer_missing_key_permissions_and_symlinks(stack, fake_sftp, tmp_path):
    settings, db, _, _, _ = stack
    store = setup_writer(stack, fake_sftp)
    fake_sftp.key_file.chmod(0o644)
    assert not store.writer.configuration()['configured']
    with pytest.raises(ValueError, match='mode 600'):
        await store.writer.write(HANDWRITTEN)
    fake_sftp.key_file.chmod(0o600)
    external = fake_sftp.root / 'other.yaml'
    external.write_text('untouched')
    fake_sftp.path.parent.mkdir(parents=True)
    fake_sftp.path.symlink_to(external)
    with pytest.raises(ValueError, match='symlinks'):
        await store.writer.write(HANDWRITTEN)
    assert external.read_text() == 'untouched'
    settings.ha_ssh_key_file = str(tmp_path / 'missing')
    assert not store.writer.configuration()['configured']
    with pytest.raises(ValueError, match='disabled'):
        await store.writer.read()
    settings.ha_config_dir = '/homeassistant/../outside'
    with pytest.raises(ValueError, match='traversal'):
        SentenceWriter(db, settings)


async def test_adopt_handwritten_file_and_confirmed_import_with_conflict(stack, fake_sftp):
    store = setup_writer(stack, fake_sftp)
    remote(fake_sftp, HANDWRITTEN)
    await store.adopt()
    assert len(store.blocks()) == 1 and store.blocks()[0]['intent'] == 'HassLightSet'
    assert fake_sftp.path.read_text() == HANDWRITTEN
    rendered = store.listing()['rendered']
    assert rendered.startswith(sentences.HEADER)
    assert sentences.render(sentences.parse(rendered)) == rendered
    changed = HANDWRITTEN.replace('set [the]', 'make [the]')
    remote(fake_sftp, changed)
    preview = await store.import_file()
    assert not preview['confirmed'] and 'make [the]' in preview['diff']
    assert store.listing()['rendered'] == rendered
    with pytest.raises(ValueError, match='preview changed'):
        await store.import_file(True, 'stale')
    await store.import_file(True, preview['fingerprint'])
    assert 'make [the]' in store.listing()['rendered']
    assert fake_sftp.path.read_text() == changed


@pytest.mark.parametrize('kind', ['missing', 'existing'])
async def test_custom_apply_reload_and_exact_rollback(stack, fake_sftp, fake_ha, kind):
    _, db, _, changes, _ = stack
    store = setup_writer(stack, fake_sftp)
    if kind == 'existing':
        remote(fake_sftp, HANDWRITTEN)
    created = await changes.create(draft())
    assert created['precheck']['passed'], created['precheck']
    assert created['capture_preview']['warnings'] == []
    fake_ha.matches['set the play room lights to blue'] = {'match': True, 'source': 'custom', 'intent': {'name': 'HassLightSet'}}
    applied = await changes.apply(created['id'])
    assert applied['status'] == 'applied', applied
    assert fake_ha.reloads == 1
    assert applied['before'] == (HANDWRITTEN if kind == 'existing' else None)
    assert fake_sftp.path.read_text() == applied['after'] == store.listing()['rendered']
    assert len(store.blocks()) == (2 if kind == 'existing' else 1)
    await changes.rollback(created['id'])
    assert fake_ha.reloads == 2
    assert len(store.blocks()) == (1 if kind == 'existing' else 0)
    assert fake_sftp.path.read_text() == HANDWRITTEN if kind == 'existing' else not fake_sftp.path.exists()


async def test_failed_match_auto_restores_file_and_blocks(stack, fake_sftp, fake_ha):
    _, _, _, changes, _ = stack
    store = setup_writer(stack, fake_sftp)
    remote(fake_sftp, HANDWRITTEN)
    created = await changes.create(draft())
    result = await changes.apply(created['id'])
    assert result['status'] == 'failed' and 'tests failed' in result['error']
    assert fake_sftp.path.read_text() == HANDWRITTEN and len(store.blocks()) == 1
    assert fake_ha.reloads == 2


async def test_writer_disabled_still_drafts_and_external_edit_blocks_apply(stack, fake_sftp):
    settings, _, _, changes, _ = stack
    settings.ha_ssh_key_file = str(fake_sftp.key_file.parent / 'missing')
    created = await changes.create(draft())
    assert created['precheck']['passed']
    with pytest.raises(ValueError, match='disabled'):
        await changes.apply(created['id'])
    store = setup_writer(stack, fake_sftp)
    await store.adopt()
    remote(fake_sftp, HANDWRITTEN)
    with pytest.raises(ValueError, match='import from HA'):
        await changes.apply(created['id'])


def test_english_hassil_live_lists_aliases_and_invalid_values():
    blocks = sentences.desired([], COLOR)
    no_alias = sentences.engine(blocks, ENTITIES, [dict(AREAS[0], aliases=[])])
    assert not sentences.match(no_alias, ['set the playroom lights to blue'])[0]['match']
    compiled = sentences.engine(blocks, ENTITIES, AREAS)
    result = sentences.match(compiled, ['set the playroom lights to blue', 'make the play room lights red',
        'set the play room lights to a warm white at 50% brightness', 'set the kitchen lights to blue'])
    assert [r['match'] for r in result] == [True, True, False, False]
    assert result[0]['source'] == 'custom' and result[0]['slots']['domain'] == 'light'
    names = sentences.match(compiled, ['turn on reading lamp', 'turn on hidden ring'])
    assert names[0]['match'] and not names[1]['match']
    assert isinstance(Intents.from_dict(sentences.english()), Intents)


def test_custom_payload_blocks_render_remove_and_validation():
    for p in [dict(COLOR, intent='MadeUp'), dict(COLOR, file='/tmp/evil'), dict(COLOR, block_id='../escape'), dict(COLOR, slots=[]), dict(COLOR, add=[])]:
        with pytest.raises(ValueError):
            ChangeDraft.model_validate(draft(**p))
    blocks = sentences.desired([], COLOR)
    extended = sentences.desired(blocks, dict(intent='HassLightSet', block_id=1, add=['paint {area} {color}']))
    assert len(extended) == 1 and len(extended[0]['sentences']) == 3
    removed = sentences.desired(extended, dict(intent='HassLightSet', remove=extended[0]['sentences']))
    assert removed == []
    with pytest.raises(ValueError, match='block_id'):
        sentences.desired(blocks, dict(intent='HassTurnOn', block_id=1, add=['on']))
    with pytest.raises(ValueError, match='Unknown sentence lists'):
        sentences.engine(sentences.desired([], dict(intent='HassLightSet', add=['set {not_a_list}'])), ENTITIES, AREAS)
    with pytest.raises(ValueError):
        sentences.parse('!!python/object/apply:os.system ["false"]')
    assert sentences.render(blocks) == sentences.render(copy.deepcopy(blocks))


async def test_custom_capture_preview_same_intent_safe_other_intent_and_trigger_flagged(stack):
    _, db, _, changes, _ = stack
    for name, handled, match in [('blue', 'local_intent', {'source': 'builtin', 'intent': {'name': 'HassLightSet'}}),
                                 ('red', 'local_intent', {'source': 'builtin', 'intent': {'name': 'HassTurnOn'}}),
                                 ('green', 'local_trigger', {'source': 'trigger'}), ('yellow', 'llm', None)]:
        ident = store(db, text=f'set the play room lights to {name}', matcher=match)
        db.save('requests', {'id': ident, 'handled_by': handled})
    result = await changes.create(draft())
    preview = result['capture_preview']
    assert len(preview['would_capture']) == 4
    assert preview['steals_local'] == 2 and preview['takes_from_llm'] == 1 and not preview['warnings']
    edited = await changes.update(result['id'], {'payload': dict(COLOR, add=['make {area} lights {color}'])})
    assert not edited['capture_preview']['would_capture']


async def test_reload_is_only_allowed_service(stack, fake_ha):
    _, _, ha, _, _ = stack
    await ha.reload_sentences()
    assert fake_ha.reloads == 1
    for method, service in [('POST', 'light.turn_on'), ('GET', 'conversation.reload'), ('POST', '../conversation/reload'), ('POST', 'conversation.process')]:
        with pytest.raises(ValueError):
            await ha.rest(method, service=service)
    assert fake_ha.reloads == 1


async def test_v3_diagnose_prefers_custom_and_templates_have_no_targets(stack, fake_openai):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    proposed = dict(title='Use built-in intent', explanation='Extend HassLightSet.', kind='custom_sentence', change=draft(), risk='low')
    fake_openai.scripts = [response(('submit_diagnosis', submission([proposed])))]
    job = agent.enqueue(request_id=store(db))
    await agent.run_job(job['id'])
    assert agent.get(job['id'])['status'] == 'done'
    assert db.rows('SELECT * FROM changes')[0]['type'] == 'custom_sentence'
    prompt = fake_openai.calls[0]['messages'][0]['content']
    assert 'Prefer (1)' in prompt and '(2) custom_sentence' in prompt and 'AFTER sentence' in prompt
    recipes = list_templates()
    assert {r['id'] for r in recipes} == {'area_light_color', 'area_light_brightness', 'thermostat_status', 'entity_reply'}
    for ident in ['area_light_color', 'area_light_brightness']:
        d = render_draft(ident, {}, ENTITIES, AREAS)
        assert d['type'] == 'custom_sentence' and 'automation_id' not in d['payload']
        compiled = sentences.engine(sentences.desired([], d['payload']), ENTITIES, AREAS)
        assert sentences.match(compiled, [d['tests']['must_match'][0]['sentence']])[0]['source'] == 'custom'
        with pytest.raises(ValueError, match='Unknown template parameters'):
            render_draft(ident, {'area_id': 'play_room'}, ENTITIES, AREAS)
    with pytest.raises(KeyError):
        render_draft('entity_status', {}, ENTITIES, AREAS)


async def test_sentences_ui_rest_mcp_and_import_confirmation(stack, fake_sftp):
    settings, _, _, _, _ = stack
    setup_writer(stack, fake_sftp)
    remote(fake_sftp, HANDWRITTEN)
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Authorization': 'Bearer ' + settings.token}) as client:
            async with app.state.service.changes.lock:
                await app.state.service.changes.sentences.adopt()
            listing = (await client.get('/api/v1/sentences')).json()
            assert listing['blocks'] and listing['writer']['configured']
            remote(fake_sftp, HANDWRITTEN.replace('set [the]', 'paint [the]'))
            preview = (await client.post('/api/v1/sentences/import', json={})).json()
            assert preview['diff'] and not preview['confirmed']
            assert (await client.post('/api/v1/sentences/import', json={'confirm': True})).status_code == 400
            result = await client.post('/api/v1/sentences/import', json={'confirm': True, 'fingerprint': preview['fingerprint']})
            assert result.json()['confirmed']
            await client.post('/login', data={'token': settings.token})
            assert 'View / import from HA' in (await client.get('/settings')).text
            assert 'The file on Home Assistant' in (await client.get('/sentences')).text
            assert 'Confirm import' in (await client.post('/ui/sentences/import', data={})).text
            created = (await client.post('/api/v1/changes', json=draft())).json()
            page = (await client.get('/changes/' + str(created['id']))).text
            assert '--- before' in page and '+++ after' in page and 'HassLightSet' in page
            response = await client.post('/mcp/', headers={'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'list_custom_sentences', 'arguments': {}}})
            assert 'HassLightSet' in response.text and 'isError":true' not in response.text


async def test_reload_failure_restores_absent_file_and_empty_blocks(stack, fake_sftp, monkeypatch):
    _, _, ha, changes, _ = stack
    store = setup_writer(stack, fake_sftp)
    calls = 0
    original = ha.reload_sentences
    async def reload():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HAError('fake reload outage')
        return await original()
    monkeypatch.setattr(ha, 'reload_sentences', reload)
    created = await changes.create(draft())
    result = await changes.apply(created['id'])
    assert result['status'] == 'failed' and calls == 2
    assert not fake_sftp.path.exists() and not store.blocks()


async def test_all_custom_blocks_share_rollback_order(stack, fake_sftp, fake_ha):
    _, _, _, changes, _ = stack
    store = setup_writer(stack, fake_sftp)
    fake_ha.matches['set the play room lights to blue'] = {'match': True, 'source': 'custom', 'intent': {'name': 'HassLightSet'}}
    first = await changes.create(draft())
    assert (await changes.apply(first['id']))['status'] == 'applied'
    other = dict(intent='HassTurnOn', add=['activate {name}'])
    second = await changes.create(dict(draft(**other), tests={'must_match': [{'sentence': 'activate sofa lamp', 'intent': 'HassTurnOn', 'source': 'custom'}]}))
    fake_ha.matches['activate sofa lamp'] = {'match': True, 'source': 'custom', 'intent': {'name': 'HassTurnOn'}}
    # The test registry has no sofa lamp, so the offline pre-check can't pass; this test is about rollback order.
    changes.db.save('changes', dict(id=second['id'], precheck={'applicable': False}))
    assert (await changes.apply(second['id']))['status'] == 'applied'
    with pytest.raises(ValueError, match='latest applied change'):
        await changes.rollback(first['id'])
    await changes.rollback(second['id'])
    assert len(store.blocks()) == 1
    await changes.rollback(first['id'])
    assert not store.blocks() and not fake_sftp.path.exists()


async def test_temporary_hardlink_is_replaced_not_truncated(stack, fake_sftp):
    store = setup_writer(stack, fake_sftp)
    fake_sftp.path.parent.mkdir(parents=True)
    other = fake_sftp.root / 'other.yaml'
    other.write_text('preserve')
    os.link(other, str(fake_sftp.path) + '.tmp')
    await store.writer.write(HANDWRITTEN)
    assert other.read_text() == 'preserve'
    assert fake_sftp.path.read_text() == HANDWRITTEN


async def test_custom_context_and_negative_tests(stack):
    _, _, ha, changes, _ = stack
    await ha.refresh()
    payload = dict(intent='HassLightSet', add=['paint {name} {color}'], requires_context={'domain': 'light'})
    # The real fixture's exposed lamp is resolved from the same cache the client uses.
    exposed = next(e for e in ha.entities if e['domain'] == 'light' and e['exposed'])
    positive = 'paint ' + exposed['name'] + ' blue'
    custom = dict(type='custom_sentence', title='Named color', payload=payload,
                  tests={'must_match': [{'sentence': positive, 'intent': 'HassLightSet', 'source': 'custom'}],
                         'must_not_match': ['paint invisible lamp blue']})
    result = await changes.create(custom)
    assert result['precheck']['passed'], result['precheck']
    result = await changes.update(result['id'], {'tests': {'must_match': [{'sentence': positive}], 'must_not_match': [positive]}})
    assert not result['precheck']['passed'] and result['precheck']['negative_results'][0]['match']


def test_custom_sentence_schema_migration_preserves_prior_rows(tmp_path):
    from app.db import DB
    path = str(tmp_path / 'migration.sqlite')
    db = DB(path)
    db.execute("INSERT INTO changes(id,title,status) VALUES (1,'existing','draft')")
    db.execute('DROP TABLE sentence_blocks')
    db.execute('ALTER TABLE changes DROP COLUMN sentence_before')
    db.execute('ALTER TABLE changes DROP COLUMN sentence_after')
    db.meta('schema_version', '4')
    db.conn.close()
    db = DB(path)
    assert db.meta('schema_version') == '5' and db.rows('SELECT * FROM sentence_blocks') == []
    change = db.rows('SELECT * FROM changes')[0]
    assert change['title'] == 'existing' and change['sentence_before'] is None and change['sentence_after'] is None
    db.conn.close()


async def test_private_key_parser_diagnostics_are_not_exposed(stack, fake_sftp, monkeypatch, caplog):
    store = setup_writer(stack, fake_sftp)
    def invalid_key(*args, **kwargs):
        raise asyncssh.KeyImportError('fake-private-key-parser-placeholder')
    monkeypatch.setattr(asyncssh, 'connect', invalid_key)
    with pytest.raises(HAError) as error:
        await store.writer.read()
    assert str(error.value) == 'SFTP writer failed: KeyImportError'
    assert 'fake-private-key-parser-placeholder' not in str(store.writer.configuration()) + caplog.text


async def test_apply_refuses_a_failed_offline_precheck(stack, fake_sftp):
    _, _, _, changes, _ = stack
    setup_writer(stack, fake_sftp)
    created = await changes.create(dict(draft(), tests={'must_match': [{'sentence': 'this sentence matches nothing at all'}]}))
    assert created['precheck']['applicable'] and created['precheck']['passed'] is False
    with pytest.raises(ValueError, match='pre-check failed'):
        await changes.apply(created['id'])
