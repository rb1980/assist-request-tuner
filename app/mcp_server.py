from mcp.server.fastmcp import FastMCP
from . import names
from . import analysis
from .precheck import check_template as offline_check
from .phrasing import list_templates


def build_mcp(service):
    mcp = FastMCP(names.TECH, stateless_http=True, json_response=True, streamable_http_path='/')
    db, ha, changes = service.db, service.ha, service.changes

    @mcp.tool()
    async def get_status() -> dict:
        """Check collector health and HA connectivity before analysis."""
        return service.status()

    @mcp.tool()
    async def get_stats(days: int = 30, source: str = 'voice') -> dict:
        """Measure home coverage and local versus LLM usage."""
        return analysis.stats(db, days, source)

    @mcp.tool()
    async def list_requests(handled_by: str = '', topic: str = '', source: str = '', device: str = '', q: str = '', pattern: str = '', since: str = '', until: str = '', include_excluded: bool = False, limit: int = 50, offset: int = 0) -> list:
        """Find captured transcripts; excluded traffic is hidden by default."""
        filters = locals().copy()
        filters.pop('db', None)
        filters['limit'] = max(1, min(limit, 2000))
        return analysis.requests(db, **filters)

    @mcp.tool()
    async def get_request(id: int) -> dict:
        """Inspect a captured request, tool arguments, results and matcher evidence."""
        return service.get_request(id)

    @mcp.tool()
    async def list_candidates(days: int = 30, limit: int = 20) -> list:
        """Rank home phrasing patterns handled by the LLM and inspect tuning hints."""
        return analysis.candidates(db, ha, days, min(max(limit, 1), 2000))

    @mcp.tool()
    async def test_sentences(sentences: list[str]) -> dict:
        """Dry-run HA's matcher; never executes an intent or automation."""
        return await service.match(sentences)

    @mcp.tool()
    async def list_intents() -> dict:
        """Inspect sentence automations and built-in intents observed locally."""
        return await service.intents()

    @mcp.tool()
    async def find_entities(q: str = '', domain: str = '', exposed: bool | None = None) -> list:
        """Find entity names, aliases, areas and Assist exposure for alias drafting."""
        return await service.entities(q, domain, exposed)

    @mcp.tool()
    async def list_areas() -> list:
        """Find area names and aliases for local matcher tuning."""
        await ha.refresh()
        return ha.areas

    @mcp.tool()
    async def create_change(type: str, title: str, payload: dict, tests: dict | None = None, allow_device_actions: bool = False) -> dict:
        """Stage an alias, exposure or sentence automation change without modifying HA."""
        return (await changes.create(dict(type=type, title=title, payload=payload, tests=tests or {}, allow_device_actions=allow_device_actions), 'mcp'))

    @mcp.tool()
    async def update_change(id: int, patch: dict) -> dict:
        """Edit an existing draft before applying it."""
        async with changes.lock:
            return (await changes.update(id, patch, 'mcp'))

    @mcp.tool()
    async def list_changes() -> list:
        """List staged and applied changes with audit history."""
        return [changes.get(c['id']) for c in db.rows('SELECT id FROM changes ORDER BY id DESC')]

    @mcp.tool()
    async def get_change(id: int) -> dict:
        """Read backup, tests, replay regressions and audit history of a change."""
        return changes.get(id)

    @mcp.tool()
    async def apply_change(id: int, confirm_regressions: bool = False) -> dict:
        """MODIFIES HOME ASSISTANT. Back up, apply, dry-run tests and replay; restore on failures or unconfirmed regressions. Review lost matches before confirmation."""
        return await changes.apply(id, confirm_regressions, 'mcp')

    @mcp.tool()
    async def rollback_change(id: int, force: bool = False) -> dict:
        """MODIFIES HOME ASSISTANT. Restore the exact backup, latest change on target first."""
        return await changes.rollback(id, force, 'mcp')

    @mcp.tool()
    async def replay_change(id: int, days: int = 30) -> dict:
        """Dry-run historical transcripts after apply; drafts cannot be previewed in HA."""
        async with changes.lock:
            report = await changes.replay(id, days)
            db.log(id, 'replay', 'mcp', report)
            return report

    @mcp.tool()
    async def set_ignore_window(minutes: float | None = None, reason: str = '', stop: bool = False) -> list:
        """Exclude captured eval traffic for minutes, stop a window, or inspect it."""
        return service.ignore(minutes, reason, stop)

    @mcp.tool()
    async def set_pattern(pattern: str, ignored: bool = False, topic_override: str | None = None, note: str = '') -> dict:
        """Hide a one-off candidate or override a pattern's topic classification."""
        return service.set_pattern(pattern, dict(ignored=ignored, topic_override=topic_override, note=note))

    @mcp.tool()
    async def diagnose_request(request_id: int) -> dict:
        """Queue read-only model analysis of an LLM-handled request; sends context to the configured provider."""
        return service.diagnose.enqueue(request_id=request_id)

    @mcp.tool()
    async def diagnose_pattern(pattern: str) -> dict:
        """Queue read-only model analysis of a candidate pattern. Proposed changes remain drafts."""
        return service.diagnose.enqueue(pattern=pattern)

    @mcp.tool()
    async def get_diagnosis(id: int) -> dict:
        """Poll diagnosis status, verified phrasings, draft links, tool steps and estimated cost."""
        return service.diagnose.get(id)

    @mcp.tool()
    async def list_diagnoses(limit: int = 50) -> list:
        """List recent diagnosis jobs and their outcomes."""
        return service.diagnose.list(limit)

    @mcp.tool()
    async def check_template(templates: list[str], sentences: list[str], slots: dict | None = None) -> dict:
        """Check proposed sentence templates offline with hassil; never contacts or modifies HA."""
        return offline_check(templates, sentences, slots)

    @mcp.tool()
    async def list_phrasing_templates() -> list:
        """Inspect the versioned library of tested sentence automation recipes."""
        return list_templates()

    @mcp.tool()
    async def create_change_from_template(template_id: str, params: dict) -> dict:
        """Render and pre-check a library recipe into a draft. Does not apply it to HA."""
        return await service.diagnose.create_from_template(template_id, params, 'mcp')

    @mcp.tool()
    async def list_custom_sentences() -> dict:
        """Read managed custom sentence blocks, rendered English YAML and writer status; no remote writes."""
        return changes.sentences.listing()

    @mcp.tool()
    async def list_blueprints() -> dict:
        """Inspect the bundled reply blueprint and its remote sync status."""
        return await changes.blueprints.listing()

    @mcp.tool()
    async def preview_reply(entity: str, reply: str) -> dict:
        """Render a reply for an exposed entity using HA's read-only template API."""
        return {'reply_preview': await changes.blueprints.preview_reply(entity, reply)}

    return mcp
