from app.diagnose import SYSTEM_PROMPT


async def test_entity_status_live_negative_allows_builtin_but_rejects_own_trigger(stack, fake_ha):
    _, _, _, changes, _ = stack
    draft = dict(type='trigger_automation', title='Existing literal status', payload={'automation_id': 'ar_tuner_existing_status', 'config': {'triggers': [{'trigger': 'conversation', 'command': ['is [the] (sofa lamp|reading lamp) on']}], 'actions': [{'set_conversation_response': 'On'}]}}, tests={'must_match': [{'sentence': 'is the Sofa Lamp on'}], 'must_not_match': ['is the Kitchen Lamp on'], 'must_not_match_scope': 'change'})
    template = draft['payload']['config']['triggers'][0]['command'][0]
    fake_ha.matches['is the Sofa Lamp on'] = dict(match=True, source='trigger', sentence_template=template)
    fake_ha.matches['is the Kitchen Lamp on'] = dict(match=True, source='builtin', intent={'name': 'HassGetState'})
    created = await changes.create(draft)
    assert created['precheck']['passed']
    assert not created['precheck']['negative_results'][0]['match']
    assert (await changes.apply(created['id']))['status'] == 'applied'
    fake_ha.matches['is the Kitchen Lamp on'] = dict(match=True, source='trigger', sentence_template=template)
    assert not (await changes.test(changes.get(created['id'])))['passed']


def test_prompt_contains_no_playroom_scenario_hint():
    assert 'playroom' not in SYSTEM_PROMPT.lower()
    assert 'play room' not in SYSTEM_PROMPT.lower()
    assert 'inspect its tool arguments' in SYSTEM_PROMPT
