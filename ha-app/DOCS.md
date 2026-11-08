# Assist Request Tuner

Finds the voice requests your LLM conversation agent had to handle that Home Assistant could answer itself, explains
why Home Assistant missed each one, and fixes it: an alias, a custom sentence or a spoken reply. Every fix is checked
before it is applied, tested on Home Assistant afterwards, and can be undone in one click.

## Setup

1. Install the App and start it. Turn on **Show in sidebar** on its Info tab (or use **Open Web UI**).
2. In your voice assistant's pipeline (**Settings → Voice assistants**), turn on **Prefer handling commands locally**,
   so that Home Assistant tries its own intents before the LLM.
3. Optional: let an AI propose fixes ("Find fixes with AI"). On the app's **Settings** page (Advanced → Settings),
   choose **AI server: Home Assistant's Local OpenAI LLM** to use a voice agent you already set up with that
   integration (HACS), or **Custom** for any OpenAI-compatible API and key.

Then use your voice assistant as usual, and open **Request Tuner** to fix the requests that went to the LLM.

The App reads your pipelines' debug data and registries through the Supervisor (and, when you choose Home Assistant's
Local OpenAI LLM as the AI server, that integration's server URL and key from `.storage/core.config_entries`, read
only). It writes only two files in your config folder: `custom_sentences/en/ar_tuner.yaml` and
`blueprints/automation/ar_tuner/voice_reply.yaml`. If you edit the sentence file by hand, the app won't apply
anything until you import your edits on its Custom sentences page.

## Coding agents (REST and MCP)

Set a port for `7380/tcp` on the **Network** tab, then use the API token shown on the app's **Settings** page:
`http://<home assistant>:<port>/api/v1/...` and the MCP endpoint `/mcp/`.

## Privacy

Requests are stored in the App's own data folder. With AI analysis set up, "Find fixes with AI" sends the request
text, what the voice assistant did and replied, and the names of your exposed devices and areas to the AI you chose.
OpenRouter's zero-data-retention routing is requested by default.
