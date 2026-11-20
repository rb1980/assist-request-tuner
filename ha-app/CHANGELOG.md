# Changelog

## 0.5.3

- The list of requests to fix is easier to read: how often and when on the first line, the devices and pipeline on the
  second, the AI's action on the third.

## 0.5.2

- Each request now shows which voice assistant pipeline it went to. On the Settings page, **Pipelines to leave out** is a
  checklist of your pipelines that says which ones send everything to the LLM; a left-out pipeline is no longer
  collected, and its requests are hidden from the lists and statistics until you tick it again.
- Custom AI setup is simpler: **Test** sits under the API key and lists the models to choose from (with a search box
  for long lists such as OpenRouter's); choosing one checks it. **Save this configuration** keeps it under a name, and
  **Saved Configurations → Load Config** switches back.
- The App's **Configuration** tab now only has what coding agents need: an optional **API token** and the API port
  (off while empty). All other settings are on the app's Settings page; options set on the old Configuration tab are
  no longer read.
- Saving an unrelated setting no longer shows Home Assistant as disconnected until the page is reloaded.
- After an update the browser always loads the new page code, without a hard refresh.
- New guides for coding agents (MCP) and the REST API.

## 0.5.1

- Old requests are now cleaned up (retention) right after the App starts, not only once the host has been up for a
  day.

## 0.5.0

- First release: find the voice requests your LLM handled that Home Assistant could answer itself, and fix them with an
  alias, exposure, a custom sentence or a spoken reply.
- AI analysis with any OpenAI-compatible API, or with a voice agent set up in Home Assistant with the Local OpenAI LLM
  integration.
- Sidebar panel through Home Assistant's login, watchdog, REST API and MCP.
