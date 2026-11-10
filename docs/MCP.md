# Coding agents (MCP)

Claude Code, Codex and other coding agents can use Assist Request Tuner through MCP. They see the same requests and
use the same safety checks as the web page: drafts are checked before they are applied, every applied change has a
backup, and anything that fails its tests is undone.

For scripts, see the [REST API](API.md) instead.

## Why use an agent instead of the web page?

The web page inside Home Assistant is the easiest way to fix requests one at a time. An agent is useful when you want
to work through many at once, or combine Assist Request Tuner with other things the agent can see.

| Use case | What you ask the agent |
|---|---|
| **Clear a backlog** | "List the top 20 candidates from the last 30 days, group the ones with the same cause, and draft one fix per group." |
| **Investigate a pattern** | "Why do requests about the garage keep going to the LLM? Show me the transcripts and what HA's matcher says." |
| **Design phrasings carefully** | "Draft a custom sentence for 'kill the {area} lights', test 15 variations offline and against HA's matcher, and show me what it would take over." |
| **Review before applying** | "Show me the replay report for change 12: which past requests would it now handle, and does it take anything away from another intent?" |
| **Test your voice setup** | "Start a 30-minute ignore window, then send these 40 test phrases through my pipeline" (with an agent that can also reach Home Assistant), so the test traffic doesn't skew the statistics. |
| **Report and track** | "Compare this week's local vs LLM share with last week's, per device." |
| **Work with your config repo** | If you keep your Home Assistant config in Git, the agent can read the sentence file and automations it writes alongside your own files, and commit snapshots after a change. |

The agent never gets more power than the web page: it can only use the tools listed below, it can't run voice
commands (testing uses Home Assistant's dry-run matcher), and only two tools change Home Assistant
(`apply_change` and `rollback_change`). Most agents ask you before calling a tool, so you approve each apply.

## 1. Turn on access

**In the Home Assistant App:**

1. **Settings → Apps → Assist Request Tuner → Configuration → Network**: enter a port for `7380/tcp` (for example `7380`) and save.
   The App restarts. Without a port, MCP can't be reached; the sidebar panel works either way.
2. For the token, either set your own **API token** (at least 16 characters) on the same **Configuration** tab and
   restart the App, or use the one the App made: **Request Tuner → Advanced → Settings → Status and maintenance → API
   and MCP access → Show API token**.

The address is `http://<home assistant host>:<port>`, for example `http://homeassistant.local:7380`.

**With Docker Compose:** use `ar_tuner.token` from your config file (the token you log in with). The address is
`http://<docker host>:7380`.

The token gives full access, including applying changes to Home Assistant. Keep it in an environment variable rather
than typing it into commands, and don't commit it:

```sh
export AR_TUNER_TOKEN='<your token>'
export AR_TUNER_URL='http://homeassistant.local:7380'
```

The port is plain HTTP. Keep it on your home network (or a VPN such as Tailscale); don't forward it to the internet.

## 2. Connect your agent

The MCP endpoint is `/mcp/` (with the trailing slash), using the streamable HTTP transport and the token as a bearer
token.

### Claude Code

```sh
claude mcp add --transport http request-tuner "$AR_TUNER_URL/mcp/" --header "Authorization: Bearer $AR_TUNER_TOKEN"
```

This saves the token in your Claude Code settings (`~/.claude.json`). To share the setup in a project without the
token, add a `.mcp.json` to the project instead; Claude Code fills in the environment variables when it starts:

```json
{
  "mcpServers": {
    "request-tuner": {
      "type": "http",
      "url": "${AR_TUNER_URL}/mcp/",
      "headers": { "Authorization": "Bearer ${AR_TUNER_TOKEN}" }
    }
  }
}
```

Check with `claude mcp list` (it should say *connected*), or `/mcp` inside Claude Code.

### Codex

```sh
codex mcp add request-tuner --url "$AR_TUNER_URL/mcp/" --bearer-token-env-var AR_TUNER_TOKEN
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.request-tuner]
url = "http://homeassistant.local:7380/mcp/"
bearer_token_env_var = "AR_TUNER_TOKEN"
```

Codex reads the token from `AR_TUNER_TOKEN` when it starts, so export it in the shell you start Codex from.

### Other agents

Any MCP client with streamable HTTP support works: point it at `<address>/mcp/` with the header
`Authorization: Bearer <token>`.

## 3. Get started

Ask the agent to call `get_status` first: it reports whether the collector is running and connected to Home
Assistant. Then something like:

> Use request-tuner. Show me the top candidates from the last 30 days, explain why Home Assistant missed the first
> one, and draft a fix. Don't apply anything yet.

A good working pattern is **look → draft → test → review → apply**: `list_candidates` / `list_requests` →
`create_change` (or `diagnose_request` to have the configured AI propose one) → `check_template` and
`test_sentences` → `get_change` to review the checks → `apply_change` when you're happy, `rollback_change` to undo.

## Tools

**Look**

| Tool | What it does |
|---|---|
| `get_status` | Version, collector state, HA connection, config folder access, request count |
| `get_stats` | Local vs LLM share of home requests, by day and device |
| `list_requests` | Captured requests, filtered by who handled them, topic, device, text, pattern or date |
| `get_request` | One request: transcript, what the LLM did and replied, matcher evidence |
| `list_candidates` | Home phrasings the LLM handled, most frequent first, with hints about the cause |
| `list_intents` | Built-in intents and sentence automations Home Assistant handled locally |
| `find_entities`, `list_areas` | Names, aliases, areas and Assist exposure, for drafting aliases |
| `list_custom_sentences` | The sentence file the app manages, block by block |
| `list_blueprints` | The bundled `voice_reply` blueprint and whether HA has the current version |

**Test (never changes anything)**

| Tool | What it does |
|---|---|
| `test_sentences` | Runs sentences through Home Assistant's dry-run matcher: what would handle them |
| `check_template` | Checks sentence templates offline (hassil), without contacting Home Assistant |
| `preview_reply` | Renders a spoken reply template for an entity with HA's read-only template API |

**Draft and review (doesn't change Home Assistant)**

| Tool | What it does |
|---|---|
| `create_change` | Drafts a change, with tests it must pass ([change types](API.md#change-types)) |
| `update_change` | Edits a draft |
| `list_phrasing_templates`, `create_change_from_template` | Tested recipes for common requests, rendered into a draft |
| `list_changes`, `get_change` | Drafts and applied changes, with checks, backup, replay report and history |
| `replay_change` | Re-tests past requests against an applied change |
| `diagnose_request`, `diagnose_pattern` | Asks the AI set up on the Settings page to find the cause and draft fixes (sends the request and your exposed names to that AI) |
| `get_diagnosis`, `list_diagnoses` | Result of an AI analysis: cause, verified phrasings, drafts, cost |

**Change Home Assistant**

| Tool | What it does |
|---|---|
| `apply_change` | Backs up, applies, tests with the matcher and replays past requests. Undoes it if a test fails, or if it would take requests away from something else, unless you confirm (`confirm_regressions`) |
| `rollback_change` | Restores the backup taken before a change (latest change on the same target first) |

**Housekeeping (the app's own data only)**

| Tool | What it does |
|---|---|
| `set_ignore_window` | Leaves out requests for a while (testing), stops it, or shows it |
| `set_pattern` | Hides a candidate, or marks it home/general |

## Troubleshooting

- **The agent says the server isn't connected**: check the URL ends in `/mcp/`, and that the token variable was set in
  the shell the agent was started from.
- **401 Unauthorized**: wrong or missing token. In the App, use the token from its Settings page, not a Home Assistant
  token.
- **Connection refused**: in the App, no port is set under Network on its Configuration tab (or the App wasn't restarted after setting it).
- **Tools fail with "Home Assistant unavailable"**: the app can't reach Home Assistant right now; `get_status` shows why.
