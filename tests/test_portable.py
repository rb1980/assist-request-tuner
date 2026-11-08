import json
import os
import httpx
import pytest
from app import config
from app.config import Settings
from app.db import DB
from app.local_writer import LocalWriter, DisabledWriter, make_writer
from app.main import create_app
from tests.test_integration import store


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in list(config.KEYS) + list(config.ALIASES) + ['SUPERVISOR_TOKEN']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AR_TUNER_SETTINGS', str(tmp_path / 'settings.json'))
    monkeypatch.setenv('AR_TUNER_CONFIG', str(tmp_path / 'config.yaml'))
    monkeypatch.setenv('AR_TUNER_OPTIONS', str(tmp_path / 'options.json'))
    return tmp_path


def test_precedence_file_then_options_then_env(clean_env, monkeypatch):
    (clean_env / 'config.yaml').write_text('home_assistant:\n  url: http://ha.example:8123\n  ssh: {port: 2222}\n'
                                           'collector:\n  ignore_pipelines: [Test pipeline]\ndiagnose:\n  model: file-model\n'
                                           '  provider: {zdr: false}\n  nightly: true\n')
    s = Settings()
    assert (s.ha_url, s.ha_ssh_port, s.ignore_pipelines, s.diagnose_model) == ('http://ha.example:8123', 2222, ['Test pipeline'], 'file-model')
    assert s.diagnose_provider == {'zdr': False} and s.nightly_diagnose is True
    assert s.ha_ssh_host == 'ha.example'  # derived from the HA URL
    (clean_env / 'options.json').write_text(json.dumps({'diagnose_model': 'option-model'}))
    assert Settings().diagnose_model == 'option-model'
    monkeypatch.setenv('DIAGNOSE_MODEL', 'env-model')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'old-name-placeholder')
    assert Settings().diagnose_model == 'env-model' and Settings().openrouter_api_key == 'old-name-placeholder'


def test_neutral_defaults(clean_env):
    s = Settings()
    assert s.ha_url == 'http://homeassistant.local:8123' and s.timezone == 'UTC' and s.ignore_pipelines == []
    assert s.ha_config_access == 'sftp' and s.websocket_url() == 'ws://homeassistant.local:8123/api/websocket'
    assert Settings(ha_url='https://ha.example').websocket_url() == 'wss://ha.example/api/websocket'


def test_app_mode_uses_supervisor(clean_env, monkeypatch):
    monkeypatch.setenv('SUPERVISOR_TOKEN', 'supervisor-placeholder')
    monkeypatch.setenv('DB_PATH', str(clean_env / 'ar_tuner.db'))
    s = Settings()
    assert (s.ha_url, s.ha_token, s.ha_config_access, s.supervised) == ('http://supervisor/core', 'supervisor-placeholder', 'local', True)
    assert s.websocket_url() == 'ws://supervisor/core/websocket'
    assert len(s.token) > 30 and Settings().token == s.token  # generated once, then reused
    assert oct((clean_env / 'api_token').stat().st_mode & 0o777) == '0o600'


async def test_local_writer_confined_atomic_and_refuses_symlinks(tmp_path):
    db = DB(str(tmp_path / 'db.sqlite'))
    base = tmp_path / 'ha'
    base.mkdir()
    (base / 'blueprints' / 'automation').mkdir(parents=True)
    settings = Settings(ha_config_dir=str(base), ha_config_access='local')
    writer = make_writer(db, settings)
    assert isinstance(writer, LocalWriter) and writer.configuration()['configured']
    assert await writer.read() is None
    await writer.write('language: en\n')
    assert (base / 'custom_sentences' / 'en' / 'ar_tuner.yaml').read_text() == 'language: en\n'
    assert await writer.read() == 'language: en\n' and not (base / 'custom_sentences' / 'en' / 'ar_tuner.yaml.tmp').exists()
    await writer.write('bp\n', 'blueprint')
    assert (base / 'blueprints' / 'automation' / 'ar_tuner' / 'voice_reply.yaml').read_text() == 'bp\n'
    await writer.write(None)
    assert not (base / 'custom_sentences' / 'en' / 'ar_tuner.yaml').exists()
    with pytest.raises(ValueError):
        writer.managed('../../etc/passwd')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (base / 'custom_sentences' / 'en').rmdir()
    os.symlink(outside, base / 'custom_sentences' / 'en')
    with pytest.raises(ValueError, match='symlinks'):
        await writer.write('x')
    assert not any(outside.iterdir())
    missing = make_writer(db, Settings(ha_config_dir=str(tmp_path / 'absent'), ha_config_access='local'))
    assert not missing.configuration()['configured'] and 'not mounted' in missing.configuration()['state']
    assert isinstance(make_writer(db, Settings(ha_config_dir=str(base), ha_config_access='none')), DisabledWriter)
    with pytest.raises(ValueError):
        make_writer(db, Settings(ha_config_dir=str(base), ha_config_access='ftp'))
    db.conn.close()


async def test_ingress_prefixes_links_and_redirects_but_api_needs_token(fake_ha, tmp_path):
    settings = Settings(token='api-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'db.sqlite'),
                        supervised=True, ha_config_access='none')
    app = create_app(settings, start_collector=False)
    root = '/api/hassio_ingress/abc123'
    async with app.router.lifespan_context(app):
        store(app.state.service.db, text='make it cozy', pattern='make it cozy')
        proxy = httpx.ASGITransport(app=app, client=('172.30.32.2', 4000))
        async with httpx.AsyncClient(transport=proxy, base_url='http://testserver', headers={'x-ingress-path': root}) as client:
            page = await client.get('/')
            assert page.status_code == 200 and f'href="{root}/history"' in page.text and f'href="{root}/static/style.css"' in page.text
            assert f'data-root="{root}"' in page.text and 'href="/history"' not in page.text
            # HA shows the panel in an iframe on its own origin; no logout (the HA login is the only one).
            assert "frame-ancestors 'self'" in page.headers['content-security-policy'] and '/logout' not in page.text
            hidden = await client.post('/fix/hide', data={'pattern': 'make it cozy'})
            assert hidden.status_code == 303 and hidden.headers['location'] == root + '/'
            # A browser form post through HA: HA's no-referrer policy makes the Origin "null"; Sec-Fetch-Site vouches.
            shown = await client.post('/fix/unhide', data={'pattern': 'make it cozy'}, headers={'origin': 'null', 'sec-fetch-site': 'same-origin'})
            assert shown.status_code == 303 and shown.headers['location'] == root + '/'
            # On plain http (homeassistant.local:8123) browsers send no Sec-Fetch-Site at all; HA's SameSite=Strict
            # ingress cookie is what keeps other sites out there.
            plain = await client.post('/fix/unhide', data={'pattern': 'make it cozy'}, headers={'origin': 'null'})
            assert plain.status_code == 303
            for forged in ({'origin': 'null', 'sec-fetch-site': 'cross-site'},
                           {'sec-fetch-site': 'same-site'}, {'origin': 'http://evil.example'}):
                assert (await client.post('/fix/hide', data={'pattern': 'make it cozy'}, headers=forged)).status_code >= 400, forged
            assert (await client.get('/api/v1/status')).status_code == 401
            assert (await client.get('/api/v1/status', headers={'Authorization': 'Bearer api-placeholder'})).status_code == 200
            assert 'Show API token' in (await client.get('/settings')).text
        # The same header from anywhere else is not trusted.
        other = httpx.ASGITransport(app=app, client=('10.1.2.3', 4000))
        async with httpx.AsyncClient(transport=other, base_url='http://testserver', headers={'x-ingress-path': root}) as client:
            page = await client.get('/')
            assert page.status_code == 303 and page.headers['location'] == '/login'
            # Directly (no ingress), a same-host Origin still works and another site's does not.
            await client.post('/login', data={'token': 'api-placeholder'})
            assert (await client.post('/fix/hide', data={'pattern': 'make it cozy'}, headers={'origin': 'http://testserver', 'sec-fetch-site': 'same-origin'})).status_code == 303
            assert (await client.post('/fix/hide', data={'pattern': 'make it cozy'}, headers={'origin': 'http://evil.example', 'sec-fetch-site': 'cross-site'})).status_code >= 400
            assert (await client.post('/fix/hide', data={'pattern': 'make it cozy'}, headers={'origin': 'null'})).status_code >= 400   # not ingress: needs Sec-Fetch-Site
            assert "frame-ancestors 'none'" in (await client.get('/login')).headers['content-security-policy']


async def test_login_behind_tls_proxy_but_not_from_other_sites(fake_ha, tmp_path):
    settings = Settings(token='api-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'db.sqlite'),
                        ha_config_access='none')
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://ar-tuner.example') as client:
            # The browser saw https; the proxy forwarded plain http with the same host.
            ok = await client.post('/login', data={'token': 'api-placeholder'}, headers={'origin': 'https://ar-tuner.example'})
            assert ok.status_code == 303
            other = await client.post('/login', data={'token': 'api-placeholder'}, headers={'origin': 'https://evil.example'})
            assert other.status_code != 303 and 'Cross-origin' in other.text


async def test_rejected_token_is_explained_and_not_retried_quickly(fake_ha, tmp_path):
    from app.ha import HA, HAError
    fake_ha.reject_token = True
    ha = HA(Settings(ha_url=fake_ha.url, ha_token='revoked-placeholder', token='x', db_path=str(tmp_path / 'db.sqlite')))
    try:
        for _ in range(3):
            with pytest.raises(HAError, match='rejected the access token Request Tuner uses'):
                await ha.call('assist_pipeline/pipeline/list')
        assert fake_ha.auth_attempts == 1   # one failed login, then it waits instead of hammering HA
        # REST calls stop too, without sending the bad token again.
        with pytest.raises(HAError, match='long-lived'):
            await ha.rest('GET', 'states')
    finally:
        await ha.close()


async def test_settings_page_saves_applies_live_and_hides_secrets(clean_env, fake_ha, monkeypatch):
    monkeypatch.setenv('HA_TOKEN', 'env-ha-placeholder')
    monkeypatch.setenv('HA_URL', fake_ha.url)
    settings = Settings(token='api-placeholder', ha_url=fake_ha.url, db_path=str(clean_env / 'db.sqlite'), ha_config_access='none')
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        ha = app.state.service.ha
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/login', data={'token': 'api-placeholder'})
            page = (await client.get('/settings')).text
            assert 'set — type to replace' in page and 'env-ha-placeholder' not in page and 'From: Environment variable' in page
            # Invalid values: nothing is saved.
            bad = await client.post('/settings', data={'HA_URL': 'homeassistant:8123', 'TZ': 'Mars/Olympus', 'HA_CONFIG_ACCESS': 'none'})
            assert bad.status_code == 200 and 'Nothing was saved' in bad.text and 'valid time zone' in bad.text
            assert not (clean_env / 'settings.json').exists()
            # A stuck rejected-token state is cleared by saving a new token.
            ha.auth_failed, ha.retry_at = True, 10 ** 12
            saved = await client.post('/settings', data={'HA_URL': fake_ha.url + '/', 'HA_TOKEN': 'new-ha-placeholder', 'HA_CONFIG_ACCESS': 'none',
                                                          'LLM_API_KEY': 'llm-placeholder', 'DIAGNOSE_MODEL': 'some/model', 'DIAGNOSE_DAILY_USD': '2.5',
                                                          'DIAGNOSE_REASONING': 'low', 'NIGHTLY_DIAGNOSE': '1', 'IGNORE_PIPELINES': 'Test, Other ,',
                                                          'TZ': 'Europe/Berlin', 'RETENTION_DAYS': '30'})
            assert saved.status_code == 303 and 'saved=1' in saved.headers['location'] and 'ok=1' in saved.headers['location']
            assert (settings.ha_token, settings.ha_url, settings.diagnose_model, settings.diagnose_daily_usd) == ('new-ha-placeholder', fake_ha.url, 'some/model', 2.5)
            assert settings.openrouter_api_key == 'llm-placeholder' and settings.nightly_diagnose and settings.ignore_pipelines == ['Test', 'Other']
            assert settings.timezone == 'Europe/Berlin' and settings.retention == 30 and settings.token == 'api-placeholder'
            assert not ha.auth_failed and ha.retry_at == 0
            assert oct((clean_env / 'settings.json').stat().st_mode & 0o777) == '0o600'
            await ha.call('assist_pipeline/pipeline/list')   # reconnects with the new token
            page = (await client.get('/settings')).text
            assert 'llm-placeholder' not in page and 'new-ha-placeholder' not in page and 'From: Saved here' in page
            # Empty secret keeps it; Clear removes the saved value, so the environment's applies again.
            await client.post('/settings', data={'HA_URL': fake_ha.url, 'HA_CONFIG_ACCESS': 'none', 'HA_TOKEN__clear': '1'})
            assert settings.ha_token == 'env-ha-placeholder' and settings.openrouter_api_key == 'llm-placeholder'
            assert settings.diagnose_model == 'qwen/qwen3.8-27b' and not settings.nightly_diagnose   # emptied fields fall back
            # A rejected token is reported after saving.
            fake_ha.reject_token = True
            rejected = await client.post('/settings', data={'HA_URL': fake_ha.url, 'HA_CONFIG_ACCESS': 'none'})
            assert 'ok=0' in rejected.headers['location'] and 'rejected' in rejected.headers['location']


async def test_settings_save_pins_only_changed_fields(clean_env, fake_ha, monkeypatch):
    monkeypatch.setenv('HA_URL', fake_ha.url)
    monkeypatch.setenv('HA_TOKEN', 'env-ha-placeholder')
    monkeypatch.setenv('IGNORE_PIPELINES', 'Voice Eval Relay')
    monkeypatch.setenv('HA_CONFIG_ACCESS', 'none')
    settings = Settings(token='api-placeholder', db_path=str(clean_env / 'db.sqlite'))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/login', data={'token': 'api-placeholder'})
            # Everything as the form shows it, plus a new token and model.
            form = {'HA_URL': fake_ha.url, 'HA_TOKEN': 'new-ha-placeholder', 'HA_CONFIG_ACCESS': 'none', 'HA_SSH_PORT': '22', 'HA_SSH_USER': 'root',
                    'LLM_BASE_URL': 'https://openrouter.ai/api/v1', 'DIAGNOSE_MODEL': 'other/model', 'DIAGNOSE_REASONING': 'medium',
                    'DIAGNOSE_DAILY_USD': '1.0', 'IGNORE_PIPELINES': 'Voice Eval Relay', 'RETENTION_DAYS': '365', 'TZ': 'UTC'}
            await client.post('/settings', data=form)
            saved = sorted(json.loads((clean_env / 'settings.json').read_text())); assert saved == ['DIAGNOSE_MODEL', 'HA_TOKEN'], saved
            # The environment still drives the untouched fields.
            monkeypatch.setenv('IGNORE_PIPELINES', 'Another')
            await client.post('/settings', data=dict(form, IGNORE_PIPELINES='Another'))
            assert settings.ignore_pipelines == ['Another'] and settings.diagnose_model == 'other/model'
            saved = sorted(json.loads((clean_env / 'settings.json').read_text())); assert saved == ['DIAGNOSE_MODEL', 'HA_TOKEN'], saved


async def test_settings_test_buttons_use_unsaved_form_values(clean_env, fake_ha, fake_openai, monkeypatch):
    monkeypatch.setenv('HA_URL', fake_ha.url)
    monkeypatch.setenv('HA_TOKEN', 'env-ha-placeholder')
    settings = Settings(token='api-placeholder', db_path=str(clean_env / 'db.sqlite'), ha_config_access='none')
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/login', data={'token': 'api-placeholder'})
            assert 'data-test="ai"' in (await client.get('/settings')).text
            base = {'HA_URL': fake_ha.url, 'HA_CONFIG_ACCESS': 'none', 'TZ': 'UTC'}

            async def run(section, **values):
                return (await client.post(f'/ui/settings/test/{section}', data=dict(base, **values))).json()

            result = await run('ha')
            assert result['ok'] and '2026.11.0' in result['message']
            assert not (await run('ha', HA_URL='http://127.0.0.1:9')).get('ok')          # unreachable
            assert 'http://' in (await run('ha', HA_URL='nope'))['message']               # doesn't parse
            assert 'Nothing to test' in (await run('writer'))['message']
            assert not (await run('ai'))['ok']                                           # no key yet
            ai = dict(LLM_BASE_URL=fake_openai.url, LLM_API_KEY='llm-placeholder')
            listed = await run('ai', **ai)                     # the default model isn't offered here: list the ones that are
            assert [m['id'] for m in listed['models']] == ['other/model', 'some/model'] and 'choose one from the list' in listed['message']
            assert 'isn’t one of them' in (await run('ai', DIAGNOSE_MODEL='nope/model', **ai))['message'].replace("'", '’')
            # A Cerebras-like provider: rejects OpenRouter's routing and reasoning object, accepts reasoning_effort.
            fake_openai.unsupported = {'provider', 'reasoning'}
            fake_openai.scripts = [{'choices': [{'message': {'role': 'assistant', 'tool_calls': [
                {'id': 'c1', 'type': 'function', 'function': {'name': 'list_areas', 'arguments': '{}'}}]}}]}]
            result = await run('ai', DIAGNOSE_MODEL='some/model', **ai)
            call = fake_openai.calls[-1]
            assert result['ok'] and 'calls tools' in result['message'] and 'Privacy routing' in result['message'], result
            assert call['tools'] and call['reasoning_effort'] == 'medium' and 'provider' not in call and 'reasoning' not in call
            assert len(fake_openai.calls) == 3   # learned in two rejections
            fake_openai.scripts = [{'choices': [{'message': {'role': 'assistant', 'content': 'Here are the areas.'}}]}]
            again = await run('ai', DIAGNOSE_MODEL='some/model', **ai)
            assert not again['ok'] and "didn't call a tool" in again['message'] and len(fake_openai.calls) == 4  # remembered
            result = await run('collector', IGNORE_PIPELINES='House, Missing pipeline')
            assert not result['ok'] and '“Missing pipeline”' in result['message'] and 'Watching 0 pipelines' in result['message']
            assert (await run('collector'))['ok']
            fake_ha.reject_token = True
            assert 'rejected' in (await run('ha', HA_TOKEN='wrong-placeholder'))['message']
            assert not (clean_env / 'settings.json').exists()   # testing never saves


async def test_ai_client_learns_what_a_provider_rejects():
    import httpx, json as _json
    from app import llm
    from app.config import Settings
    from app.db import DB
    import tempfile, os
    db = DB(os.path.join(tempfile.mkdtemp(), 'llm.sqlite'))
    settings = Settings(llm_base_url='https://api.example.test/v1', openrouter_api_key='k-placeholder', diagnose_model='m')
    client = llm.Client(settings, db)
    full = client.body([], tools=[{'type': 'function'}])
    assert full['provider'] == {'zdr': True} and full['reasoning'] == {'effort': 'medium'} and full['reasoning_effort'] == 'medium'
    settings.diagnose_reasoning = 'default'
    assert 'reasoning' not in client.body([]) and 'reasoning_effort' not in client.body([])
    settings.diagnose_reasoning = 'medium'

    sent = []
    errors = {'provider': "Unrecognized request argument supplied: provider",                       # OpenAI
              'reasoning': "Extra inputs are not permitted (body.reasoning)",                           # Mistral / pydantic
              'max_tokens': "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.",
              'reasoning_effort': "reasoning_effort is not supported with this model k-placeholder"}
    def reply(request):
        payload = _json.loads(request.content)
        sent.append(payload)
        for name, message in errors.items():
            if name in payload:
                return httpx.Response(400, json={'error': {'message': message}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        response = await client.post(http, client.body([]))
        assert response.status_code == 200 and len(sent) == 5
        assert set(sent[-1]) == {'model', 'messages', 'max_completion_tokens'}
        assert client.profile == {'unsupported': ['provider', 'reasoning', 'reasoning_effort'], 'token_field': 'max_completion_tokens'}
        sent.clear()
        await llm.Client(settings, db).post(http, llm.Client(settings, db).body([]))   # remembered: right first time
        assert len(sent) == 1

    # Another failure that merely names a field must not drop it (privacy routing stays on).
    other = llm.Client(settings, db, model='other')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            400, json={'error': {'message': 'No endpoints found matching your data policy (provider: zdr)'}}))) as http:
        response = await other.post(http, other.body([]))
    assert response.status_code == 400 and other.profile == {}
    assert 'k-placeholder' not in llm.error_text(httpx.Response(400, json={'message': 'bad k-placeholder'}), 'k-placeholder')


async def test_ai_models_list_reads_tools_and_prices():
    import httpx
    from app import llm
    from app.config import Settings
    settings = Settings(llm_base_url='https://openrouter.example.test/api/v1', openrouter_api_key='k-placeholder')
    listing = {'data': [{'id': 'b/tools', 'supported_parameters': ['tools', 'reasoning'], 'pricing': {'prompt': '0.0000002', 'completion': '0.0000025'}},
                        {'id': 'a/plain', 'supported_parameters': ['temperature']}, {'id': 'c/unknown'}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=listing))) as http:
        models = await llm.Client(settings).models(http)
    assert models == [dict(id='a/plain', tools=False, price_in=None, price_out=None),
                      dict(id='b/tools', tools=True, price_in=0.2, price_out=2.5),
                      dict(id='c/unknown', tools=None, price_in=None, price_out=None)]
async def test_ai_rate_limits_are_waited_out():
    import httpx
    from app import llm
    from app.config import Settings
    settings = Settings(llm_base_url='https://api.cerebras.ai/v1', openrouter_api_key='k-placeholder')
    client = llm.Client(settings)
    replies = [httpx.Response(429, headers={'retry-after': '3'}, json={'message': 'Requests per minute limit exceeded'}),
               httpx.Response(429, json={'message': 'Requests per minute limit exceeded'}),
               httpx.Response(200, json={'choices': []})]
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: replies.pop(0))) as http:
        response = await client.post(http, client.body([]), sleep=sleep)
    assert response.status_code == 200 and waits == [3.0, 10.0]
    always = lambda request: httpx.Response(429, json={'message': 'slow down'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(always)) as http:
        response = await client.post(http, client.body([]), retries=2, sleep=sleep)
    assert response.status_code == 429 and llm.error_text(response, None) == 'slow down'


async def test_analysis_button_names_the_model_and_estimates_cost(clean_env, fake_ha, monkeypatch):
    monkeypatch.setenv('HA_URL', fake_ha.url)
    monkeypatch.setenv('HA_TOKEN', 'env-ha-placeholder')
    settings = Settings(token='api-placeholder', db_path=str(clean_env / 'db.sqlite'), ha_config_access='none',
                        llm_base_url='https://api.cerebras.ai/v1', openrouter_api_key='k-placeholder', diagnose_model='qwen-3.8-27b')
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        service = app.state.service
        agent = service.diagnose
        assert agent.host() == 'cerebras.ai' and agent.estimate() == 'about 3 cents'     # typical tokens at the default prices
        with service.db.conn:
            service.db.conn.execute("INSERT INTO diagnoses(target_type, target, status, tokens_in, tokens_out) VALUES ('pattern', 'x', 'done', 70000, 6000)")
        settings.diagnose_price_in, settings.diagnose_price_out = 0.05, 0.2
        assert agent.estimate() == 'under 1 cent'
        settings.diagnose_price_in = settings.diagnose_price_out = 0
        assert agent.estimate() == 'no cost at the prices set'
        settings.llm_base_url = 'http://localhost:11434/v1'
        assert agent.host() == 'localhost:11434'
        store(service.db, text='make the den cozy', pattern='make den cozy')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            await client.post('/login', data={'token': 'api-placeholder'})
            page = (await client.get('/fix/pattern', params={'p': 'make den cozy'})).text
            assert 'Uses qwen-3.8-27b on localhost:11434, no cost at the prices set.' in page and 'OpenRouter' not in page


async def test_saved_ai_setups_switch_back_and_forth(clean_env, fake_ha, monkeypatch):
    monkeypatch.setenv('HA_URL', fake_ha.url)
    monkeypatch.setenv('HA_TOKEN', 'env-ha-placeholder')
    monkeypatch.setenv('HA_CONFIG_ACCESS', 'none')
    settings = Settings(token='api-placeholder', db_path=str(clean_env / 'db.sqlite'))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', follow_redirects=True) as client:
            await client.post('/login', data={'token': 'api-placeholder'})
            base = {'HA_URL': fake_ha.url, 'HA_CONFIG_ACCESS': 'none', 'TZ': 'UTC', 'DIAGNOSE_REASONING': 'medium'}
            cloud = dict(base, LLM_BASE_URL='https://api.cerebras.ai/v1', LLM_API_KEY='cloud-key-placeholder', DIAGNOSE_MODEL='qwen-3.8-27b',
                         DIAGNOSE_PRICE_IN='0.6', DIAGNOSE_PRICE_OUT='1.2', DIAGNOSE_DAILY_USD='1')
            local = dict(base, LLM_BASE_URL='http://192.168.1.50:1234/v1', LLM_API_KEY='lm-studio', DIAGNOSE_MODEL='local/qwen',
                         DIAGNOSE_PRICE_IN='0', DIAGNOSE_PRICE_OUT='0', DIAGNOSE_DAILY_USD='0')
            page = await client.post('/ui/settings/presets/save', data=dict(cloud, PRESET_NAME='Cerebras Qwen'))
            assert 'Saved the AI setup “Cerebras Qwen”' in page.text and settings.diagnose_model != 'qwen-3.8-27b'   # saving doesn't switch
            await client.post('/ui/settings/presets/save', data=dict(local, PRESET_NAME='  LM   Studio '))
            assert oct((clean_env / 'ai_setups.json').stat().st_mode & 0o777) == '0o600'
            assert (await client.post('/ui/settings/presets/save', data=dict(local, PRESET_NAME=' '))).status_code == 400

            page = await client.post('/ui/settings/presets/use', data={'PRESET': 'LM Studio'})
            assert 'Now using the AI setup “LM Studio”' in page.text and 'LM Studio · local/qwen on 192.168.1.50:1234 (in use)' in page.text
            assert settings.llm_base_url == 'http://192.168.1.50:1234/v1' and settings.openrouter_api_key == 'lm-studio' and settings.diagnose_daily_usd == 0
            page = await client.post('/ui/settings/presets/use', data={'PRESET': 'Cerebras Qwen'})
            assert settings.diagnose_model == 'qwen-3.8-27b' and settings.openrouter_api_key == 'cloud-key-placeholder'
            assert settings.diagnose_price_in == 0.6 and settings.diagnose_daily_usd == 1
            assert 'Cerebras Qwen · qwen-3.8-27b on api.cerebras.ai (in use)' in page.text and 'cloud-key-placeholder' not in page.text

            page = await client.post('/ui/settings/presets/delete', data={'PRESET': 'LM Studio'})
            assert 'Deleted the saved AI setup' in page.text and 'LM Studio ·' not in page.text and settings.diagnose_model == 'qwen-3.8-27b'
            assert (await client.post('/ui/settings/presets/use', data={'PRESET': 'LM Studio'})).status_code == 404
