'use strict';

// Overlay renderer:
//   pill — quiet status bar (armed vs listening, timer, pause, settings, expand)
//   card — streaming answers/summaries, clickable citations, Q&A history, ask bar

const $ = (id) => document.getElementById(id);

const rootEl = $('root');
const surfaceEl = $('surface');
const scrollEl = $('scroll');
const dotEl = $('dot');
const statusEl = $('status');
const toastEl = $('toast');
const sourcesEl = $('sources');
const answerEl = $('answer');
const questionEl = $('question');
const answerTextEl = $('answer-text');
const citationsEl = $('citations');
const historyNavEl = $('history-nav');
const histPrevBtn = $('hist-prev');
const histNextBtn = $('hist-next');
const histPosEl = $('hist-pos');
const askForm = $('ask');
const askInput = $('ask-input');
const transcriptEl = $('transcript');
const turnsEl = $('turns');

const MAX_VISIBLE_TURNS = 60;
const MAX_ANSWER_HISTORY = 12;
const DEFAULT_LABELS = {
  jira: 'Jira',
  confluence: 'Wiki',
  code: 'Code',
  notes: 'Notes',
  anki: 'Anki',
  pubmed: 'Papers',
  canvas: 'Canvas',
  notion: 'Notion',
  calendar: 'Cal',
  screen: 'Screen',
};

let mode = 'pill';
let maxWindowHeight = 600;
let capturing = false;
let armed = false;
let paused = false;
let speaking = false;
let activeSpeaker = 'them';
let sessionStart = null;
let askFocused = false;
let answerHistory = [];
let historyIndex = -1;

// ---------------------------------------------------------------- mode & rAF resize

function applyMode(next) {
  mode = next;
  rootEl.dataset.mode = next;
  fit();
}

window.copilot.onMode((payload) => applyMode(payload.mode));
window.copilot.onConfig((cfg) => {
  maxWindowHeight = cfg.maxHeight ?? maxWindowHeight;
  fit();
});

$('expand').addEventListener('click', () => window.copilot.setMode('card'));
$('collapse').addEventListener('click', () => {
  releaseAsk();
  window.copilot.setMode('pill');
});
$('hide').addEventListener('click', () => {
  releaseAsk();
  window.copilot.hide();
});
$('settings-btn').addEventListener('click', () => {
  releaseAsk();
  window.copilot.openSettings();
});

function fit() {
  if (mode !== 'card') {
    scrollEl.style.maxHeight = '';
    return;
  }
  const chrome = surfaceEl.offsetHeight - scrollEl.offsetHeight;
  const room = Math.max(120, maxWindowHeight - 12 - chrome);
  scrollEl.style.maxHeight = `${room}px`;
}

let resizeRaf = 0;
let lastReportedHeight = 0;
new ResizeObserver(() => {
  if (mode !== 'card' || resizeRaf) return;
  resizeRaf = requestAnimationFrame(() => {
    resizeRaf = 0;
    if (mode !== 'card') return;
    const nextHeight = Math.ceil(surfaceEl.getBoundingClientRect().height) + 12;
    if (Math.abs(nextHeight - lastReportedHeight) >= 2) {
      lastReportedHeight = nextHeight;
      window.copilot.reportHeight(nextHeight);
    }
  });
}).observe(surfaceEl);

// ---------------------------------------------------------------- status

function renderStatus() {
  dotEl.classList.toggle('live', capturing && !paused);
  dotEl.classList.toggle('armed', !capturing && armed && !paused);
  dotEl.classList.toggle('paused', paused);
  dotEl.classList.toggle('speaking', speaking && capturing && !paused);

  if (paused) {
    statusEl.textContent = 'Paused';
  } else if (capturing && sessionStart) {
    const seconds = Math.floor((Date.now() - sessionStart) / 1000);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = String(seconds % 60).padStart(2, '0');
    const timer = h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    statusEl.textContent = speaking ? `${timer} · ${activeSpeaker === 'me' ? 'You' : 'Call'}` : timer;
  } else if (armed) {
    statusEl.textContent = 'Ready';
  } else {
    statusEl.textContent = capturing ? 'Listening' : 'Idle';
  }

  const pauseBtn = $('pause');
  pauseBtn.title = paused ? 'Resume listening' : 'Pause listening';
  $('pause-icon').replaceChildren(svgPath(paused ? 'M5 3.5l7 4.5-7 4.5z' : 'M5.5 3.5v9M10.5 3.5v9'));
}

function svgPath(d) {
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', d);
  return path;
}

setInterval(() => { if (capturing && !paused) renderStatus(); }, 1000);

window.copilot.onStatus((status) => {
  if (typeof status.capturing === 'boolean') {
    capturing = status.capturing;
    if (capturing && !sessionStart) sessionStart = Date.now();
    if (!capturing) sessionStart = null;
  }
  if (typeof status.armed === 'boolean') armed = status.armed;
  if (typeof status.paused === 'boolean') paused = status.paused;
  renderStatus();
});

window.copilot.onActivity((activity) => {
  speaking = Boolean(activity?.speaking);
  if (activity?.speaker) activeSpeaker = activity.speaker;
  renderStatus();
});

$('pause').addEventListener('click', () => window.copilot.togglePause());

const statusSlotEl = $('status-slot');
let toastTimer = null;
window.copilot.onToast((payload) => {
  toastEl.textContent = payload.text ?? '';
  statusSlotEl.classList.add('toasting');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => statusSlotEl.classList.remove('toasting'), 1600);
});

const healthEl = $('health');
window.copilot.onHealth((health) => {
  const problem = health?.problem;
  healthEl.hidden = !problem;
  if (problem) healthEl.title = `${problem} — click to open Settings`;
});
healthEl.addEventListener('click', () => window.copilot.openSettings());

$('shortcuts-toggle').addEventListener('click', () => $('shortcuts').classList.toggle('open'));
$('help').addEventListener('click', () => {
  const sheet = $('shortcuts');
  if (mode !== 'card') {
    sheet.classList.add('open');
    window.copilot.setMode('card');
  } else {
    sheet.classList.toggle('open');
  }
});

// ---------------------------------------------------------------- dynamic sources

function renderSources(state) {
  const labels = { ...DEFAULT_LABELS, ...(state?.labels ?? {}) };
  let keys = Array.isArray(state?.hotkey_sources) && state.hotkey_sources.length
    ? state.hotkey_sources
    : Object.keys(DEFAULT_LABELS).filter((k) => Object.prototype.hasOwnProperty.call(state ?? {}, k));
  keys = keys.slice(0, 4);

  sourcesEl.replaceChildren(...keys.map((name, idx) => {
    const btn = document.createElement('button');
    btn.className = `src${state?.[name] ? ' on' : ''}`;
    btn.dataset.source = name;
    const label = labels[name] ?? name;
    btn.textContent = label;
    btn.title = `${label} (⌘⌥${idx + 1})`;
    btn.addEventListener('click', () => window.copilot.toggleSource(name));
    return btn;
  }));

  const shortcutNames = keys.map((k) => labels[k] ?? k).join(', ');
  if (shortcutNames) {
    $('shortcuts-sources').textContent = `Toggle ${shortcutNames}`;
  }
}

window.copilot.onSources(renderSources);

// ---------------------------------------------------------------- answers & history

let streamingBuffer = '';
let streaming = false;

function showAnswer() {
  answerEl.classList.add('visible');
}

function setQuestion(text) {
  questionEl.textContent = text ?? '';
}

function renderCitations(answer) {
  citationsEl.replaceChildren();
  const seen = new Set();
  const links = Array.isArray(answer?.citation_links) && answer.citation_links.length
    ? answer.citation_links
    : (answer?.citations ?? []).map((label) => ({ label }));

  for (const item of links) {
    const label = String(item?.label ?? '').trim();
    if (!label || seen.has(label)) continue;
    seen.add(label);

    const hasTarget = Boolean(item?.url || item?.path);
    const tag = document.createElement(hasTarget ? 'button' : 'span');
    tag.className = `citation${hasTarget ? ' clickable' : ''}`;
    tag.textContent = label;
    tag.title = hasTarget ? `${label} — click to open (⌥-click to copy)` : label;
    if (hasTarget) {
      tag.addEventListener('click', (event) => {
        if (event.altKey) {
          window.copilot.copy(label);
          window.copilot.toast('Citation copied');
          return;
        }
        window.copilot.openCitation({ url: item.url, path: item.path, label });
      });
    }
    citationsEl.append(tag);
  }
}

function renderHistoryControls() {
  if (answerHistory.length <= 1 || streaming) {
    historyNavEl.hidden = true;
    return;
  }
  historyNavEl.hidden = false;
  histPosEl.textContent = `${historyIndex + 1}/${answerHistory.length}`;
  histPrevBtn.disabled = historyIndex <= 0;
  histNextBtn.disabled = historyIndex >= answerHistory.length - 1;
}

function displayHistoryEntry(entry) {
  if (!entry) return;
  answerTextEl.classList.remove('streaming');
  setQuestion(entry.question ?? '');
  if (entry.format === 'summary') {
    renderFormatted(answerTextEl, entry.text ?? '');
  } else {
    answerTextEl.classList.remove('formatted');
    answerTextEl.textContent = entry.text ?? '';
  }
  renderCitations(entry);
  renderHistoryControls();
  showAnswer();
}

histPrevBtn.addEventListener('click', () => {
  if (historyIndex > 0) {
    historyIndex -= 1;
    displayHistoryEntry(answerHistory[historyIndex]);
  }
});

histNextBtn.addEventListener('click', () => {
  if (historyIndex < answerHistory.length - 1) {
    historyIndex += 1;
    displayHistoryEntry(answerHistory[historyIndex]);
  }
});

window.copilot.onAnswerStart((payload) => {
  streamingBuffer = '';
  streaming = true;
  answerTextEl.classList.remove('formatted');
  answerTextEl.textContent = '';
  answerTextEl.classList.add('streaming');
  citationsEl.replaceChildren();
  historyNavEl.hidden = true;
  setQuestion(payload?.question);
  showAnswer();
});

window.copilot.onAnswerDelta((payload) => {
  if (!streaming) return;
  if (typeof payload?.full === 'string') streamingBuffer = payload.full;
  else streamingBuffer += payload?.text ?? '';
  answerTextEl.textContent = streamingBuffer;
});

// Walkthrough coach marks
const tourEl = document.getElementById('tour');
let tourTarget = null;

window.copilot.onTour((step) => {
  tourTarget?.classList.remove('tour-target');
  tourTarget = null;
  if (!step) {
    tourEl.hidden = true;
    return;
  }
  tourTarget = document.getElementById(step.target);
  if (!tourTarget) {
    window.copilot.tourNext();
    return;
  }
  tourTarget.classList.add('tour-target');
  document.getElementById('tour-title').textContent = step.title;
  document.getElementById('tour-text').textContent = step.text;
  document.getElementById('tour-count').textContent = `${step.index + 1} of ${step.total}`;
  document.getElementById('tour-next').textContent = step.index + 1 === step.total ? 'Done' : 'Next';
  tourEl.hidden = false;

  const surface = document.getElementById('surface').getBoundingClientRect();
  const target = tourTarget.getBoundingClientRect();
  const below = target.top - surface.top < surface.height / 2;
  tourEl.style.top = below ? `${target.bottom - surface.top + 10}px` : '';
  tourEl.style.bottom = below ? '' : `${surface.bottom - target.top + 10}px`;
});
document.getElementById('tour-next').addEventListener('click', () => window.copilot.tourNext());
document.getElementById('tour-skip').addEventListener('click', () => window.copilot.tourEnd());

window.copilot.onProfile((profile) => {
  const root = document.documentElement;
  root.dataset.profile = profile?.effective_name ?? profile?.name ?? 'general';
  root.dataset.clinical = profile?.clinical_mode ? 'true' : 'false';
  if (profile?.ui?.show_transcript === false) root.dataset.hideTranscript = '';
  else delete root.dataset.hideTranscript;
});

window.copilot.onAnswer((answer) => {
  streaming = false;
  const entry = {
    question: answer.question !== undefined ? answer.question : questionEl.textContent,
    text: answer.text ?? '',
    citations: answer.citations ?? [],
    citation_links: answer.citation_links ?? [],
    format: answer.format ?? 'answer',
  };
  answerHistory.push(entry);
  if (answerHistory.length > MAX_ANSWER_HISTORY) answerHistory.shift();
  historyIndex = answerHistory.length - 1;
  displayHistoryEntry(entry);
});

function renderFormatted(container, text) {
  container.classList.add('formatted');
  container.replaceChildren();
  let list = null;
  for (const raw of text.split('\n')) {
    const line = raw.trim().replace(/\*\*(.+?)\*\*/g, '$1');
    if (!line) { list = null; continue; }
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    if (heading) {
      list = null;
      const h = document.createElement('h3');
      h.textContent = heading[1];
      container.append(h);
    } else if (bullet) {
      if (!list) {
        list = document.createElement('ul');
        container.append(list);
      }
      const li = document.createElement('li');
      li.textContent = bullet[1];
      list.append(li);
    } else {
      list = null;
      const p = document.createElement('p');
      p.textContent = line;
      container.append(p);
    }
  }
}

$('copy').addEventListener('click', async () => {
  const text = answerTextEl.innerText.trim();
  if (!text) return;
  await window.copilot.copy(text);
  window.copilot.toast('Copied');
});

$('clear').addEventListener('click', () => {
  streaming = false;
  answerEl.classList.remove('visible');
  answerTextEl.textContent = '';
  answerTextEl.classList.remove('streaming', 'formatted');
  citationsEl.replaceChildren();
  historyNavEl.hidden = true;
  setQuestion('');
});

// ---------------------------------------------------------------- chips & ask bar

for (const chip of document.querySelectorAll('.chip')) {
  chip.addEventListener('click', () => {
    const intent = chip.dataset.intent;
    if (intent === 'recap') window.copilot.summarise();
    else window.copilot.runIntent(intent);
  });
}

function openAsk() {
  if (askFocused) {
    askInput.focus();
    return;
  }
  window.copilot.focusAskBar();
}

function releaseAsk() {
  if (!askFocused) return;
  askFocused = false;
  askForm.classList.remove('focused');
  askInput.blur();
  window.copilot.releaseFocus();
}

askInput.addEventListener('mousedown', (event) => {
  if (!askFocused) {
    event.preventDefault();
    openAsk();
  }
});

window.copilot.onAskFocus(() => {
  askFocused = true;
  askForm.classList.add('focused');
  askInput.focus();
});

window.copilot.onAskBlur(() => {
  askFocused = false;
  askForm.classList.remove('focused');
});

askForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const question = askInput.value.trim();
  askInput.value = '';
  releaseAsk();
  if (!question) return;
  const activeEntry = historyIndex >= 0 ? answerHistory[historyIndex] : null;
  const followUp = activeEntry && answerEl.classList.contains('visible') && activeEntry.format !== 'summary'
    ? { previous_question: activeEntry.question, previous_answer: activeEntry.text }
    : null;
  window.copilot.ask(question, followUp);
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    releaseAsk();
    return;
  }
  if (event.metaKey && event.key === ',') {
    event.preventDefault();
    releaseAsk();
    window.copilot.openSettings();
    return;
  }
  if (event.metaKey && event.altKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
    event.preventDefault();
    window.copilot.nudge(event.key === 'ArrowLeft' ? -1 : 1);
  }
});

// ---------------------------------------------------------------- transcript

$('transcript-toggle').addEventListener('click', () => {
  transcriptEl.classList.toggle('open');
  turnsEl.scrollTop = turnsEl.scrollHeight;
});

window.copilot.onTranscript((turn) => {
  const row = document.createElement('div');
  row.className = `turn ${turn.speaker === 'them' ? 'them' : 'me'}`;
  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = turn.speaker === 'them' ? 'Them' : 'Me';
  row.append(who, document.createTextNode(turn.text));
  turnsEl.append(row);
  while (turnsEl.children.length > MAX_VISIBLE_TURNS) turnsEl.firstElementChild.remove();
  turnsEl.scrollTop = turnsEl.scrollHeight;
});

renderStatus();
