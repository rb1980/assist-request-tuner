// Path prefix when served through Home Assistant ingress ('' otherwise).
const ROOT = document.documentElement.dataset.root || '';
document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
  if (!confirm(form.dataset.confirm)) event.preventDefault();
}));
// Fix list: Ignore in place, without reloading (which would jump back to the top). The card stays where it is,
// dimmed, with Undo; it is gone the next time the page loads. If anything fails, the form posts normally.
function cardsFor(pattern) {
  return [...document.querySelectorAll('.choice[data-pattern]')].filter(card => card.dataset.pattern === pattern);
}
async function postPattern(path, pattern) {
  const body = new FormData();
  body.append('pattern', pattern);
  const response = await fetch(`${ROOT}${path}`, {method: 'POST', body, headers: {'Accept': 'application/json'}});
  if (!response.ok) throw new Error(response.status);
}
function markIgnored(pattern, ignored) {
  for (const card of cardsFor(pattern)) {
    card.classList.toggle('ignored', ignored);
    card.querySelectorAll('.actions').forEach(el => { el.hidden = ignored; });
    card.querySelector('.ignored-note')?.remove();
    if (!ignored) continue;
    const note = document.createElement('span');
    note.className = 'ignored-note';
    note.setAttribute('role', 'status');
    note.textContent = 'Ignored';
    const undo = document.createElement('button');
    undo.type = 'button';
    undo.textContent = 'Undo';
    undo.addEventListener('click', async () => {
      undo.disabled = true;
      try { await postPattern('/fix/unhide', pattern); markIgnored(pattern, false); card.querySelector('form.ignore button')?.focus({preventScroll: true}); announce('Request restored.'); }
      catch (_) { undo.disabled = false; undo.textContent = 'Undo failed, try again'; }
    });
    note.append(' · ', undo);
    card.append(note);
    undo.focus({preventScroll: true});
  }
}
document.querySelectorAll('.choice[data-pattern] form.ignore').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  const pattern = form.querySelector('input[name="pattern"]').value;
  const button = form.querySelector('button');
  button.disabled = true;
  try { await postPattern('/fix/hide', pattern); markIgnored(pattern, true); announce('Request ignored. Undo is available on this row.'); }
  catch (_) { form.submit(); }
  finally { button.disabled = false; }
}));
document.querySelectorAll('form.matcher').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  const output = form.querySelector('.result');
  output.textContent = 'Matching…';
  try {
    const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
    output.textContent = JSON.stringify(await response.json(), null, 2);
  } catch (_) { output.textContent = 'Matcher unavailable. Please try again.'; }
}));
document.querySelectorAll('[data-diagnosis-id]').forEach(panel => {
  if (!['queued', 'running'].includes(panel.dataset.status)) return;
  async function poll() {
    try {
      const response = await fetch(`${ROOT}/ui/diagnoses/${panel.dataset.diagnosisId}`);
      if (response.ok) {
        const job = await response.json();
        if (!['queued', 'running'].includes(job.status)) {
          window.location.reload();
          return;
        }
      }
    } catch (_) { /* Retry transient connectivity failures. */ }
    window.setTimeout(poll, 1500);
  }
  window.setTimeout(poll, 1500);
});

for (const button of document.querySelectorAll('.preview-reply')) {
  button.addEventListener('click', async () => {
    const form = button.closest('form');
    const output = form.querySelector('.reply-preview');
    try {
      const response = await fetch(ROOT + '/ui/blueprints/preview_reply', {method: 'POST', body: new FormData(form)});
      const data = await response.json();
      output.textContent = response.ok ? data.reply_preview : JSON.stringify(data);
    } catch (error) { output.textContent = 'Reply preview failed'; }
  });
}

// Apply takes a few seconds: say so and prevent double submits.
for (const form of document.querySelectorAll('form.apply')) {
  form.addEventListener('submit', event => {
    const button = form.querySelector('button.primary');
    if (button.disabled) { event.preventDefault(); return; }
    button.disabled = true;
    button.textContent = 'Applying and testing…';
    form.setAttribute('aria-busy', 'true');
    const output = form.querySelector('[data-apply-status]');
    if (output) output.textContent = 'Applying the change and checking your examples. Wait for the actual result.';
  });
}

// Close the Advanced menu when clicking elsewhere.
document.addEventListener('click', event => {
  for (const menu of document.querySelectorAll('details.advanced[open]')) {
    if (!menu.contains(event.target)) menu.open = false;
  }
});
// Settings page: test one section with the values currently typed in the form. The AI test also lists the API's
// models (filling the Model field's choices) and the chosen model's prices, when the API publishes them.
async function runTest(button) {
  const output = button.parentElement.querySelector('.test-result');
  output.className = 'test-result';
  output.textContent = 'Testing…';
  button.disabled = true;
  try {
    const response = await fetch(`${ROOT}/ui/settings/test/${button.dataset.test}`, {method: 'POST', body: new FormData(button.form)});
    const result = await response.json();
    if (result.ok) clearSettingErrors(button.form);
    else showSettingErrors(result.message || result.detail || 'Test failed', button.form);
    const summary = document.querySelector('[data-settings-summary]');
    if (summary) summary.textContent = result.ok ? '' : 'Settings were not saved. ' + (result.message || result.detail || 'Connection test failed.');
    output.className = 'test-result ' + (result.ok ? 'ok' : 'bad');
    output.textContent = (result.ok ? '✓ ' : '✗ ') + (result.message || result.detail || 'Test failed');
    const list = document.getElementById('model-list');
    if (list && Array.isArray(result.models)) {
      list.replaceChildren(...result.models.map(m => {
        const option = document.createElement('option');
        option.value = m.id;
        if (m.price_in != null) option.label = `${m.id} · $${m.price_in} in / $${m.price_out} out per million tokens`;
        return option;
      }));
    }
    for (const [field, value] of [['DIAGNOSE_PRICE_IN', result.price_in], ['DIAGNOSE_PRICE_OUT', result.price_out]]) {
      const input = button.form.elements[field];
      if (input && value != null && input.value !== String(value)) {
        input.value = value;
        input.classList.add('changed');   // filled in from the API: highlighted until saved
      }
    }
    button.form.dispatchEvent(new Event('input'));
  } catch (_) {
    output.className = 'test-result bad';
    output.textContent = '✗ The test could not run. Your edits are still here. Please try again.';
    const summary = document.querySelector('[data-settings-summary]');
    if (summary) summary.textContent = 'The test could not run. Your edits have been kept; check the connection and try again.';
  } finally { button.disabled = false; }
}
document.querySelectorAll('button[data-test]').forEach(button => button.addEventListener('click', () => runTest(button)));
// Choosing a model from the list checks it straight away.
const modelInput = document.getElementById('DIAGNOSE_MODEL');
if (modelInput) modelInput.addEventListener('change', () => {
  const listed = [...document.querySelectorAll('#model-list option')].some(o => o.value === modelInput.value);
  const button = modelInput.form.querySelector('button[data-test="ai"]');
  if (listed && button) runTest(button);
});
// Settings page: the AI server choice shows only the fields that apply (Custom: URL and key; Home Assistant: which
// agent). With Home Assistant, the model, reasoning, prices and budget come from HA: shown greyed out with HA's values
// (disabled fields aren't sent, so saving keeps the Custom values for switching back).
const aiSource = document.getElementById('AI_SOURCE');
const haAgent = document.getElementById('AI_HA_SERVER');
function showAiFields() {
  const ha = aiSource.value === 'ha';
  for (const row of document.querySelectorAll('[data-when]')) row.hidden = row.dataset.when !== aiSource.value;
  for (const row of document.querySelectorAll('[data-from-ha]')) {
    const input = row.querySelector('input, select');
    if (!input) continue;
    if (!('custom' in input.dataset) || !input.disabled) input.dataset.custom = input.value;
    input.value = ha ? (row.dataset.fromHa || haAgent?.selectedOptions[0]?.dataset.model || '') : input.dataset.custom;
    input.disabled = ha;
    row.classList.toggle('from-ha', ha);
  }
  document.querySelectorAll('button[data-test="ai"] ~ .test-result').forEach(out => { out.textContent = ''; });
}
if (aiSource) {
  aiSource.addEventListener('change', showAiFields);
  haAgent?.addEventListener('change', showAiFields);
  showAiFields();
}


function announce(message) {
  const live = document.getElementById('live-status');
  if (live) live.textContent = message;
}

// Theme stays local and uses system preference until explicitly changed.
const themeSelect = document.getElementById('theme');
if (themeSelect) {
  let theme = 'system';
  try { theme = localStorage.getItem('request-tuner-theme') || 'system'; } catch (_) { /* Storage can be disabled in an ingress iframe. */ }
  if (!['system', 'light', 'dark'].includes(theme)) theme = 'system';
  themeSelect.value = theme;
  function setTheme(value) {
    if (value === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = value;
  }
  setTheme(theme);
  themeSelect.addEventListener('change', () => {
    setTheme(themeSelect.value);
    try { localStorage.setItem('request-tuner-theme', themeSelect.value); } catch (_) { /* System fallback remains usable. */ }
  });
}

// Form edits invalidate the readiness of the saved draft. Rechecking is the existing server-side edit action.
for (const editor of document.querySelectorAll('[data-review-edit]')) {
  const initial = new URLSearchParams(new FormData(editor)).toString();
  const apply = document.querySelector('form.apply button.primary');
  const consent = document.querySelector('form.apply input[name="allow_device_actions"]');
  function ready() {
    const changed = new URLSearchParams(new FormData(editor)).toString() !== initial;
    apply.disabled = changed || apply.dataset.blocked === 'true' || Boolean(consent && !consent.checked);
    editor.querySelector('[data-edit-status]').textContent = changed ? 'Your edits need to be saved and rechecked before applying.' : '';
  }
  editor.addEventListener('input', ready);
  consent?.addEventListener('change', ready);
  ready();
}
// Device consent remains required by both the native form and the backend.
for (const consent of document.querySelectorAll('form.apply input[name="allow_device_actions"]')) {
  if (document.querySelector('[data-review-edit]')) continue;
  const button = consent.form.querySelector('button.primary');
  const update = () => { button.disabled = button.dataset.blocked === 'true' || !consent.checked; };
  consent.addEventListener('change', update);
  update();
}

for (const form of document.querySelectorAll('form[data-busy]')) {
  form.addEventListener('submit', event => {
    if (form.dataset.submitted) { event.preventDefault(); return; }
    form.dataset.submitted = 'true';
    form.setAttribute('aria-busy', 'true');
    const button = form.querySelector('button');
    button.disabled = true;
    button.textContent = form.dataset.busy;
    announce(form.dataset.busy);
  });
}

function clearSettingErrors(form) {
  form.querySelectorAll('[aria-invalid]').forEach(input => { input.removeAttribute('aria-invalid'); const help = document.getElementById('help-' + input.name); if (help) input.setAttribute('aria-describedby', help.id); else input.removeAttribute('aria-describedby'); });
  form.querySelectorAll('.setting-error').forEach(error => error.remove());
}
function showSettingErrors(message, form) {
  clearSettingErrors(form);
  let first;
  for (const row of form.querySelectorAll('.setting')) {
    const label = row.querySelector('label')?.textContent.trim();
    if (!label || !message.includes(label + ':')) continue;
    const input = row.querySelector('input, select');
    if (!input || input.disabled || row.hidden) continue;
    input.setAttribute('aria-invalid', 'true');
    const error = document.createElement('p');
    error.className = 'setting-error';
    error.id = 'error-' + input.name;
    error.textContent = message.slice(message.indexOf(label + ':')).split(';')[0];
    input.setAttribute('aria-describedby', [document.getElementById('help-' + input.name)?.id, error.id].filter(Boolean).join(' '));
    row.append(error);
    row.closest('details')?.setAttribute('open', '');
    first ||= input;
  }
  first?.focus();
}

const settingsForm = document.querySelector('form.settings');
if (settingsForm) {
  function fingerprint() {
    return JSON.stringify([...settingsForm.elements].filter(el => el.name && !['PRESET', 'PRESET_NAME'].includes(el.name)).map(el => [el.name, el.type === 'checkbox' ? el.checked : el.value]));
  }
  const original = fingerprint();
  let submitting = false;
  const dirty = () => fingerprint() !== original;
  settingsForm.addEventListener('input', () => {
    document.querySelector('[data-settings-dirty]').textContent = dirty() ? 'You have unsaved changes.' : 'Changes apply without a restart.';
  });
  settingsForm.addEventListener('change', () => settingsForm.dispatchEvent(new Event('input')));
  window.addEventListener('beforeunload', event => {
    if (!submitting && dirty()) { event.preventDefault(); event.returnValue = ''; }
  });
  settingsForm.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    const action = event.submitter?.getAttribute('formaction') || '';
    if (action && !action.endsWith('/save') && dirty() && !confirm('Discard your unsaved settings and continue?')) {
      event.preventDefault();
      return;
    }
    submitting = true;
  });
  document.querySelectorAll('form:not(.settings)').forEach(form => form.addEventListener('submit', event => {
    if (dirty() && !confirm('Discard your unsaved settings and continue?')) event.preventDefault();
    else submitting = true;
  }));
  const errors = document.querySelector('[data-validation-errors]');
  if (errors) { showSettingErrors(errors.textContent, settingsForm); if (!settingsForm.querySelector('[aria-invalid]')) errors.focus(); }
}
function openHashSection() {
  if (!location.hash) return;
  const target = document.getElementById(location.hash.slice(1));
  if (target?.tagName === 'DETAILS') target.open = true;
}
openHashSection();
window.addEventListener('hashchange', openHashSection);
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  for (const menu of document.querySelectorAll('details.advanced[open]')) { menu.open = false; menu.querySelector('summary').focus(); }
});
// Full-page wizard navigation places keyboard focus on the new screen heading.
const heading = document.querySelector('.steps') && document.querySelector('main h1');
if (heading && !location.hash) { heading.tabIndex = -1; heading.focus({preventScroll: true}); }
// A back/forward-cache restore must not leave a completed submit button stuck disabled.
window.addEventListener('pageshow', event => { if (event.persisted && document.querySelector('form[aria-busy="true"]')) location.reload(); });
