'use strict';

// Settings window: live-reloads settings via POST /settings, manages write-only
// .env credentials, triggers background source indexing, and uses native file
// dialogs so nobody has to hand-edit paths or .env in a terminal.

const $ = (id) => document.getElementById(id);

const TEXT_FIELDS = [
  'USER_NAME', 'USER_ROLE', 'USER_FOCUS',
  'SPEECH_LOCALE', 'CLUELESS_CORRECTIONS',
  'LLM_PROVIDER', 'LLM_BASE_URL', 'LIVE_MODEL', 'SUMMARY_MODEL', 'EMBED_MODEL',
  'GGUF_PATH', 'LLAMACPP_SERVER', 'LIVE_NUM_CTX', 'LIVE_NUM_PREDICT', 'LIVE_TEMPERATURE',
  'LOCAL_NOTES_DIRS', 'ANKI_PATH', 'ANKI_DECK_FILTER', 'PUBMED_QUERY', 'ZOTERO_BIB_PATH',
  'CANVAS_URL', 'NOTION_QUERY',
  'JIRA_SITE', 'JIRA_PROJECT_KEY', 'JIRA_BOARD_ID', 'CONFLUENCE_SPACES',
  'GITHUB_ORG', 'GITHUB_ACCOUNT', 'GITHUB_REPOS',
];
const FLAG_FIELDS = ['CLINICAL_MODE', 'REDACT_PHI', 'AUTO_DETECT_CALLS', 'DEVELOPER_MODE', 'CALENDAR_ENABLED'];
const CRED_KEYS = [
  'ATLASSIAN_EMAIL', 'ATLASSIAN_API_TOKEN', 'GITHUB_TOKEN',
  'CANVAS_TOKEN', 'NOTION_TOKEN', 'NCBI_API_KEY',
];
const SOURCE_LABELS = {
  notes: 'Notes',
  anki: 'Anki',
  pubmed: 'Papers',
  canvas: 'Canvas',
  notion: 'Notion',
  jira: 'Jira',
  confluence: 'Wiki',
  code: 'Code',
};

let selectedProfile = 'student';
let profiles = [
  { name: 'developer', label: 'Developer', tagline: 'Jira, Confluence, code & PRs, local docs, raw logs and model tuning.' },
  { name: 'student', label: 'Student', tagline: 'Lectures, research & med school — slides/PDFs, Anki, PubMed, Canvas, plus Clinical mode.' },
  { name: 'general', label: 'General', tagline: 'Simple controls, plain-language answers, local notes, Notion and calendar.' },
];

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
    if (button.dataset.tab === 'sources') refreshIndexStatus();
  });
}

function syncPersonaVisibility() {
  const isStudent = selectedProfile === 'student';
  const isClinical = isStudent && $('CLINICAL_MODE').checked;
  const showDev = selectedProfile === 'developer' || ($('DEVELOPER_MODE').checked && !isClinical);

  $('clinical-box').hidden = !isStudent;
  $('about-you-section').hidden = isClinical;

  if (isClinical) {
    $('REDACT_PHI').checked = true;
    $('REDACT_PHI').disabled = true;
  } else {
    $('REDACT_PHI').disabled = false;
  }

  for (const el of document.querySelectorAll('[data-dev-only]')) el.hidden = !showDev;
  $('network-academic-fields').hidden = isClinical;
  $('general-sources-section').hidden = isClinical;
  $('developer-sources-section').hidden = isClinical || (!showDev && selectedProfile !== 'developer');
  $('credentials-section').hidden = isClinical;
}

$('CLINICAL_MODE').addEventListener('change', syncPersonaVisibility);
$('DEVELOPER_MODE').addEventListener('change', syncPersonaVisibility);

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
      if (profile.name !== 'student') $('CLINICAL_MODE').checked = false;
      renderProfiles();
      syncPersonaVisibility();
    });
    container.append(card);
  }
}

// ---------------------------------------------------------------- credentials badges

function renderCredentialBadges(presence) {
  for (const key of CRED_KEYS) {
    const badgeEl = document.querySelector(`[data-cred-badge="${key}"]`);
    if (!badgeEl) continue;
    const isSet = Boolean(presence?.[key]);
    badgeEl.textContent = isSet ? 'Saved' : 'Not set';
    badgeEl.className = `badge${isSet ? ' ok' : ''}`;
  }
}

// ---------------------------------------------------------------- index status & pickers

function renderIndexStatus(state) {
  if (!state || state.error && !state.chunks) return;
  const badge = $('sync-badge');
  if (state.running) {
    badge.textContent = state.step || 'Syncing…';
    badge.className = 'badge warn';
  } else if (state.error) {
    badge.textContent = 'Error';
    badge.className = 'badge warn';
  } else {
    badge.textContent = state.network_indexing === false ? 'Offline mode' : 'Ready';
    badge.className = 'badge ok';
  }

  $('sync-now').disabled = Boolean(state.running);
  $('sync-rebuild').disabled = Boolean(state.running);

  if (state.chunks) {
    const pills = Object.entries(SOURCE_LABELS).map(([src, label]) => {
      const count = Number(state.chunks[src] ?? 0);
      const span = document.createElement('span');
      span.className = `stat-pill${count > 0 ? ' populated' : ''}`;
      span.textContent = `${label}: ${count}`;
      return span;
    });
    $('index-counts').replaceChildren(...pills);
  }

  if (state.error) {
    $('sync-detail').textContent = state.error;
  } else if (state.last_synced_at) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - state.last_synced_at) / 60));
    $('sync-detail').textContent = mins === 0 ? 'Last synced just now' : `Last synced ${mins}m ago`;
  } else {
    $('sync-detail').textContent = 'Not synced yet';
  }

  if (state.calendar) {
    const ok = Boolean(state.calendar.authorized);
    $('allow-calendar').textContent = ok ? 'Calendar Allowed' : 'Allow Calendar';
    $('allow-calendar').disabled = ok;
  }
}

async function refreshIndexStatus() {
  const status = await window.settingsApi.indexStatus();
  renderIndexStatus(status);
}

window.settingsApi.onIndexStatus((event) => {
  if (event.result?.totals) {
    renderIndexStatus({
      running: false,
      last_synced_at: event.result.synced_at,
      chunks: event.result.totals,
      network_indexing: !event.result.network_skipped,
    });
  } else {
    renderIndexStatus(event);
  }
});

async function saveAndTriggerIndex(refresh = false) {
  const ok = await save();
  if (!ok) return;
  $('sync-badge').textContent = 'Starting…';
  $('sync-badge').className = 'badge warn';
  await window.settingsApi.runIndex({ refresh });
}

$('sync-now').addEventListener('click', () => saveAndTriggerIndex(false));
$('sync-rebuild').addEventListener('click', () => saveAndTriggerIndex(true));

$('pick-notes-dir').addEventListener('click', async () => {
  const chosen = await window.settingsApi.pickFolder();
  if (!chosen) return;
  const existing = $('LOCAL_NOTES_DIRS').value.split(';').map((s) => s.trim()).filter(Boolean);
  if (!existing.includes(chosen)) existing.push(chosen);
  $('LOCAL_NOTES_DIRS').value = existing.join(';');
});
$('clear-notes-dir').addEventListener('click', () => { $('LOCAL_NOTES_DIRS').value = ''; });

$('anki-auto').addEventListener('click', () => { $('ANKI_PATH').value = 'auto'; });
$('pick-anki').addEventListener('click', async () => {
  const file = await window.settingsApi.pickFile('anki');
  if (file) $('ANKI_PATH').value = file;
});
$('pick-bib').addEventListener('click', async () => {
  const file = await window.settingsApi.pickFile('bib');
  if (file) $('ZOTERO_BIB_PATH').value = file;
});
$('pick-gguf').addEventListener('click', async () => {
  const file = await window.settingsApi.pickFile('gguf');
  if (file) $('GGUF_PATH').value = file;
});

$('allow-calendar').addEventListener('click', async () => {
  $('allow-calendar').disabled = true;
  const res = await window.settingsApi.requestCalendar();
  $('allow-calendar').textContent = res?.authorized ? 'Calendar Allowed' : `Status: ${res?.status ?? 'Check Settings'}`;
  $('allow-calendar').disabled = Boolean(res?.authorized);
});

$('harvest-vocab').addEventListener('click', async () => {
  $('harvest-vocab').disabled = true;
  const res = await window.settingsApi.harvestVocab();
  $('harvest-vocab').disabled = false;
  $('vocab-status').textContent = res?.error
    ? `Harvest failed: ${res.error}`
    : `Harvested ${res.terms_count ?? 0} domain terms (${res.pairs_count ?? 0} phrase repairs active).`;
});

// ---------------------------------------------------------------- providers & model

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

$('test-model').addEventListener('click', async () => {
  $('test-model').disabled = true;
  $('model-test-status').textContent = 'Testing…';
  const res = await window.settingsApi.testModel();
  $('test-model').disabled = false;
  $('model-test-status').textContent = res?.error
    ? `Failed: ${res.error}`
    : `Answered in ${(res.ms / 1000).toFixed(1)}s (${res.model ?? res.provider})`;
});

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
    renderProfiles();
    syncPersonaVisibility();
    setStatus(`Waiting for background API… (${settings.error})`, true);
    return;
  }
  for (const field of TEXT_FIELDS) {
    const element = $(field);
    if (element && settings[field] !== undefined) element.value = settings[field];
  }
  if (!$('LLM_PROVIDER').value) $('LLM_PROVIDER').value = 'auto';
  for (const field of FLAG_FIELDS) {
    const el = $(field);
    if (el) el.checked = truthy(settings[field]);
  }

  if (!profile.error) {
    profiles = profile.available ?? [];
    selectedProfile = profile.name ?? 'general';
    $('CLINICAL_MODE').checked = Boolean(profile.clinical_mode);
    $('REDACT_PHI').checked = Boolean(profile.redact_phi);
  }
  renderProfiles();
  syncProviderFields();
  syncPersonaVisibility();
  renderCredentialBadges(settings.credentials ?? {});
  $('version').textContent = `Clueless ${settings.version ?? ''}`;
  $('data-dir').textContent = settings.data_dir ?? '';
}

async function save() {
  const payload = { PROFILE: selectedProfile };
  for (const field of TEXT_FIELDS) {
    const element = $(field);
    if (element) payload[field] = element.value.trim();
  }
  for (const field of FLAG_FIELDS) {
    const el = $(field);
    if (el) payload[field] = el.checked ? '1' : '0';
  }

  // Collect only non-empty credential inputs so blank fields preserve existing secrets.
  const credPayload = {};
  for (const key of CRED_KEYS) {
    const input = $(`cred-${key}`);
    if (input && input.value.trim()) credPayload[key] = input.value.trim();
  }

  $('save').disabled = true;
  setStatus('Applying…');
  try {
    if (Object.keys(credPayload).length > 0) {
      const credRes = await window.settingsApi.saveCredentials(credPayload);
      if (credRes.error) {
        setStatus(credRes.error, true);
        return false;
      }
      for (const key of CRED_KEYS) {
        const input = $(`cred-${key}`);
        if (input) input.value = '';
      }
      renderCredentialBadges(credRes.credentials ?? {});
    }

    const result = await window.settingsApi.save(payload);
    if (result.error) {
      setStatus(result.error, true);
      return false;
    }
    setStatus('Saved & applied live');
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
