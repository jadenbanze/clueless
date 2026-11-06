'use strict';

// First-run walkthrough. A small step engine: each step may define
//   enter()    runs when the step is shown (probes, fetches)
//   validate() returns true to allow Continue, or a string explaining why not
//   canSkip    whether Skip is offered
// Progress is saved as ONBOARDING_STEP so a closed window resumes in place.
// All dynamic text is set with textContent; nothing here parses HTML.

const api = window.onboardingApi;
const $ = (id) => document.getElementById(id);

const state = {
  profile: 'general',
  profiles: [],
  totalMemGB: 16,
  micOK: false,
  systemOK: false,
  heardSomething: false,
};

const PROFILE_NOTES = {
  developer: 'Adds Jira, Confluence and code as context sources, plus logs and advanced tuning in Settings.',
  medical: 'HIPAA-aware, not HIPAA-certified. Names, dates, record numbers and contact details are redacted on screen, '
    + 'the transcript panel is hidden, and nothing is fetched from the network. Consent and any BAA obligations remain yours.',
  general: 'Transcript and screen context only. Capture starts by itself when a call is detected.',
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
  // Peak is linear 0–1; speech typically sits around 0.05–0.3.
  el.style.width = `${Math.min(100, Math.round(Math.sqrt(Math.max(0, peak ?? 0)) * 140))}%`;
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
      const previous = (await api.state()).profile?.name;
      await api.save({
        PROFILE: state.profile,
        // Explicit so the profile's privacy default applies even if an earlier
        // run saved a different choice.
        REDACT_PHI: state.profile === 'medical' ? '1' : '0',
        AUTO_DETECT_CALLS: state.profile === 'general' ? '1' : '0',
      });
      if (previous !== state.profile) await api.restartApi();
    },
  },
  {
    id: 'system',
    enter: renderSystemCheck,
    validate: () => true,
  },
  {
    id: 'permissions',
    enter: refreshScreenStatus,
    validate: () => (state.micOK && state.systemOK) || 'Both audio permissions are needed to transcribe a meeting. You can skip and grant them later.',
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
    if (step.leave) await step.leave();
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

// ---------------------------------------------------------------- profile

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
      document.documentElement.dataset.profile = profile.name;
      renderProfiles();
    });
    return card;
  }));
  $('profile-note').textContent = PROFILE_NOTES[state.profile] ?? '';
}

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
    note($('sys-note'), 'No system audio was captured. Allow CluelessTap under System Settings → Privacy & Security → Screen & System Audio Recording, check the volume is up, then test again.', true);
    api.openPrivacy('audio');
  }
});

async function refreshScreenStatus() {
  const { status } = await api.screenStatus();
  if (status === 'granted') badge($('screen-badge'), 'Allowed', 'ok');
  else badge($('screen-badge'), 'Not allowed', '');
}
$('screen-btn').addEventListener('click', () => api.openPrivacy('screen'));

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
  // Fallback mirrors server/profiles.py for when the API is still starting.
  state.profiles = initial.profile?.available ?? [
    { name: 'developer', label: 'Developer', tagline: 'Jira, Confluence and code context, raw logs, every model knob.' },
    { name: 'medical', label: 'Medical', tagline: 'Identifiers redacted on screen, zero network access, larger type.' },
    { name: 'general', label: 'General', tagline: 'Simple controls, plain-language answers and high-level recaps.' },
  ];
  state.profile = initial.profile?.name ?? 'general';
  state.totalMemGB = initial.totalMemGB ?? 16;
  document.documentElement.dataset.profile = state.profile;
  // Resume where a closed window left off. A saved step past the welcome page
  // means consent was given in that earlier session.
  const saved = Number.parseInt(initial.settings?.ONBOARDING_STEP ?? '0', 10);
  const resume = Number.isFinite(saved) && saved > 0 && saved < steps.length - 1 ? saved : 0;
  $('consent').checked = resume > 0;
  await show(resume);
})();
