import copy
import pytest
from app.parse import parse_run
from app.classify import classify
from app.config import Settings
from app.patterns import normalize, pattern
from app.db import DB
from app.changes import ChangeDraft, changes_devices
from conftest import fixture


def test_voice_parser():
    r = parse_run(fixture('run_voice'))
    assert r['source'] == 'voice'
    assert r['transcript'] == "Turn on Sam's office."
    assert r['processed_locally'] is True
    assert r['device_id'] == '359bda8ac92542d1bc3f517c0f5abbad'
    assert r['reply'] == 'Activated'
    assert r['duration_s'] == pytest.approx(4.114182)
    assert r['llm_s'] is None


def test_text_parser():
    r = parse_run(fixture('run_text_llm_tool'))
    assert r['source'] == 'text'
    assert r['processed_locally'] is False
    assert r['device_id'] is None
    assert r['tools'][0]['args'] == {'name': 'Test Lamp', 'brightness': 30}
    assert r['tools'][0]['result']['data']['success'][0]['id'] == 'light.test_lamp'
    assert r['reply'] == 'Test Lamp dimmed to thirty percent.'
    assert r['llm_s'] == pytest.approx(4.059452)


def test_in_progress_and_truncation():
    payload = fixture('run_text_llm_tool')
    payload['events'] = payload['events'][:-2]
    for event in payload['events']:
        delta = (event.get('data') or {}).get('chat_log_delta', {})
        if 'tool_result' in delta:
            delta['tool_result'] = 'x' * 3000
    row = parse_run(payload)
    assert row['status'] == 'in_progress'
    assert len(row['tools'][0]['result']) == 2000
    assert row['reply'].startswith('Test Lamp')


@pytest.mark.parametrize('tools,text,local,topic', [
    (['assist__script__home_readings'], 'hello', False, 'home'),
    (['homeassistant__GetLiveContext'], 'hello', False, 'home'),
    (['home-web-search__web_search'], 'room', False, 'general'),
    (['home-web-search__world_time'], 'time', False, 'general'),
    ([], 'light please', False, 'home'),
    ([], 'a poem', False, 'general'),
    ([], 'activate anything', True, 'home'),
    ([], '', None, 'unknown'),
    ([], 'space', False, 'general'),
])
def test_topics(tools, text, local, topic):
    row = dict(tools=[{'name': n} for n in tools], transcript=text, processed_locally=local, status='complete')
    assert classify(row, None, Settings())['topic'] == topic


def test_registry_and_trigger_classification():
    r = parse_run(fixture('run_voice'))
    assert classify(r, fixture('matcher_debug')['results'][0], Settings())['handled_by'] == 'local_trigger'
    r.update(processed_locally=False, transcript='activate cozy glow')
    assert classify(r, None, Settings(), [{'name': 'cozy glow', 'exposed': True, 'aliases': [None]}])['topic'] == 'home'
    r['error'] = True
    assert classify(r, None, Settings())['handled_by'] == 'error'


def test_patterns():
    assert normalize('Hey Jarvis, please, can you DIM the Test Lamp?!') == 'dim the test lamp'
    assert pattern('dim the test lamp to 30 percent', [{'name': 'Test Lamp', 'aliases': [None, 'lamp'], 'exposed': True}]) == 'dim the {name} to {number} percent'
    assert pattern('Kitchen lights in living room', [{'name': 'Kitchen lights', 'aliases': [], 'exposed': True}], [{'name': 'Kitchen'}, {'name': 'Living Room'}]) == '{name} in {area}'
    assert normalize('computer do this', ['computer']) == 'do this'


def test_migration_empty_and_reopen(tmp_path):
    path = str(tmp_path / 'db')
    db = DB(path)
    assert db.meta('schema_version') == '5'
    assert db.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert {r['name'] for r in db.rows("SELECT name FROM sqlite_master WHERE type='table'")} >= {'requests', 'patterns', 'changes', 'changes_log', 'ignore_windows', 'meta'}
    db.conn.close()
    db = DB(path)
    assert db.meta('schema_version') == '5'
    db.conn.close()


@pytest.mark.parametrize('payload', [
    {'entity_id': '../bad', 'add': ['x']}, {'entity_id': 'light.x', 'add': [None]},
    {'entity_id': 'light.x', 'add': []}, {'entity_id': 'light.x', 'add': ['x'], 'unexpected': 1},
])
def test_invalid_payload(payload):
    with pytest.raises(ValueError):
        ChangeDraft(type='entity_alias', title='test', payload=payload)


@pytest.mark.parametrize('actions,expected', [
    ([{'variables': {'x': 1}}, {'set_conversation_response': '{{ x }}'}], False),
    ([{'choose': [{'conditions': [], 'sequence': [{'set_conversation_response': 'ok'}]}], 'default': [{'stop': 'done'}]}], False),
    ([{'if': [], 'then': [{'action': 'light.turn_on'}]}], True),
    ([{'choose': [], 'default': [{'service': 'switch.turn_off'}]}], True),
    ([{'parallel': [{'set_conversation_response': 'x'}]}], True),
    ([{'set_conversation_response': 'x', 'action': 'light.turn_on'}], True),
    ([{'action': '{{ dynamic }}'}], True),
])
def test_device_action_check(actions, expected):
    assert changes_devices(actions) is expected


def test_real_automation_readonly():
    assert not changes_devices(fixture('automation_config_voice')['config']['actions'])
    assert not changes_devices([{'action': 'weather.get_forecasts'}], ['weather.get_forecasts'])
    assert changes_devices([{'action': 'weather.get_forecasts'}])


@pytest.mark.parametrize('exposed', [False, None, True])
def test_pattern_and_topic_use_only_assist_exposed_entities(exposed):
    entities = [{'name': 'Temperature', 'aliases': ['warmth'], 'exposed': exposed},
                {'name': 'Outside', 'aliases': [], 'exposed': False}]
    areas = [{'name': 'Outside', 'aliases': ['garden']}]
    expected = '{name} {name} {area}' if exposed is True else 'temperature warmth {area}'
    assert pattern('temperature warmth outside', entities, areas) == expected
    row = dict(transcript='warmth', tools=[], processed_locally=False, status='complete')
    assert classify(row, None, Settings(keywords=[]), entities, areas)['topic'] == ('home' if exposed is True else 'general')
    row['transcript'] = 'garden'
    assert classify(row, None, Settings(keywords=[]), entities, areas)['topic'] == 'home'


def test_examples_are_generic_and_parse():
    from pathlib import Path
    import yaml
    lines = Path('.env.example').read_text().splitlines()
    assert 'HA_URL=http://homeassistant.local:8123' in lines
    assert 'HA_TOKEN=replace-with-admin-long-lived-token' in lines
    assert 'AR_TUNER_TOKEN=replace-with-a-random-token' in lines
    example = yaml.safe_load(Path('config.example.yaml').read_text())
    assert example['home_assistant']['url'] == 'http://homeassistant.local:8123' and example['ar_tuner']['token'] == ''
    from app.config import KEYS
    assert all(isinstance(path, str) for path in KEYS.values())


def test_formatted_templates_preserve_editable_values():
    from pathlib import Path
    from html.parser import HTMLParser
    from jinja2 import Environment, FileSystemLoader
    import json

    class Textareas(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.values = []

        def handle_starttag(self, tag, attrs):
            if tag == 'textarea':
                self.active = True
                self.values.append('')

        def handle_endtag(self, tag):
            if tag == 'textarea':
                self.active = False

        def handle_data(self, data):
            if self.active:
                self.values[-1] += data

    env = Environment(loader=FileSystemLoader('app/templates'), autoescape=True)
    env.filters['pretty'] = lambda value: json.dumps(value, indent=2, ensure_ascii=False)
    draft = {'title': '<reading lamp>', 'payload': {'add': ['reading glow']}}
    parser = Textareas()
    parser.feed(env.get_template('draft.html').render(draft=draft, status={'collector': 'up'}))
    assert parser.values == [env.filters['pretty'](draft)]
    parser = Textareas()
    sentence = '  first <sentence>\nsecond sentence  '
    parser.feed(env.get_template('matcher.html').render(sentence=sentence))
    assert parser.values == [sentence]
    for path in Path('app/templates').glob('*.html'):
        assert len(path.read_text().splitlines()) > 5
        env.parse(path.read_text())


def test_startup_url_diagnostics_omit_credentials_and_query():
    from app.ha import safe_url
    settings = Settings(ha_url='http://user:password-placeholder@127.0.0.1:8123/?token=query-placeholder', ha_token='ha-placeholder', token='app-placeholder')
    assert safe_url(settings) == 'http://127.0.0.1:8123/'


def test_refresh_runs_first_time_on_a_freshly_booted_host(monkeypatch):
    import asyncio
    from app.ha import HA
    from app.config import Settings
    monkeypatch.setattr('time.monotonic', lambda: 5.0)  # host booted 5 s ago
    ha = HA(Settings(ha_token='x', token='y'))
    calls = []
    async def fake_call(kind, **fields):
        calls.append(kind)
        return {'exposed_entities': {}} if kind == 'homeassistant/expose_entity/list' else []
    ha.call = fake_call
    asyncio.run(ha.refresh())
    assert 'config/entity_registry/list' in calls


def test_area_forms_add_a_spoken_form_without_punctuation():
    from app.phrasing import area_forms
    forms = area_forms([{'area_id': 'office_bar', 'name': 'Study/Library', 'aliases': []}], {'office_bar': ['light.x']})
    assert forms['study library'] == 'office_bar' and forms['study/library'] == 'office_bar'
