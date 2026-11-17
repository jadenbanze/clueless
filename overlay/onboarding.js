'use strict';

// First-run setup wizard: consent -> persona (Developer / Student w/ Clinical
// sub-mode / General) + About You -> system check -> permissions -> persona
// context sources -> local model -> live microphone check -> tour.

const api = window.onboardingApi;
const $ = (id) => document.getElementById(id);

const state = {
  profile: 'general',
  clinicalMode: false,
  profiles: [],
  totalMemGB: 16,
  micOK: false,
  systemOK: false,
  heardSomething: false,
};

const PROFILE_NOTES = {
  developer: 'Enables Jira, Confluence, GitHub code & PRs, local docs, logs and model tuning.',
  student: 'Tailored for lectures, seminars, research and med school (slides/PDFs, Anki, PubMed, Canvas). Toggle Clinical mode above for HIPAA-aware clinical rotations.',
  student_clinical: 'HIPAA-aware clinical sub-mode: patient identifiers redacted on screen, live transcript hidden, zero outbound network syncing (local PDFs & offline Anki stay available).',
  general: 'Simple controls, plain-language answers, local notes, Notion and calendar context.',
};

function badge(el, text, kind) {
  el.textContent = text;
  el.className = `badge${kind ? ` ${kind}` : ''}`;
}

function note(el, text, warn = false) {
  el.hidden = !text;
  el.textContent = text ?? '';
  el.classList.toggle('warn', warn);
}

function meter(el, peak) {
  el.style.width = `${Math.min(100, Math.round(Math.sqrt(Math.max(0, peak ?? 0)) * 140))}%`;
}

async function persistWizardSources() {
  const settingsPayload = {
    LOCAL_NOTES_DIRS: $('wiz-notes-dir').value.trim(),
  };
  const credsPayload = {};

  if (state.profile === 'student') {
    settingsPayload.ANKI_PATH = $('wiz-anki').value.trim();
    if (!state.clinicalMode) {
      settingsPayload.PUBMED_QUERY = $('wiz-pubmed').value.trim();
      settingsPayload.CANVAS_URL = $('wiz-canvas-url').value.trim();
      if ($('wiz-canvas-token').value.trim()) credsPayload.CANVAS_TOKEN = $('wiz-canvas-token').value.trim();
    }
  } else if (state.profile === 'developer') {
    settingsPayload.JIRA_SITE = $('wiz-jira-site').value.trim();
    settingsPayload.JIRA_PROJECT_KEY = $('wiz-jira-key').value.trim();
    settingsPayload.CONFLUENCE_SPACES = $('wiz-wiki-spaces').value.trim();
    settingsPayload.GITHUB_ORG = $('wiz-gh-org').value.trim();
    settingsPayload.GITHUB_REPOS = $('wiz-gh-repos').value.trim();
    if ($('wiz-jira-email').value.trim()) credsPayload.ATLASSIAN_EMAIL = $('wiz-jira-email').value.trim();
    if ($('wiz-jira-token').value.trim()) credsPayload.ATLASSIAN_API_TOKEN = $('wiz-jira-token').value.trim();
  } else {
    settingsPayload.NOTION_QUERY = $('wiz-notion-query').value.trim();
    if ($('wiz-notion-token').value.trim()) credsPayload.NOTION_TOKEN = $('wiz-notion-token').value.trim();
  }

  if (Object.keys(credsPayload).length > 0) {
    const credRes = await api.saveCredentials(credsPayload);
    if (credRes?.error) return credRes;
    $('wiz-canvas-token').value = '';
    $('wiz-jira-token').value = '';
    $('wiz-notion-token').value = '';
  }
  return api.save(settingsPayload);
}

// ---------------------------------------------------------------- steps

const steps = [
  {
    id: 'welcome',
    validate: () => $('consent').checked || 'Please confirm the consent notice to continue.',
  },
  {
    id: 'profile',
    enter: renderProfiles,
    validate: () => true,
    async leave() {
      const clinical = state.profile === 'student' && state.clinicalMode;
      await api.save({
        PROFILE: state.profile,
        CLINICAL_MODE: clinical ? '1' : '0',
        REDACT_PHI: clinical ? '1' : '0',
        AUTO_DETECT_CALLS: '1',
        USER_NAME: clinical ? '' : $('wiz-user-name').value.trim(),
        USER_ROLE: clinical ? '' : $('wiz-user-role').value.trim(),
      });
    },
  },
  {
    id: 'system',
    enter: renderSystemCheck,
    validate: () => true,
  },
  {
    id: 'permissions',
    enter: refreshOptionalPermissions,
    validate: () => (state.micOK && state.systemOK) || 'Both audio permissions are needed to transcribe a session. You can skip and grant them later.',
    canSkip: true,
  },
  {
    id: 'sources',
    enter: renderSourcesStep,
    validate: () => true,
    async leave() {
      const res = await persistWizardSources();
      if (res?.error) flash(res.error);
    },
    canSkip: true,
  },
  {
    id: 'model',
    enter: renderModels,
    validate: () => true,
    canSkip: true,
  },
  {
    id: 'test',
    async enter() {
      const result = await api.startCapture();
      if (result?.error) $('test-note').textContent = 'Capture could not start. Check permissions, then try again from the tray.';
    },
    validate: () => true,
    canSkip: true,
  },
  {
    id: 'done',
    validate: () => true,
  },
];

let current = 0;

function renderPips() {
  const pips = $('pips');
  pips.replaceChildren(...steps.map((_, index) => {
    const pip = document.createElement('span');
    pip.className = `pip${index < current ? ' done' : ''}${index === current ? ' current' : ''}`;
    return pip;
  }));
}

async function show(index) {
  current = Math.max(0, Math.min(steps.length - 1, index));
  const step = steps[current];
  for (const section of document.querySelectorAll('.step')) {
    section.classList.toggle('active', section.dataset.step === step.id);
  }
  renderPips();
  $('back').hidden = current === 0;
  $('skip').hidden = !step.canSkip;
  $('next').textContent = step.id === 'done' ? 'Show me around' : step.id === 'welcome' ? 'Get started' : 'Continue';
  $('skip').textContent = step.id === 'done' ? 'Finish' : 'Skip';
  if (step.id === 'done') $('skip').hidden = false;
  api.save({ ONBOARDING_STEP: String(current) });
  if (step.enter) await step.enter();
}

async function advance({ skip = false } = {}) {
  const step = steps[current];
  if (step.id === 'done') {
    $('next').disabled = true;
    await api.finish({ tour: !skip });
    return;
  }
  if (!skip) {
    const verdict = step.validate();
    if (verdict !== true) {
      flash(verdict);
      return;
    }
  }
  $('next').disabled = true;
  try {
    if (!skip && step.leave) await step.leave();
  } finally {
    $('next').disabled = false;
  }
  await show(current + 1);
}

function flash(message) {
  const step = document.querySelector('.step.active');
  let el = step.querySelector('.flash');
  if (!el) {
    el = document.createElement('p');
    el.className = 'note warn flash';
    step.append(el);
  }
  el.textContent = message;
}

$('next').addEventListener('click', () => advance());
$('skip').addEventListener('click', () => advance({ skip: true }));
$('back').addEventListener('click', () => show(current - 1));

// ---------------------------------------------------------------- profile & clinical sub-mode

function syncProfileNote() {
  const isStudent = state.profile === 'student';
  const clinical = isStudent && state.clinicalMode;
  $('wiz-clinical-card').hidden = !isStudent;
  $('wiz-about-card').hidden = clinical;
  document.documentElement.dataset.profile = clinical ? 'medical' : state.profile;
  document.documentElement.dataset.clinical = clinical ? 'true' : 'false';
  $('profile-note').textContent = clinical
    ? PROFILE_NOTES.student_clinical
    : (PROFILE_NOTES[state.profile] ?? '');
}

function renderProfiles() {
  const container = $('profiles');
  container.replaceChildren(...state.profiles.map((profile) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `choice${profile.name === state.profile ? ' selected' : ''}`;
    const title = document.createElement('b');
    title.textContent = profile.label;
    const tagline = document.createElement('small');
    tagline.textContent = profile.tagline;
    card.append(title, tagline);
    card.addEventListener('click', () => {
      state.profile = profile.name;
      if (profile.name !== 'student') {
        state.clinicalMode = false;
        $('wiz-clinical').checked = false;
      }
      renderProfiles();
    });
    return card;
  }));
  syncProfileNote();
}

$('wiz-clinical').addEventListener('change', () => {
  state.clinicalMode = $('wiz-clinical').checked;
  syncProfileNote();
});

// ---------------------------------------------------------------- sources step

function renderSourcesStep() {
  const isStudent = state.profile === 'student';
  const isClinical = isStudent && state.clinicalMode;
  const isDev = state.profile === 'developer';
  const isGen = state.profile === 'general';

  $('wiz-student-sources').hidden = !isStudent;
  $('wiz-pubmed-wrap').hidden = isClinical;
  $('wiz-canvas-wrap').hidden = isClinical;
  $('wiz-dev-sources').hidden = !isDev;
  $('wiz-general-sources').hidden = !isGen;

  $('wiz-sources-lead').textContent = isClinical
    ? 'Clinical mode is offline-only: point Clueless at local clinical guidelines, PDFs or Anki decks on this Mac.'
    : 'Connect local folders, study decks or workspace tools so answers cite your own material.';
}

$('wiz-pick-notes').addEventListener('click', async () => {
  const folder = await api.pickFolder();
  if (folder) $('wiz-notes-dir').value = folder;
});
$('wiz-anki-auto').addEventListener('click', () => { $('wiz-anki').value = 'auto'; });
$('wiz-pick-anki').addEventListener('click', async () => {
  const file = await api.pickFile('anki');
  if (file) $('wiz-anki').value = file;
});

$('wiz-sync-btn').addEventListener('click', async () => {
  $('wiz-sync-btn').disabled = true;
  $('wiz-sync-status').textContent = 'Saving & starting sync…';
  const saved = await persistWizardSources();
  if (saved?.error) {
    $('wiz-sync-status').textContent = `Check fields: ${saved.error}`;
    $('wiz-sync-btn').disabled = false;
    return;
  }
  await api.runIndex();
  $('wiz-sync-status').textContent = 'Syncing in background — you can continue.';
  $('wiz-sync-btn').disabled = false;
});

api.onIndexStatus((ev) => {
  if (ev.running) $('wiz-sync-status').textContent = ev.step || 'Syncing…';
  else if (ev.error) $('wiz-sync-status').textContent = `Sync note: ${ev.error}`;
  else if (ev.result) $('wiz-sync-status').textContent = 'Context synced and ready.';
});

// ---------------------------------------------------------------- system check

function checkRow(title, detail, kind, label) {
  const card = document.createElement('div');
  card.className = 'card';
  const row = document.createElement('div');
  row.className = 'row';
  const text = document.createElement('div');
  text.className = 'grow';
  const b = document.createElement('b');
  b.textContent = title;
  text.append(b);
  if (detail) {
    const p = document.createElement('p');
    p.textContent = detail;
    text.append(p);
  }
  const tag = document.createElement('span');
  badge(tag, label, kind);
  row.append(text, tag);
  card.append(row);
  return card;
}

async function renderSystemCheck() {
  const container = $('checks');
  container.replaceChildren(checkRow('Checking…', '', '', '…'));
  const verdict = await api.preflight();
  if (verdict?.error) {
    container.replaceChildren(checkRow('System check unavailable', verdict.error, 'warn', 'Unknown'));
    return;
  }
  container.replaceChildren(
    ...verdict.blocking.map((item) => checkRow(item.title, item.detail, 'bad', 'Required')),
    ...verdict.warnings.map((item) => checkRow(item.title, item.detail, 'warn', 'Heads up')),
    ...verdict.passed.map((item) => checkRow(item.title, '', 'ok', 'OK')),
  );
}
$('recheck-system').addEventListener('click', renderSystemCheck);

// ---------------------------------------------------------------- permissions

$('mic-btn').addEventListener('click', async () => {
  $('mic-btn').disabled = true;
  badge($('mic-badge'), 'Listening…', '');
  note($('mic-note'), 'Say something so the level moves.');
  const result = await api.microphone();
  $('mic-btn').disabled = false;
  if (!result.granted) {
    badge($('mic-badge'), 'Denied', 'bad');
    note($('mic-note'), 'Turn on Clueless in System Settings → Privacy & Security → Microphone, then try again.', true);
    api.openPrivacy('microphone');
    return;
  }
  const peak = result.level?.peak ?? 0;
  meter($('mic-meter'), peak);
  state.micOK = peak > 0;
  if (state.micOK) {
    badge($('mic-badge'), 'Working', 'ok');
    note($('mic-note'), '');
    $('mic-btn').textContent = 'Test again';
  } else {
    badge($('mic-badge'), 'No signal', 'warn');
    note($('mic-note'), 'Permission is granted but no sound arrived. Check the input device in System Settings → Sound.', true);
  }
});

$('sys-btn').addEventListener('click', async () => {
  $('sys-btn').disabled = true;
  badge($('sys-badge'), 'Checking…', '');
  note($('sys-note'), 'If macOS asks to let “CluelessTap” record system audio, choose Allow.');
  const result = await api.systemAudio();
  $('sys-btn').disabled = false;
  const peak = result.level?.peak ?? 0;
  meter($('sys-meter'), peak);
  state.systemOK = peak > 0;
  if (state.systemOK) {
    badge($('sys-badge'), 'Working', 'ok');
    note($('sys-note'), '');
    $('sys-btn').textContent = 'Test again';
  } else {
    badge($('sys-badge'), 'Not allowed', 'bad');
    note($('sys-note'), 'No system audio was captured. Allow CluelessTap under System Settings → Privacy & Security → Screen & System Audio Recording, then test again.', true);
    api.openPrivacy('audio');
  }
});

async function refreshOptionalPermissions() {
  const isClinical = state.profile === 'student' && state.clinicalMode;
  $('wiz-cal-perm-card').hidden = isClinical;

  const { status } = await api.screenStatus();
  if (status === 'granted') badge($('screen-badge'), 'Allowed', 'ok');
  else badge($('screen-badge'), 'Optional', '');

  if (!isClinical) {
    const cal = await api.calendarStatus(false);
    if (cal?.authorized) {
      badge($('cal-badge'), 'Allowed', 'ok');
      $('cal-btn').disabled = true;
    }
  }
}
$('screen-btn').addEventListener('click', () => api.openPrivacy('screen'));
$('cal-btn').addEventListener('click', async () => {
  $('cal-btn').disabled = true;
  const res = await api.calendarStatus(true);
  if (res?.authorized) {
    badge($('cal-badge'), 'Allowed', 'ok');
  } else {
    badge($('cal-badge'), res?.status ?? 'Check Settings', '');
    $('cal-btn').disabled = false;
  }
});

// ---------------------------------------------------------------- model

function recommendedModel() {
  return state.totalMemGB < 16 ? 'llama3.2' : 'llama3.1:8b';
}

async function renderModels() {
  const list = $('model-list');
  list.replaceChildren(checkRow('Looking for local models…', '', '', '…'));
  const result = await api.providers();
  if (result.error) {
    $('model-missing').hidden = true;
    $('test-model').disabled = true;
    list.replaceChildren(checkRow(
      "Couldn't check for models yet",
      'Clueless is still starting its background service. Press “Check again” in a moment.',
      'warn', 'Waiting',
    ));
    return;
  }
  const running = (result.providers ?? []).filter((provider) => provider.running);
  $('model-missing').hidden = running.length > 0;
  $('test-model').disabled = running.length === 0;
  const rec = recommendedModel();
  $('cmd-pull').textContent = `ollama pull ${rec}`;

  const rows = [];
  for (const provider of running) {
    const models = provider.chat_models ?? provider.models ?? [];
    const card = document.createElement('div');
    card.className = 'card';
    const title = document.createElement('b');
    title.textContent = provider.label;
    card.append(title);
    if (models.length === 0) {
      const p = document.createElement('p');
      p.textContent = provider.id === 'foundation' ? 'Built in — no download needed.' : 'Running, but no chat models are installed.';
      card.append(p);
    }
    for (const model of models) {
      const label = document.createElement('label');
      label.className = 'consent';
      label.style.marginTop = '8px';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'model';
      radio.checked = result.active?.provider === provider.id && result.active?.model === model;
      radio.addEventListener('change', () => api.save({
        LLM_PROVIDER: provider.kind, LLM_BASE_URL: provider.url, LIVE_MODEL: model,
      }));
      const text = document.createElement('span');
      text.textContent = model;
      if (model.split(':')[0] === rec.split(':')[0]) {
        const tag = document.createElement('span');
        badge(tag, 'Recommended', 'ok');
        tag.style.marginLeft = '8px';
        text.append(tag);
      }
      label.append(radio, text);
      card.append(label);
    }
    rows.push(card);
  }
  list.replaceChildren(...rows);
  if (result.active?.message) $('model-result').textContent = result.active.message;
}

$('recheck-model').addEventListener('click', renderModels);
$('test-model').addEventListener('click', async () => {
  $('test-model').disabled = true;
  $('model-result').textContent = 'Asking the model…';
  const result = await api.testModel();
  $('test-model').disabled = false;
  $('model-result').textContent = result.error
    ? `Failed: ${result.error}`
    : `Answered in ${(result.ms / 1000).toFixed(1)}s with ${result.model ?? result.provider}.`;
});

for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', () => {
    api.copy($(button.dataset.copy).textContent);
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = 'Copy'; }, 1500);
  });
}

// ---------------------------------------------------------------- test session

api.onTurn((turn) => {
  $('turns-waiting').hidden = true;
  state.heardSomething = true;
  const row = document.createElement('div');
  row.className = `turn ${turn.speaker === 'me' ? 'me' : 'them'}`;
  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = turn.speaker === 'me' ? 'You' : 'Them';
  const text = document.createElement('span');
  text.textContent = turn.text;
  row.append(who, text);
  $('turns').append(row);
  $('turns').scrollTop = $('turns').scrollHeight;
  $('test-note').textContent = 'That is the transcript. Continue when you are ready.';
});

// ---------------------------------------------------------------- boot

(async () => {
  const initial = await api.state();
  state.profiles = initial.profile?.available ?? [
    { name: 'developer', label: 'Developer', tagline: 'Jira, Confluence, code & PRs, local docs, raw logs and model tuning.' },
    { name: 'student', label: 'Student', tagline: 'Lectures, research & med school — slides/PDFs, Anki, PubMed, Canvas, plus Clinical mode.' },
    { name: 'general', label: 'General', tagline: 'Simple controls, plain-language answers, local notes, Notion and calendar.' },
  ];
  state.profile = initial.profile?.name ?? 'general';
  state.clinicalMode = Boolean(initial.profile?.clinical_mode);
  state.totalMemGB = initial.totalMemGB ?? 16;

  $('wiz-clinical').checked = state.clinicalMode;
  if (initial.settings) {
    $('wiz-user-name').value = initial.settings.USER_NAME ?? '';
    $('wiz-user-role').value = initial.settings.USER_ROLE ?? '';
    $('wiz-notes-dir').value = initial.settings.LOCAL_NOTES_DIRS ?? '';
    $('wiz-anki').value = initial.settings.ANKI_PATH ?? '';
    $('wiz-pubmed').value = initial.settings.PUBMED_QUERY ?? '';
    $('wiz-canvas-url').value = initial.settings.CANVAS_URL ?? '';
    $('wiz-jira-site').value = initial.settings.JIRA_SITE ?? '';
    $('wiz-jira-key').value = initial.settings.JIRA_PROJECT_KEY ?? '';
    $('wiz-wiki-spaces').value = initial.settings.CONFLUENCE_SPACES ?? '';
    $('wiz-gh-org').value = initial.settings.GITHUB_ORG ?? '';
    $('wiz-gh-repos').value = initial.settings.GITHUB_REPOS ?? '';
    $('wiz-notion-query').value = initial.settings.NOTION_QUERY ?? '';
  }

  syncProfileNote();
  const saved = Number.parseInt(initial.settings?.ONBOARDING_STEP ?? '0', 10);
  const resume = Number.isFinite(saved) && saved > 0 && saved < steps.length - 1 ? saved : 0;
  $('consent').checked = resume > 0;
  await show(resume);
})();
