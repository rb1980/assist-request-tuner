"""AI analysis through a Local OpenAI LLM server set up in Home Assistant, and every way that can be missing."""
import json
import httpx
import pytest
from app import ha_llm, llm
from app.config import Settings
from app.local_writer import make_writer
from app.main import create_app
from tests.test_portable import clean_env  # noqa: F401  (fixture)


def entries_file(config_dir, *entries):
    storage = config_dir / '.storage'
    storage.mkdir(parents=True, exist_ok=True)
    (storage / 'core.config_entries').write_text(json.dumps({'version': 1, 'data': {'entries': list(entries)}}))


def server(entry_id, url, key='', title='Local LLM Server', models=('qwen/qwen3.5-4b',), disabled=None):
    return {'entry_id': entry_id, 'domain': 'local_openai', 'title': title, 'disabled_by': disabled,
            'data': {'base_url': url, 'api_key': key, 'server_name': title, 'server_type': 'generic'},
            'subentries': [{'subentry_id': f'{entry_id}-{n}', 'subentry_type': 'conversation', 'title': f'{title}: {m}', 'data': {'model': m}}
                           for n, m in enumerate(models)]}


def test_parse_keeps_only_usable_local_openai_servers():
    text = json.dumps({'data': {'entries': [
        server('a', 'http://192.168.1.50:1234/v1/', key='k', models=('m1', 'm1', 'm2')),
        server('b', 'http://192.168.1.60:8080/v1', disabled='user'),          # disabled in HA
        server('c', 'not a url'),
        {'entry_id': 'd', 'domain': 'openai_conversation', 'data': {'api_key': 'secret'}},   # another integration: ignored
        'junk']}})
    found = ha_llm.parse(text)
    assert [(s['id'], s['base_url'], s['api_key'], [a['model'] for a in s['agents']]) for s in found] == \
        [('a', 'http://192.168.1.50:1234/v1', 'k', ['m1', 'm1', 'm2'])]
    # The agent picks the model; a server without agents has none to offer.
    assert ha_llm.choose(found, 'a/a-2')[1]['model'] == 'm2' and ha_llm.choose(found, 'a/gone')[1]['id'] == 'a-0'
    assert ha_llm.choose(ha_llm.parse(json.dumps({'data': {'entries': [server('e', 'http://x:1/v1', models=())]}})), '')[1] is None
    with pytest.raises(ValueError):
        ha_llm.parse('{not json')
    assert ha_llm.parse(json.dumps({'data': {'entries': []}})) == []


async def test_without_the_integration_or_config_access_analysis_is_off_with_a_reason(stack, tmp_path):
    settings, db, *_ = stack
    settings.ai_source = 'ha'
    # No config folder access at all (Docker without SFTP, or access "none").
    settings.ha_config_access = 'none'
    await ha_llm.resolve(settings, make_writer(db, settings), force=True)
    assert "can't read Home Assistant's config folder" in llm.unavailable(settings)
    # Access, but HA has no such integration.
    config_dir = tmp_path / 'ha'
    entries_file(config_dir, {'entry_id': 'x', 'domain': 'met', 'data': {}})
    settings.ha_config_access, settings.ha_config_dir = 'local', str(config_dir)
    await ha_llm.resolve(settings, make_writer(db, settings), force=True)
    assert 'No Local OpenAI LLM server is set up in Home Assistant' in llm.unavailable(settings)
    # Access, but the file is missing or broken.
    (config_dir / '.storage' / 'core.config_entries').write_text('{broken')
    await ha_llm.resolve(settings, make_writer(db, settings), force=True)
    assert "couldn't be read" in llm.unavailable(settings)
    # Custom stays independent of all this.
    settings.ai_source, settings.openrouter_api_key = 'custom', 'llm-placeholder'
    await ha_llm.resolve(settings, make_writer(db, settings), force=True)
    assert llm.unavailable(settings) == '' and settings.ai_ha_problem == ''


async def test_uses_the_chosen_server_follows_changes_and_never_writes_the_file(stack, tmp_path, fake_openai):
    settings, db, *_ = stack
    config_dir = tmp_path / 'ha'
    entries_file(config_dir, server('one', 'http://127.0.0.1:9/v1', key='ha-key-placeholder', title='Old'),
                 server('two', fake_openai.url, title='LM Studio', models=('voice/small', 'some/model')))
    settings.ai_source, settings.ai_ha_server = 'ha', 'two/two-1'
    settings.diagnose_model, settings.diagnose_daily_usd = 'custom/model', 5.0   # Custom's values: not used with HA
    settings.ha_config_access, settings.ha_config_dir = 'local', str(config_dir)
    settings.openrouter_api_key = 'custom-placeholder'   # a custom key is never sent to HA's server
    writer = make_writer(db, settings)
    found, problem = await ha_llm.resolve(settings, writer, force=True)
    assert problem is None and [s['id'] for s in found] == ['one', 'two']
    assert (settings.ai_ha_url, settings.ai_ha_title, llm.unavailable(settings)) == (fake_openai.url, 'LM Studio', '')
    assert llm.effective(settings) == dict(model='some/model', reasoning='default', price_in=0.0, price_out=0.0, budget=0.0)
    client = llm.Client(settings, db)
    assert client.model == 'some/model'
    async with httpx.AsyncClient() as http:
        assert (await client.post(http, client.body([{'role': 'user', 'content': 'hi'}]))).status_code == 200
    assert fake_openai.auth == [None]   # no key in HA: no Authorization header at all
    assert fake_openai.calls[-1]['model'] == 'some/model' and 'reasoning' not in fake_openai.calls[-1]
    # Removed in HA: the other server is used; all removed: off, with the reason.
    entries_file(config_dir, server('one', 'http://127.0.0.1:9/v1', key='ha-key-placeholder', title='Old'))
    await ha_llm.resolve(settings, writer, force=True)
    assert (settings.ai_ha_title, settings.ai_ha_key) == ('Old', 'ha-key-placeholder')
    entries_file(config_dir, server('one', 'http://127.0.0.1:9/v1', title='Bare', models=()))
    await ha_llm.resolve(settings, writer, force=True)
    assert settings.ai_ha_url == '' and '“Bare” has no conversation agent' in llm.unavailable(settings)
    entries_file(config_dir)
    await ha_llm.resolve(settings, writer, force=True)
    assert settings.ai_ha_url == '' and 'No Local OpenAI LLM server' in llm.unavailable(settings)
    with pytest.raises(ValueError, match='read only'):
        await writer.write('{}', 'config_entries')


async def test_settings_page_offers_ha_only_when_found_and_test_uses_it(clean_env, fake_ha, fake_openai, tmp_path, monkeypatch):  # noqa: F811
    config_dir = tmp_path / 'ha'
    # As sources, so they survive the reload that saving does.
    for name, value in (('HA_URL', fake_ha.url), ('HA_TOKEN', 'ha-placeholder'), ('HA_CONFIG_ACCESS', 'local'), ('HA_CONFIG_DIR', str(config_dir))):
        monkeypatch.setenv(name, value)
    entries_file(config_dir, {'entry_id': 'x', 'domain': 'met', 'data': {}})
    settings = Settings(token='api-placeholder', ha_url=fake_ha.url, ha_token='ha-placeholder', db_path=str(clean_env / 'db.sqlite'),
                        ha_config_access='local', ha_config_dir=str(config_dir))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/login', data={'token': 'api-placeholder'})
            page = (await client.get('/settings')).text
            assert '(none set up in Home Assistant)' in page and 'value="ha"' in page and 'value="custom" selected' in page
            entries_file(config_dir, server('srv', fake_openai.url, title='Home LLM', models=('some/model',)))
            (clean_env / 'settings.json').write_text(json.dumps({'DIAGNOSE_MODEL': 'other/model'}))   # an earlier Custom model
            ha_llm.CACHE_S, cache = 0, ha_llm.CACHE_S
            try:
                page = (await client.get('/settings')).text
                assert '(none set up' not in page and 'value="srv/srv-0" data-model="some/model" selected' in page
                assert 'some/model · Home LLM · ' + fake_openai.url in page and 'data-from-ha="default"' in page
                # As the browser sends it: the greyed-out fields (model, reasoning, prices, budget) are not in the form.
                base = {'HA_URL': fake_ha.url, 'HA_CONFIG_ACCESS': 'local', 'TZ': 'UTC', 'AI_SOURCE': 'ha', 'AI_HA_SERVER': 'srv/srv-0'}
                result = (await client.post('/ui/settings/test/ai', data=base)).json()
                assert result['message'].startswith(f'Using “Home LLM” from Home Assistant: some/model at {fake_openai.url}.')
                assert 'models' not in result and fake_openai.calls[-1]['model'] == 'some/model'
                saved = await client.post('/settings', data=base, follow_redirects=True)
                assert '“Home LLM” from Home Assistant' in saved.text and settings.ai_source == 'ha' and settings.ai_ha_url == fake_openai.url
                saved_file = json.loads((clean_env / 'settings.json').read_text())
                assert saved_file['AI_SOURCE'] == 'ha' and saved_file['DIAGNOSE_MODEL'] == 'other/model'   # kept for Custom
                assert app.state.service.diagnose.configuration()['model'] == 'some/model'
            finally:
                ha_llm.CACHE_S = cache
