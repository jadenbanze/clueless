'use strict';

// Settings window. Talks to the API only through window.settingsApi (main
// proxies a fixed set of calls), and builds every dynamic node with
// textContent — provider names and log lines are not trusted HTML.

const $ = (id) => document.getElementById(id);

const TEXT_FIELDS = [
  'SPEECH_LOCALE', 'CLUELESS_CORRECTIONS',
  'LLM_PROVIDER', 'LLM_BASE_URL', 'LIVE_MODEL', 'SUMMARY_MODEL', 'EMBED_MODEL',
  'GGUF_PATH', 'LLAMACPP_SERVER', 'LIVE_NUM_CTX', 'LIVE_NUM_PREDICT', 'LIVE_TEMPERATURE',
  'JIRA_SITE', 'JIRA_PROJECT_KEY', 'JIRA_BOARD_ID', 'CONFLUENCE_SPACES',
  'GITHUB_ORG', 'GITHUB_ACCOUNT', 'GITHUB_REPOS',
];
const FLAG_FIELDS = ['REDACT_PHI', 'AUTO_DETECT_CALLS', 'DEVELOPER_MODE'];

let selectedProfile = 'general';
let profiles = [];

function setStatus(text, isError = false) {
  $('status').textContent = text;
  $('status').classList.toggle('error', isError);
}

function truthy(value) {
  return ['1', 'true', 'yes'].includes(String(value ?? '').toLowerCase());
}

// ---------------------------------------------------------------- tabs

for (const button of document.querySelectorAll('nav button')) {
  button.addEventListener('click', () => {
    for (const other of document.querySelectorAll('nav button')) other.classList.toggle('active', other === button);
    for (const panel of document.querySelectorAll('.panel')) {
      panel.classList.toggle('active', panel.id === `panel-${button.dataset.tab}`);
    }
    if (button.dataset.tab === 'logs') loadLog();
    if (button.dataset.tab === 'model') loadProviders();
  });
}

function showDeveloperTabs(show) {
  for (const el of document.querySelectorAll('[data-dev-only]')) el.hidden = !show;
}

// ---------------------------------------------------------------- profile cards

function renderProfiles() {
  const container = $('profiles');
  container.replaceChildren();
  for (const profile of profiles) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `profile-card${profile.name === selectedProfile ? ' selected' : ''}`;
    const title = document.createElement('b');
    title.textContent = profile.label;
    const tagline = document.createElement('small');
    tagline.textContent = profile.tagline;
    card.append(title, tagline);
    card.addEventListener('click', () => {
      selectedProfile = profile.name;
      renderProfiles();
      // Medical implies redaction; preselect it so the default is the safe one.
      if (profile.name === 'medical') $('REDACT_PHI').checked = true;
      showDeveloperTabs(profile.name === 'developer' || $('DEVELOPER_MODE').checked);
    });
    container.append(card);
  }
}

// ---------------------------------------------------------------- providers

function syncProviderFields() {
  $('gguf-fields').hidden = $('LLM_PROVIDER').value !== 'llamacpp';
}
$('LLM_PROVIDER').addEventListener('change', syncProviderFields);

async function loadProviders() {
  const container = $('providers');
  const result = await window.settingsApi.providers();
  container.replaceChildren();
  if (result.error) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = result.error;
    container.append(p);
    return;
  }
  const models = new Set();
  for (const provider of result.providers ?? []) {
    const row = document.createElement('div');
    row.className = 'provider';
    const dot = document.createElement('span');
    dot.className = `dot${provider.running ? ' up' : ''}`;
    const name = document.createElement('span');
    name.textContent = provider.label;
    const detail = document.createElement('small');
    detail.textContent = provider.running
      ? `${provider.models.length} model${provider.models.length === 1 ? '' : 's'} · ${provider.url}`
      : 'not running';
    row.append(dot, name, detail);
    container.append(row);
    for (const model of provider.models ?? []) models.add(model);
  }
  if (result.active) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = `In use: ${result.active.label}${result.active.model ? ` · ${result.active.model}` : ''}`
      + (result.active.message ? ` — ${result.active.message}` : '');
    container.append(p);
  }
  const list = $('model-list');
  list.replaceChildren(...[...models].sort().map((model) => {
    const option = document.createElement('option');
    option.value = model;
    return option;
  }));
}
$('recheck').addEventListener('click', loadProviders);

// ---------------------------------------------------------------- logs

async function loadLog() {
  const result = await window.settingsApi.logs($('log-name').value);
  const pre = $('log-text');
  pre.textContent = result.error ?? (result.text || '(empty)');
  pre.scrollTop = pre.scrollHeight;
}
$('log-name').addEventListener('change', loadLog);
$('log-refresh').addEventListener('click', loadLog);

// ---------------------------------------------------------------- load / save

async function load() {
  const [settings, profile] = await Promise.all([window.settingsApi.get(), window.settingsApi.profile()]);
  if (settings.error) {
    setStatus(`Could not load settings: ${settings.error}`, true);
    return;
  }
  for (const field of TEXT_FIELDS) {
    const element = $(field);
    if (element && settings[field] !== undefined) element.value = settings[field];
  }
  if (!$('LLM_PROVIDER').value) $('LLM_PROVIDER').value = 'auto';
  for (const field of FLAG_FIELDS) $(field).checked = truthy(settings[field]);

  if (!profile.error) {
    profiles = profile.available ?? [];
    selectedProfile = profile.name;
    $('REDACT_PHI').checked = Boolean(profile.redact_phi);
  }
  renderProfiles();
  syncProviderFields();
  showDeveloperTabs(selectedProfile === 'developer' || $('DEVELOPER_MODE').checked);
  $('version').textContent = `Clueless ${settings.version ?? ''}`;
  $('data-dir').textContent = settings.data_dir ?? '';
}

async function save() {
  const payload = { PROFILE: selectedProfile };
  for (const field of TEXT_FIELDS) {
    const element = $(field);
    if (element) payload[field] = element.value.trim();
  }
  for (const field of FLAG_FIELDS) payload[field] = $(field).checked ? '1' : '0';

  $('save').disabled = true;
  setStatus('Saving…');
  try {
    const result = await window.settingsApi.save(payload);
    if (result.error) {
      setStatus(result.error, true);
      return false;
    }
    setStatus('Saved — restart to apply');
    return true;
  } finally {
    $('save').disabled = false;
  }
}

$('save').addEventListener('click', save);
$('restart').addEventListener('click', async () => {
  if (await save()) window.settingsApi.restart();
});
$('cancel').addEventListener('click', () => window.settingsApi.close());
$('walkthrough').addEventListener('click', () => window.settingsApi.runWalkthrough());
load();
