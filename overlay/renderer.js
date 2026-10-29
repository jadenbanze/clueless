'use strict';

const bodyEl = document.getElementById('body');
const placeholderEl = document.getElementById('placeholder');
const answerEl = document.getElementById('answer');
const answerTextEl = document.getElementById('answer-text');
const dotEl = document.getElementById('dot');

const MAX_VISIBLE_TURNS = 60;

function appendTurn(turn) {
  placeholderEl?.remove();

  const row = document.createElement('div');
  row.className = `turn ${turn.speaker === 'them' ? 'them' : 'me'}`;

  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = turn.speaker === 'them' ? 'Them: ' : 'Me: ';

  // textContent throughout — transcript text is untrusted input and must never
  // be interpolated as HTML.
  const text = document.createTextNode(turn.text);

  row.append(who, text);
  bodyEl.append(row);

  while (bodyEl.children.length > MAX_VISIBLE_TURNS) {
    bodyEl.firstElementChild.remove();
  }
  bodyEl.scrollTop = bodyEl.scrollHeight;
}

window.copilot.onTranscript(appendTurn);

// Streaming state. Deltas accumulate into a buffer; the final answer replaces
// it, because cleaning (dropping a spurious "No info" suffix, removing headings)
// can only be applied once the whole response exists.
let streamingBuffer = '';
let streaming = false;

window.copilot.onAnswerStart((payload) => {
  streamingBuffer = '';
  streaming = true;
  answerTextEl.textContent = '';
  answerTextEl.classList.add('streaming');
  document.getElementById('answer-citations').replaceChildren();
  answerEl.classList.add('visible');

  const questionEl = document.getElementById('answer-question');
  questionEl.textContent = payload?.question ? `Q: ${payload.question}` : '';
});

window.copilot.onAnswerDelta((payload) => {
  if (!streaming) return;
  streamingBuffer += payload?.text ?? '';
  answerTextEl.textContent = streamingBuffer;
});

window.copilot.onAnswer((answer) => {
  streaming = false;
  answerTextEl.classList.remove('streaming');
  answerTextEl.textContent = answer.text ?? '';

  const citationsEl = document.getElementById('answer-citations');
  citationsEl.replaceChildren();
  const citations = answer.citations ?? [];
  if (citations.length) {
    // De-duplicated: retrieval can surface the same file for several windows.
    const unique = [...new Set(citations)];
    const label = document.createElement('div');
    label.className = 'citations-label';
    label.textContent = 'Sources';
    citationsEl.append(label);
    for (const citation of unique) {
      const item = document.createElement('div');
      item.className = 'citation';
      item.textContent = citation;
      citationsEl.append(item);
    }
  }

  answerEl.classList.add('visible');
});

window.copilot.onSources((state) => {
  for (const chip of document.querySelectorAll('.src')) {
    const name = chip.dataset.source;
    // The API only reports sources this mode offers, so an absent key means
    // "not available here" — hide it rather than showing a dead control.
    const available = Object.prototype.hasOwnProperty.call(state ?? {}, name);
    chip.hidden = !available;
    chip.classList.toggle('on', Boolean(state?.[name]));
  }
});

// Speech activity. The transcriber only emits finalized text, so without this
// the panel sits unchanged for several seconds while someone is mid-sentence and
// looks frozen.
window.copilot.onActivity((activity) => {
  const speaking = Boolean(activity?.speaking);
  dotEl.classList.toggle('speaking', speaking);
  const hint = document.getElementById('hint');
  if (hint) {
    hint.textContent = speaking ? 'listening…' : '⌘⇧\\ to ask';
  }
});

window.copilot.onStatus((status) => {
  const capturing = Boolean(status.capturing);
  dotEl.classList.toggle('live', capturing);
  // The footer is the only place the user can tell whether audio is actually
  // being captured — without it a silent failure looks identical to a quiet
  // meeting.
  const footStatus = document.getElementById('foot-status');
  footStatus.textContent = capturing ? '● capturing' : '○ not capturing';
  footStatus.style.color = capturing ? '#4ad07a' : 'var(--muted)';
});
