import copy
import re
import uuid
from . import names


def area_forms(areas, area_lights):
    forms = {}
    for area in areas:
        if area['area_id'] not in area_lights:
            continue
        for form in [area['name'], *area.get('aliases', []), area['name'].replace(' ', '')]:
            if isinstance(form, str) and form.strip():
                forms[form.lower().strip()] = area['area_id']
                # Speech-to-text never writes punctuation: "Study/Library" is spoken "study library".
                spoken = ' '.join(re.sub(r'[^\w\s]+', ' ', form.lower()).split())
                if spoken:
                    forms[spoken] = area['area_id']
    return forms


def spoken_key(value):
    return re.sub(r'[\W_]+', '', value.lower())


def area_snapshot(entities, areas):
    """Freeze exposed light targets from the resolved entity registry cache."""
    known = {a['area_id'] for a in areas}
    area_lights = {}
    for entity in entities:
        area = entity.get('area') or entity.get('area_id')
        if entity.get('exposed') is True and entity['entity_id'].startswith('light.') and area in known:
            area_lights.setdefault(area, []).append(entity['entity_id'])
    area_lights = {area: sorted(set(lights)) for area, lights in area_lights.items()}
    area_names = {}
    for area in areas:
        if area['area_id'] not in area_lights:
            continue
        for name in [area['name'], *area.get('aliases', [])]:
            if isinstance(name, str) and name.strip():
                area_names[spoken_key(name)] = area['area_id']
    return area_names, area_lights


def managed_description(description):
    pattern = r'Managed by Assist Request Tuner; rendered \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z, re-render after adding lights'
    return description if isinstance(description, str) and re.fullmatch(pattern, description) else names.MANAGED_BY


RECIPES = [
    dict(id='area_light_color', title='Area light color', version=4, type='custom_sentence',
         description="Extend HassLightSet with HA's live exposed entities, areas, aliases and colors.",
         intent='HassLightSet', sentences=['[<set>] [the] {area} lights [to] {color}', '(turn|make) [the] {area} lights {color}'],
         slots={'domain': 'light'}, changes_devices=False),
    dict(id='area_light_brightness', title='Area light brightness', version=5, type='custom_sentence',
         description="Extend HassLightSet brightness phrasing using HA's live lists.", intent='HassLightSet',
         sentences=['[<set>] [the] {area} lights [to] {brightness}[([ ]%)| percent]', '(dim|brighten) [the] {area} lights to {brightness}[([ ]%)| percent]'],
         slots={'domain': 'light'}, changes_devices=False),
    dict(id='thermostat_status', title='Thermostat status', version=2, type='blueprint_instance',
         description='Report mode, setpoint, current temperature and HVAC activity.',
         sentences=['what is the (thermostat|ac|air) set to', "what's the (ac|thermostat) (set to|doing)",
                    'is the (ac|air [conditioning]) (on|running)'],
         slots=[], changes_devices=False),
    dict(id='entity_reply', title='Entity reply', version=1, type='blueprint_instance',
         description='Answer literal questions about one exposed entity with a custom reply.',
         sentences=[], slots=[], changes_devices=False),
]


THERMOSTAT_REPLY = "{% set mode = states(entity) %}{% set target = state_attr(entity, 'temperature') %}{% set now = state_attr(entity, 'current_temperature') %}{% set action = state_attr(entity, 'hvac_action') %}{% if mode == 'off' %}The thermostat is off.{% else %}The thermostat is on {{ mode }}{% if target is not none %}, set to {{ target | round(0) | int }}{% endif %}{% if action %}, and it's {{ action }} right now{% endif %}.{% endif %}{% if now is not none %} It's {{ now | round(0) | int }} degrees inside.{% endif %}"


def list_templates():
    return copy.deepcopy(RECIPES)


def render_draft(ident, params, entities, areas):
    item = next((r for r in RECIPES if r['id'] == ident), None)
    if not item:
        raise KeyError('Template not found')
    if not isinstance(params, dict) or ('sentences' in params and (not isinstance(params['sentences'], list) or any(not isinstance(s, str) or not s.strip() for s in params['sentences']))):
        raise ValueError('Template params must contain a list of nonempty test sentences')
    if set(params) - ({'sentences'} if item['type'] == 'custom_sentence' else {'sentences', 'entity_id', 'commands', 'reply', 'unavailable_reply', 'alias'} if ident == 'entity_reply' else {'sentences', 'entity_id'}):
        raise ValueError('Unknown template parameters; custom sentences use live HA lists, not area/light parameters')
    if item['type'] == 'custom_sentence':
        area = next((a['name'] for a in areas), None)
        examples = [f"set the {area} lights to {'blue' if ident.endswith('color') else '30'}"] if area else []
        payload = dict(intent=item['intent'], add=item['sentences'], slots=item['slots'])
        tests = {'must_match': [{'sentence': s, 'source': 'custom', 'intent': item['intent']} for s in params.get('sentences', examples)]}
    else:
        entity = next((e for e in entities if e['entity_id'] == params.get('entity_id') and e.get('exposed') is True
                       and (ident == 'entity_reply' or e['domain'] == 'climate')), None)
        if not entity:
            raise ValueError('Select an exposed ' + ('climate entity' if ident == 'thermostat_status' else 'entity'))
        inputs = dict(entity=entity['entity_id'], sentences=item['sentences'] if ident == 'thermostat_status' else params.get('commands', []),
                      reply=THERMOSTAT_REPLY if ident == 'thermostat_status' else params.get('reply', ''))
        if 'unavailable_reply' in params:
            inputs['unavailable_reply'] = params['unavailable_reply']
        payload = dict(automation_id=names.AUTOMATION_PREFIX + ident + '_' + uuid.uuid4().hex[:12], alias=params.get('alias', item['title']),
                       blueprint='voice_reply', inputs=inputs)
        examples = ['what is the thermostat set to'] if ident == 'thermostat_status' else []
        tests = {'must_match': [{'sentence': s, 'source': 'trigger'} for s in params.get('sentences', examples)]}
    if not isinstance(params.get('sentences', []), list) or not tests['must_match']:
        raise ValueError('Provide at least one test sentence')
    return dict(type=item['type'], title=item['title'], payload=payload, tests=tests, allow_device_actions=False)
