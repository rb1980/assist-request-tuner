import httpx
from app import wizard
from app.config import Settings
from app.main import create_app
from tests.conftest import fixture
from tests.test_integration import store

TRIGGER = {'match': True, 'source': 'trigger', 'sentence_template': 'x'}


def change(**extra):
    return dict(dict(id=1, type='trigger_sentences', status='draft', error=None, changes_devices=False,
                     payload={'automation_id': 'a1', 'add': ['what speed is [the] fan', 'fan speed']},
                     tests={'must_match': [{'sentence': 'what speed is the fan'}]}), **extra)


def test_describe_uses_names_not_ids():
    ctx = dict(entities={'light.x': 'Couch Lamp'}, areas={'play_room': 'Play Room'}, automations={'a1': 'Voice: fan status'}, templates={})
    assert wizard.describe(change(), ctx)['headline'] == 'Teaches your automation “Voice: fan status” 2 new ways to ask.'
    alias = change(type='area_alias', payload={'area_id': 'play_room', 'add': ['playroom']})
    assert wizard.describe(alias, ctx)['headline'] == 'Gives the Play Room area another name: “playroom”.'
    custom = change(type='custom_sentence', payload={'intent': 'HassLightSet', 'add': ['a']})
    assert 'set lights' in wizard.describe(custom, ctx)['headline']
    assert wizard.describe(change(status='applied'), ctx)['status'] == 'Live in Home Assistant'


def test_checks_and_outcome_in_plain_words():
    bad = change(precheck={'applicable': True, 'results': [{'sentence': 'nope', 'match': False}], 'total': 1})
    assert wizard.checks(bad)[0] == ('bad', "These examples don't match the new phrasings: “nope”")
    stolen = change(capture_preview={'would_capture': [{'transcript': 'turn on the fan', 'steals_local': 1}]}, changes_devices=True)
    levels = [level for level, _ in wizard.checks(stolen)]
    assert levels == ['warn', 'warn']
    assert wizard.outcome(change(status='failed', error='Change tests failed'))[0] == 'bad'
    assert 'undone automatically' in wizard.outcome(change(status='failed', error='Change tests failed'))[1]
    assert wizard.outcome(change(status='applied'))[1].startswith('Done.')


def test_ai_actions_and_handling():
    assert wizard.ai_actions([{'name': 'assist__light__HassLightSet', 'args': {'name': 'Couch Lamp', 'area': 'Play Room'}}]) == \
        ['set lights (color or brightness): Couch Lamp, Play Room']
    ctx = dict(entities={'light.a': 'Lamp A'}, templates={'x': 'Voice: status'})
    assert wizard.handling(TRIGGER, ctx) == 'Your automation “Voice: status”'
    assert wizard.handling(None, ctx).startswith('Not recognised')
    builtin = {'match': True, 'source': 'builtin', 'intent': {'name': 'HassTurnOn'}, 'targets': {'light.a': {'matched': True}}}
    assert wizard.handling(builtin, ctx) == "Home Assistant's built-in “turn on” on Lamp A"


async def test_live_answer_only_when_nothing_matching_changes_devices(stack, fake_ha):
    _, _, ha, _, _ = stack
    fake_ha.matches['what is the temperature in the bedroom'] = dict(TRIGGER)
    fake_ha.automations['garage'] = {'id': 'garage', 'alias': 'Voice: garage', 'triggers': [{'trigger': 'conversation', 'command': ['open the garage']}],
                                     'actions': [{'action': 'cover.open_cover', 'target': {'entity_id': 'cover.garage'}}]}
    fake_ha.matches['open the garage'] = dict(TRIGGER)
    fake_ha.matches['turn on test lamp'] = fixture('matcher_debug')['results'][1]
    rows = await wizard.answers(ha, ['what is the temperature in the bedroom', 'open the garage', 'turn on test lamp', 'gibberish'])
    assert rows[0]['answer'] == 'Answer to what is the temperature in the bedroom'
    assert rows[1]['answer'] is None and 'change a device' in rows[1]['note']
    assert rows[2]['answer'] is None and 'change a device' in rows[2]['note']
    assert not rows[3]['local'] and rows[3]['handled'].startswith('Not recognised')
    assert [a['text'] for a in fake_ha.asked] == ['what is the temperature in the bedroom']
    assert all(a['agent_id'] == 'conversation.home_assistant' for a in fake_ha.asked)


async def test_ask_refuses_other_agents(stack):
    _, _, ha, _, _ = stack
    import pytest
    with pytest.raises(ValueError):
        await ha.rest('POST', config={'text': 'hi', 'language': 'en', 'agent_id': 'conversation.llm'}, conversation=True)


async def test_wizard_flow_alias_apply_undo(fake_ha, tmp_path):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'ui.sqlite'), reload_timeout=0.01)
    fake_ha.exposed['light.test_lamp'] = {'conversation': True}
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        store(app.state.service.db, text=fake_ha.alias_sentence, pattern='turn on glow')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            await client.post('/login', data={'token': 'ui-placeholder'})
            start = await client.get('/')
            assert 'Which request should Home Assistant handle itself?' in start.text and fake_ha.alias_sentence in start.text
            understand = await client.get('/fix/pattern', params={'p': 'turn on glow'})
            assert 'What happened' in understand.text and 'Choose a fix' in understand.text
            review = await client.post('/fix/alias', data={'kind': 'entity_alias', 'target': 'light.test_lamp', 'alias': 'reading glow', 'sentence': fake_ha.alias_sentence})
            assert '/fix/change/' in str(review.url) and 'After this, you can say' in review.text
            assert 'Gives Test Lamp another name: “reading glow”.' in review.text
            ident = str(review.url).rstrip('/').split('/')[-1]
            done = await client.post(f'/fix/change/{ident}/apply')
            assert str(done.url).endswith('/done') and 'Done. Home Assistant now handles this itself.' in done.text
            assert fake_ha.entities['light.test_lamp']['aliases'] == [None, 'reading glow']
            assert 'Try it' in done.text and 'turn on' in done.text
            assert not fake_ha.asked  # HassTurnOn changes a device: no live answer
            history = await client.get('/history')
            assert 'Live in Home Assistant' in history.text
            undone = await client.post(f'/fix/change/{ident}/undo')
            assert 'Undone.' in undone.text and fake_ha.entities['light.test_lamp']['aliases'] == [None]
            missing = await client.get('/fix/change/999', headers={'accept': 'text/html'})
            assert missing.status_code == 404 and "That didn't work" in missing.text
            assert (await client.get('/api/v1/changes/999', headers={'Authorization': 'Bearer ui-placeholder'})).json()['detail']


async def test_hide_unhide_and_sentence_drafts(fake_ha, tmp_path):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'ui.sqlite'), reload_timeout=0.01)
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        db = app.state.service.db
        store(db, text='make the den cozy', pattern='make den cozy')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            await client.post('/login', data={'token': 'ui-placeholder'})
            assert 'make the den cozy' in (await client.get('/')).text
            page = await client.post('/fix/hide', data={'pattern': 'make den cozy'})
            assert 'Ignored (1)' in page.text and 'Fix “make the den cozy”' not in page.text
            assert 'Nothing left to fix here. The home requests that went to the AI are 1 ignored (see below).' in page.text
            assert 'every home request' not in page.text
            assert db.rows('SELECT count(*) AS n FROM requests')[0]['n'] == 1  # history kept
            page = await client.post('/fix/unhide', data={'pattern': 'make den cozy'})
            assert 'Ignored (' not in page.text and 'Fix “make the den cozy”' in page.text
            # Ignore in place from the list: a small JSON answer, no page reload.
            quick = await client.post('/fix/hide', data={'pattern': 'make den cozy'}, headers={'accept': 'application/json'})
            assert quick.json() == {'ok': True, 'ignored': True} and 'Ignored (1)' in (await client.get('/')).text
            quick = await client.post('/fix/unhide', data={'pattern': 'make den cozy'}, headers={'accept': 'application/json'})
            assert quick.json() == {'ok': True, 'ignored': False} and 'data-pattern="make den cozy"' in (await client.get('/')).text
            add = await client.post('/fix/sentences/add', data={'intent': 'HassLightSet', 'domain': 'light',
                                                                  'add': '[<set>] [the] {area} lights [to] {color}', 'examples': 'set the living room lights to red'})
            assert 'Teaches Home Assistant 1 new way to set lights' in add.text
            ident = int(str(add.url).rstrip('/').split('/')[-1])
            change = app.state.service.changes.get(ident)
            assert change['payload']['slots'] == {'domain': 'light'} and change['tests']['must_match'][0]['source'] == 'custom'
            sentences_page = await client.get('/sentences')
            assert 'Add phrasings for another built-in action' in sentences_page.text and 'set lights' in sentences_page.text
            app.state.service.changes.sentences.replace([dict(id=1, intent='HassLightSet', sentences=['a {area} b'], slots={}, requires_context={}, comment='', created_at='', change_id=None)])
            remove = await client.post('/fix/sentences/remove', data={'intent': 'HassLightSet', 'sentence': 'a {area} b', 'block_id': '1'})
            assert 'Stops Home Assistant recognising 1 phrasing' in remove.text
            missing = await client.post('/fix/sentences/add', data={'intent': 'HassLightSet', 'add': 'x', 'examples': ''}, headers={'accept': 'text/html'})
            assert missing.status_code == 400 and 'Give at least one example' in missing.text


async def test_request_with_applied_fix_leaves_the_list(fake_ha, tmp_path):
    # The fix made a related phrasing local ("pause the echo show"), not the exact words captured, so HA's dry run
    # still misses the original. It must leave the list anyway, and come back if the fix is undone.
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'ui.sqlite'), reload_timeout=0.01)
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        db = app.state.service.db
        store(db, text='stop the music on the echo show', pattern='stop music echo show')
        store(db, text='make the den cozy', pattern='make den cozy')
        with db.conn:
            db.conn.execute("INSERT INTO diagnoses(id, target_type, target, status) VALUES (1, 'pattern', 'stop music echo show', 'done')")
            db.conn.execute("""INSERT INTO changes(id, type, title, payload, tests, status, diagnosis_id) VALUES (1, 'entity_alias',
                'Echo Show alias', '{}', '{"must_match": [{"sentence": "pause the echo show"}]}', 'applied', 1)""")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            await client.post('/login', data={'token': 'ui-placeholder'})
            page = (await client.get('/')).text
            assert 'Fix “stop the music on the echo show”' not in page and 'Fix “make the den cozy”' in page
            assert 'Fixed (1)' in page and 'Echo Show alias' in page and 'now say “pause the echo show”' in page
            db.save('changes', dict(id=1, status='rolled_back'))
            page = (await client.get('/')).text
            assert 'Fix “stop the music on the echo show”' in page and 'Fixed (' not in page


def test_same_intent_call_through_a_custom_sentence_is_not_a_regression():
    from app.changes import same_outcome
    builtin = {'match': True, 'source': 'builtin', 'intent': {'name': 'HassLightSet'}, 'slots': {'area': 'back room', 'color': 'purple'},
               'targets': {'light.a': {'matched': True}, 'light.b': {'matched': True}}}
    custom = dict(builtin, source='custom', sentence_template='(turn|set) [the] lights [in] [the] {area} to [all] {color}')
    assert same_outcome(builtin, custom)
    assert not same_outcome(builtin, dict(custom, slots={'area': 'back room', 'color': 'white'}))   # does something else
    assert not same_outcome(builtin, dict(custom, targets={'light.a': {'matched': True}}))           # fewer lights
    assert not same_outcome(builtin, dict(custom, source='trigger'))                                 # an automation takes over
    assert not same_outcome(builtin, dict(custom, match=False))


async def test_undone_fix_shows_as_undone_and_offers_a_different_fix(fake_ha, tmp_path):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'ui.sqlite'), reload_timeout=0.01)
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        db = app.state.service.db
        store(db, text='turn the back room lights to all white', pattern='all white')
        with db.conn:
            db.conn.execute("""INSERT INTO diagnoses(id, target_type, target, status, result) VALUES (1, 'pattern', 'all white', 'done',
                '{"fixes": [{"title": "Allow all before color", "change_id": 1}]}')""")
            db.conn.execute("""INSERT INTO changes(id, type, title, payload, tests, status, diagnosis_id, error, replay_result) VALUES (1, 'custom_sentence',
                'Allow all before color', '{"intent": "HassLightSet", "add": ["x"]}', '{"must_match": []}', 'failed', 1,
                'Regressions found; restored backup.', '{"lost": [{"sentence": "turn the back room lights to purple"}]}')""")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            await client.post('/login', data={'token': 'ui-placeholder'})
            page = (await client.get('/')).text
            assert 'Fix didn&#39;t pass the checks' in page and 'Try a different fix →' in page and 'Fixes ready' not in page
            db.save('changes', dict(id=1, status='rolled_back'))
            page = (await client.get('/')).text
            assert 'Fix undone' in page and 'Try a different fix →' in page
            detail = (await client.get('/fix/pattern', params={'p': 'all white'})).text
            assert 'Try a different fix with AI' in detail
        attempts = app.state.service.diagnose.previous_attempts('all white')
        assert attempts[0]['title'] == 'Allow all before color' and attempts[0]['outcome'] == 'undone by the user'
        assert attempts[0]['broke'] == ['turn the back room lights to purple']


async def test_fix_list_intro_fits_what_was_recorded(fake_ha, tmp_path):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'ui.sqlite'))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        db = app.state.service.db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            await client.post('/login', data={'token': 'ui-placeholder'})
            assert 'No voice requests yet.' in (await client.get('/')).text   # fresh install
            store(db, text='make the den cozy', pattern='make den cozy', source='typed')
            typed = (await client.get('/')).text   # typed only: no voice coverage to report, but a list
            assert 'No voice requests yet.' not in typed and 'answered 0%' not in typed and 'make the den cozy' in typed
            store(db, text='turn on the den lamp', local=True, pattern='turn on den lamp')
            assert 'answered 100% of home requests' in (await client.get('/')).text
