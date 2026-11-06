'use strict';

// Electron shell for Clueless.
//
// Responsibilities beyond drawing the overlay:
//
//  1. Hold the audio permissions. macOS attributes a TCC request to the
//     topmost owning application, so a spawned helper inherits this app's
//     grants. The Python API and the microphone helper both descend from here.
//     The system-audio helper is the exception: it must be started through
//     LaunchServices (`open -n`) so it runs under its own bundle identity,
//     because AudioCapture is never granted to a child process. That split is
//     handled in server/api.py; see AGENTS.md.
//
//  2. Stay invisible to screen sharing via setContentProtection(true).
//
//  3. Own the hotkey and forward answers to the renderer.

const {
  app, BrowserWindow, globalShortcut, systemPreferences, ipcMain, desktopCapturer,
  Tray, Menu, dialog, nativeImage, clipboard, screen, shell,
} = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const { runPreflight } = require('./preflight');

// In development __dirname is overlay/, so the project root is one level up.
// In a packaged build __dirname is inside app.asar, where '..' is meaningless —
// the helpers and server live in Resources/ instead. Getting this wrong makes
// the packaged app fail to find either, with no useful error.
const PROJECT_ROOT = app.isPackaged
  ? process.resourcesPath
  : path.join(__dirname, '..');
// Packaged builds must not write .env / settings / chroma into Resources.
// Application Support is writable and survives app updates.
const DATA_DIR = app.isPackaged
  ? app.getPath('userData')
  : PROJECT_ROOT;
const API_BASE = 'http://127.0.0.1:8766';
const API_WS = 'ws://127.0.0.1:8766/events';
// Per-launch secret for the loopback API (see server/security.py). Passed to
// the API through the environment, never argv, and never given to a renderer:
// every API call goes through main.
const API_TOKEN = require('crypto').randomBytes(32).toString('hex');

// settings.json is owned by the API, but launch decisions (locale for the
// preflight speech check, whether onboarding is done) are made before it runs.
function readSavedSettings() {
  try {
    return JSON.parse(require('fs').readFileSync(path.join(DATA_DIR, '.cache', 'settings.json'), 'utf8'));
  } catch {
    return {};
  }
}

function apiFetch(pathname, options = {}) {
  return fetch(`${API_BASE}${pathname}`, {
    ...options,
    headers: { ...(options.headers ?? {}), Authorization: `Bearer ${API_TOKEN}` },
  });
}

// Verified free: macOS has only four enabled symbolic hotkeys (79-82, the
// Mission Control space switches) and Teams does not bind either of these.
const HOTKEY_ASK = 'CommandOrControl+Shift+\\';
// Per-intent actions, following the pattern every mature Clueless-style tool uses.
const HOTKEY_SUGGEST = 'CommandOrControl+Shift+A';
const HOTKEY_EXPLAIN = 'CommandOrControl+Shift+E';
const HOTKEY_TOGGLE = 'CommandOrControl+Shift+H';
// Opens the card and focuses the ask bar (Cluely's "stealth" chord).
const HOTKEY_ASK_BAR = 'CommandOrControl+Shift+Return';

// Overlay geometry. The window includes 6px of transparent padding on each
// side for the shadow (see body padding in index.html).
const PILL_SIZE = { width: 372, height: 54 };
const CARD_WIDTH = 472;
const CARD_MIN_HEIGHT = 200;
// Card height follows content up to this share of the work area.
const CARD_MAX_SHARE = 0.7;
const NUDGE_PX = 40;

// Source toggles. Which references are relevant depends entirely on what the
// meeting is about, and no automatic relevance gate is reliable here, so the
// user decides.
const DEVELOPER_SOURCES = ['jira', 'confluence', 'code'];
// Populated once the API answers /sources, before hotkeys are bound.
let initialSources = null;

// ⌘⇧3 and ⌘⇧4 are reserved by macOS for screenshots, so globalShortcut.register
// returns false for them and the press never reaches the app. ⌘⌥1–4 are free.
const SOURCE_HOTKEYS = {
  'CommandOrControl+Alt+1': 'jira',
  'CommandOrControl+Alt+2': 'confluence',
  'CommandOrControl+Alt+3': 'code',
  'CommandOrControl+Alt+4': 'screen',
};
const SOURCE_HOTKEY_LABELS = {
  jira: '⌘⌥1',
  confluence: '⌘⌥2',
  code: '⌘⌥3',
  screen: '⌘⌥4',
};

let overlayWindow = null;
let apiProcess = null;
let socket = null;
let reconnectTimer = null;
let tray = null;
// Last summary, kept so it can be saved. Transcripts are never written to disk,
// so a summary is the only artefact that can outlive a meeting — and only if the
// user explicitly saves it.
let lastSummary = null;
let paused = false;
let settingsWindow = null;
let sourceState = { jira: true, confluence: true, code: false, screen: false };
let capturing = false;
// Set when the user hides the overlay, so reconnects don't pop it back up.
let userHidOverlay = false;
// 'hidden' | 'pill' | 'card'. Main owns this because it owns the bounds.
let overlayMode = 'hidden';
let cardHeight = 320;
// Saved as horizontal centre + top so switching pill/card width keeps the
// overlay anchored where the user put it.
let overlayAnchor = null;
let askFocused = false;

/** Prompts for microphone access if undecided. Returns the final state. */
async function ensureMicrophoneAccess() {
  const status = systemPreferences.getMediaAccessStatus('microphone');
  console.log(`[permissions] microphone: ${status}`);
  if (status === 'granted') return true;
  if (status === 'denied' || status === 'restricted') {
    console.warn('[permissions] denied — enable in System Settings > Privacy & Security > Microphone');
    return false;
  }
  const granted = await systemPreferences.askForMediaAccess('microphone');
  console.log(`[permissions] microphone request -> ${granted ? 'granted' : 'denied'}`);
  return granted;
}

function ensureDataDir() {
  const fs = require('fs');
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.mkdirSync(path.join(DATA_DIR, '.cache'), { recursive: true });
  const destEnv = path.join(DATA_DIR, '.env');
  if (app.isPackaged && !fs.existsSync(destEnv)) {
    // First packaged launch: optional seed path from pack-local / the shell.
    // Never bake .env into the bundle — credentials stay outside Resources.
    const seed = process.env.CLUELESS_SEED_ENV;
    if (seed && fs.existsSync(seed)) {
      fs.copyFileSync(seed, destEnv);
      console.log(`[data] seeded .env into ${destEnv}`);
    } else {
      console.warn(
        `[data] no .env at ${destEnv} — copy one there (see .env.example), `
        + 'or launch with CLUELESS_SEED_ENV=/path/to/.env',
      );
    }
  }
}

function startApiServer() {
  // Prefer the bundled relocatable Python so a packaged build needs nothing
  // installed. PyInstaller was tried first and abandoned: its bootloader hung
  // before Python started, and the one-file binary was 363 MB — larger than the
  // whole app — for the same dependency set this ships directly.
  const bundled = path.join(PROJECT_ROOT, 'native', 'build', 'python', 'bin', 'python3');
  const interpreter = require('fs').existsSync(bundled) ? bundled : 'python3';

  ensureDataDir();
  const env = {
    ...process.env,
    // Always set so the API writes settings/chroma next to credentials.
    CLUELESS_DATA_DIR: DATA_DIR,
    CLUELESS_API_TOKEN: API_TOKEN,
    CLUELESS_VERSION: app.getVersion(),
    // FastAPI depends on opentelemetry-api. Without an SDK installed it is a
    // no-op; this keeps it one even if an SDK ever arrives transitively.
    OTEL_SDK_DISABLED: 'true',
  };
  // Bytecode written inside a signed bundle breaks its code-signature seal,
  // after which Gatekeeper reports the app as damaged.
  if (app.isPackaged) env.PYTHONPYCACHEPREFIX = path.join(DATA_DIR, '.cache', 'pycache');

  apiProcess = spawn(interpreter, ['-m', 'server.api'], {
    cwd: PROJECT_ROOT,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  // Mirror API output to a file as well as stdout. A packaged app's stdout goes
  // nowhere useful, which made a failed server spawn impossible to diagnose.
  // Raw stdout/stderr catches crashes before the API's own rotating log starts.
  const logDir = path.join(DATA_DIR, 'logs');
  require('fs').mkdirSync(logDir, { recursive: true, mode: 0o700 });
  const logPath = path.join(logDir, 'api-stdout.log');
  const logStream = require('fs').createWriteStream(logPath, { flags: 'w', mode: 0o600 });
  apiProcess.stdout.on('data', (chunk) => { process.stdout.write(`[api] ${chunk}`); logStream.write(chunk); });
  apiProcess.stderr.on('data', (chunk) => { process.stderr.write(`[api] ${chunk}`); logStream.write(chunk); });
  console.log(`[api] log: ${logPath}`);
  apiProcess.on('exit', (code) => {
    if (code !== 0 && code !== null) console.error(`[api] exited with code ${code}`);
    apiProcess = null;
  });
  console.log(`[api] server starting (${interpreter === 'python3' ? 'system python3' : 'bundled python'})`);
}

function send(channel, payload) {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.webContents.send(channel, payload);
  }
}

function connectEvents() {
  if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
  try {
    // WebSockets can't carry an Authorization header from every client, so the
    // token travels as a subprotocol; the API echoes it on accept.
    socket = new WebSocket(API_WS, [`clueless.${API_TOKEN}`]);
  } catch (error) {
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    console.log('[ws] connected');
    capturing = true;
    updateTray();
    send('status:update', { capturing: true, paused });
    loadProfile();
    // Appear as the pill once a session is live, never earlier — and not over
    // the walkthrough, which shows the overlay itself during the tour.
    if (overlayMode === 'hidden' && !userHidOverlay && !onboardingWindow) setOverlayMode('pill');
  };

  socket.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === 'turn') {
      send('transcript:turn', { speaker: message.speaker, text: message.text });
      if (onboardingWindow && !onboardingWindow.isDestroyed()) {
        onboardingWindow.webContents.send('onboarding:turn', { speaker: message.speaker, text: message.text });
      }
    } else if (message.type === 'answer_start') {
      send('answer:start', { question: message.question });
    } else if (message.type === 'answer_delta') {
      send('answer:delta', { text: message.text, full: message.full });
    } else if (message.type === 'answer') {
      send('answer:result', {
        text: message.text, citations: message.citations, question: message.question,
      });
    } else if (message.type === 'activity') {
      send('activity:update', { speaker: message.speaker, speaking: message.speaking });
    } else if (message.type === 'paused') {
      paused = Boolean(message.paused);
      updateTray();
      send('status:update', { paused });
    } else if (message.type === 'sources') {
      sourceState = message;
      updateTray();
      send('sources:update', message);
    }
  };

  socket.onclose = () => {
    capturing = false;
    updateTray();
    send('status:update', { capturing: false });
    scheduleReconnect();
  };
  socket.onerror = () => {};
}

/**
 * Polls /health so the pill can warn when no model can answer. Without it the
 * first sign of a missing provider is a failed answer mid-meeting.
 */
let lastHealth = null;
let profileState = null;

async function loadProfile() {
  try {
    const response = await apiFetch('/profile');
    if (response.ok) {
      profileState = await response.json();
      send('profile:update', profileState);
    }
  } catch { /* API still booting; retried on reconnect */ }
}

async function pollHealth() {
  try {
    const response = await apiFetch(`/health`);
    const health = await response.json();
    lastHealth = health;
    const provider = health.provider ?? {};
    let problem = null;
    if (provider.status === 'none') problem = provider.message || 'No local model provider is running';
    else if (provider.status === 'degraded') problem = provider.message || 'Running in degraded mode';
    send('health:update', { problem, provider });
  } catch {
    // API still booting or restarting; the WebSocket status already says so.
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  // The API takes a few seconds to boot; retry until it answers.
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectEvents();
  }, 2000);
}

/**
 * POSTs to the local API, retrying while it boots.
 *
 * The API is spawned alongside the window, so the first request usually lands
 * before uvicorn has bound the port. Without retrying, capture silently never
 * starts and the app looks connected but records nothing.
 */
async function postWithRetry(pathname, { attempts = 12, delayMs = 1000 } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await apiFetch(`${pathname}`, { method: 'POST' });
      if (response.ok) return await response.json();
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  throw lastError ?? new Error('unreachable');
}

/**
 * Grabs the current screen and sends it for OCR.
 *
 * Capture happens here because Screen Recording permission belongs to this app;
 * OCR happens in the Swift Vision helper, fully on-device. Without the
 * permission macOS returns an empty or black thumbnail rather than an error, so
 * a zero-character result is reported to the user instead of failing silently.
 */
async function captureScreenContext() {
  try {
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: 1600, height: 1000 },
    });
    if (!sources.length) {
      return { ok: false, reason: 'no screen available' };
    }
    const image = sources[0].thumbnail.toDataURL();
    const response = await apiFetch(`/screen`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image }),
    });
    const result = await response.json();
    console.log(`[screen] ok=${result.ok} chars=${result.chars ?? 0}`);
    if (!result.ok) {
      send('answer:result', {
        text:
          'Could not read the screen. Grant Screen Recording to Clueless in '
          + 'System Settings > Privacy & Security, then try again.',
        citations: [],
      });
      setOverlayMode('card');
    }
    return result;
  } catch (error) {
    console.error(`[screen] capture failed: ${error.message}`);
    return { ok: false, reason: error.message };
  }
}

async function askQuestion(intent = 'answer', question = null) {
  setOverlayMode('card');
  // The POST returns immediately; the answer arrives over the WebSocket as
  // answer_start / answer_delta / answer frames.
  try {
    const response = await apiFetch(`/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 90s of transcript is enough to answer what was just said, and keeps the
      // prompt short enough to stay responsive.
      body: JSON.stringify({ window_seconds: 90, intent, ...(question ? { question } : {}) }),
    });
    const result = await response.json();
    if (result.text) {
      // Non-streaming path (e.g. no audio captured yet) still returns a body.
      send('answer:result', {
        text: result.text, citations: result.citations ?? [], question: question ?? '',
      });
    }
  } catch (error) {
    send('answer:result', { text: `API unreachable: ${error.message}`, citations: [] });
  }
}

async function summariseMeeting() {
  setOverlayMode('card');
  send('answer:start', { question: 'Recap of the meeting so far' });
  try {
    const response = await apiFetch(`/summary`, { method: 'POST' });
    const summary = await response.json();
    lastSummary = summary.text ?? null;
    updateTray();
    send('answer:result', { text: summary.text, citations: [], format: 'summary' });
  } catch (error) {
    send('answer:result', { text: `API unreachable: ${error.message}`, citations: [] });
  }
}

async function toggleSource(source) {
  try {
    const response = await apiFetch(`/sources/${source}/toggle`, { method: 'POST' });
    const state = await response.json();
    if (!response.ok) {
      send('toast:show', { text: state.detail ?? `${source} unavailable` });
      return;
    }
    sourceState = state;
    updateTray();
    send('sources:update', state);
    const label = { confluence: 'Wiki' }[source] ?? source[0].toUpperCase() + source.slice(1);
    send('toast:show', { text: `${label} ${state[source] ? 'on' : 'off'}` });

    // Turning screen context on captures immediately, so the next question
    // already has it. Capture-on-enable rather than continuous capture:
    // cheaper, and it cannot quietly collect things you did not mean to share.
    if (source === 'screen' && state.screen) {
      await captureScreenContext();
    }
  } catch (error) {
    console.error(`[sources] toggle failed: ${error.message}`);
  }
}

async function togglePause() {
  try {
    const endpoint = paused ? '/resume' : '/pause';
    const response = await apiFetch(`${endpoint}`, { method: 'POST' });
    const state = await response.json();
    paused = Boolean(state.paused);
    updateTray();
    send('status:update', { paused });
    console.log(`[pause] ${paused ? 'paused' : 'resumed'}`);
  } catch (error) {
    console.error(`[pause] failed: ${error.message}`);
  }
}

/** Settings window. A normal, resizable window — deliberately not the overlay. */
function openSettings() {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 780,
    height: 680,
    minWidth: 680,
    minHeight: 480,
    backgroundColor: '#1c1c21',
    title: 'Clueless Settings',
    resizable: true,
    minimizable: true,
    webPreferences: {
      preload: path.join(__dirname, 'settings-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  settingsWindow.loadFile(path.join(__dirname, 'settings.html'));
  settingsWindow.on('closed', () => { settingsWindow = null; });
}

/** Saves the last summary to a file the user chooses. */
async function saveSummary() {
  if (!lastSummary) {
    dialog.showMessageBox({
      message: 'No summary yet',
      detail: 'Press ⌘⇧S to summarise the meeting first.',
      type: 'info',
    });
    return;
  }
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
  const result = await dialog.showSaveDialog({
    title: 'Save meeting summary',
    defaultPath: `meeting-summary-${stamp}.md`,
    filters: [{ name: 'Markdown', extensions: ['md'] }],
  });
  if (result.canceled || !result.filePath) return;
  require('fs').writeFileSync(result.filePath, lastSummary, 'utf8');
  console.log(`[summary] saved to ${result.filePath}`);
}

/**
 * Menu bar item.
 *
 * Without it the app is invisible until a hotkey fires, and "is it running?"
 * has no answer short of `ps`. It also gives a click path for every action, so
 * nothing depends on remembering four shortcuts.
 */
function createTray() {
  // Text-only item: no icon asset to ship or keep in sync with the theme.
  tray = new Tray(nativeImage.createEmpty());
  tray.setToolTip('Clueless');
  tray.setTitle('◉');
  updateTray();
}

function updateTray() {
  if (!tray) return;
  tray.setTitle(paused ? '⏸' : capturing ? '◉' : '○');

  const sourceItems = [
    ['jira', SOURCE_HOTKEY_LABELS.jira],
    ['confluence', SOURCE_HOTKEY_LABELS.confluence],
    ['code', SOURCE_HOTKEY_LABELS.code],
    ['screen', SOURCE_HOTKEY_LABELS.screen],
  ].map(([name, accel]) => ({
    label: `${name}  (${accel})`,
    type: 'checkbox',
    checked: Boolean(sourceState[name]),
    click: () => toggleSource(name),
  }));

  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: paused ? 'Paused' : capturing ? 'Listening' : 'Idle',
      enabled: false,
    },
    {
      label: paused ? 'Resume listening' : 'Pause listening',
      click: togglePause,
    },
    { type: 'separator' },
    { label: 'Ask  (⌘⇧\\)', click: () => askQuestion() },
    { label: 'Summarise  (⌘⇧S)', click: summariseMeeting },
    { label: 'Save summary…', click: saveSummary, enabled: Boolean(lastSummary) },
    { type: 'separator' },
    { label: 'Context sources', submenu: sourceItems },
    { type: 'separator' },
    { label: 'Settings…', click: openSettings },
    { label: 'Run walkthrough again…', click: openOnboarding },
    { type: 'separator' },
    { label: 'Show overlay  (⌘⇧H)', click: () => setOverlayMode('card') },
    { type: 'separator' },
    { label: 'Quit Clueless', role: 'quit' },
  ]));
}

/**
 * Default anchor: directly under the notch, horizontally centred.
 *
 * `workArea` (not `bounds`) is what accounts for the menu bar, so on a notched
 * Mac the overlay sits beneath the notch rather than under it.
 */
function defaultAnchor() {
  const area = screen.getPrimaryDisplay().workArea;
  // Flush against workArea.y read as glued to the menu bar; a small gap looks
  // deliberate.
  return { cx: Math.round(area.x + area.width / 2), y: area.y + 10 };
}

function anchorPath() {
  return path.join(app.getPath('userData'), 'overlay-position.json');
}

/** Restores the saved position, ignoring one that is no longer on any display. */
function loadAnchor() {
  try {
    const saved = JSON.parse(require('fs').readFileSync(anchorPath(), 'utf8'));
    if (Number.isFinite(saved.cx) && Number.isFinite(saved.y)) {
      const onScreen = screen.getAllDisplays().some(({ workArea: a }) =>
        saved.cx >= a.x && saved.cx <= a.x + a.width && saved.y >= a.y && saved.y <= a.y + a.height - 40);
      if (onScreen) return { cx: saved.cx, y: saved.y };
    }
  } catch { /* no saved position yet */ }
  return defaultAnchor();
}

function saveAnchor() {
  try {
    require('fs').writeFileSync(anchorPath(), JSON.stringify(overlayAnchor));
  } catch (error) {
    console.error(`[overlay] could not save position: ${error.message}`);
  }
}

function workAreaFor(anchor) {
  return screen.getDisplayNearestPoint({ x: anchor.cx, y: anchor.y }).workArea;
}

function maxCardHeight() {
  return Math.round(workAreaFor(overlayAnchor).height * CARD_MAX_SHARE);
}

function boundsFor(mode) {
  const width = mode === 'card' ? CARD_WIDTH : PILL_SIZE.width;
  const height = mode === 'card'
    ? Math.min(Math.max(cardHeight, CARD_MIN_HEIGHT), maxCardHeight())
    : PILL_SIZE.height;
  const area = workAreaFor(overlayAnchor);
  // Keep the whole window on the display it lives on.
  const x = Math.min(Math.max(Math.round(overlayAnchor.cx - width / 2), area.x),
    area.x + area.width - width);
  const y = Math.min(Math.max(overlayAnchor.y, area.y), area.y + area.height - height);
  return { x, y, width, height };
}

// Programmatic setBounds also fires 'moved'; this suppresses re-anchoring on
// our own resizes so the anchor only follows the user's drags.
let applyingBounds = false;
function applyBounds(animate) {
  if (!overlayWindow || overlayMode === 'hidden') return;
  applyingBounds = true;
  overlayWindow.setBounds(boundsFor(overlayMode), animate);
  setTimeout(() => { applyingBounds = false; }, animate ? 300 : 50);
}

/** Single entry point for visibility: 'hidden' | 'pill' | 'card'. */
function setOverlayMode(mode) {
  if (!overlayWindow) return;
  const previous = overlayMode;
  overlayMode = mode;
  if (mode === 'hidden') {
    releaseAskFocus({ handBack: false });
    overlayWindow.hide();
  } else {
    userHidOverlay = false;
    applyBounds(previous !== 'hidden' && previous !== mode);
    // showInactive: never steal focus from the meeting.
    if (!overlayWindow.isVisible()) overlayWindow.showInactive();
  }
  send('mode:set', { mode });
}

function createOverlay() {
  overlayAnchor = loadAnchor();
  overlayWindow = new BrowserWindow({
    ...boundsFor('pill'),
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: false,
    // Buttons must respond to the first click even though the window is never
    // key — otherwise every click would be swallowed as an activation click.
    acceptFirstMouse: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // The whole point: excluded from screen capture, so nobody you are sharing
  // your screen with can see it.
  overlayWindow.setContentProtection(true);

  // 'screen-saver' keeps it above full-screen apps, including Teams.
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // Dragging the grip moves the window; remember where the user put it.
  overlayWindow.on('moved', () => {
    if (applyingBounds) return;
    const b = overlayWindow.getBounds();
    overlayAnchor = { cx: Math.round(b.x + b.width / 2), y: b.y };
    saveAnchor();
  });

  // Focus left by other means (the user clicked another app): give up
  // focusability so the overlay can't grab the keyboard again by accident.
  overlayWindow.on('blur', () => {
    if (!askFocused) return;
    askFocused = false;
    overlayWindow.setFocusable(false);
    send('ask:blur');
  });

  overlayWindow.webContents.on('did-finish-load', () => {
    send('overlay:config', { maxHeight: maxCardHeight() });
    send('mode:set', { mode: overlayMode === 'hidden' ? 'pill' : overlayMode });
    send('status:update', { capturing, paused });
    send('sources:update', sourceState);
    if (profileState) send('profile:update', profileState);
  });

  overlayWindow.loadFile(path.join(__dirname, 'index.html'));
  return overlayWindow;
}

/** ⌘⇧H: card → pill → hidden → pill. */
function toggleOverlay() {
  if (!overlayWindow) return;
  if (overlayMode === 'card') setOverlayMode('pill');
  else if (overlayMode === 'pill') {
    userHidOverlay = true;
    setOverlayMode('hidden');
  } else setOverlayMode('pill');
}

/**
 * Borrows keyboard focus for the ask bar. The overlay is created unfocusable
 * so it can never take focus from the meeting by accident; typing is the one
 * case that needs it, and only for as long as the ask bar is open.
 */
function focusAskBar() {
  if (!overlayWindow) return;
  if (overlayMode !== 'card') setOverlayMode('card');
  askFocused = true;
  overlayWindow.setFocusable(true);
  overlayWindow.show();
  overlayWindow.focus();
  send('ask:focus');
}

/**
 * Gives focus back. app.hide() deactivates Clueless, which makes macOS
 * reactivate the previously frontmost app (the meeting); showInactive then
 * brings the overlay back without taking focus again.
 */
function releaseAskFocus({ handBack = true } = {}) {
  if (!overlayWindow || !askFocused) return;
  askFocused = false;
  overlayWindow.setFocusable(false);
  if (handBack && process.platform === 'darwin') {
    app.hide();
    setTimeout(() => {
      if (overlayMode !== 'hidden') overlayWindow.showInactive();
    }, 60);
  }
}

function nudgeOverlay(direction) {
  if (!overlayAnchor) return;
  overlayAnchor = { ...overlayAnchor, cx: overlayAnchor.cx + direction * NUDGE_PX };
  // Re-derive from the clamped bounds so repeated nudges can't push the
  // anchor past the screen edge.
  const b = boundsFor(overlayMode === 'hidden' ? 'pill' : overlayMode);
  overlayAnchor.cx = Math.round(b.x + b.width / 2);
  applyBounds(true);
  saveAnchor();
}

const HELPER_APP = path.join(PROJECT_ROOT, 'native', 'build', 'CluelessTap.app');

function parseProbe(text) {
  const result = {};
  for (const line of `${text}`.split('\n')) {
    const name = line.match(/PROBE \[(\w+)\]/)?.[1];
    const peak = line.match(/ peak=([0-9.e-]+)/)?.[1];
    const rms = line.match(/ rms=([0-9.e-]+)/)?.[1];
    if (name && peak) result[name] = { peak: parseFloat(peak), rms: rms ? parseFloat(rms) : null };
  }
  return result;
}

/** Microphone probe: the helper runs as our child so it inherits our mic grant. */
function probeMicrophone(seconds = 4) {
  const { execFile } = require('child_process');
  const helper = path.join(HELPER_APP, 'Contents', 'MacOS', 'audiotap');
  return new Promise((resolve) => {
    execFile(helper, ['--global', '--mic', '--probe', String(seconds)], { timeout: (seconds + 15) * 1000 },
      (_error, _stdout, stderr) => resolve(parseProbe(stderr).mic ?? null));
  });
}

/**
 * System-audio probe through LaunchServices (`open -n`) so the helper runs under
 * its own bundle identity. AudioDeviceStart blocks while the permission dialog
 * is open, hence the generous deadline. When `withTone` is set a system sound
 * plays during the probe: a granted tap over a silent Mac reads as zero, which
 * would be indistinguishable from a denial.
 */
function probeSystemAudio({ seconds = 4, deadlineMs = 14000, withTone = false } = {}) {
  const fs = require('fs');
  const logPath = path.join(require('os').tmpdir(), `clueless_tap_${process.pid}_${Date.now()}.log`);
  let tone = null;
  if (withTone) {
    const play = () => spawn('/usr/bin/afplay', ['/System/Library/Sounds/Submarine.aiff']);
    play();
    tone = setInterval(play, 900);
  }
  return new Promise((resolve) => {
    const child = spawn('open', ['-n', '-a', HELPER_APP,
      '--args', '--global', '--probe', String(seconds), '--log', logPath]);
    child.on('close', () => {
      const deadline = Date.now() + deadlineMs;
      const poll = setInterval(() => {
        let text = '';
        try { text = fs.readFileSync(logPath, 'utf8'); } catch { /* not yet */ }
        if (/RESULT:/.test(text) || Date.now() > deadline) {
          clearInterval(poll);
          if (tone) clearInterval(tone);
          try { fs.unlinkSync(logPath); } catch { /* ignore */ }
          resolve(parseProbe(text).system ?? null);
        }
      }, 400);
    });
  });
}

/** Self-test used by `npm run check-permissions`; see native/check-permissions.sh. */
async function runPermissionCheck() {
  const micOK = await ensureMicrophoneAccess();
  console.log('[check] probing microphone (helper as child)…');
  const mic = (await probeMicrophone(5))?.peak;
  console.log('[check] probing system audio (helper via LaunchServices)…');
  const system = (await probeSystemAudio({ seconds: 5 }))?.peak;
  console.log('\n===== PERMISSION CHECK =====');
  console.log(`microphone grant: ${micOK ? 'granted' : 'NOT granted'}`);
  console.log(`mic peak (child):        ${mic === undefined ? 'absent' : mic}`);
  console.log(`system peak (LaunchServices): ${system === undefined ? 'absent' : system}`);
  const bothWork = mic > 0 && system > 0;
  console.log(bothWork ? 'RESULT: SUCCESS — both streams capturable' : 'RESULT: INCOMPLETE');
  app.exit(bothWork ? 0 : 1);
}

/** Fictional meeting content per profile, so screenshots match what that profile can show. */
const SCREENSHOT_DEMOS = {
  developer: {
    turns: [
      { speaker: 'them', text: 'Are we still blocked on the Kafka authentication failure for the billing service?' },
      { speaker: 'me', text: 'Priya is investigating the JWT processing error.' },
      { speaker: 'them', text: 'Which ticket covers the audit write path remediation?' },
    ],
    question: 'Which ticket covers the audit write path remediation?',
    partial: 'The ticket that covers the audit write path remediation is ',
    answer: 'PROJ-2879 covers the audit write path remediation. It is the runbook and alerts task, not the read-path spike.',
    citations: [
      'PROJ-2879 — Create audit write-path runbook and alerts',
      'example-api/docs/runbooks/audit-write-path.md',
    ],
  },
  medical: {
    turns: [
      { speaker: 'them', text: 'Overnight the patient [NAME] needed two doses of breakthrough pain relief.' },
      { speaker: 'me', text: 'Physio wants a mobility review before discharge.' },
      { speaker: 'them', text: 'What is still outstanding before discharge?' },
    ],
    question: 'What is still outstanding before discharge?',
    partial: 'A mobility review by physio is ',
    answer: 'A physio mobility review is still outstanding. The overnight pain relief was mentioned, but no dose change was agreed.',
    citations: [],
  },
  general: {
    turns: [
      { speaker: 'them', text: 'Can we move the launch review to Thursday so design can join?' },
      { speaker: 'me', text: 'Thursday works, as long as the budget numbers are in by Wednesday.' },
      { speaker: 'them', text: 'So who is sending the budget numbers?' },
    ],
    question: 'Who is sending the budget numbers?',
    partial: 'Nobody has ',
    answer: 'Nobody has taken the budget numbers yet. They are needed by Wednesday for the Thursday launch review.',
    citations: [],
  },
};

/**
 * Renders the overlay offscreen and writes a PNG.
 *
 * `screencapture` needs Screen Recording permission, which is not granted here,
 * and capturing the desktop would not prove what the overlay itself renders.
 * webContents.capturePage() needs no permission and captures the real DOM.
 */
async function captureOverlay(outputPath) {
  // --state pill | card | ask (default card). --streaming implies card.
  const stateIndex = process.argv.indexOf('--state');
  const state = stateIndex !== -1 ? process.argv[stateIndex + 1] : 'card';

  createOverlay();
  await new Promise((resolve) => overlayWindow.webContents.once('did-finish-load', resolve));
  setOverlayMode(state === 'pill' ? 'pill' : 'card');

  // Populate with representative content so the screenshot exercises the
  // transcript rows, the answer pane and the citation list.
  overlayWindow.webContents.send('status:update', { capturing: true, paused: false });
  const profileArg = process.argv.indexOf('--profile');
  const shotProfile = profileArg !== -1 ? process.argv[profileArg + 1] : 'developer';
  // Mirrors the API: non-developer profiles don't offer the work sources at all.
  overlayWindow.webContents.send('sources:update', shotProfile === 'developer'
    ? { jira: true, confluence: true, code: false, screen: false }
    : { screen: false });
  if (state === 'ask') {
    // Renders the focused ask bar without actually taking focus.
    overlayWindow.webContents.send('ask:focus');
  }
  if (process.argv.includes('--toast')) {
    setTimeout(() => overlayWindow.webContents.send('toast:show', { text: 'Jira off' }), 900);
  }
  if (process.argv.includes('--degraded')) {
    overlayWindow.webContents.send('health:update', { problem: 'No local model provider is running' });
  }
  const tourIndexArg = process.argv.indexOf('--tour-step');
  if (tourIndexArg !== -1) {
    tourIndex = Number.parseInt(process.argv[tourIndexArg + 1], 10) || 0;
    setTimeout(sendTourStep, 700);
  }
  const profileIndex = process.argv.indexOf('--profile');
  if (profileIndex !== -1) {
    const name = process.argv[profileIndex + 1];
    overlayWindow.webContents.send('profile:update', { name, ui: { show_transcript: name !== 'medical' } });
  }
  const demo = SCREENSHOT_DEMOS[shotProfile] ?? SCREENSHOT_DEMOS.developer;
  for (const turn of demo.turns) overlayWindow.webContents.send('transcript:turn', turn);
  if (process.argv.includes('--streaming')) {
    // Mid-stream state: question line, partial answer, blinking caret, no
    // citations yet. Verifies the streaming UI rather than the settled one.
    overlayWindow.webContents.send('answer:start', { question: demo.question });
    overlayWindow.webContents.send('answer:delta', { text: demo.partial });
  } else {
    overlayWindow.webContents.send('answer:result', { text: demo.answer, citations: demo.citations });
  }

  await new Promise((resolve) => setTimeout(resolve, 1200));
  const image = await overlayWindow.webContents.capturePage();
  require('fs').writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath} (${image.getSize().width}x${image.getSize().height})`);
  app.exit(0);
}

// ---------------------------------------------------------------- preflight

let preflightResult = null;

function secureWindow(options, preload) {
  return new BrowserWindow({
    backgroundColor: '#1c1c21',
    show: false,
    ...options,
    webPreferences: {
      preload: path.join(__dirname, preload),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
}

/** Shown instead of everything else when the Mac cannot run Clueless. */
function showRequirements() {
  const win = secureWindow({
    width: 540, height: 480, resizable: false, minimizable: false,
    title: "Clueless can't run on this Mac",
  }, 'requirements-preload.js');
  win.loadFile(path.join(__dirname, 'requirements.html'));
  win.once('ready-to-show', () => { win.show(); app.focus({ steal: true }); });
  win.on('closed', () => app.quit());
  return win;
}

ipcMain.handle('requirements:get', () => ({ verdict: preflightResult?.verdict }));
ipcMain.handle('requirements:copy', () => clipboard.writeText(preflightResult?.report ?? ''));
ipcMain.handle('requirements:quit', () => app.quit());

async function captureRequirements(outputPath) {
  preflightResult = {
    verdict: {
      ok: false,
      blocking: [
        { id: 'macos', title: 'macOS 26 or later is required', detail: 'This Mac is running macOS 15.5. On-device transcription uses the Speech framework introduced in macOS 26.' },
        { id: 'arch', title: 'Apple silicon is required', detail: 'Clueless is built for M-series Macs. Intel Macs are not supported.' },
      ],
      warnings: [
        { id: 'ram', title: '8 GB memory', detail: 'An 8B model may be slow. A 3B model such as llama3.2 is recommended on this Mac.' },
      ],
      passed: [],
    },
    report: '',
  };
  const win = showRequirements();
  win.removeAllListeners('closed');
  await new Promise((resolve) => win.webContents.once('did-finish-load', resolve));
  await new Promise((resolve) => setTimeout(resolve, 600));
  const image = await win.webContents.capturePage();
  require('fs').writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath}`);
  app.exit(0);
}

// ---------------------------------------------------------------- onboarding

let onboardingWindow = null;
let captureStarted = false;
// Profile the API was started with; a change made in the walkthrough needs a
// relaunch so hotkeys for developer-only sources are (un)registered.
let bootProfile = null;

const truthy = (value) => ['1', 'true', 'yes'].includes(String(value ?? '').toLowerCase());

/** Starts capture once. Deferred until consent on a first run. */
async function startCapture({ force = false } = {}) {
  if (captureStarted && !force) return { ok: true };
  captureStarted = true;
  try {
    const started = await postWithRetry(`/meeting/start?with_mic=true${force ? '&force=true' : ''}`);
    // The API arms rather than captures when auto-detection is on; saying
    // "capture started" there would be wrong and hides why nothing is recording.
    console.log(started?.auto_detect
      ? '[meeting] armed — capture begins when a call is detected'
      : '[meeting] capture started');
    return { ok: true, autoDetect: Boolean(started?.auto_detect) };
  } catch (error) {
    captureStarted = false;
    console.error(`[meeting] could not start capture: ${error.message}`);
    return { error: 'Could not start capture' };
  }
}

/** Restarts only the Python API, so a profile chosen mid-walkthrough applies at once. */
async function restartApiServer() {
  if (apiProcess) {
    const previous = apiProcess;
    await new Promise((resolve) => {
      previous.once('exit', resolve);
      previous.kill();
      setTimeout(resolve, 5000);
    });
  }
  captureStarted = false;
  startApiServer();
  await waitForApi(20000);
  connectEvents();
  await loadProfile();
  return { ok: true };
}

/** Resolves true once /livez answers, false after `timeoutMs`. */
async function waitForApi(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(`${API_BASE}/livez`)).ok) return true;
    } catch { /* still booting */ }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

function openOnboarding() {
  if (onboardingWindow && !onboardingWindow.isDestroyed()) {
    onboardingWindow.focus();
    return;
  }
  if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.close();
  setOverlayMode('hidden');
  onboardingWindow = secureWindow({
    width: 760, height: 620, resizable: false, minimizable: false, fullscreenable: false,
    title: 'Welcome to Clueless',
  }, 'onboarding-preload.js');
  onboardingWindow.loadFile(path.join(__dirname, 'onboarding.html'));
  onboardingWindow.once('ready-to-show', () => { onboardingWindow.show(); app.focus({ steal: true }); });
  onboardingWindow.on('closed', () => {
    onboardingWindow = null;
    if (captureStarted && !userHidOverlay) setOverlayMode('pill');
  });
}

// Fixed deep links only; the renderer picks a key, never a URL.
const PRIVACY_PANES = {
  microphone: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone',
  audio: 'x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture',
  screen: 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
};

function fromOnboarding(event) {
  return onboardingWindow && !onboardingWindow.isDestroyed() && event.sender === onboardingWindow.webContents;
}

ipcMain.handle('onboarding:state', async (event) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  const [settings, profile] = await Promise.all([apiJson('/settings'), apiJson('/profile')]);
  const shotStep = process.argv.indexOf('--onboarding-step');
  if (shotStep !== -1) settings.ONBOARDING_STEP = process.argv[shotStep + 1];
  return {
    settings, profile,
    preflight: preflightResult?.verdict ?? null,
    totalMemGB: preflightResult?.facts?.totalMemGB ?? require('os').totalmem() / 1024 ** 3,
    health: lastHealth,
  };
});
ipcMain.handle('onboarding:save', (event, payload) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return { error: 'invalid payload' };
  return apiJson('/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
});
ipcMain.handle('onboarding:restart-api', (event) => (fromOnboarding(event) ? restartApiServer() : { error: 'forbidden' }));
ipcMain.handle('onboarding:preflight', async (event) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  preflightResult = await runPreflight({
    dataDir: DATA_DIR, projectRoot: PROJECT_ROOT, locale: readSavedSettings().SPEECH_LOCALE || 'en-US',
  });
  return preflightResult.verdict;
});
ipcMain.handle('onboarding:mic', async (event) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  const granted = await ensureMicrophoneAccess();
  if (!granted) return { granted, status: systemPreferences.getMediaAccessStatus('microphone') };
  return { granted, level: await probeMicrophone(3) };
});
ipcMain.handle('onboarding:system-audio', async (event) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  return { level: await probeSystemAudio({ seconds: 3, deadlineMs: 60000, withTone: true }) };
});
ipcMain.handle('onboarding:screen-status', (event) => (
  fromOnboarding(event) ? { status: systemPreferences.getMediaAccessStatus('screen') } : { error: 'forbidden' }));
ipcMain.handle('onboarding:open-privacy', (event, pane) => {
  if (!fromOnboarding(event) || !Object.prototype.hasOwnProperty.call(PRIVACY_PANES, pane)) return;
  shell.openExternal(PRIVACY_PANES[pane]);
});
// On first launch the walkthrough can reach this step before the API has
// finished starting; answering "no models" then would send people off to
// install Ollama they already have.
ipcMain.handle('onboarding:providers', async (event) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  if (!(await waitForApi(10000))) return { error: 'unreachable' };
  return apiJson('/providers');
});
ipcMain.handle('onboarding:test-model', (event) => (
  fromOnboarding(event) ? apiJson('/providers/test', { method: 'POST' }) : { error: 'forbidden' }));
ipcMain.handle('onboarding:start-capture', (event) => (fromOnboarding(event) ? startCapture({ force: true }) : { error: 'forbidden' }));
ipcMain.handle('onboarding:copy', (event, text) => {
  if (fromOnboarding(event) && typeof text === 'string') clipboard.writeText(text.slice(0, 2000));
});
ipcMain.handle('onboarding:finish', async (event, { tour } = {}) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  await apiJson('/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ONBOARDING_COMPLETE: '1', ONBOARDING_STEP: '0' }),
  });
  const profileChanged = (readSavedSettings().PROFILE || null) !== bootProfile;
  onboardingWindow.close();
  if (profileChanged) {
    app.relaunch({ args: [...process.argv.slice(1).filter((arg) => arg !== '--tour'), ...(tour ? ['--tour'] : [])] });
    app.exit(0);
    return { ok: true };
  }
  await startCapture();
  if (tour) startTour();
  return { ok: true };
});
ipcMain.handle('settings:walkthrough', () => openOnboarding());

/** Screenshot of one walkthrough step (`--onboarding-step N`); the API need not run. */
async function captureOnboarding(outputPath) {
  preflightResult = await runPreflight({ dataDir: DATA_DIR, projectRoot: PROJECT_ROOT });
  openOnboarding();
  await new Promise((resolve) => onboardingWindow.webContents.once('did-finish-load', resolve));
  await new Promise((resolve) => setTimeout(resolve, 2500));
  const image = await onboardingWindow.webContents.capturePage();
  require('fs').writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath}`);
  app.exit(0);
}

// ---------------------------------------------------------------- tour

// Coach marks on the real overlay: each step names an element id in
// index.html; the renderer outlines it and anchors a tooltip below.
const TOUR_STEPS = [
  { target: 'dot', title: 'Listening', text: 'Green means Clueless is transcribing. Click the pause button or use the tray to stop.' },
  { target: 'sources', title: 'Context sources', text: 'Turn sources on or off with ⌘⌥1–4. Answers only use what is switched on.' },
  { target: 'chips', title: 'One-key answers', text: '⌘⇧\\ answers from the last 90 seconds. ⌘⇧A suggests what to say, ⌘⇧E explains.' },
  { target: 'ask', title: 'Ask anything', text: '⌘⇧↩ focuses this box so you can type a question about the meeting.' },
  { target: 'hide', title: 'Out of the way', text: '⌘⇧H collapses or hides the overlay. It never appears in screen shares.' },
];
let tourIndex = -1;

function sendTourStep() {
  const step = TOUR_STEPS[tourIndex];
  send('tour:step', step ? { ...step, index: tourIndex, total: TOUR_STEPS.length } : null);
}

function startTour() {
  setOverlayMode('card');
  tourIndex = 0;
  setTimeout(sendTourStep, 400);
}

ipcMain.handle('overlay:tour-next', () => {
  if (tourIndex < 0) return;
  tourIndex += 1;
  if (tourIndex >= TOUR_STEPS.length) tourIndex = -1;
  sendTourStep();
});
ipcMain.handle('overlay:tour-end', () => {
  tourIndex = -1;
  sendTourStep();
});

// No window ever needs to navigate or open another: every page is a local file
// loaded once. Denying both means injected content (a transcript is untrusted
// input) can never turn a renderer into a browser pointed elsewhere.
app.on('web-contents-created', (_event, contents) => {
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  contents.on('will-navigate', (event) => event.preventDefault());
  contents.on('will-attach-webview', (event) => event.preventDefault());
});

app.whenReady().then(async () => {
  if (app.dock) app.dock.hide();

  if (process.argv.includes('--check-permissions')) {
    await runPermissionCheck();
    return;
  }

  const shotIndex = process.argv.indexOf('--screenshot');
  if (shotIndex !== -1 && process.argv[shotIndex + 1]) {
    if (process.argv.includes('--requirements')) await captureRequirements(process.argv[shotIndex + 1]);
    else if (process.argv.includes('--onboarding-step')) await captureOnboarding(process.argv[shotIndex + 1]);
    else await captureOverlay(process.argv[shotIndex + 1]);
    return;
  }

  preflightResult = await runPreflight({
    dataDir: DATA_DIR,
    projectRoot: PROJECT_ROOT,
    locale: readSavedSettings().SPEECH_LOCALE || 'en-US',
  });
  if (process.argv.includes('--preflight-json')) {
    const { facts, verdict } = preflightResult;
    process.stdout.write(`${JSON.stringify({ version: app.getVersion(), facts, verdict }, null, 2)}\n`);
    app.exit(verdict.ok ? 0 : 1);
    return;
  }
  console.log(`[preflight] ${preflightResult.report.split('\n')[0]}`
    + ` — ${preflightResult.verdict.warnings.length} warning(s)`);
  if (!preflightResult.verdict.ok) {
    showRequirements();
    return;
  }

  const saved = readSavedSettings();
  bootProfile = saved.PROFILE || null;
  const needsOnboarding = !truthy(saved.ONBOARDING_COMPLETE) && !process.argv.includes('--skip-onboarding');
  // On a first run the walkthrough asks for the microphone in context, after
  // explaining why, rather than as a bare system prompt at launch.
  if (!needsOnboarding) await ensureMicrophoneAccess();
  startApiServer();

  createOverlay();
  createTray();
  // Wait for the API to bind before connecting; connectEvents retries anyway.
  setTimeout(connectEvents, 2500);
  setTimeout(pollHealth, 4000);
  setInterval(pollHealth, 20000);

  if (!globalShortcut.register(HOTKEY_ASK, () => askQuestion())) {
    console.error(`[hotkey] failed to register ${HOTKEY_ASK}`);
  }
  if (!globalShortcut.register(HOTKEY_SUGGEST, () => askQuestion('suggest'))) {
    console.error(`[hotkey] failed to register ${HOTKEY_SUGGEST}`);
  }
  if (!globalShortcut.register(HOTKEY_EXPLAIN, () => askQuestion('explain'))) {
    console.error(`[hotkey] failed to register ${HOTKEY_EXPLAIN}`);
  }
  if (!globalShortcut.register(HOTKEY_TOGGLE, toggleOverlay)) {
    console.error(`[hotkey] failed to register ${HOTKEY_TOGGLE}`);
  }
  // Summarising is a deliberate action, not an automatic one: transcripts are
  // never written to disk, so a summary only exists if you ask for it.
  if (!globalShortcut.register('CommandOrControl+Shift+S', summariseMeeting)) {
    console.error('[hotkey] failed to register CommandOrControl+Shift+S');
  }
  if (!globalShortcut.register(HOTKEY_ASK_BAR, focusAskBar)) {
    console.error(`[hotkey] failed to register ${HOTKEY_ASK_BAR}`);
  }

  // Source state has to be known before the toggles are bound. Binding first
  // treated developer mode as off (initialSources was still null), so ⌘⌥1–3
  // were never registered.
  for (let attempt = 0; attempt < 15; attempt += 1) {
    try {
      const response = await apiFetch(`/sources`);
      if (response.ok) {
        initialSources = await response.json();
        sourceState = initialSources;
        updateTray();
        send('sources:update', sourceState);
        break;
      }
    } catch { /* API still booting */ }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  const developerMode = Boolean(initialSources?.developer_mode);
  for (const [accelerator, source] of Object.entries(SOURCE_HOTKEYS)) {
    // Developer sources are not offered outside developer mode, so binding their
    // shortcuts would only produce a 403 and a confusing overlay.
    if (!developerMode && DEVELOPER_SOURCES.includes(source)) continue;
    const ok = globalShortcut.register(accelerator, () => {
      // The toast confirming the toggle needs a visible overlay to land on.
      if (overlayMode === 'hidden') setOverlayMode('pill');
      toggleSource(source);
    });
    if (!ok) console.error(`[hotkey] failed to register ${accelerator} (${source})`);
    else console.log(`[hotkey] ${accelerator} → ${source}`);
  }
  console.log(`[hotkey] ask=${HOTKEY_ASK} askbar=${HOTKEY_ASK_BAR} toggle=${HOTKEY_TOGGLE} summary=CommandOrControl+Shift+S`);

  // A first run captures nothing until the walkthrough's consent step.
  if (needsOnboarding) {
    openOnboarding();
    return;
  }
  // Begin capture. The API spawns both helpers on start.
  await startCapture();
  if (process.argv.includes('--tour')) startTour();
});

ipcMain.handle('overlay:open-settings', () => openSettings());
ipcMain.handle('overlay:hide', () => {
  userHidOverlay = true;
  setOverlayMode('hidden');
});
ipcMain.handle('overlay:mode', (_event, mode) => {
  if (['hidden', 'pill', 'card'].includes(mode)) setOverlayMode(mode);
});
// The renderer's ResizeObserver reports the card's natural height; the window
// follows it so there is never empty glass below the content.
ipcMain.on('overlay:height', (_event, height) => {
  if (!Number.isFinite(height)) return;
  cardHeight = Math.round(height);
  if (overlayMode === 'card') applyBounds(false);
});
ipcMain.handle('overlay:nudge', (_event, direction) => nudgeOverlay(Math.sign(direction) || 0));
ipcMain.handle('overlay:ask', (_event, question) => {
  const text = typeof question === 'string' ? question.trim().slice(0, 500) : '';
  if (text) askQuestion('answer', text);
});
ipcMain.handle('overlay:intent', (_event, intent) => {
  if (['answer', 'suggest', 'explain'].includes(intent)) askQuestion(intent);
});
ipcMain.handle('overlay:summarise', () => summariseMeeting());
ipcMain.handle('overlay:pause', () => togglePause());
ipcMain.handle('overlay:source', (_event, source) => {
  if (Object.prototype.hasOwnProperty.call(SOURCE_HOTKEY_LABELS, source)) toggleSource(source);
});
ipcMain.handle('overlay:focus-ask', () => focusAskBar());
ipcMain.handle('overlay:release-focus', () => releaseAskFocus());
ipcMain.handle('overlay:copy', (_event, text) => {
  if (typeof text === 'string') clipboard.writeText(text);
});
ipcMain.handle('overlay:toast', (_event, text) => {
  if (typeof text === 'string') send('toast:show', { text });
});
ipcMain.handle('settings:close', () => settingsWindow?.close());

async function apiJson(pathname, options) {
  try {
    const response = await apiFetch(pathname, options);
    const body = await response.json();
    if (!response.ok) return { error: body.error ?? body.detail ?? `HTTP ${response.status}` };
    return body;
  } catch (error) {
    return { error: `API unreachable: ${error.message}` };
  }
}

ipcMain.handle('settings:get', () => apiJson('/settings'));
ipcMain.handle('settings:profile', () => apiJson('/profile'));
ipcMain.handle('settings:save', (_event, payload) => {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return { error: 'invalid payload' };
  return apiJson('/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
});
ipcMain.handle('settings:providers', () => apiJson('/providers'));
ipcMain.handle('settings:logs', (_event, name) => {
  if (!['api', 'mic', 'system'].includes(name)) return { error: 'unknown log' };
  return apiJson(`/logs/${name}`);
});
ipcMain.handle('settings:restart', () => {
  app.relaunch();
  app.exit(0);
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (socket) socket.close();
  if (apiProcess && !apiProcess.killed) apiProcess.kill();
});

app.on('window-all-closed', () => {});
