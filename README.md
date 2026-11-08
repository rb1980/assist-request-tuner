# HA Assist Request Tuner

**Find the requests your Assistant passed to the LLM that Home Assistant could have potentially answered itself, and fix them in a few clicks.**

If your Assist pipeline uses an LLM conversation agent with **Prefer handling commands locally** turned on, Home
Assistant tries its own intents first and sends everything it doesn't recognise to the LLM. Those requests are slower,
can cost money, and sometimes go wrong. Many HA users choose to run a local LLM to keep their data privacy intact and have to rely on smaller/quantized models that may not be as good at reasoning and sorting out HA requests, and can make mistakes.

Assist Request Tuner watches your pipelines, lists the home requests that went to the LLM, explains in plain words why
Home Assistant internal processing missed each one, and fixes it, so next time Home Assistant handles it locally and instantly.

> **Example.** "Kill the lights in the bedroom" went to the LLM (18 s). Home Assistant doesn't know "kill" means "turn
> off". One fix later, Home Assistant answers it locally in a fraction of a second, for every room, along with
> "kill the kitchen lights" and "kill the bed light".

## What it can fix

| Fix | When | What it changes in Home Assistant |
|---|---|---|
| **Alias** | Speech-to-text says "playroom", the area is called "Play Room" | Adds an alias to the area or device |
| **Expose** | The device isn't exposed to Assist | Exposes it |
| **Custom sentence** | HA can do it, but not with your words ("kill the {area} lights") | Adds sentences to a built-in intent, in its own sentence file |
| **Spoken reply** | A question with a custom answer ("what is the thermostat set to") | An automation from its read-only `voice_reply` blueprint |
| **More phrasings** | An existing sentence-trigger automation is right but needs more wording | Adds sentences to it |

Every fix is checked before it is applied (your examples must match; nothing Home Assistant already handles may be
taken over), tested on Home Assistant afterwards, compared with your last 30 days of requests, and undone
automatically if the test fails. You can undo any fix later with one click. It never runs a voice command to test a
fix: it uses Home Assistant's dry-run matcher.

## Before you start

- **Home Assistant** with Assist (tested with 2026.11), and a pipeline whose conversation agent is an LLM (Local OpenAI
  LLM, OpenAI, Ollama, Google, Anthropic or any other).
- In that pipeline (**Settings → Voice assistants →** your assistant), turn on **Prefer handling commands locally**.
  Without it every request goes straight to the LLM and there is nothing for the app to compare.
- Optional, for **Find fixes with AI**: an OpenAI-compatible API with a model that supports tool calling. It can be the
  LLM your voice assistant already uses, a local server (LM Studio, Ollama, llama.cpp, vLLM), or a cloud provider
  (OpenRouter, OpenAI, Cerebras, Groq…). Without one, fixes are still available from the built-in checks, by hand and
  from recipes.

## Install

### Home Assistant OS or Supervised: as an App (recommended)

1. **Settings → Apps → App store → ⋮ → Repositories**, add `https://github.com/rb1980/assist-request-tuner`.
2. Find **Assist Request Tuner** in the store, **Install**, then **Start**.
3. On its **Info** tab, turn on **Show in sidebar** (or use **Open Web UI**). **Request Tuner** appears in the sidebar.

As an App it needs no token, password or SSH key: it reaches Home Assistant through the Supervisor, uses Home
Assistant's own login, and writes its files straight into your config folder.

### Docker Compose (any Home Assistant install)

```sh
git clone https://github.com/rb1980/assist-request-tuner && cd assist-request-tuner
mkdir data && cp config.example.yaml data/config.yaml && chmod 600 data/config.yaml
# edit data/config.yaml: home_assistant.url, home_assistant.token and ar_tuner.token (see below)
sudo chown -R 10001:10001 data
docker compose up -d
```

- `home_assistant.token`: a long-lived access token of an **administrator** (your HA profile → **Security** tab →
  **Long-lived access tokens**). Admin rights are needed to read pipeline debug data and to add aliases.
- `ar_tuner.token`: any long random string (for example `openssl rand -hex 32`). It is the password for the web page and
  the token for the REST API and MCP.

Open `http://<host>:7380` and log in with `ar_tuner.token`. You can change every setting later on the app's Settings page.

Custom sentences and spoken replies are files in Home Assistant's config folder, so for those the app needs a way to
write there (`home_assistant.config_access`, also on the Settings page):

- **`sftp`** (default): through the **Advanced SSH & Web Terminal** App. Create a key just for this
  (`ssh-keygen -t ed25519 -N '' -f data/ha_ssh_key -C ar-tuner`), add `data/ha_ssh_key.pub` to that App's
  `authorized_keys`, and turn on SFTP there. This key gets shell access to Home Assistant, although the app only ever
  writes its own two files.
- **`local`**: Docker runs on the same machine as Home Assistant. Mount HA's config folder at `/homeassistant` (see
  `docker-compose.yml`).
- **`none`**: those fixes can be drafted and checked but not applied. Aliases and exposing still work.

## Use it

### 1. Let it collect requests

Use your voice assistant as usual. Every pipeline run is logged: what was said, whether Home Assistant or the LLM
handled it, and what the LLM did and replied. Requests that went to the LLM about your home (lights, climate, media,
scenes…) are the ones it can fix; general questions ("what's the weather in Paris") are kept apart.

Testing your setup and don't want it counted? **Stats → Ignore test traffic** pauses counting for a while, and
**Settings → Collecting requests** can leave out a test pipeline or device for good.

### 2. Fix a request

**Fix requests** (the start page) walks you through it in four steps:

1. **Choose a request.** Home requests the LLM handled, most frequent first. Not worth fixing? **🚫 Ignore** removes it
   from the list (Home Assistant isn't changed, and you can undo it).
2. **Why and how to fix.** What was said, what the LLM did, why Home Assistant missed it, and phrasings that already
   work. Then the fixes: ready-made ones when the cause is simple (a missing alias, an unexposed device), or **Find
   fixes with AI**, which works out the cause and proposes fixes in about a minute. You can also **write a fix by
   hand** or **use a recipe**.
3. **Review.** The fix in one sentence, what you'll be able to say afterwards, and the checks. Adjust the phrasings or
   examples if you like, then **Apply to Home Assistant**.
4. **Done.** How Home Assistant handles each example now (and what it answers, for questions), other past requests the
   fix also covers, and **Undo this change**.

Fixed requests leave the list (see **Fixed** at the bottom of the page). A fix that was undone, or didn't pass its
checks, is marked on its card, with **Try a different fix**.

### 3. Keep an eye on it

- **Stats**: how many home requests Home Assistant handled itself, local and LLM requests by day, and per device.
- **History**: every fix, with one-click undo.
- **Advanced → Custom sentences**: the sentence file it manages, phrasing by phrasing. If you edit that file in Home
  Assistant yourself, the app notices and asks you to import your edits before it applies anything else.
- **Advanced → Requests, Candidates, Intents, Changes, Recipes**: the raw material, for when you want details.

## AI analysis

**Find fixes with AI** sends one request (with similar ones, what the voice assistant did and replied, and the names
of your exposed devices and areas) to an AI, which uses Home Assistant's dry-run matcher to test its ideas and
proposes fixes. The AI only ever drafts: applying is always your click. Set it up under **Settings → AI
analysis**, with one of two choices:

- **Home Assistant's Local OpenAI LLM.** Uses the server and model of a voice agent you already set up with the
  [Local OpenAI LLM](https://github.com/skye-harris/hass_local_openai_llm) integration (HACS). Pick the agent; there
  is nothing to type. The app reads that server's URL and key from Home Assistant's settings
  (`.storage/core.config_entries`, read only, through the same config folder access it uses for its own files) and
  follows changes you make there. Without that integration, or without config folder access, this choice is shown as
  unavailable.
- **Custom.** Any OpenAI-compatible API: enter its URL and key, press **Test**, and choose a model from the list. Test
  lists only models that can call tools, checks the one you choose with a real request, and fills in its prices when
  the API publishes them (OpenRouter does), for the daily budget. Save several as **AI setups** to switch between them. 

Bigger models give better analyses: in testing, Qwen 3.8 27B did well (locally in LM Studio, or fast on Cerebras), while
some others skipped their own checks. Providers differ in which optional request fields they accept; the app finds out
and adapts on its own (see [Configuration](#configuration)).

## Configuration

The easiest way is the app's own **Settings** tab: the Home Assistant connection, access to
its config folder, the AI analysis, and what to collect. Each section has a **Test** button that checks the values
before you save. Changes apply immediately, without a restart. Secrets are never shown again once saved; they are kept
in `/data/settings.json` (mode 600).

Every setting can also come from a file or the environment. [`config.example.yaml`](config.example.yaml) lists them all
with their defaults and environment variable names. From lowest to highest priority: defaults, the config file, the
App's **Configuration** tab, environment variables, then the Settings page. The Settings page shows where each value
comes from; empty a field there to go back to the value below it.

## REST API and MCP

Everything the web page does is also available to scripts and coding agents. All `/api/v1/*` and `/mcp/` calls need
`Authorization: Bearer <token>`: `ar_tuner.token`, or in the App the token shown at the bottom of its Settings page (and
set a port for `7380/tcp` on the App's **Network** tab). The OpenAPI docs are at `/docs`.

```sh
curl -H "Authorization: Bearer $TOKEN" http://<host>:7380/api/v1/status
claude mcp add --transport http request-tuner http://<host>:7380/mcp/ --header "Authorization: Bearer $TOKEN"
```

To keep test traffic out of the statistics, POST `{"minutes": 30, "reason": "testing"}` to `/api/v1/ignore`.

## Privacy and security

- Requests (transcripts, what the LLM did and replied) are stored in a local SQLite database. No audio is stored.
  Nothing leaves your network unless you set up a cloud AI for analysis.
- With AI analysis, the request, what the voice assistant did, and the names of your exposed devices and areas go to
  the AI you chose. OpenRouter's zero-data-retention routing is requested by default.
- The Home Assistant client is limited to the registry, pipeline debug and dry-run matcher calls it needs, plus Home
  Assistant's own agent for read-only answers.
- It writes only its own two files in HA's config folder (`custom_sentences/en/ar_tuner.yaml` and
  `blueprints/automation/ar_tuner/voice_reply.yaml`), refuses symlinks, and replaces files atomically.
- The web page uses an HttpOnly, SameSite=Strict cookie and loads no external assets. Inside the App it relies on Home
  Assistant's own login.


## Development

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
docker compose up -d --build     # build the image from source
```

Tests use fake Home Assistant, SFTP and AI servers on loopback, with synthetic data. No test touches a real Home
Assistant.

To try the App in a real Home Assistant without touching yours, `scripts/dev-ha.sh up` starts a throwaway Home
Assistant with a real Supervisor in Docker (Home Assistant's App-development container, stable channel) and installs
the App from your working copy; `scripts/dev-ha.sh rebuild` picks up code changes. Its web page is on
http://localhost:8125 (login in `.data/dev-ha-login.txt`). `scripts/dev-ha.sh assistant <api_url> <model>` adds an LLM
voice assistant on any OpenAI-compatible API (demo lights in rooms, Home Assistant's intents first, then the LLM), and
`scripts/dev-ha.sh say "kill the lights in the bedroom"` sends requests through it.

Releases: set the same version in `app/__init__.py` and `ha-app/config.yaml` and push a `vX.Y.Z` tag. The workflow in
`.github/workflows/release.yml` runs the tests and publishes the Docker image (amd64, arm64) and the App images (amd64,
aarch64) to GHCR.

## License

MIT, see [LICENSE](LICENSE).
