'use strict';

// Overlay renderer. Two modes, owned by main (it owns the window bounds):
//   pill — one line of quiet chrome: status, timer, pause, expand
//   card — answer, action chips, ask bar, collapsible transcript
// The renderer asks for a mode change; main resizes the window and echoes the
// mode back, so hotkeys and clicks converge on the same state.

const $ = (id) => document.getElementById(id);

const rootEl = $('root');
const surfaceEl = $('surface');
const scrollEl = $('scroll');
const dotEl = $('dot');
const statusEl = $('status');
const toastEl = $('toast');
const answerEl = $('answer');
const questionEl = $('question');
const answerTextEl = $('answer-text');
const citationsEl = $('citations');
const askForm = $('ask');
const askInput = $('ask-input');
const transcriptEl = $('transcript');
const turnsEl = $('turns');

const MAX_VISIBLE_TURNS = 60;

let mode = 'pill';
let maxWindowHeight = 600;
let capturing = false;
let paused = false;
let speaking = false;
let sessionStart = null;
let askFocused = false;

// ---------------------------------------------------------------- mode

function applyMode(next) {
  mode = next;
  rootEl.dataset.mode = next;
  fit();
}

window.copilot.onMode((payload) => applyMode(payload.mode));
window.copilot.onConfig((config) => {
  maxWindowHeight = config.maxHeight ?? maxWindowHeight;
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

// ---------------------------------------------------------------- sizing

// The window is sized to its content. The scroll region gets whatever height
// is left under the cap, so a long answer scrolls instead of being clipped.
function fit() {
  if (mode !== 'card') {
    scrollEl.style.maxHeight = '';
    return;
  }
  const chrome = surfaceEl.offsetHeight - scrollEl.offsetHeight;
  const room = Math.max(120, maxWindowHeight - 12 - chrome);
  scrollEl.style.maxHeight = `${room}px`;
}

new ResizeObserver(() => {
  if (mode !== 'card') return;
  // body padding (6px each side) is part of the window.
  window.copilot.reportHeight(Math.ceil(surfaceEl.getBoundingClientRect().height) + 12);
}).observe(surfaceEl);

// ---------------------------------------------------------------- status

function renderStatus() {
  dotEl.classList.toggle('live', capturing && !paused);
  dotEl.classList.toggle('paused', paused);
  dotEl.classList.toggle('speaking', speaking && capturing && !paused);

  if (paused) {
    statusEl.textContent = 'Paused';
  } else if (capturing && sessionStart) {
    const seconds = Math.floor((Date.now() - sessionStart) / 1000);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = String(seconds % 60).padStart(2, '0');
    statusEl.textContent = h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
  } else {
    statusEl.textContent = capturing ? 'Listening' : 'Not capturing';
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
  if (typeof status.paused === 'boolean') paused = status.paused;
  renderStatus();
});

window.copilot.onActivity((activity) => {
  speaking = Boolean(activity?.speaking);
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

// Health: main polls /health and reports whether a model provider is usable.
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

// ---------------------------------------------------------------- sources

window.copilot.onSources((state) => {
  for (const chip of document.querySelectorAll('.src')) {
    const name = chip.dataset.source;
    // The API only reports sources this mode offers; an absent key means
    // "not available here", so hide it rather than show a dead control.
    chip.hidden = !Object.prototype.hasOwnProperty.call(state ?? {}, name);
    chip.classList.toggle('on', Boolean(state?.[name]));
  }
});

for (const chip of document.querySelectorAll('.src')) {
  chip.addEventListener('click', () => window.copilot.toggleSource(chip.dataset.source));
}

// ---------------------------------------------------------------- answers

let streamingBuffer = '';
let streaming = false;

function showAnswer() {
  answerEl.classList.add('visible');
}

function setQuestion(text) {
  questionEl.textContent = text ?? '';
}

window.copilot.onAnswerStart((payload) => {
  streamingBuffer = '';
  streaming = true;
  answerTextEl.classList.remove('formatted');
  answerTextEl.textContent = '';
  answerTextEl.classList.add('streaming');
  citationsEl.replaceChildren();
  setQuestion(payload?.question);
  showAnswer();
});

window.copilot.onAnswerDelta((payload) => {
  if (!streaming) return;
  // Redacting profiles send the whole redacted answer so far (`full`), because
  // a name split across two fragments cannot be redacted fragment by fragment.
  if (typeof payload?.full === 'string') streamingBuffer = payload.full;
  else streamingBuffer += payload?.text ?? '';
  answerTextEl.textContent = streamingBuffer;
});

// Walkthrough coach marks. Main drives the sequence; the renderer only
// outlines the named element and places the tooltip on the far side of it.
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

// Profile: type scale and contrast come from tokens.css via data-profile;
// layout differences are attributes the stylesheet keys off.
window.copilot.onProfile((profile) => {
  const root = document.documentElement;
  root.dataset.profile = profile?.name ?? 'general';
  if (profile?.ui?.show_transcript === false) root.dataset.hideTranscript = '';
  else delete root.dataset.hideTranscript;
});

// The final answer replaces the stream: cleaning (dropping a spurious
// "No info" suffix, removing headings) only happens once the whole response exists.
window.copilot.onAnswer((answer) => {
  streaming = false;
  answerTextEl.classList.remove('streaming');
  if (answer.question !== undefined) setQuestion(answer.question);

  if (answer.format === 'summary') {
    renderFormatted(answerTextEl, answer.text ?? '');
  } else {
    answerTextEl.classList.remove('formatted');
    answerTextEl.textContent = answer.text ?? '';
  }

  citationsEl.replaceChildren();
  // De-duplicated: retrieval can surface the same file for several windows.
  for (const citation of [...new Set(answer.citations ?? [])]) {
    const tag = document.createElement('span');
    tag.className = 'citation';
    tag.textContent = citation;
    tag.title = citation;
    citationsEl.append(tag);
  }
  showAnswer();
});

// Minimal markdown for summaries: "## heading", "- bullet", paragraphs.
// Built from DOM nodes with textContent — the text is model output over an
// untrusted transcript and must never be interpreted as HTML.
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
  setQuestion('');
});

// ---------------------------------------------------------------- chips

for (const chip of document.querySelectorAll('.chip')) {
  chip.addEventListener('click', () => {
    const intent = chip.dataset.intent;
    if (intent === 'recap') window.copilot.summarise();
    else window.copilot.runIntent(intent);
  });
}

// ---------------------------------------------------------------- ask bar

// The overlay window is not focusable, so it can never steal focus from the
// meeting. Typing needs focus, so the ask bar borrows it on demand and hands it
// back on Return or Esc.
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

// Main has made the window focusable and focused it.
window.copilot.onAskFocus(() => {
  askFocused = true;
  askForm.classList.add('focused');
  askInput.focus();
});

// Focus left the window by other means (clicked another app).
window.copilot.onAskBlur(() => {
  askFocused = false;
  askForm.classList.remove('focused');
});

askForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const question = askInput.value.trim();
  askInput.value = '';
  releaseAsk();
  if (question) window.copilot.ask(question);
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
  // Nudge only while the overlay holds focus. As a global shortcut, ⌘⌥←/→
  // would steal tab switching from every browser and editor.
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
  // textContent throughout — transcript text is untrusted input.
  row.append(who, document.createTextNode(turn.text));
  turnsEl.append(row);
  while (turnsEl.children.length > MAX_VISIBLE_TURNS) turnsEl.firstElementChild.remove();
  turnsEl.scrollTop = turnsEl.scrollHeight;
});

renderStatus();
