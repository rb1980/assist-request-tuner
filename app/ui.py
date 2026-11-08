import difflib
import hmac
import json
from pathlib import Path
from urllib.parse import quote, urlsplit, urlencode
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from . import names
from . import analysis
from .changes import ChangeDraft, matched
from .phrasing import list_templates, area_snapshot
from .ha import HAError
from .explain import near_miss_text, alias_suggestions
from . import ha_llm, wizard, sentences, settings_store


def ui_router(service, settings):
    r = APIRouter()
    templates = Jinja2Templates(directory=Path(__file__).parent / 'templates')
    templates.env.filters['pretty'] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)
    templates.env.filters['localtime'] = lambda v: datetime.fromisoformat(v).astimezone(ZoneInfo(settings.timezone)).strftime('%b %d %H:%M:%S') if v else '—'
    db, ha, changes = service.db, service.ha, service.changes

    def page(request, name, **context):
        return templates.TemplateResponse(request=request, name=name, context=dict(context, status=service.status(), diagnose_config=service.diagnose.configuration(),
                                                                               root=getattr(request.state, 'root', ''), api_token=settings.token if settings.supervised else None))

    async def form(request):
        # SameSite cookies plus an origin check prevent cross-site configuration writes.
        # Compare hosts only: behind a TLS-terminating proxy (OrbStack, Caddy) the browser says https and we see http.
        # Browsers also say where a request comes from (Sec-Fetch-Site), but only to https and localhost addresses.
        # Through Home Assistant ingress the Origin is "null": HA serves App pages with Referrer-Policy: no-referrer,
        # and browsers then hide the origin of a form post. On plain http (http://homeassistant.local:8123, the usual
        # setup) there is no Sec-Fetch-Site either. Such a post is still safe to accept through ingress: it only
        # reaches us with HA's ingress_session cookie, which HA sets SameSite=Strict, so other sites can't send one.
        # On the direct port a null Origin needs Sec-Fetch-Site to vouch for it.
        origin = request.headers.get('origin')
        site = request.headers.get('sec-fetch-site')
        host = request.headers.get('host', request.url.netloc)
        ingress = bool(getattr(request.state, 'root', ''))
        cross = (site is not None and site not in ('same-origin', 'none')) \
            or (origin == 'null' and site != 'same-origin' and not (ingress and site is None)) \
            or (origin and origin != 'null' and urlsplit(origin).netloc != host)
        if cross:
            raise ValueError('Cross-origin form rejected')
        return await request.form()

    @r.get('/login')
    async def login(request: Request):
        return page(request, 'login.html')

    @r.post('/login')
    async def authenticate(request: Request):
        data = await form(request)
        if not hmac.compare_digest(str(data.get('token', '')), settings.token):
            return HTMLResponse('Invalid token', status_code=401)
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(names.COOKIE, settings.token, httponly=True, samesite='strict', secure=request.url.scheme == 'https')
        return response

    @r.post('/logout')
    async def logout(request: Request):
        await form(request)
        response = RedirectResponse('/login', status_code=303)
        response.delete_cookie(names.COOKIE)
        return response

    @r.get('/stats')
    async def overview(request: Request):
        return page(request, 'overview.html', stats=analysis.stats(db), windows=service.ignore())

    @r.post('/ui/ignore')
    async def ignore(request: Request):
        data = await form(request)
        service.ignore(stop=True) if data.get('stop') else service.ignore(float(data.get('minutes', 30)), str(data.get('reason', '')))
        return RedirectResponse('/', status_code=303)

    @r.get('/requests')
    async def requests(request: Request):
        filters = {k: v for k, v in request.query_params.items() if k in {'handled_by', 'topic', 'source', 'device', 'q', 'pattern', 'since', 'until'}}
        offset = max(0, int(request.query_params.get('offset', 0)))
        return page(request, 'requests.html', rows=analysis.requests(db, **filters, offset=offset, include_excluded=request.query_params.get('include_excluded') == 'true'), offset=offset)

    @r.get('/requests/{ident}')
    async def request_detail(request: Request, ident: int):
        return page(request, 'request.html', row=service.get_request(ident), diagnosis=service.diagnose.latest('request', ident))

    @r.post('/ui/requests/{ident}/topic')
    async def topic(request: Request, ident: int):
        data = await form(request)
        service.set_topic(ident, data.get('topic_override') or None)
        return RedirectResponse(f'/requests/{ident}', status_code=303)

    @r.get('/candidates')
    async def candidates(request: Request):
        rows = analysis.candidates(db, ha)
        for row in rows:
            row['diagnosis'] = service.diagnose.latest('pattern', row['pattern'])
        return page(request, 'candidates.html', candidates=rows)

    @r.post('/ui/pattern')
    async def pattern(request: Request):
        data = await form(request)
        patch = {'ignored': data.get('ignored') == 'true'} if 'ignored' in data else {'topic_override': data.get('topic_override') or None, 'note': data.get('note', '')}
        service.set_pattern(data['pattern'], patch)
        return RedirectResponse('/candidates', status_code=303)

    @r.get('/intents')
    async def intents(request: Request):
        try:
            value = await service.intents()
        except Exception:
            value = {'error': 'Home Assistant unavailable'}
        return page(request, 'intents.html', intents=value)

    @r.post('/ui/match')
    async def match(request: Request):
        data = await form(request)
        return await service.match([s for s in str(data.get('sentences', '')).splitlines() if s.strip()])

    @r.get('/changes')
    async def list_changes(request: Request):
        return page(request, 'changes.html', changes=[changes.get(c['id']) for c in db.rows('SELECT id FROM changes ORDER BY id DESC')])

    @r.get('/changes/new')
    async def new_change(request: Request):
        sentence = request.query_params.get('sentence', '')
        kind = request.query_params.get('type', 'entity_alias')
        payload = {'entity_id': 'light.replace_me', 'add': [sentence], 'remove': []}
        if kind == 'trigger_automation':
            payload = {'automation_id': names.AUTOMATION_PREFIX + 'replace_me', 'config': {'alias': 'Voice: new status question', 'description': names.MANAGED_BY, 'triggers': [{'trigger': 'conversation', 'command': [sentence]}], 'actions': [{'set_conversation_response': 'TODO: sensor template'}]}}
        draft = dict(type=kind, title='New change', payload=payload, tests={'must_match': [{'sentence': sentence}], 'must_not_match': []}, allow_device_actions=False)
        return page(request, 'draft.html', draft=draft, ident=None)

    @r.post('/ui/changes')
    async def create_change(request: Request):
        data = await form(request)
        draft = json.loads(data['draft'])
        change = (await changes.create(draft, 'ui'))
        return RedirectResponse('/changes/' + str(change['id']), status_code=303)

    @r.get('/changes/{ident}')
    async def detail(request: Request, ident: int):
        change = changes.get(ident)
        if change['type'] == 'custom_sentence':
            before = change.get('rendered_before', change['before']) or ''
            after = change.get('rendered_after', change['after']) or ''
        else:
            before, after = json.dumps(change['before'], indent=2), json.dumps(change['after'], indent=2)
        diff = '\n'.join(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile='before', tofile='after', lineterm=''))
        draft = {k: change[k] for k in ChangeDraft.model_fields}
        return page(request, 'change.html', change=change, diff=diff, draft=draft)

    @r.post('/ui/changes/{ident}/{action}')
    async def change_action(request: Request, ident: int, action: str):
        data = await form(request)
        if action == 'apply':
            async with changes.lock:
                current = changes.get(ident)
                if current['status'] == 'draft' and current['changes_devices']:
                    await changes.update(ident, {'allow_device_actions': data.get('allow_device_actions') == 'true'}, 'ui')
            await changes.apply(ident, data.get('confirm_regressions') == 'true', 'ui')
        elif action == 'rollback':
            await changes.rollback(ident, data.get('force') == 'true', 'ui')
        elif action == 'replay':
            async with changes.lock:
                result = await changes.replay(ident, int(data.get('days', 30)))
                db.log(ident, 'replay', 'ui', result)
        elif action == 'edit':
            async with changes.lock:
                (await changes.update(ident, json.loads(data['draft']), 'ui'))
        elif action == 'clone':
            draft = {k: changes.get(ident)[k] for k in ChangeDraft.model_fields}
            created = (await changes.create(draft, 'ui'))
            return RedirectResponse('/changes/' + str(created['id']), status_code=303)
        else:
            raise ValueError('Unknown action')
        return RedirectResponse(f'/changes/{ident}', status_code=303)

    @r.post('/ui/diagnose')
    async def diagnose(request: Request):
        data = await form(request)
        job = service.diagnose.enqueue(request_id=int(data['request_id']) if data.get('request_id') else None,
                                       pattern=data.get('pattern') or None)
        return RedirectResponse('/diagnoses/' + str(job['id']), status_code=303)

    @r.get('/diagnoses/{ident}')
    async def diagnosis_page(request: Request, ident: int):
        return page(request, 'diagnosis.html', diagnosis=service.diagnose.get(ident))

    @r.get('/ui/diagnoses/{ident}')
    async def diagnosis_status(ident: int):
        job = service.diagnose.get(ident)
        return {'id': job['id'], 'status': job['status']}

    async def ha_servers():
        # The Local OpenAI LLM servers set up in HA, for the AI server choice (without their keys).
        found, problem = await ha_llm.servers(changes.sentences.writer, force=True)   # fresh: the page is where changes are checked
        return dict(choice_labels=settings_store.CHOICE_LABELS, ha_problem=problem, from_ha=settings_store.FROM_HA,
                    ha_shows=settings_store.HA_SHOWS,
                    ha_servers=[dict(id=x['id'], title=x['title'], url=x['base_url'], agents=x['agents']) for x in found],
                    ha_chosen='/'.join(filter(None, [(c := ha_llm.choose(found, settings.ai_ha_server))[0] and c[0]['id'],
                                                     c[1] and c[1]['id']])))

    @r.get('/settings')
    async def settings_page(request: Request):
        blueprint, error = None, None
        try:
            blueprint = await changes.blueprints.listing()
        except (ValueError, HAError) as exc:
            error = str(exc)
        return page(request, 'settings.html', blueprint=blueprint, blueprint_error=error, sections=settings_store.SECTIONS,
                    fields=settings_store.current(settings), saved=request.query_params.get('saved'),
                    check=request.query_params.get('check'), check_ok=request.query_params.get('ok') == '1', errors=[],
                    note=request.query_params.get('note'), presets=settings_store.presets_view(settings), **await ha_servers())

    @r.post('/settings')
    async def save_settings(request: Request):
        data = await form(request)
        updates, errors = settings_store.parse(data, settings.supervised)
        if errors:
            fields = settings_store.current(settings)
            for name, field in fields.items():
                if field['kind'] == 'bool':
                    field['value'] = bool(data.get(name))
                elif field['kind'] != 'secret' and name in data:
                    field['value'] = str(data[name])
            return page(request, 'settings.html', blueprint=None, blueprint_error=None, sections=settings_store.SECTIONS,
                        fields=fields, saved=None, check=None, check_ok=False, errors=errors,
                        note=None, presets=settings_store.presets_view(settings), **await ha_servers())
        settings_store.save(settings_store.changed_only(updates, settings))
        async with changes.lock:
            await settings_store.apply(settings, service)
        ok, message = (True, '') if settings.supervised else await settings_store.check_ha(settings.ha_url, settings.ha_token)
        return RedirectResponse('/settings?' + urlencode({'saved': '1', 'ok': int(ok), 'check': message}), status_code=303)

    @r.post('/ui/blueprints/sync')
    async def sync_blueprint(request: Request):
        await form(request)
        async with changes.lock:
            await changes.blueprints.ensure_blueprint()
        return RedirectResponse('/settings', status_code=303)

    @r.post('/ui/settings/presets/save')
    async def save_preset(request: Request):
        data = await form(request)
        trial, errors = settings_store.candidate(data, settings)
        if errors:
            raise ValueError('Fix these first: ' + '; '.join(errors))
        name = settings_store.preset_name(data.get('PRESET_NAME'))
        settings_store.save_preset(name, trial)
        return RedirectResponse('/settings?' + urlencode({'note': f'Saved the AI setup “{name}”. It is not in use until you choose Use.'}), status_code=303)

    @r.post('/ui/settings/presets/use')
    async def use_preset(request: Request):
        name = (await form(request)).get('PRESET', '')
        settings_store.use_preset(name, settings)
        async with changes.lock:
            await settings_store.apply(settings, service)
        return RedirectResponse('/settings?' + urlencode({'saved': '1', 'ok': 1, 'check': f'Now using the AI setup “{name}”: {settings.diagnose_model}.'}), status_code=303)

    @r.post('/ui/settings/presets/delete')
    async def delete_preset(request: Request):
        name = (await form(request)).get('PRESET', '')
        settings_store.delete_preset(name)
        return RedirectResponse('/settings?' + urlencode({'note': f'Deleted the saved AI setup “{name}”. The current settings are unchanged.'}), status_code=303)

    @r.post('/ui/settings/test/{section}')
    async def test_settings(section: str, request: Request):
        data = await form(request)
        return await settings_store.test(section, data, settings, service)

    @r.post('/ui/writer/forget_host_key')
    async def forget_host_key(request: Request):
        await form(request)
        if hasattr(changes.sentences.writer, 'forget_host_key'):
            changes.sentences.writer.forget_host_key()
        return RedirectResponse('/settings', status_code=303)

    @r.post('/ui/blueprints/preview_reply')
    async def preview_reply(request: Request):
        data = await form(request)
        return {'reply_preview': await changes.blueprints.preview_reply(data.get('entity_id', ''), data.get('reply', ''))}

    def sentence_choices():
        known = sorted(sentences.english()['intents'])
        common = [n for n in wizard.INTENT_WORDS if n in known]
        return dict(intents=common + [n for n in known if n not in common], words=wizard.intent_words)

    @r.post('/fix/sentences/add')
    async def sentences_add(request: Request):
        data = await form(request)
        lines = lambda key: [v.strip() for v in str(data.get(key, '')).splitlines() if v.strip()]
        intent = str(data.get('intent', ''))
        payload = {'intent': intent, 'add': lines('add')}
        if data.get('block_id'):
            payload['block_id'] = int(data['block_id'])
        elif data.get('domain'):
            payload['slots'] = {'domain': str(data['domain'])}
        if not lines('examples'):
            raise ValueError('Give at least one example of something you would say')
        change = await changes.create(dict(type='custom_sentence', title='Add phrasings: ' + wizard.intent_words(intent), payload=payload,
            tests={'must_match': [{'sentence': e, 'source': 'custom', 'intent': intent} for e in lines('examples')]}), 'ui')
        return RedirectResponse(f"/fix/change/{change['id']}", status_code=303)

    @r.post('/fix/sentences/remove')
    async def sentences_remove(request: Request):
        data = await form(request)
        intent = str(data.get('intent', ''))
        change = await changes.create(dict(type='custom_sentence', title='Remove phrasing: ' + wizard.intent_words(intent),
            payload={'intent': intent, 'remove': [str(data.get('sentence', ''))], 'block_id': int(data['block_id'])}), 'ui')
        return RedirectResponse(f"/fix/change/{change['id']}", status_code=303)

    @r.get('/sentences')
    async def current_sentences(request: Request):
        current, error = None, None
        try:
            current = await changes.sentences.writer.read()
        except (ValueError, HAError) as exc:
            error = str(exc)
        return page(request, 'sentences.html', listing=changes.sentences.listing(), current=current, error=error, preview=None, **sentence_choices())

    @r.post('/ui/sentences/import')
    async def import_sentences(request: Request):
        data = await form(request)
        async with changes.lock:
            preview = await changes.sentences.import_file(data.get('confirm') == 'true', data.get('fingerprint'))
        if preview['confirmed']:
            return RedirectResponse('/sentences', status_code=303)
        return page(request, 'sentences.html', listing=changes.sentences.listing(), current=preview['current_file'], preview=preview, error=None, **sentence_choices())

    @r.get('/templates')
    async def template_page(request: Request):
        error = None
        try:
            await ha.refresh()
        except HAError:
            error = 'Home Assistant unavailable; targets cannot be refreshed.'
        _, light_areas = area_snapshot(ha.entities, ha.areas)
        return page(request, 'templates.html', recipes=list_templates(),
                    areas=[a for a in ha.areas if a['area_id'] in light_areas],
                    entities=[e for e in ha.entities if e.get('exposed') is True], error=error)

    @r.post('/ui/templates/{ident}/draft')
    async def template_draft(request: Request, ident: str):
        data = await form(request)
        params = {key: str(data[key]) for key in ['area_id', 'entity_id', 'color', 'brightness', 'reply', 'alias', 'unavailable_reply'] if data.get(key)}
        if data.get('sentences', '').strip():
            params['sentences'] = [s.strip() for s in data['sentences'].splitlines() if s.strip()]
        if ident == 'entity_reply':
            params['commands'] = [s.strip() for s in data.get('commands', '').splitlines() if s.strip()]
        change = await service.diagnose.create_from_template(ident, params, 'ui')
        return RedirectResponse('/changes/' + str(change['id']), status_code=303)

    @r.get('/make-local')
    async def make_local(request: Request):
        error, automations = None, []
        try:
            await ha.refresh()
            automations = await ha.automations()
        except HAError:
            error = 'Home Assistant unavailable; targets cannot be refreshed.'
        return page(request, 'make_local.html', areas=ha.areas,
                    entities=[e for e in ha.entities if e.get('exposed') is True], automations=automations,
                    sentence=request.query_params.get('sentence', ''), error=error)

    @r.post('/ui/make-local/alias')
    async def alias_form(request: Request):
        data = await form(request)
        await ha.refresh()
        kind, target = data.get('target', ':').split(':', 1)
        if kind == 'entity_alias':
            if not any(e['entity_id'] == target and e.get('exposed') is True for e in ha.entities):
                raise ValueError('Select an exposed entity')
            payload = {'entity_id': target, 'add': [data.get('alias', '').strip()]}
        elif kind == 'area_alias':
            if not any(a['area_id'] == target for a in ha.areas):
                raise ValueError('Select a known area')
            payload = {'area_id': target, 'add': [data.get('alias', '').strip()]}
        else:
            raise ValueError('Select an entity or area')
        change = await changes.create(dict(type=kind, title='Alias: ' + data.get('alias', ''), payload=payload,
            tests={'must_match': [{'sentence': data.get('sentence', '')}]}), 'ui')
        return RedirectResponse('/changes/' + str(change['id']), status_code=303)

    @r.post('/ui/make-local/sentences')
    async def sentences_form(request: Request):
        data = await form(request)
        commands = [s.strip() for s in data.get('commands', '').splitlines() if s.strip()]
        sentences = [s.strip() for s in data.get('sentences', '').splitlines() if s.strip()]
        if not sentences:
            raise ValueError('Provide a test sentence')
        change = await changes.create(dict(type='trigger_sentences', title='Add phrasings',
            payload={'automation_id': data.get('automation_id'), 'add': commands},
            tests={'must_match': [{'sentence': s, 'source': 'trigger'} for s in sentences]}), 'ui')
        return RedirectResponse('/changes/' + str(change['id']), status_code=303)

    # Fix wizard: choose a request -> why and how to fix -> review -> done.
    async def refresh():
        try:
            await ha.refresh()
        except HAError:
            pass

    async def ctx():
        try:
            automations = await ha.automations()
        except HAError:
            automations = []
        return wizard.context(ha, automations)

    def back_link(change):
        if change.get('diagnosis_id'):
            d = service.diagnose.get(change['diagnosis_id'])
            return f"/fix/request/{d['target']}" if d['target_type'] == 'request' else '/fix/pattern?p=' + quote(d['target'])
        tests = change['tests'].get('must_match', [])
        if tests:
            rows = db.rows('SELECT id FROM requests WHERE transcript=? ORDER BY id DESC LIMIT 1', (tests[0]['sentence'],))
            if rows:
                return f"/fix/request/{rows[0]['id']}"
        return None

    def fixes(diagnosis):
        found = []
        for fix in ((diagnosis or {}).get('result') or {}).get('fixes', []):
            if fix.get('change_id'):
                change = changes.get(fix['change_id'])
                if change['type'] == 'trigger_automation' and change['status'] == 'draft':
                    continue  # pre-v4 free-form automation drafts; the wizard offers aliases, custom sentences and blueprint replies
                info = wizard.describe(change, wizard.context(ha))
                found.append(dict(change_id=change['id'], title=fix.get('title'), explanation=fix.get('explanation'), draft=change['status'] == 'draft', state=change['status'],
                                  headline=info['headline'], status=info['status'], changes_devices=change['changes_devices']))
        return found

    def applied_fixes():
        """Patterns that an applied fix was made for, with what that fix lets you say."""
        rows = db.rows("SELECT c.id, c.title, c.tests, d.target_type, d.target FROM changes c "
                       "LEFT JOIN diagnoses d ON d.id = c.diagnosis_id WHERE c.status = 'applied' ORDER BY c.id DESC")
        found = {}
        for row in rows:
            tests = row['tests'] if isinstance(row['tests'], dict) else json.loads(row['tests'] or '{}')
            tests = tests.get('must_match', [])
            sentences = [t['sentence'] for t in tests if t.get('sentence')]
            if row['target_type'] == 'pattern':
                patterns = [row['target']]
            elif row['target_type'] == 'request':
                patterns = [r['pattern'] for r in db.rows('SELECT pattern FROM requests WHERE id=?', (row['target'],))]
            else:  # made in the wizard without AI: it was started from a request whose transcript is the first test
                patterns = [r['pattern'] for r in db.rows('SELECT DISTINCT pattern FROM requests WHERE transcript=?', (sentences[0] if sentences else '',))]
            for pattern in patterns:
                found.setdefault(pattern, dict(pattern=pattern, change_id=row['id'], title=row['title'], say=sentences[:2]))
        return found

    def fix_badge(pattern, diagnosis):
        """(badge, button) for a request on the list, from the state of the fixes found for it."""
        if diagnosis and diagnosis['status'] in ('queued', 'running'):
            return 'Looking for fixes…', 'Fix this →'
        statuses = [r['status'] for r in db.rows("SELECT c.status FROM changes c JOIN diagnoses d ON d.id = c.diagnosis_id "
                                                 "WHERE d.target_type = 'pattern' AND d.target = ? ORDER BY c.id DESC", (pattern,))]
        if diagnosis and any(f['draft'] for f in fixes(diagnosis)):
            return 'Fixes ready', 'Fix this →'
        if 'rolled_back' in statuses:
            return 'Fix undone', 'Try a different fix →'
        if 'failed' in statuses:
            return "Fix didn't pass the checks", 'Try a different fix →'
        return None, 'Fix this →'

    @r.get('/')
    async def fix_start(request: Request):
        await refresh()
        done = applied_fixes()
        found = [c for c in analysis.candidates(db, ha) if c['pattern'] not in done]
        for c in found:
            c['actions'] = wizard.ai_actions(c['tools'][:1])
            latest = db.rows('SELECT started_at FROM requests WHERE pattern=? ORDER BY started_at DESC LIMIT 1', (c['pattern'],))
            c['last_at'] = latest[0]['started_at'] if latest else None
            d = service.diagnose.latest('pattern', c['pattern'])
            c['badge'], c['go'] = fix_badge(c['pattern'], d)
        patterns = {c['pattern'] for c in found}
        hidden = db.rows('SELECT pattern FROM patterns WHERE ignored=1 ORDER BY pattern')
        for h in hidden:
            example = db.rows('SELECT transcript FROM requests WHERE pattern=? ORDER BY id DESC LIMIT 1', (h['pattern'],))
            h['example'] = example[0]['transcript'] if example else None
        skip = patterns | {h['pattern'] for h in hidden} | set(done)
        recent = [r for r in analysis.requests(db, handled_by='llm', limit=40) if r['pattern'] not in skip][:10]
        # Hide what an earlier fix already made local: candidates come from how requests were handled when captured.
        fixed = 0
        try:
            current = await ha.match([c['examples'][0] for c in found] + [r['transcript'] for r in recent]) if found or recent else []
            top, rest = current[:len(found)], current[len(found):]
            fixed = sum(bool(matched(m)) for m in top)
            found = [c for c, m in zip(found, top) if not matched(m)]
            recent = [r for r, m in zip(recent, rest) if not matched(m)]
        except HAError:
            pass
        for fix in done.values():
            example = db.rows('SELECT transcript FROM requests WHERE pattern=? ORDER BY id DESC LIMIT 1', (fix['pattern'],))
            fix['example'] = example[0]['transcript'] if example else fix['pattern']
        return page(request, 'fix_start.html', candidates=found, recent=recent, stats=analysis.stats(db), fixed=fixed, hidden=hidden,
                    done=list(done.values()))

    async def understand(request, rows, pattern=None, ident=None):
        if not rows:
            raise KeyError('No requests found for this; it may have been handled locally since')
        await refresh()
        row = rows[0]
        diagnosis = service.diagnose.latest('pattern', pattern) if pattern else service.diagnose.latest('request', ident)
        result = (diagnosis or {}).get('result') or {}
        when = wizard_time(row['started_at'])
        asked = f"Asked {len(rows)} times, last on {when}" if len(rows) > 1 else f"Asked on {when}"
        asked += ' from ' + (row['device_name'] or 'a typed request')
        try:
            now_handled = (await ha.match([row['transcript']]))[0]
        except HAError:
            now_handled = None
        aliases = []
        area_names = {str(n).lower().replace(' ', '') for a in ha.areas for n in [a['name'], *a.get('aliases', [])] if n}
        for hint in [] if matched(now_handled) else alias_suggestions(row['matcher'], ha.entities, ha.areas):
            if hint['text'].lower().replace(' ', '') in area_names:
                continue
            # Offer only the closest target, and only if it doesn't already have that name.
            target = next((t for t in hint['targets'] if hint['text'].lower() not in [str(n).lower() for n in [t.get('name'), *t.get('aliases', [])] if n]), None)
            if target and target is hint['targets'][0]:
                aliases.append(dict(hint, targets=[target]))
        return page(request, 'fix_understand.html', said=row['transcript'],
                    now_local=wizard.handling(now_handled, await ctx()) if matched(now_handled) else None, asked=asked, actions=wizard.ai_actions(row['tools']),
                    reply=row['reply'], others=list(dict.fromkeys(r['transcript'] for r in rows[1:] if r['transcript'] != row['transcript']))[:4],
                    near_miss=near_miss_text(row['matcher']), working=result.get('working_phrasings') or row.get('working_phrasings') or [],
                    diagnosis=diagnosis, fixes=fixes(diagnosis), aliases=aliases,
                    pattern=pattern, hide_pattern=pattern or row['pattern'], target_field='pattern' if pattern else 'request_id', target_value=pattern or ident)

    def wizard_time(value):
        return datetime.fromisoformat(value).astimezone(ZoneInfo(settings.timezone)).strftime('%b %-d at %-I:%M %p')

    @r.get('/fix/pattern')
    async def fix_pattern(request: Request, p: str):
        return await understand(request, analysis.requests(db, handled_by='llm', pattern=p, limit=20), pattern=p)

    @r.get('/fix/request/{ident}')
    async def fix_request(request: Request, ident: int):
        return await understand(request, [service.get_request(ident)], ident=ident)

    @r.post('/fix/analyze')
    async def fix_analyze(request: Request):
        data = await form(request)
        pattern = data.get('pattern') or None
        ident = int(data['request_id']) if data.get('request_id') else None
        service.diagnose.enqueue(request_id=ident, pattern=pattern)
        return RedirectResponse('/fix/pattern?p=' + quote(pattern) if pattern else f'/fix/request/{ident}', status_code=303)

    @r.post('/fix/hide')
    async def fix_hide(request: Request):
        # Only the app's own fix list: sets patterns.ignored; Home Assistant and the stored requests are untouched.
        data = await form(request)
        service.set_pattern(str(data['pattern']), {'ignored': True})
        if 'application/json' in request.headers.get('accept', ''):   # ignored in place from the fix list
            return JSONResponse({'ok': True, 'ignored': True})
        return RedirectResponse('/', status_code=303)

    @r.post('/fix/unhide')
    async def fix_unhide(request: Request):
        data = await form(request)
        service.set_pattern(str(data['pattern']), {'ignored': False})
        if 'application/json' in request.headers.get('accept', ''):
            return JSONResponse({'ok': True, 'ignored': False})
        return RedirectResponse('/', status_code=303)

    @r.post('/fix/alias')
    async def fix_alias(request: Request):
        data = await form(request)
        await ha.refresh()
        kind, target, alias = data.get('kind'), str(data.get('target', '')), str(data.get('alias', '')).strip()
        if kind == 'entity_alias' and any(e['entity_id'] == target and e.get('exposed') is True for e in ha.entities):
            payload = {'entity_id': target, 'add': [alias]}
        elif kind == 'area_alias' and any(a['area_id'] == target for a in ha.areas):
            payload = {'area_id': target, 'add': [alias]}
        else:
            raise ValueError('Choose an exposed device or a known area')
        change = await changes.create(dict(type=kind, title='Name: ' + alias, payload=payload,
                                           tests={'must_match': [{'sentence': str(data.get('sentence', ''))}]}), 'ui')
        return RedirectResponse(f"/fix/change/{change['id']}", status_code=303)

    @r.get('/fix/change/{ident}')
    async def fix_review(request: Request, ident: int):
        change = changes.get(ident)
        if change['status'] != 'draft':
            return RedirectResponse(f'/fix/change/{ident}/done', status_code=303)
        context = await ctx()
        checks = wizard.checks(change)
        state = service.status()
        if not state['ha_connected']:
            checks.append(('bad', 'Home Assistant is unavailable. Reconnect before applying this fix.'))
        if change['type'] in ('custom_sentence', 'blueprint_instance') and not state['writer']['configured']:
            checks.append(('bad', 'Config folder access is unavailable: ' + state['writer']['state']))
        if change['type'] == 'custom_sentence' and state['writer']['configured']:
            try:
                remote = await changes.sentences.writer.read()
                expected = db.meta('sentence_remote_hash')
                if expected and expected != sentences.fingerprint(remote):
                    checks.append(('bad', 'Managed file changed outside Request Tuner; import from HA before applying.'))
            except (ValueError, HAError, OSError) as exc:
                checks.append(('bad', 'The managed sentence file could not be checked: ' + str(exc)))
        return page(request, 'fix_review.html', change=change, info=wizard.describe(change, context), checks=checks,
                    blocked=any(level == 'bad' for level, _ in checks), back=back_link(change),
                    editable=change['type'] in ('trigger_sentences', 'custom_sentence') and not change['payload'].get('remove'))

    @r.post('/fix/change/{ident}/edit')
    async def fix_edit(request: Request, ident: int):
        data = await form(request)
        change = changes.get(ident)
        lines = lambda key: [s.strip() for s in str(data.get(key, '')).splitlines() if s.strip()]
        source = {'custom_sentence': 'custom', 'trigger_sentences': 'trigger'}.get(change['type'])
        intent = change['payload'].get('intent') if change['type'] == 'custom_sentence' else None
        async with changes.lock:
            await changes.update(ident, {'payload': dict(change['payload'], add=lines('add')),
                                         'tests': dict(change['tests'], must_match=[{'sentence': s, 'source': source, 'intent': intent} for s in lines('examples')])}, 'ui')
        return RedirectResponse(f'/fix/change/{ident}', status_code=303)

    @r.post('/fix/change/{ident}/apply')
    async def fix_apply(request: Request, ident: int):
        data = await form(request)
        async with changes.lock:
            current = changes.get(ident)
            if current['status'] == 'draft' and current['changes_devices']:
                await changes.update(ident, {'allow_device_actions': data.get('allow_device_actions') == 'true'}, 'ui')
        await changes.apply(ident, False, 'ui')
        return RedirectResponse(f'/fix/change/{ident}/done', status_code=303)

    @r.get('/fix/change/{ident}/done')
    async def fix_done(request: Request, ident: int):
        change = changes.get(ident)
        if change['status'] == 'draft':
            return RedirectResponse(f'/fix/change/{ident}', status_code=303)
        info = wizard.describe(change, await ctx())
        rows = None
        if info['examples']:
            try:
                rows = await wizard.answers(ha, info['examples'], settings.readonly_services)
            except HAError:
                rows = None
        replay = change.get('replay_result') or {}
        also = [r['sentence'] for r in replay.get('newly_local', []) if r['sentence'] not in info['examples']] if change['status'] == 'applied' else []
        level, headline = wizard.outcome(change)
        return page(request, 'fix_done.html', change=change, info=info, rows=rows, also=also, level=level, headline=headline,
                    lost=[r['sentence'] for r in replay.get('lost', [])], restore_failed='backup restore failed' in (change.get('error') or ''), back=back_link(change))

    @r.post('/fix/change/{ident}/undo')
    async def fix_undo(request: Request, ident: int):
        await form(request)
        await changes.rollback(ident, False, 'ui')
        return RedirectResponse(f'/fix/change/{ident}/done', status_code=303)

    @r.get('/history')
    async def history(request: Request):
        context = await ctx()
        items = []
        for row in db.rows('SELECT id FROM changes ORDER BY id DESC LIMIT 200'):
            change = changes.get(row['id'])
            info = wizard.describe(change, context)
            items.append(dict(id=change['id'], status=change['status'], headline=info['headline'], status_words=info['status'],
                              updated_at=change.get('updated_at'), level=wizard.outcome(change)[0]))
        return page(request, 'history.html', items=items)

    return r
