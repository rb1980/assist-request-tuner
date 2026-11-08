"""UI regression coverage for pre-apply blockers and honest recovery states."""
import httpx
import pytest
from app import wizard, sentences
from app.config import Settings
from app.main import create_app


def test_restore_failure_is_never_presented_as_undone():
    change = dict(type='entity_alias', status='failed', error='Change tests failed; backup restore failed: retry rollback',
                  payload={'entity_id': 'light.lamp', 'add': ['glow']}, tests={})
    level, headline = wizard.outcome(change)
    assert level == 'bad' and 'could not be restored' in headline
    assert 'undone' not in headline and 'Nothing changed' not in headline
    assert 'uncertain' in wizard.describe(change, {'entities': {}})['status']
    change['error'] = 'Change tests failed'
    assert 'previous configuration was restored' in wizard.outcome(change)[1]


def test_failed_empty_precheck_blocks_review_and_custom_scope_is_bounded():
    change = dict(type='custom_sentence', status='draft', error=None,
                  payload={'intent': 'HassLightSet', 'add': ['[broken'], 'slots': {'domain': 'light'}}, tests={},
                  precheck={'applicable': True, 'passed': False, 'total': 0, 'error': 'Syntax error'})
    assert wizard.checks(change)[0][0] == 'bad'
    info = wizard.describe(change, {})
    assert 'any room or device' not in info['headline'] and 'Limited' in info['scope']


@pytest.mark.parametrize('mode', ['none', 'local'])
async def test_review_blocks_missing_access_and_external_drift(fake_ha, tmp_path, mode):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url,
                        db_path=str(tmp_path / 'ui.sqlite'), ha_config_access=mode, ha_config_dir=str(tmp_path))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        service = app.state.service
        await service.ha.refresh()
        draft = await service.changes.create(dict(type='custom_sentence', title='Test phrasing',
            payload={'intent': 'HassTurnOn', 'add': ['wake up [the] lights']},
            tests={'must_match': [{'sentence': 'wake up the lights', 'source': 'custom'}]}))
        if mode == 'local':
            await service.changes.sentences.adopt()
            path = tmp_path / 'custom_sentences' / 'en' / 'ar_tuner.yaml'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sentences.render([]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/login', data={'token': settings.token})
            review = await client.get(f"/fix/change/{draft['id']}")
            assert review.status_code == 200
            assert 'data-blocked="true" disabled' in review.text
            assert ('Config folder access is unavailable' if mode == 'none' else 'Managed file changed outside') in review.text
            if mode == 'local':
                assert path.read_text() == sentences.render([])  # A review must not overwrite an external edit.


async def test_result_retains_restore_failure_recovery_and_lost_match_evidence(fake_ha, tmp_path):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'ui.sqlite'))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        changes = app.state.service.changes
        change = await changes.create(dict(type='entity_alias', title='Glow', payload={'entity_id': 'light.test_lamp', 'add': ['glow']}))
        app.state.service.db.save('changes', dict(id=change['id'], status='failed',
            error='Change tests failed; backup restore failed: retry rollback',
            replay_result={'lost': [{'sentence': 'turn on the study lamp'}]}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/login', data={'token': settings.token})
            page = await client.get(f"/fix/change/{change['id']}/done")
            assert page.status_code == 200
            assert 'could not be restored' in page.text and 'Retry restoration' in page.text
            assert 'Historical matches affected' in page.text and 'study lamp' in page.text
            assert 'Nothing changed' not in page.text


async def test_rejected_settings_preserve_nonsecret_edits_and_conceal_secrets(fake_ha, tmp_path):
    settings = Settings(token='ui-placeholder', ha_token='ha-placeholder', ha_url=fake_ha.url, db_path=str(tmp_path / 'ui.sqlite'))
    app = create_app(settings, start_collector=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/login', data={'token': settings.token})
            page = await client.post('/settings', data={'HA_URL': fake_ha.url, 'TZ': 'Invalid/Timezone',
                'DIAGNOSE_MODEL': 'unsaved-model', 'LLM_API_KEY': 'must-not-echo-this', 'NIGHTLY_DIAGNOSE': '1'})
            assert page.status_code == 200 and 'Nothing was saved' in page.text
            assert 'value="Invalid/Timezone"' in page.text and 'value="unsaved-model"' in page.text
            assert 'must-not-echo-this' not in page.text
