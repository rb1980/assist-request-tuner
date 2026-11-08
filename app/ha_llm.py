"""Use an LLM server and model already set up in Home Assistant with the Local OpenAI LLM integration (HACS:
skye-harris/hass_local_openai_llm, domain local_openai) for AI analysis, instead of typing its URL and key again.

HA's API never returns an integration's API key, so the servers are read from HA's own config storage
(.storage/core.config_entries) through the config folder access Request Tuner already has (the App's mounted folder, or
SFTP). Read only; only local_openai entries are used, and nothing from the file is stored or logged.

Everything here degrades to a plain message: no integration, no config folder access, an unreadable file or a server
removed in HA all leave AI analysis off with the reason shown, never an error.
"""
import json
import time

DOMAIN = 'local_openai'
CACHE_S = 60   # HA's file is re-read at most once a minute (each read over SFTP is a new connection)


def parse(text):
    """The Local OpenAI LLM servers in a core.config_entries file: [{id, title, base_url, api_key, agents}], where
    agents are the server's conversation agents in HA: [{id, title, model}]."""
    try:
        entries = json.loads(text)['data']['entries']
    except (ValueError, KeyError, TypeError):
        raise ValueError("Home Assistant's config entries file couldn't be read") from None
    servers = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get('domain') != DOMAIN or entry.get('disabled_by'):
            continue
        data = entry.get('data') if isinstance(entry.get('data'), dict) else {}
        url = str(data.get('base_url') or '').strip().rstrip('/')
        if not url.startswith(('http://', 'https://')):
            continue
        agents = [dict(id=str(s.get('subentry_id', '')), title=str(s.get('title') or s['data']['model']), model=str(s['data']['model']))
                  for s in entry.get('subentries') or []
                  if isinstance(s, dict) and s.get('subentry_type') == 'conversation' and isinstance(s.get('data'), dict)
                  and s['data'].get('model')]
        servers.append(dict(id=str(entry.get('entry_id', '')), title=str(entry.get('title') or data.get('server_name') or url),
                            base_url=url, api_key=str(data.get('api_key') or ''), agents=agents))
    return servers


async def servers(writer, force=False):
    """(servers, problem). problem is a sentence when they can't be read, else None. Cached on the writer."""
    cached = getattr(writer, '_ha_llm', None)
    if cached and not force and time.monotonic() - cached[0] < CACHE_S:
        return cached[1], cached[2]
    if not writer.configuration()['configured']:
        found, problem = [], "Request Tuner can't read Home Assistant's config folder (see “Writing to the HA config folder”)."
    else:
        try:
            text = await writer.read('config_entries')
            found, problem = (parse(text), None) if text is not None else ([], "Home Assistant's config entries file wasn't found.")
        except Exception as exc:   # SFTP, permissions, a malformed file: report, never raise
            found, problem = [], f"Couldn't read Home Assistant's settings: {str(exc) or type(exc).__name__}"
    writer._ha_llm = (time.monotonic(), found, problem)
    return found, problem


def choose(found, wanted):
    """(server, agent) to use. wanted is "<entry id>/<agent id>" (or an entry id): that one if it still exists, else the
    first agent of that server, else the first server's first agent. The agent is None when a server has none."""
    entry, _, agent_id = (wanted or '').partition('/')
    server = next((s for s in found if s['id'] == entry), None) or (found[0] if found else None)
    if not server:
        return None, None
    agent = next((a for a in server['agents'] if a['id'] == agent_id), None) or (server['agents'][0] if server['agents'] else None)
    return server, agent


async def resolve(settings, writer, force=False):
    """Fill in settings.ai_ha_* from HA when the AI source is Home Assistant. Returns the servers found."""
    found, problem = await servers(writer, force) if settings.ai_source == 'ha' or force else ([], None)
    server, agent = choose(found, settings.ai_ha_server) if settings.ai_source == 'ha' else (None, None)
    usable = server and agent
    settings.ai_ha_url = server['base_url'] if usable else ''
    settings.ai_ha_key = server['api_key'] if usable else ''
    settings.ai_ha_title = server['title'] if server else ''
    settings.ai_ha_model = agent['model'] if usable else ''
    if usable or settings.ai_source != 'ha':
        settings.ai_ha_problem = ''
    elif server:
        settings.ai_ha_problem = (f"“{server['title']}” has no conversation agent in Home Assistant, so there is no model to use. "
                                  'Add one there, or choose Custom.')
    else:
        settings.ai_ha_problem = problem or 'No Local OpenAI LLM server is set up in Home Assistant. Add one there, or choose Custom.'
    return found, problem
