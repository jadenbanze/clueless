'use strict';

const API = 'http://127.0.0.1:8766';
const FIELDS = [
  'JIRA_SITE', 'JIRA_PROJECT_KEY', 'JIRA_BOARD_ID', 'CONFLUENCE_SPACES',
  'GITHUB_ORG', 'GITHUB_ACCOUNT', 'GITHUB_REPOS',
  'SPEECH_LOCALE', 'CLUELESS_CORRECTIONS',
];

const statusEl = document.getElementById('status');
const saveEl = document.getElementById('save');

function setStatus(text) {
  statusEl.textContent = text;
}

async function load() {
  try {
    const response = await fetch(`${API}/settings`);
    const settings = await response.json();

    for (const field of FIELDS) {
      const element = document.getElementById(field);
      if (element && settings[field] !== undefined) element.value = settings[field];
    }
    // DEVELOPER_MODE arrives as a string ("1"/"true"/""), so normalise it.
    const mode = String(settings.DEVELOPER_MODE ?? '').toLowerCase();
    document.getElementById('DEVELOPER_MODE').checked =
      mode === '1' || mode === 'true' || mode === 'yes';

    document.getElementById('version').textContent = `v${settings.version ?? '?'}`;
  } catch (error) {
    setStatus(`Could not load settings: ${error.message}`);
  }
}

async function save() {
  const payload = {};
  for (const field of FIELDS) {
    const element = document.getElementById(field);
    if (element) payload[field] = element.value.trim();
  }
  payload.DEVELOPER_MODE = document.getElementById('DEVELOPER_MODE').checked ? '1' : '';

  saveEl.disabled = true;
  setStatus('Saving…');
  try {
    const response = await fetch(`${API}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    setStatus(result.restart_required ? 'Saved — restart to apply' : 'Saved');
  } catch (error) {
    setStatus(`Save failed: ${error.message}`);
  } finally {
    saveEl.disabled = false;
  }
}

saveEl.addEventListener('click', save);
document.getElementById('cancel').addEventListener('click', () => window.settingsApi.close());
load();
