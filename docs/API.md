# REST API

Everything the web page does is also available over a REST API, for scripts, dashboards and automations. It uses the
same data and the same safety checks as the web page: drafts are checked before they are applied, every applied change
has a backup, and anything that fails its tests is undone.

For Claude Code, Codex and other coding agents, use [MCP](MCP.md) instead.

## Turn on access

**In the Home Assistant App:**

1. **Settings → Apps → Assist Request Tuner → Configuration → Network**: enter a port for `7380/tcp` (for example `7380`) and save.
   The App restarts. Without a port, the API can't be reached; the sidebar panel works either way.
2. For the token, either set your own **API token** (at least 16 characters) on the same **Configuration** tab and
   restart the App, or use the one the App made: **Request Tuner → Advanced → Settings → Status and maintenance → API
   and MCP access → Show API token**.

**With Docker Compose:** use `ar_tuner.token` from your config file (the token you log in with).

The token gives full access, including applying changes to Home Assistant. Keep it in an environment variable, don't
commit it, and keep the port (plain HTTP) on your home network:

```sh
export AR_TUNER_TOKEN='<your token>'
export AR_TUNER_URL='http://homeassistant.local:7380'
curl -H "Authorization: Bearer $AR_TUNER_TOKEN" "$AR_TUNER_URL/api/v1/status"
```

## Basics

- Base path `/api/v1`, header `Authorization: Bearer <token>`, JSON in and out.
- Interactive OpenAPI docs are at `/docs` (log in to the web page on the same address first).
- Errors: `400` invalid input, `401` missing or wrong token, `404` not found, `422` invalid change, `503` Home
  Assistant unavailable. The body is `{"detail": "..."}`.

## Endpoints

**Requests and statistics**

| Method and path | What it does |
|---|---|
| `GET /status` | Version, collector state, HA connection, config folder access, request count |
| `GET /stats?days=30&source=voice` | Local vs LLM share of home requests, by day and device |
| `GET /requests?handled_by=llm&topic=home&device=&q=&since=&until=&limit=50&offset=0` | Captured requests |
| `GET /requests/{id}` | One request: transcript, what the LLM did and replied, matcher evidence |
| `PATCH /requests/{id}` `{"topic_override": "home"}` | Reclassifies one request (`home`, `general`, `unknown` or `null`) |
| `GET /candidates?days=30&limit=20` | Home phrasings the LLM handled, most frequent first |
| `PATCH /patterns/{pattern}` `{"ignored": true}` | Hides a candidate, or sets `topic_override` for a pattern |
| `GET /export?days=30` | All requests as JSON Lines, for your own analysis |

**Home Assistant (read only)**

| Method and path | What it does |
|---|---|
| `POST /match` `{"sentences": [...]}` | Runs sentences through Home Assistant's dry-run matcher |
| `GET /intents` | Built-in intents and sentence automations Home Assistant handled locally |
| `GET /entities?q=&domain=&exposed=` | Entity names, aliases, areas and Assist exposure |
| `GET /areas` | Area names and aliases |

**Changes**

| Method and path | What it does |
|---|---|
| `GET /changes`, `GET /changes/{id}` | Drafts and applied changes, with checks, backup, replay report and history |
| `POST /changes` | Drafts a change (see [change types](#change-types)); returns `201` |
| `PATCH /changes/{id}`, `DELETE /changes/{id}` | Edits or deletes a draft |
| `POST /changes/{id}/apply` `{"confirm_regressions": false}` | **Changes Home Assistant.** Backs up, applies, tests and replays; undoes it on failure |
| `POST /changes/{id}/rollback` `{"force": false}` | **Changes Home Assistant.** Restores the backup |
| `POST /changes/{id}/replay` `{"days": 30}` | Re-tests past requests against an applied change |
| `POST /check-template` `{"templates": [...], "sentences": [...], "slots": {...}}` | Checks sentence templates offline |
| `GET /templates`, `POST /templates/{id}/draft` | Tested recipes, rendered into a draft |
| `GET /sentences`, `POST /sentences/import` | The managed sentence file; import your own edits to it |
| `GET /blueprints`, `POST /blueprints/sync` | The `voice_reply` blueprint; install the current version in HA |
| `POST /blueprints/preview_reply` `{"entity": "...", "reply": "..."}` | Renders a spoken reply with HA's read-only template API |

**AI analysis** (sends the request and your exposed names to the AI set up on the Settings page)

| Method and path | What it does |
|---|---|
| `POST /diagnose` `{"request_id": 42}` or `{"pattern": "..."}` | Queues an analysis; returns `202` and its id |
| `GET /diagnoses?limit=50`, `GET /diagnoses/{id}` | Status and result: cause, verified phrasings, drafts, cost |

**Other**

| Method and path | What it does |
|---|---|
| `GET /ignore`, `POST /ignore` `{"minutes": 30, "reason": "testing"}`, `DELETE /ignore` | Leaves out requests for a while (testing) |
| `GET /settings` | Current settings (secrets left out) |

## Change types

| `type` | `payload` |
|---|---|
| `entity_alias` | `{"entity_id": "light.bed_light", "add": ["bedside lamp"], "remove": []}` |
| `area_alias` | `{"area_id": "play_room", "add": ["playroom"]}` |
| `expose_entity` | `{"entity_id": "fan.attic", "expose": true}` |
| `custom_sentence` | Sentences added to a built-in intent, in the app's sentence file |
| `blueprint_instance` | A spoken reply: an automation from the `voice_reply` blueprint |
| `trigger_automation` | A sentence-trigger automation the app owns (id starts with `ar_tuner_`) |
| `trigger_sentences` | `{"automation_id": "...", "add": ["..."]}`: more phrasings for an existing sentence-trigger automation |

`tests` takes `must_match` (a list of `{"sentence": "...", "intent": "HassTurnOff"}`) and `must_not_match` (sentences
that must keep their current handling). After applying, a change whose `must_match` sentences don't match is undone
automatically. The easiest way to see a complete payload for the complex types is to draft one in the web page (or
with `POST /templates/{id}/draft`) and read it with `GET /changes/{id}`.

## Examples

```sh
H="Authorization: Bearer $AR_TUNER_TOKEN"

# Top candidates
curl -s -H "$H" "$AR_TUNER_URL/api/v1/candidates?days=30&limit=10"

# What would Home Assistant do with this?
curl -s -H "$H" -H 'Content-Type: application/json' -d '{"sentences": ["kill the bedroom lights"]}' \
  "$AR_TUNER_URL/api/v1/match"

# Draft an alias, then apply it (use the id the first call returns)
curl -s -H "$H" -H 'Content-Type: application/json' "$AR_TUNER_URL/api/v1/changes" -d '{
  "type": "area_alias", "title": "Playroom alias", "payload": {"area_id": "play_room", "add": ["playroom"]},
  "tests": {"must_match": [{"sentence": "turn on the playroom lights"}]}}'
curl -s -H "$H" -X POST "$AR_TUNER_URL/api/v1/changes/1/apply"

# Keep a 30-minute test session out of the statistics
curl -s -H "$H" -H 'Content-Type: application/json' -d '{"minutes": 30, "reason": "testing"}' "$AR_TUNER_URL/api/v1/ignore"
```
