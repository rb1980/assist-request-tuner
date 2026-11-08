"""Plain-language descriptions for the fix wizard: what a change does, how HA handles a sentence, and its live answer."""
from .changes import changes_devices, matched
from .ha import HAError
from .precheck import check_template

INTENT_WORDS = {
    'HassTurnOn': 'turn on', 'HassTurnOff': 'turn off', 'HassToggle': 'toggle', 'HassLightSet': 'set lights (color or brightness)',
    'HassGetState': 'answer a status question', 'HassClimateGetTemperature': 'read the temperature',
    'HassClimateSetTemperature': 'set the thermostat', 'HassFanSetSpeed': 'set fan speed', 'HassGetWeather': 'give the weather',
    'HassGetCurrentTime': 'tell the time', 'HassGetCurrentDate': 'tell the date', 'HassMediaPause': 'pause media',
    'HassMediaUnpause': 'resume media', 'HassMediaNext': 'skip to the next track', 'HassSetVolume': 'set the volume',
    'HassSetPosition': 'set a position (blinds, covers)', 'HassBroadcast': 'broadcast a message', 'HassNevermind': 'cancel',
}
# Intents that only answer; asking HA's own agent for their reply changes nothing.
READ_ONLY_INTENTS = {'HassGetState', 'HassClimateGetTemperature', 'HassGetWeather', 'HassGetCurrentTime', 'HassGetCurrentDate'}
STATUS_WORDS = {'draft': 'Not applied yet', 'applying': 'Applying…', 'applied': 'Live in Home Assistant',
                'failed': "Didn't pass the checks, undone", 'rolled_back': 'Undone'}


def intent_words(name):
    return INTENT_WORDS.get(name, name or 'an action')


def context(ha, automations=()):
    return dict(entities={e['entity_id']: e['name'] for e in ha.entities}, areas={a['area_id']: a['name'] for a in ha.areas},
                automations={a['id']: a.get('alias') or a['id'] for a in automations},
                templates={s: a.get('alias') or a['id'] for a in automations for s in a['sentences']})


def quoted(values):
    return ', '.join(f'“{v}”' for v in values)


def describe(change, ctx):
    p, kind = change['payload'], change['type']
    examples = [t['sentence'] for t in change['tests'].get('must_match', [])]
    count = len(p.get('add', []))
    if kind == 'entity_alias':
        headline = f"Gives {ctx['entities'].get(p['entity_id'], p['entity_id'])} another name: {quoted(p.get('add', []))}."
    elif kind == 'area_alias':
        headline = f"Gives the {ctx['areas'].get(p['area_id'], p['area_id'])} area another name: {quoted(p.get('add', []))}."
    elif kind == 'trigger_sentences':
        name = ctx['automations'].get(p['automation_id'], 'an existing automation')
        headline = f"Teaches your automation “{name}” {count} new way{'s' * (count != 1)} to ask."
    elif kind == 'custom_sentence' and p.get('remove') and not count:
        removed = len(p['remove'])
        headline = f"Stops Home Assistant recognising {removed} phrasing{'s' * (removed != 1)} to {intent_words(p.get('intent'))}: {quoted(p['remove'])}."
    elif kind == 'custom_sentence':
        headline = f"Teaches Home Assistant {count} new way{'s' * (count != 1)} to {intent_words(p.get('intent'))}."
    elif kind == 'blueprint_instance':
        inputs = p['inputs']
        headline = f"Adds a spoken answer about {ctx['entities'].get(inputs['entity'], inputs['entity'])}."
    elif kind == 'trigger_automation':
        headline = f"Creates a new automation “{p['config'].get('alias', p['automation_id'])}”."
    else:
        verb = 'Lets' if p.get('expose') else 'Stops'
        headline = f"{verb} voice control {ctx['entities'].get(p['entity_id'], p['entity_id'])}."
    if kind == 'custom_sentence':
        constraints = {k: p[k] for k in ('slots', 'requires_context', 'excludes_context') if p.get(k)}
        scope = 'Limited to the configured slot values and context; see Technical details for the exact constraints.' if constraints else 'Applies wherever these phrasings and their slots match.'
    elif kind == 'blueprint_instance':
        scope = 'Adds a voice-reply automation and installs or updates its shared blueprint.'
    elif kind in ('entity_alias', 'area_alias', 'expose_entity'):
        scope = 'Updates the selected device or area in Home Assistant.'
    else:
        scope = 'Updates only the automation described above.'
    return dict(headline=headline, scope=scope, examples=examples, status=('Restoration failed · configuration uncertain' if 'backup restore failed' in (change.get('error') or '') else STATUS_WORDS.get(change['status'], change['status'])))


def handling(result, ctx):
    """One line on how HA handles a sentence, from a dry-run matcher result."""
    if not matched(result):
        return "Not recognised, so it goes to the AI"
    if result.get('source') == 'trigger':
        name = ctx['templates'].get(result.get('sentence_template'))
        return f"Your automation “{name}”" if name else 'One of your automations'
    targets = [ctx['entities'].get(t, t) for t, v in (result.get('targets') or {}).items() if v.get('matched')]
    words = f"Home Assistant's built-in “{intent_words((result.get('intent') or {}).get('name'))}”"
    if targets:
        words += ' on ' + (', '.join(targets[:4]) + (f' and {len(targets) - 4} more' if len(targets) > 4 else ''))
    return words


def checks(change):
    """Pre-apply checks as (level, text) pairs: ok, warn or bad."""
    items = []
    precheck = change.get('precheck') or {}
    if precheck.get('applicable') and (precheck.get('total') or precheck.get('error') or precheck.get('passed') is False):
        failed = [r['sentence'] for r in precheck.get('results', []) if not r['match']]
        if precheck.get('error'):
            items.append(('bad', 'The phrasings have a syntax error: ' + precheck['error']))
        elif failed:
            items.append(('bad', "These examples don't match the new phrasings: " + quoted(failed)))
        elif precheck.get('passed') is False:
            items.append(('bad', 'The offline precheck did not pass. Review the draft before applying.'))
        else:
            items.append(('ok', f"All {precheck.get('total', 0)} examples match the new phrasings."))
    preview = change.get('capture_preview') or {}
    if preview:
        stolen = [r['transcript'] for r in preview.get('would_capture', []) if r.get('steals_local')]
        gained = [r['transcript'] for r in preview.get('would_capture', []) if r.get('takes_from_llm')]
        if stolen:
            items.append(('warn', f"It would take over {len(stolen)} past request{'s' * (len(stolen) != 1)} Home Assistant already handles: " + quoted(stolen[:5])))
        else:
            items.append(('ok', "It won't take over anything Home Assistant already handles."))
        if gained:
            items.append(('ok', f"It would also handle {len(gained)} past request{'s' * (len(gained) != 1)} that went to the AI: " + quoted(gained[:5])))
        if preview.get('warnings'):
            items.append(('warn', 'Some phrasings use wildcards, which can catch requests they were not meant for.'))
    if change.get('changes_devices'):
        items.append(('warn', 'This can change devices, so it needs your OK below.'))
    if change.get('reply_preview'):
        items.append(('ok', 'Home Assistant will answer: “' + change['reply_preview'] + '”'))
    return items


def outcome(change):
    """Headline and level for the summary screen."""
    status, error = change['status'], change.get('error') or ''
    if status == 'applied':
        return 'ok', 'Done. Home Assistant now handles this itself.'
    if status == 'rolled_back':
        return 'warn', 'Undone. Home Assistant is back to how it was before this change.'
    if status == 'applying':
        return 'warn', 'Applying and verifying the change. Wait for the final result.'
    if status == 'failed':
        if 'backup restore failed' in error:
            return 'bad', 'The fix failed and the previous configuration could not be restored.'
        if error.startswith('Change tests failed'):
            reason = "After the change, Home Assistant still didn't recognise your examples"
        elif error.startswith('Regressions found'):
            reason = 'It would have broken requests that already worked'
        else:
            reason = error or 'Something went wrong'
        return 'bad', reason + ', so it was undone automatically. The previous configuration was restored.'
    return 'warn', 'Not applied yet.'


async def answers(ha, sentences, readonly=()):
    """How HA handles each sentence now, plus HA's spoken answer when nothing that matches can change a device."""
    results = await ha.match(sentences)
    automations = await ha.automations()
    ctx = context(ha, automations)
    rows = []
    for sentence, result in zip(sentences, results):
        row = dict(sentence=sentence, local=matched(result), handled=handling(result, ctx), answer=None, note=None)
        rows.append(row)
        if not row['local']:
            continue
        safe = False
        if result.get('source') == 'trigger':
            hits = []
            for automation in automations:
                try:
                    if automation['enabled'] and check_template(automation['sentences'], [sentence])['matched']:
                        hits.append(automation)
                except ValueError:
                    continue
            safe = bool(hits)
            for automation in hits:
                if automation.get('blueprint'):
                    continue
                config = await ha.rest('GET', automation['id']) or {}
                if changes_devices(config.get('actions', config.get('action', [])), readonly):
                    safe = False
        else:
            safe = (result.get('intent') or {}).get('name') in READ_ONLY_INTENTS
        if safe:
            try:
                row['answer'] = await ha.ask(sentence)
            except HAError:
                row['note'] = "Couldn't get Home Assistant's answer."
        else:
            row['note'] = 'Not run here, because it would change a device. Try it by voice.'
    return rows


def ai_actions(tools):
    """What the voice AI did, e.g. 'set lights (color or brightness): Sofa Lamp, Play Room'."""
    lines = []
    for tool in tools or []:
        name, args = tool.get('name', '').split('__')[-1], tool.get('args') or {}
        target = ', '.join(str(args[k]) for k in ('name', 'area', 'floor') if args.get(k))
        if name.startswith('Hass'):
            lines.append(intent_words(name) + (': ' + target if target else ''))
        else:
            lines.append('looked up ' + name.replace('_', ' ') + (': ' + target if target else ''))
    return lines
