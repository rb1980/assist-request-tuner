# Assist Request Tuner: initial UI/UX proposal

Status: **design for approval; not production implementation**. Prepared by GPT-6 Astra. Approved production work should be handed to GPT-6.1 Sol.

Open `index.html` for the self-contained clickable preview. It uses invented sample records, makes no network requests, and never connects to Home Assistant. Its preview controls expose light/dark mode, install type, and exception states. Reloading resets the demo. Existing application files, including the user's changes to `README.md` and `app/diagnose.py`, are preserved.

## Direction

Keep the existing four-step wizard and make it the organizing structure: **Choose request → Understand → Review → Result**. Use a restrained Home Assistant utility layout: an app header, compact tabs, a single column, sentence-case copy, and one emphasized action per decision. No promotional imagery or oversized dashboard chrome.

The visual system follows Home Assistant's [layout](https://contributors.home-assistant.io/design/foundations/layout/) guidance: 720px content width plus 16px gutters for the wizard and forms. Tables and diagnostic logs may use the available width. [Typography](https://contributors.home-assistant.io/design/foundations/typography/) informs the Roboto-first stack and compact scale; production should bundle fonts locally, while this offline preview falls back to Arial. [Semantic colors](https://contributors.home-assistant.io/design/foundations/color/) organize surfaces, emphasis, and status. The preview uses local equivalents; its dark mode is explicit and independent of HA.

The blue primary action is intentionally darker than the bright brand blue so small white labels retain contrast. Production should check the resolved theme colors, not assume every HA token pairing meets AA. The [accessibility guidance](https://contributors.home-assistant.io/design/foundations/accessibility/) itself identifies some default combinations that fall below text-contrast requirements.

## Screens and interaction

| Screen | Main question | Proposed behavior |
|---|---|---|
| Choose request | Which repeated request should I improve? | Frequency-ordered rows show the exact transcript, frequency, device, and recency. A compact local-coverage summary links to Stats. Review is explicit; Ignore has immediate Undo and a permanent restore location. General questions are separate. |
| Understand | What happened, and what could fix it? | Transcript and observed AI action/reply come first. Explain the missed match in plain words and show known working wording. Distinguish built-in checks from AI drafts. Offer AI only when useful, with the configured model/provider and data disclosure. Keep manual editing and recipes visible secondary routes. |
| Review | What changes, and what has been checked? | Show the concrete change, its scope, example phrases, and checks. Phrasing/example editing and raw payload are collapsed. Editing invalidates readiness until rechecked. The final button says “Apply to Home Assistant”. Preserve explicit device-action consent whenever `changes_devices` is true. |
| Result | Did it work, and how do I recover? | Report recognition results for each example, actual spoken answers where available, and other historical matches covered. Offer another request and Undo. Never equate a matcher result with a command being executed or a measured future latency. |

Diagnosis copy must reflect backend evidence. A likely cause should say “may” rather than presenting an inference as proven. Show unknown or unavailable AI actions as unknown; do not invent an action from its spoken reply. The examples in this preview assume fixture evidence.

The final review is the approval interaction for applying a fix; do not add an unnecessary second generic confirmation. Retain the existing explicit confirmation for undoing an applied HA change. Ignoring a queue item stays a reversible one-click action. These choices use Home Assistant's [confirmation and undo](https://contributors.home-assistant.io/design/patterns/confirmations/) pattern while preserving the application's current consent semantics.

## Keep every existing capability

| Current capability | Destination |
|---|---|
| `fix_start`, `fix_understand`, `fix_review`, `fix_done` | Main four-step flow; preserve current endpoints and checks. |
| Alias, exposure, custom sentence, spoken reply, extra phrasing fixes | Same review shell with type-specific change summary. The preview exercises alias, custom sentence, and spoken reply examples. Exposure and existing-automation edits are specified, not fully mocked. |
| Built-in diagnosis, AI diagnosis, retry, manual draft, recipes | Understand screen, with existing diagnostics available under details. |
| Draft editing, prechecks, apply, regression comparison, rollback, undo | Review and Result, including error outcomes. No safety check is removed. |
| Fixed and ignored request groups | Expandable group below the queue; restore remains discoverable. |
| History | Top-level tab. Open a draft to Review; applied/failed/undone changes open their Result. |
| Coverage, daily counts, per-device counts, ignore windows | Stats tab; clarify home-only percentage versus all-request counts. |
| HA connection, writer access, AI source/model/prices/budget, saved AI setups, collection settings | Visible Settings tab. The App summarizes managed connection settings; sidecar exposes URL/token and local/SFTP/none config access. |
| Settings origins, secret replacement/clearing, connection tests, blueprint status/sync, SSH host key, API/MCP access | Relevant Settings expansions. Preserve field-level source labels, precedence behavior, concealed secrets, and existing maintenance actions. |
| Requests, Candidates, Intents, Changes, Recipes, Custom sentences | Advanced menu, retaining current views, filters, diagnostics, raw payloads, editors, and direct links. |
| External sentence-file edits and import | Custom sentences workspace: explicit diff/import requirement before further file writes. |

Settings uses visible labels and helper text, field errors next to the relevant input, and a form-level summary for connection failures. Test operates on unsaved values; Save remains explicit. Preserve edits on failed tests and warn before discarding actual unsaved edits. This follows [forms and validation](https://contributors.home-assistant.io/design/patterns/forms/) and [button labeling](https://contributors.home-assistant.io/design/ui-text/buttons-and-labels/).

## States that must survive implementation

- Empty collection: explain that normal Assist use supplies requests; link to collection settings. Distinguish this from an all-caught-up queue.
- HA unavailable: retain cached records and drafts, show last successful collection, and block dependent actions. Provide a direct connection-settings route.
- AI disabled, unavailable, budget exhausted, timed out, or no useful fix: explain the specific reason; keep manual and recipe paths usable. The preview includes disabled and failed variants.
- Running diagnosis: a polite live status, not a fake percentage; prevent duplicate submissions and keep the draft associated with its original request.
- Failed precheck, missing writer access, or external file drift: show what needs attention beside the affected action. Never enable Apply merely because the page was revisited.
- Device-controlling fix: describe the affected devices and behavior; require the existing opt-in checkbox. Classification must come from the backend flag, not just the displayed fix type.
- Applying: disable repeat application and show progress until the actual backend result arrives.
- Successful apply: describe verified recognition; do not promise every future utterance is fixed.
- Failed verification with successful restoration: say the previous configuration was restored; offer another fix.
- Restoration failure: say the current state is uncertain or partial. Keep a prominent route to diagnostics and connection recovery. Never say “nothing changed”.
- Historical match loss: show the lost matches explicitly and offer Undo. Avoid silently collapsing this into a successful outcome.

## Ingress, sidecar, and accessibility

Use the same app interior in both deployment modes. Ingress already supplies Home Assistant's outer shell and authentication, so do not duplicate the HA sidebar or add a second login/logout control. Keep every route, asset, form, and fetch relative to the existing ingress root. The sidecar keeps its own login/logout and connection setup. Do not assume an ingress iframe inherits the parent theme, fonts, custom elements, or safe-area behavior; use a supported integration path or local fallbacks. The HTML preview deliberately avoids HA-only components.

At narrow widths, request metadata wraps, actions remain visible, example results stack, and the progress display shows the current step with numbered markers for the others. Preserve 16px gutters and 44px interactive targets. Avoid fixed action bars that can cover content or clash with an iframe/mobile keyboard.

Use native semantic controls; meaningful heading order; keyboard-visible focus; named navigation, fields, and dialogs; textual status in addition to color; and reduced-motion behavior. Move focus to the new screen heading/content on step changes and the first invalid field on errors. A screen reader should hear real asynchronous status changes without the whole page repeatedly announcing itself. The preview demonstrates a skip link, live status, focus movement, native dialogs, and high-contrast light/dark states. Full screen-reader and production-theme verification remains implementation work.

## Review scope and handoff

Approve the information hierarchy, visual direction, four-step flow, Settings placement, and handling of checks/results. Then GPT-6.1 Sol can implement against the existing FastAPI/Jinja endpoints and preserve tests and backend behavior. This is not a proposal to replace the application framework.

The preview is not a full editor, authentication system, persistence layer, or live integration. Advanced views indicate placement; their existing detailed functionality remains required. Browser QA should cover the main flow, mobile layout, dark mode, focus, blocked checks, device consent, and both restoration outcomes. JavaScript syntax and absence of external asset/network calls were checked during design preparation.
