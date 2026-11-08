import copy
import json
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from app.db import DB
from app.main import create_app
from app.precheck import check_template
from app.diagnose import SYSTEM_PROMPT
from test_integration import store
from test_v2 import configure, response, submission

def trigger_draft(commands, automation_id='ar_tuner_preview'):
    return dict(type='trigger_automation', title='Preview', payload={'automation_id': automation_id, 'config': {
        'triggers': [{'trigger': 'conversation', 'command': commands}], 'actions': [{'set_conversation_response': 'Ready'}]}},
        tests={'must_match': [{'sentence': 'set the playroom lights to blue'}]})


async def test_preview_all_handling_latest_distinct_window_exclusions_and_edit(stack):
    _, db, _, changes, _ = stack
    builtin = store(db, text='set the playroom lights to blue', local=True, matcher={'match': True, 'source': 'builtin', 'intent': {'name': 'HassLightSet'}})
    other = store(db, text='set the playroom lights to red', local=True, matcher={'match': True, 'source': 'trigger', 'automation_id': 'other'})
    db.save('requests', {'id': other, 'handled_by': 'local_trigger'})
    store(db, text='set the playroom lights to green')
    store(db, text='set the playroom lights to green')
    error = store(db, text='set the playroom lights to yellow')
    db.save('requests', {'id': error, 'handled_by': 'error'})
    excluded = store(db, text='set the playroom lights to purple')
    db.save('requests', {'id': excluded, 'excluded': True})
    old = store(db, text='set the playroom lights to orange')
    db.save('requests', {'id': old, 'started_at': (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()})
    created = await changes.create(trigger_draft(['set the playroom lights to {color}']))
    preview = created['capture_preview']
    assert preview['checked'] == 4 and len(preview['would_capture']) == 4
    assert preview['steals_local'] == 2 and preview['takes_from_llm'] == 1
    assert next(r for r in preview['would_capture'] if r['transcript'].endswith('blue'))['intent'] == 'HassLightSet'
    assert len(preview['warnings']) == 1
    edited = await changes.update(created['id'], {'payload': trigger_draft(['set the playroom lights to green'])['payload']})
    assert edited['capture_preview']['warnings'] == []
    assert edited['capture_preview']['steals_local'] == 0
    assert len(edited['capture_preview']['would_capture']) == 1
    assert changes.get(created['id'])['capture_preview'] == edited['capture_preview']


async def test_trigger_sentences_preview_and_own_identity(stack, fake_ha):
    _, db, _, changes, _ = stack
    fake_ha.automations['existing'] = {'triggers': [{'trigger': 'conversation', 'command': ['keep this', 'remove this']}], 'actions': []}
    for text, aid in [('keep this', 'existing'), ('remove this', 'existing'), ('new text', 'other')]:
        ident = store(db, text=text, local=True, matcher={'source': 'trigger', 'automation_id': aid})
        db.save('requests', {'id': ident, 'handled_by': 'local_trigger'})
    draft = dict(type='trigger_sentences', title='Edit existing', payload={'automation_id': 'existing', 'add': ['new {name}'], 'remove': ['remove this']}, tests={'must_match': [{'sentence': 'new text'}]})
    created = await changes.create(draft)
    preview = created['capture_preview']
    assert {r['transcript'] for r in preview['would_capture']} == {'keep this', 'new text'}
    assert preview['steals_local'] == 1
    assert preview['warnings'][0]['sentence'] == 'new {name}'
    edited = await changes.update(created['id'], {'payload': {'automation_id': 'existing', 'add': ['new safe'], 'remove': ['remove this']}})
    assert edited['capture_preview']['warnings'] == []
    assert edited['capture_preview']['steals_local'] == 0


async def test_capture_preview_batches_over_200_transcripts(stack):
    _, db, _, changes, _ = stack
    for i in range(205):
        store(db, text=f'new text {i}')
    draft = trigger_draft(['new {value}'])
    draft['tests'] = {'must_match': [{'sentence': 'new text 1'}]}
    result = (await changes.create(draft))['capture_preview']
    assert len(result['would_capture']) == 205 and result['takes_from_llm'] == 205
    assert not result['errors']


async def test_model_receives_capture_preview_then_narrows(stack, fake_openai, fake_ha):
    _, db, _, _, _ = stack
    agent = configure(stack, fake_openai)
    ident = store(db, text='set the playroom lights to blue')
    store(db, text='set the playroom lights to red', local=True, matcher={'source': 'builtin', 'intent': {'name': 'HassLightSet'}})
    fake_ha.automations['existing_reply'] = trigger_draft(['unrelated question'])['payload']['config']
    def proposed(command):
        change = dict(type='trigger_sentences', title='Preview', payload={'automation_id': 'existing_reply', 'add': [command]},
                      tests={'must_match': [{'sentence': 'set the playroom lights to blue'}]})
        return submission([dict(title='Color reply', explanation='Local phrasing', kind='trigger_sentences', change=change, risk='low')])
    narrow = proposed('set the playroom lights to blue')
    fake_openai.scripts = [response(('submit_diagnosis', proposed('set the playroom lights to {color}'))),
                          response(('submit_diagnosis', narrow)), response(('submit_diagnosis', narrow))]
    job = agent.enqueue(request_id=ident)
    await agent.run_job(job['id'])
    assert agent.get(job['id'])['status'] == 'done'
    feedback = json.loads(fake_openai.calls[1]['messages'][-1]['content'])
    preview = feedback['capture_previews'][0]['capture_preview']
    assert preview['steals_local'] == 1 and preview['takes_from_llm'] == 1 and preview['warnings']
    assert feedback['rejected_submissions'] == 0
    drafts = db.rows('SELECT * FROM changes')
    assert len(drafts) == 1 and drafts[0]['capture_preview']['steals_local'] == 0
    assert 'wildcards capture any words;' in SYSTEM_PROMPT


async def test_preview_visible_ui_rest_mcp(stack):
    settings, db, _, _, _ = stack
    store(db, text='set the playroom lights to blue', local=True, matcher={'source': 'builtin', 'intent': {'name': 'HassLightSet'}})
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Authorization': 'Bearer ' + settings.token}) as client:
            draft = (await client.post('/api/v1/changes', json=trigger_draft(['set the playroom lights to {color}']))).json()
            ident = draft['id']
            assert draft['capture_preview']['steals_local'] == 1
            assert (await client.get(f'/api/v1/changes/{ident}')).json()['capture_preview'] == draft['capture_preview']
            assert (await client.get('/api/v1/changes')).json()[0]['capture_preview'] == draft['capture_preview']
            await client.post('/login', data={'token': settings.token})
            page = (await client.get(f'/changes/{ident}')).text
            assert page.index('Offline capture preview') < page.index('Apply and replay')
            assert 'wildcards capture any words; prefer literal alternatives' in page
            assert 'would be stolen from existing local handling' in page
            headers = {'Accept': 'application/json, text/event-stream'}
            result = await client.post('/mcp/', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'get_change', 'arguments': {'id': ident}}})
            assert 'capture_preview' in result.text and 'steals_local' in result.text


def test_preview_migration(tmp_path):
    path = str(tmp_path / 'migration.db')
    db = DB(path)
    db.execute("INSERT INTO changes(id,title,status) VALUES (1,'keep','draft')")
    db.execute('ALTER TABLE changes DROP COLUMN capture_preview')
    db.meta('schema_version', '3')
    db.conn.close()
    db = DB(path)
    row = db.rows('SELECT * FROM changes')[0]
    assert row['title'] == 'keep' and row['capture_preview'] is None
    assert db.meta('schema_version') == '5'
    db.conn.close()

