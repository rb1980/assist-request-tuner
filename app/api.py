import json
from typing import Literal
from fastapi import APIRouter, Body, Query, Response
from pydantic import BaseModel, Field
from . import analysis
from .changes import ChangeDraft
from .blueprint import ReplyInput
from .diagnose import DiagnoseInput, TemplateInput
from .precheck import check_template
from .phrasing import list_templates


class TopicPatch(BaseModel):
    topic_override: Literal['home', 'general', 'unknown'] | None = None


class MatchInput(BaseModel):
    sentences: list[str] = Field(min_length=1, max_length=2000)


class ApplyInput(BaseModel):
    confirm_regressions: bool = False


class RollbackInput(BaseModel):
    force: bool = False


class ReplayInput(BaseModel):
    days: int = Field(default=30, ge=1, le=3650)


class IgnoreInput(BaseModel):
    minutes: float = Field(gt=0, le=1440)
    reason: str = ''


def router(service):
    r = APIRouter(prefix='/api/v1')
    db, ha, changes = service.db, service.ha, service.changes

    @r.get('/status')
    async def status():
        return service.status()

    @r.get('/stats')
    async def stats(days: int = Query(30, ge=1, le=3650), source: str = 'voice'):
        return analysis.stats(db, days, source)

    @r.get('/requests')
    async def requests(handled_by: str = '', topic: str = '', source: str = '', device: str = '', q: str = '', pattern: str = '', since: str = '', until: str = '', include_excluded: bool = False, limit: int = Query(50, ge=1, le=2000), offset: int = Query(0, ge=0)):
        filters = locals().copy()
        filters.pop('db', None)
        return analysis.requests(db, **filters)

    @r.get('/requests/{ident}')
    async def get_request(ident: int):
        return service.get_request(ident)

    @r.patch('/requests/{ident}')
    async def patch_request(ident: int, body: TopicPatch):
        return service.set_topic(ident, body.topic_override)

    @r.get('/candidates')
    async def candidates(days: int = Query(30, ge=1, le=3650), limit: int = Query(20, ge=1, le=2000)):
        return analysis.candidates(db, ha, days, limit)

    @r.patch('/patterns/{pattern:path}')
    async def patch_pattern(pattern: str, body: dict = Body(...)):
        return service.set_pattern(pattern, body)

    @r.post('/match')
    async def match(body: MatchInput):
        return await service.match(body.sentences)

    @r.get('/intents')
    async def intents():
        return await service.intents()

    @r.get('/entities')
    async def entities(q: str = '', domain: str = '', exposed: bool | None = None):
        return await service.entities(q, domain, exposed)

    @r.get('/areas')
    async def areas():
        await ha.refresh()
        return ha.areas

    @r.get('/changes')
    async def list_changes():
        return [changes.get(c['id']) for c in db.rows('SELECT id FROM changes ORDER BY id DESC')]

    @r.post('/changes', status_code=201)
    async def create_change(body: ChangeDraft):
        return (await changes.create(body.model_dump()))

    @r.get('/changes/{ident}')
    async def get_change(ident: int):
        return changes.get(ident)

    @r.patch('/changes/{ident}')
    async def update_change(ident: int, body: dict = Body(...)):
        async with changes.lock:
            return (await changes.update(ident, body))

    @r.delete('/changes/{ident}')
    async def delete_change(ident: int):
        async with changes.lock:
            if changes.get(ident)['status'] != 'draft':
                raise ValueError('Only drafts may be deleted')
            db.log(ident, 'delete', 'api', {})
            db.execute('DELETE FROM changes WHERE id=?', (ident,))
        return {'deleted': True}

    @r.post('/changes/{ident}/apply')
    async def apply(ident: int, body: ApplyInput = Body(default=ApplyInput())):
        return await changes.apply(ident, body.confirm_regressions)

    @r.post('/changes/{ident}/rollback')
    async def rollback(ident: int, body: RollbackInput = Body(default=RollbackInput())):
        return await changes.rollback(ident, body.force)

    @r.post('/changes/{ident}/replay')
    async def replay(ident: int, body: ReplayInput = Body(default=ReplayInput())):
        async with changes.lock:
            report = await changes.replay(ident, body.days)
            db.log(ident, 'replay', 'api', report)
            return report

    @r.get('/ignore')
    async def get_ignore():
        return service.ignore()

    @r.post('/ignore')
    async def start_ignore(body: IgnoreInput):
        return service.ignore(body.minutes, body.reason)

    @r.delete('/ignore')
    async def stop_ignore():
        return service.ignore(stop=True)

    @r.get('/export')
    async def export(format: Literal['jsonl'] = 'jsonl', days: int = Query(30, ge=1, le=3650)):
        rows = analysis.requests(db, since=analysis.cutoff(days), include_excluded=True, limit=-1)
        return Response(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), media_type='application/x-ndjson')

    @r.post('/diagnose', status_code=202)
    async def diagnose(body: DiagnoseInput):
        return service.diagnose.enqueue(body.request_id, body.pattern)

    @r.get('/diagnoses')
    async def diagnoses(limit: int = Query(50, ge=1, le=200)):
        return service.diagnose.list(limit)

    @r.get('/diagnoses/{ident}')
    async def diagnosis(ident: int):
        return service.diagnose.get(ident)

    @r.post('/check-template')
    async def check(body: TemplateInput):
        return check_template(body.templates, body.sentences, body.slots)

    @r.get('/templates')
    async def templates():
        return list_templates()

    @r.post('/templates/{ident}/draft', status_code=201)
    async def template_draft(ident: str, body: dict = Body(...)):
        if set(body) != {'params'} or not isinstance(body['params'], dict):
            raise ValueError('Provide a params object')
        return await service.diagnose.create_from_template(ident, body['params'])

    @r.get('/sentences')
    async def sentences():
        return changes.sentences.listing()

    @r.post('/sentences/import')
    async def import_sentences(body: dict = Body(default={})):
        if set(body) - {'confirm', 'fingerprint'} or type(body.get('confirm', False)) is not bool:
            raise ValueError('Provide confirm and the preview fingerprint')
        async with changes.lock:
            return await changes.sentences.import_file(body.get('confirm', False), body.get('fingerprint'))

    @r.get('/blueprints')
    async def blueprints():
        return await changes.blueprints.listing()

    @r.post('/blueprints/sync')
    async def sync_blueprints():
        async with changes.lock:
            return await changes.blueprints.ensure_blueprint()

    @r.post('/blueprints/preview_reply')
    async def preview_reply(body: ReplyInput):
        return {'reply_preview': await changes.blueprints.preview_reply(body.entity, body.reply)}

    @r.get('/settings')
    async def settings():
        return dict(service.diagnose.configuration(), writer=changes.sentences.writer.configuration())

    return r
