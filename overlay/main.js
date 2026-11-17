'use strict';

// Electron shell for Clueless:
//  1. Holds Microphone, AudioCapture (via CluelessTap.app), Screen & Calendar grants
//  2. Keeps the overlay excluded from screen capture via setContentProtection(true)
//  3. Owns global hotkeys, dynamic persona source toggles, Settings & Setup Wizard

const {
  app, BrowserWindow, globalShortcut, systemPreferences, ipcMain, desktopCapturer,
  Tray, Menu, dialog, nativeImage, clipboard, screen, shell,
} = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { runPreflight } = require('./preflight');

const PROJECT_ROOT = app.isPackaged
  ? process.resourcesPath
  : path.join(__dirname, '..');
const DATA_DIR = app.isPackaged
  ? app.getPath('userData')
  : PROJECT_ROOT;
const API_BASE = 'http://127.0.0.1:8766';
const API_WS = 'ws://127.0.0.1:8766/events';
const API_TOKEN = require('crypto').randomBytes(32).toString('hex');

function readSavedSettings() {
  try {
    return JSON.parse(fs.readFileSync(path.join(DATA_DIR, '.cache', 'settings.json'), 'utf8'));
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

const HOTKEY_ASK = 'CommandOrControl+Shift+\\';
const HOTKEY_SUGGEST = 'CommandOrControl+Shift+A';
const HOTKEY_EXPLAIN = 'CommandOrControl+Shift+E';
const HOTKEY_TOGGLE = 'CommandOrControl+Shift+H';
const HOTKEY_ASK_BAR = 'CommandOrControl+Shift+Return';
const SOURCE_SLOT_ACCELERATORS = [
  'CommandOrControl+Alt+1',
  'CommandOrControl+Alt+2',
  'CommandOrControl+Alt+3',
  'CommandOrControl+Alt+4',
];

const PILL_SIZE = { width: 392, height: 54 };
const CARD_WIDTH = 484;
const CARD_MIN_HEIGHT = 200;
const CARD_MAX_SHARE = 0.72;
const NUDGE_PX = 40;

let overlayWindow = null;
let apiProcess = null;
let socket = null;
let reconnectTimer = null;
let tray = null;
let lastSummary = null;
let paused = false;
let armed = false;
let capturing = false;
let settingsWindow = null;
let onboardingWindow = null;
let sourceState = {
  available: ['notes', 'screen'],
  hotkey_sources: ['notes', 'screen'],
  labels: { notes: 'Notes', screen: 'Screen' },
  notes: true,
  screen: false,
};
let lastScreenCaptureAt = 0;
let userHidOverlay = false;
let overlayMode = 'hidden';
let cardHeight = 320;
let overlayAnchor = null;
let askFocused = false;
let lastHealth = null;
let profileState = null;

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
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.mkdirSync(path.join(DATA_DIR, '.cache'), { recursive: true });
  const destEnv = path.join(DATA_DIR, '.env');
  if (app.isPackaged && !fs.existsSync(destEnv)) {
    const seed = process.env.CLUELESS_SEED_ENV;
    if (seed && fs.existsSync(seed)) {
      fs.copyFileSync(seed, destEnv);
      console.log(`[data] seeded .env into ${destEnv}`);
    }
  }
}

function startApiServer() {
  const bundled = path.join(PROJECT_ROOT, 'native', 'build', 'python', 'bin', 'python3');
  const interpreter = fs.existsSync(bundled) ? bundled : 'python3';

  ensureDataDir();
  const env = {
    ...process.env,
    CLUELESS_DATA_DIR: DATA_DIR,
    CLUELESS_API_TOKEN: API_TOKEN,
    CLUELESS_VERSION: app.getVersion(),
    OTEL_SDK_DISABLED: 'true',
  };
  if (app.isPackaged) env.PYTHONPYCACHEPREFIX = path.join(DATA_DIR, '.cache', 'pycache');

  apiProcess = spawn(interpreter, ['-m', 'server.api'], {
    cwd: PROJECT_ROOT,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const logDir = path.join(DATA_DIR, 'logs');
  fs.mkdirSync(logDir, { recursive: true, mode: 0o700 });
  const logPath = path.join(logDir, 'api-stdout.log');
  const logStream = fs.createWriteStream(logPath, { flags: 'w', mode: 0o600 });
  apiProcess.stdout.on('data', (chunk) => { process.stdout.write(`[api] ${chunk}`); logStream.write(chunk); });
  apiProcess.stderr.on('data', (chunk) => { process.stderr.write(`[api] ${chunk}`); logStream.write(chunk); });
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
    socket = new WebSocket(API_WS, [`clueless.${API_TOKEN}`]);
  } catch {
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    console.log('[ws] connected');
    loadProfile();
    loadSources();
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
    } else if (message.type === 'status') {
      if (typeof message.capturing === 'boolean') capturing = message.capturing;
      if (typeof message.armed === 'boolean') armed = message.armed;
      if (typeof message.paused === 'boolean') paused = message.paused;
      updateTray();
      send('status:update', { capturing, armed, paused });
    } else if (message.type === 'answer_start') {
      send('answer:start', { question: message.question });
    } else if (message.type === 'answer_delta') {
      send('answer:delta', { text: message.text, full: message.full });
    } else if (message.type === 'answer') {
      if (message.format === 'summary' && message.text) {
        lastSummary = message.text;
        updateTray();
      }
      send('answer:result', {
        text: message.text,
        citations: message.citations,
        citation_links: message.citation_links,
        question: message.question,
        format: message.format,
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
    } else if (message.type === 'profile') {
      profileState = message;
      send('profile:update', message);
    } else if (message.type === 'index_status') {
      if (settingsWindow && !settingsWindow.isDestroyed()) {
        settingsWindow.webContents.send('settings:index-event', message);
      }
      if (onboardingWindow && !onboardingWindow.isDestroyed()) {
        onboardingWindow.webContents.send('onboarding:index-event', message);
      }
      if (!message.running && message.result) {
        send('toast:show', { text: 'Context synced' });
      }
    }
  };

  socket.onclose = () => {
    capturing = false;
    armed = false;
    updateTray();
    send('status:update', { capturing: false, armed: false });
    scheduleReconnect();
  };
  socket.onerror = () => {};
}

async function loadProfile() {
  try {
    const response = await apiFetch('/profile');
    if (response.ok) {
      profileState = await response.json();
      send('profile:update', profileState);
    }
  } catch { /* retried on reconnect */ }
}

async function loadSources() {
  try {
    const response = await apiFetch('/sources');
    if (response.ok) {
      sourceState = await response.json();
      updateTray();
      send('sources:update', sourceState);
    }
  } catch { /* retried on reconnect */ }
}

async function pollHealth() {
  try {
    const response = await apiFetch('/health');
    const health = await response.json();
    lastHealth = health;
    if (typeof health.capturing === 'boolean') capturing = health.capturing;
    if (typeof health.armed === 'boolean') armed = health.armed;
    if (typeof health.paused === 'boolean') paused = health.paused;
    updateTray();
    send('status:update', { capturing, armed, paused });

    const provider = health.provider ?? {};
    let problem = null;
    if (provider.status === 'none') problem = provider.message || 'No local model provider is running';
    else if (provider.status === 'degraded') problem = provider.message || 'Running in degraded mode';
    send('health:update', { problem, provider });
  } catch { /* API booting */ }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectEvents();
  }, 1500);
}

async function postWithRetry(pathname, { attempts = 12, delayMs = 600 } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await apiFetch(pathname, { method: 'POST' });
      if (response.ok) return await response.json();
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  throw lastError ?? new Error('unreachable');
}

async function captureScreenContext({ silentOnEmpty = false } = {}) {
  try {
    const targetDisplay = overlayAnchor
      ? screen.getDisplayNearestPoint({ x: overlayAnchor.cx, y: overlayAnchor.y })
      : screen.getPrimaryDisplay();

    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: 1600, height: 1000 },
    });
    if (!sources.length) {
      return { ok: false, reason: 'no screen available' };
    }
    const matched = sources.find((s) => String(s.display_id) === String(targetDisplay.id)) ?? sources[0];
    const image = matched.thumbnail.toDataURL();
    const response = await apiFetch('/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image }),
    });
    const result = await response.json();
    console.log(`[screen] ok=${result.ok} chars=${result.chars ?? 0}`);
    if (result.ok) {
      lastScreenCaptureAt = Date.now();
    } else if (!silentOnEmpty) {
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

async function askQuestion(intent = 'answer', question = null, followUp = null) {
  setOverlayMode('card');

  // Auto-refresh screen OCR on "Explain screen" or when Screen context is active & stale (>10s).
  const needsFreshScreen = intent === 'explain'
    || (Boolean(sourceState?.screen) && Date.now() - lastScreenCaptureAt > 10000);
  if (needsFreshScreen && Date.now() - lastScreenCaptureAt > 6000) {
    await captureScreenContext({ silentOnEmpty: intent !== 'explain' });
  }

  try {
    const body = {
      window_seconds: 90,
      intent,
      ...(question ? { question } : {}),
      ...(followUp?.previous_question ? { previous_question: String(followUp.previous_question).slice(0, 300) } : {}),
      ...(followUp?.previous_answer ? { previous_answer: String(followUp.previous_answer).slice(0, 600) } : {}),
    };
    const response = await apiFetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (result.text) {
      send('answer:result', {
        text: result.text,
        citations: result.citations ?? [],
        citation_links: result.citation_links ?? [],
        question: question ?? '',
      });
    }
  } catch (error) {
    send('answer:result', { text: `API unreachable: ${error.message}`, citations: [] });
  }
}

async function summariseMeeting() {
  setOverlayMode('card');
  try {
    const response = await apiFetch('/summary?stream=true', { method: 'POST' });
    const summary = await response.json();
    if (summary.text) {
      lastSummary = summary.text;
      updateTray();
      send('answer:result', { text: summary.text, citations: [], format: 'summary' });
    }
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
    const label = state.labels?.[source] ?? (source[0].toUpperCase() + source.slice(1));
    send('toast:show', { text: `${label} ${state[source] ? 'on' : 'off'}` });

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
    const response = await apiFetch(endpoint, { method: 'POST' });
    const state = await response.json();
    paused = Boolean(state.paused);
    updateTray();
    send('status:update', { paused });
  } catch (error) {
    console.error(`[pause] failed: ${error.message}`);
  }
}

function openSettings() {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 840,
    height: 700,
    minWidth: 720,
    minHeight: 500,
    backgroundColor: '#1b1b20',
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

async function saveSummary() {
  if (!lastSummary) {
    dialog.showMessageBox({
      message: 'No summary yet',
      detail: 'Press ⌘⇧S to summarise the session first.',
      type: 'info',
    });
    return;
  }
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
  const result = await dialog.showSaveDialog({
    title: 'Save summary',
    defaultPath: `session-summary-${stamp}.md`,
    filters: [{ name: 'Markdown', extensions: ['md'] }],
  });
  if (result.canceled || !result.filePath) return;
  fs.writeFileSync(result.filePath, lastSummary, 'utf8');
}

function createTray() {
  tray = new Tray(nativeImage.createEmpty());
  tray.setToolTip('Clueless');
  tray.setTitle('○');
  updateTray();
}

function updateTray() {
  if (!tray) return;
  tray.setTitle(paused ? '⏸' : capturing ? '◉' : armed ? '◎' : '○');

  const available = Array.isArray(sourceState?.available)
    ? sourceState.available
    : ['notes', 'screen'];
  const hotkeys = Array.isArray(sourceState?.hotkey_sources) ? sourceState.hotkey_sources : [];
  const labels = sourceState?.labels ?? {};

  const sourceItems = available.map((name) => {
    const slot = hotkeys.indexOf(name);
    const accel = slot >= 0 && slot < 4 ? `  (⌘⌥${slot + 1})` : '';
    const title = labels[name] ?? name;
    return {
      label: `${title}${accel}`,
      type: 'checkbox',
      checked: Boolean(sourceState?.[name]),
      click: () => toggleSource(name),
    };
  });

  const statusLabel = paused
    ? 'Paused'
    : capturing
      ? 'Listening'
      : armed
        ? 'Ready (waiting for call)'
        : 'Idle';

  tray.setContextMenu(Menu.buildFromTemplate([
    { label: statusLabel, enabled: false },
    { label: paused ? 'Resume listening' : 'Pause listening', click: togglePause },
    { type: 'separator' },
    { label: 'Ask  (⌘⇧\\)', click: () => askQuestion() },
    { label: 'What should I say?  (⌘⇧A)', click: () => askQuestion('suggest') },
    { label: 'Explain screen  (⌘⇧E)', click: () => askQuestion('explain') },
    { label: 'Summarise  (⌘⇧S)', click: summariseMeeting },
    { label: 'Save summary…', click: saveSummary, enabled: Boolean(lastSummary) },
    { type: 'separator' },
    { label: 'Context sources', submenu: sourceItems },
    {
      label: 'Sync context sources now',
      click: () => apiFetch('/index', { method: 'POST' }).catch(() => {}),
    },
    { type: 'separator' },
    { label: 'Settings…', click: openSettings },
    { label: 'Run setup wizard…', click: openOnboarding },
    { type: 'separator' },
    { label: 'Show overlay  (⌘⇧H)', click: () => setOverlayMode('card') },
    { type: 'separator' },
    { label: 'Quit Clueless', role: 'quit' },
  ]));
}

function defaultAnchor() {
  const area = screen.getPrimaryDisplay().workArea;
  return { cx: Math.round(area.x + area.width / 2), y: area.y + 10 };
}

function anchorPath() {
  return path.join(app.getPath('userData'), 'overlay-position.json');
}

function loadAnchor() {
  try {
    const saved = JSON.parse(fs.readFileSync(anchorPath(), 'utf8'));
    if (Number.isFinite(saved.cx) && Number.isFinite(saved.y)) {
      const onScreen = screen.getAllDisplays().some(({ workArea: a }) =>
        saved.cx >= a.x && saved.cx <= a.x + a.width && saved.y >= a.y && saved.y <= a.y + a.height - 40);
      if (onScreen) return { cx: saved.cx, y: saved.y };
    }
  } catch { /* default */ }
  return defaultAnchor();
}

function saveAnchor() {
  try {
    fs.writeFileSync(anchorPath(), JSON.stringify(overlayAnchor));
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
  const x = Math.min(Math.max(Math.round(overlayAnchor.cx - width / 2), area.x),
    area.x + area.width - width);
  const y = Math.min(Math.max(overlayAnchor.y, area.y), area.y + area.height - height);
  return { x, y, width, height };
}

let applyingBounds = false;
function applyBounds(animate) {
  if (!overlayWindow || overlayMode === 'hidden') return;
  applyingBounds = true;
  overlayWindow.setBounds(boundsFor(overlayMode), animate);
  setTimeout(() => { applyingBounds = false; }, animate ? 300 : 40);
}

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
    acceptFirstMouse: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  overlayWindow.setContentProtection(true);
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  overlayWindow.on('moved', () => {
    if (applyingBounds) return;
    const b = overlayWindow.getBounds();
    overlayAnchor = { cx: Math.round(b.x + b.width / 2), y: b.y };
    saveAnchor();
  });

  overlayWindow.on('blur', () => {
    if (!askFocused) return;
    askFocused = false;
    overlayWindow.setFocusable(false);
    send('ask:blur');
  });

  overlayWindow.webContents.on('did-finish-load', () => {
    send('overlay:config', { maxHeight: maxCardHeight() });
    send('mode:set', { mode: overlayMode === 'hidden' ? 'pill' : overlayMode });
    send('status:update', { capturing, armed, paused });
    send('sources:update', sourceState);
    if (profileState) send('profile:update', profileState);
  });

  overlayWindow.loadFile(path.join(__dirname, 'index.html'));
  return overlayWindow;
}

function toggleOverlay() {
  if (!overlayWindow) return;
  if (overlayMode === 'card') setOverlayMode('pill');
  else if (overlayMode === 'pill') {
    userHidOverlay = true;
    setOverlayMode('hidden');
  } else setOverlayMode('pill');
}

function focusAskBar() {
  if (!overlayWindow) return;
  if (overlayMode !== 'card') setOverlayMode('card');
  askFocused = true;
  overlayWindow.setFocusable(true);
  overlayWindow.show();
  overlayWindow.focus();
  send('ask:focus');
}

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

function probeMicrophone(seconds = 4) {
  const { execFile } = require('child_process');
  const helper = path.join(HELPER_APP, 'Contents', 'MacOS', 'audiotap');
  return new Promise((resolve) => {
    execFile(helper, ['--global', '--mic', '--probe', String(seconds)], { timeout: (seconds + 15) * 1000 },
      (_error, _stdout, stderr) => resolve(parseProbe(stderr).mic ?? null));
  });
}

function probeSystemAudio({ seconds = 4, deadlineMs = 14000, withTone = false } = {}) {
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
        try { text = fs.readFileSync(logPath, 'utf8'); } catch { /* wait */ }
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

const SCREENSHOT_DEMOS = {
  developer: {
    sources: {
      jira: true, confluence: true, code: false, screen: false,
      hotkey_sources: ['jira', 'confluence', 'code', 'screen'],
      labels: { jira: 'Jira', confluence: 'Wiki', code: 'Code', screen: 'Screen' },
    },
    turns: [
      { speaker: 'them', text: 'Are we still blocked on the Kafka authentication failure for the billing service?' },
      { speaker: 'me', text: 'Priya is investigating the JWT processing error.' },
      { speaker: 'them', text: 'Which ticket covers the audit write path remediation?' },
    ],
    question: 'Which ticket covers the audit write path remediation?',
    partial: 'The ticket that covers the audit write path remediation is ',
    answer: 'PROJ-2879 covers the audit write path remediation. It is the runbook and alerts task, not the read-path spike.',
    citations: [
      { label: 'PROJ-2879 — Create audit write-path runbook and alerts', url: 'https://example.atlassian.net/browse/PROJ-2879' },
      { label: 'example-api/docs/runbooks/audit-write-path.md', url: 'https://github.com/example/example-api/blob/HEAD/docs/runbooks/audit-write-path.md#L1' },
    ],
  },
  student: {
    sources: {
      notes: true, anki: true, pubmed: true, screen: false,
      hotkey_sources: ['notes', 'anki', 'pubmed', 'screen'],
      labels: { notes: 'Notes', anki: 'Anki', pubmed: 'Papers', screen: 'Screen' },
    },
    turns: [
      { speaker: 'them', text: 'Notice how loop diuretics abolish the corticomedullary osmotic gradient.' },
      { speaker: 'me', text: 'Does that explain why urinary calcium excretion increases compared with thiazides?' },
      { speaker: 'them', text: 'Exactly — paracellular calcium reabsorption depends on the lumen-positive potential.' },
    ],
    question: 'Why do loop diuretics increase urinary calcium excretion?',
    partial: 'Loop diuretics block NKCC2 in the thick ascending limb, abolishing ',
    answer: 'Loop diuretics inhibit NKCC2 in the thick ascending limb, abolishing the lumen-positive potential that normally drives paracellular Ca2+ and Mg2+ reabsorption.',
    citations: [
      { label: 'renal-lecture-04.pdf (p. 14)', path: '/tmp/renal-lecture-04.pdf' },
      { label: 'Renal::Diuretics: Furosemide mechanism & electrolytes' },
    ],
  },
  medical: {
    sources: {
      notes: true, anki: false, screen: false,
      hotkey_sources: ['notes', 'anki', 'screen'],
      labels: { notes: 'Notes', anki: 'Anki', screen: 'Screen' },
    },
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
    sources: {
      notes: true, notion: false, calendar: true, screen: false,
      hotkey_sources: ['notes', 'notion', 'calendar', 'screen'],
      labels: { notes: 'Notes', notion: 'Notion', calendar: 'Cal', screen: 'Screen' },
    },
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

async function captureOverlay(outputPath) {
  const stateIndex = process.argv.indexOf('--state');
  const state = stateIndex !== -1 ? process.argv[stateIndex + 1] : 'card';

  createOverlay();
  await new Promise((resolve) => overlayWindow.webContents.once('did-finish-load', resolve));
  setOverlayMode(state === 'pill' ? 'pill' : 'card');

  overlayWindow.webContents.send('status:update', { capturing: true, armed: true, paused: false });
  const profileArg = process.argv.indexOf('--profile');
  const shotProfile = profileArg !== -1 ? process.argv[profileArg + 1] : 'developer';
  const demo = SCREENSHOT_DEMOS[shotProfile] ?? SCREENSHOT_DEMOS.developer;

  overlayWindow.webContents.send('sources:update', demo.sources);
  overlayWindow.webContents.send('profile:update', {
    name: shotProfile === 'medical' ? 'student' : shotProfile,
    effective_name: shotProfile,
    clinical_mode: shotProfile === 'medical',
    ui: { show_transcript: shotProfile !== 'medical' },
  });

  if (state === 'ask') {
    overlayWindow.webContents.send('ask:focus');
  }
  if (process.argv.includes('--toast')) {
    setTimeout(() => overlayWindow.webContents.send('toast:show', { text: 'Notes on' }), 900);
  }
  if (process.argv.includes('--degraded')) {
    overlayWindow.webContents.send('health:update', { problem: 'No local model provider is running' });
  }
  const tourIndexArg = process.argv.indexOf('--tour-step');
  if (tourIndexArg !== -1) {
    tourIndex = Number.parseInt(process.argv[tourIndexArg + 1], 10) || 0;
    setTimeout(sendTourStep, 700);
  }
  for (const turn of demo.turns) overlayWindow.webContents.send('transcript:turn', turn);
  if (process.argv.includes('--streaming')) {
    overlayWindow.webContents.send('answer:start', { question: demo.question });
    overlayWindow.webContents.send('answer:delta', { text: demo.partial });
  } else {
    overlayWindow.webContents.send('answer:result', {
      question: demo.question,
      text: demo.answer,
      citations: demo.citations.map((c) => c.label),
      citation_links: demo.citations,
    });
  }

  await new Promise((resolve) => setTimeout(resolve, 1100));
  const image = await overlayWindow.webContents.capturePage();
  fs.writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath} (${image.getSize().width}x${image.getSize().height})`);
  app.exit(0);
}

async function captureSettingsScreenshot(outputPath) {
  openSettings();
  await new Promise((resolve) => settingsWindow.webContents.once('did-finish-load', resolve));
  const tabIndex = process.argv.indexOf('--settings-tab');
  if (tabIndex !== -1 && process.argv[tabIndex + 1]) {
    const tab = JSON.stringify(process.argv[tabIndex + 1]);
    await settingsWindow.webContents.executeJavaScript(
      `document.querySelector('nav button[data-tab=' + ${tab} + ']')?.click()`,
    );
  }
  await new Promise((resolve) => setTimeout(resolve, 800));
  const image = await settingsWindow.webContents.capturePage();
  fs.writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath}`);
  app.exit(0);
}

// ---------------------------------------------------------------- preflight

let preflightResult = null;

function secureWindow(options, preload) {
  return new BrowserWindow({
    backgroundColor: '#1b1b20',
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
  fs.writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath}`);
  app.exit(0);
}

// ---------------------------------------------------------------- onboarding & dialogs

let captureStarted = false;

const truthy = (value) => ['1', 'true', 'yes'].includes(String(value ?? '').toLowerCase());

async function startCapture({ force = false } = {}) {
  if (captureStarted && !force) return { ok: true };
  captureStarted = true;
  try {
    const started = await postWithRetry(`/meeting/start?with_mic=true${force ? '&force=true' : ''}`);
    armed = true;
    capturing = Boolean(started?.capturing);
    updateTray();
    send('status:update', { capturing, armed, paused });
    return { ok: true, autoDetect: Boolean(started?.auto_detect) };
  } catch (error) {
    captureStarted = false;
    console.error(`[meeting] could not start capture: ${error.message}`);
    return { error: 'Could not start capture' };
  }
}

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
  await Promise.all([loadProfile(), loadSources()]);
  return { ok: true };
}

async function waitForApi(timeoutMs, intervalMs = 180) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(`${API_BASE}/livez`)).ok) return true;
    } catch { /* booting */ }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
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
    width: 780, height: 650, resizable: false, minimizable: false, fullscreenable: false,
    title: 'Welcome to Clueless',
  }, 'onboarding-preload.js');
  onboardingWindow.loadFile(path.join(__dirname, 'onboarding.html'));
  onboardingWindow.once('ready-to-show', () => { onboardingWindow.show(); app.focus({ steal: true }); });
  onboardingWindow.on('closed', () => {
    onboardingWindow = null;
    if (captureStarted && !userHidOverlay) setOverlayMode('pill');
  });
}

const PRIVACY_PANES = {
  microphone: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone',
  audio: 'x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture',
  screen: 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
  calendar: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars',
};

const FILE_FILTERS = {
  anki: [{ name: 'Anki Deck / Collection', extensions: ['apkg', 'anki2', 'anki21', 'sqlite', 'sqlite3', 'db'] }],
  bib: [{ name: 'Bibliography', extensions: ['bib', 'ris'] }],
  gguf: [{ name: 'GGUF Model', extensions: ['gguf'] }],
};

async function pickFolder(parentWindow) {
  const result = await dialog.showOpenDialog(parentWindow ?? undefined, {
    properties: ['openDirectory', 'createDirectory'],
  });
  return result.canceled || !result.filePaths.length ? null : result.filePaths[0];
}

async function pickFile(parentWindow, kind) {
  const filters = FILE_FILTERS[kind];
  if (!filters) return null;
  const result = await dialog.showOpenDialog(parentWindow ?? undefined, {
    properties: ['openFile'],
    filters,
  });
  return result.canceled || !result.filePaths.length ? null : result.filePaths[0];
}

function fromOnboarding(event) {
  return onboardingWindow && !onboardingWindow.isDestroyed() && event.sender === onboardingWindow.webContents;
}

function fromSettings(event) {
  return settingsWindow && !settingsWindow.isDestroyed() && event.sender === settingsWindow.webContents;
}

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

ipcMain.handle('onboarding:state', async (event) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  const [settings, profile] = await Promise.all([apiJson('/settings'), apiJson('/profile')]);
  const shotStep = process.argv.indexOf('--onboarding-step');
  if (shotStep !== -1) settings.ONBOARDING_STEP = process.argv[shotStep + 1];
  return {
    settings,
    profile,
    preflight: preflightResult?.verdict ?? null,
    totalMemGB: preflightResult?.facts?.totalMemGB ?? require('os').totalmem() / 1024 ** 3,
    health: lastHealth,
  };
});
ipcMain.handle('onboarding:save', async (event, payload) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return { error: 'invalid payload' };
  const res = await apiJson('/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  await Promise.all([loadProfile(), loadSources()]);
  return res;
});
ipcMain.handle('onboarding:save-credentials', (event, payload) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return { error: 'invalid payload' };
  return apiJson('/credentials', {
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
ipcMain.handle('onboarding:calendar', async (event, requestAccess) => {
  if (!fromOnboarding(event)) return { error: 'forbidden' };
  if (requestAccess) return apiJson('/calendar/permission', { method: 'POST' });
  const st = await apiJson('/index/status');
  return st?.calendar ?? { authorized: false };
});
ipcMain.handle('onboarding:open-privacy', (event, pane) => {
  if (!fromOnboarding(event) || !Object.prototype.hasOwnProperty.call(PRIVACY_PANES, pane)) return;
  shell.openExternal(PRIVACY_PANES[pane]);
});
ipcMain.handle('onboarding:pick-folder', (event) => (
  fromOnboarding(event) ? pickFolder(onboardingWindow) : null));
ipcMain.handle('onboarding:pick-file', (event, kind) => (
  fromOnboarding(event) ? pickFile(onboardingWindow, kind) : null));
ipcMain.handle('onboarding:run-index', (event) => (
  fromOnboarding(event) ? apiJson('/index', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }) : { error: 'forbidden' }));
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
  await Promise.all([loadProfile(), loadSources()]);
  onboardingWindow.close();
  await startCapture();
  if (tour) startTour();
  return { ok: true };
});
ipcMain.handle('settings:walkthrough', () => openOnboarding());

async function captureOnboarding(outputPath) {
  preflightResult = await runPreflight({ dataDir: DATA_DIR, projectRoot: PROJECT_ROOT });
  openOnboarding();
  await new Promise((resolve) => onboardingWindow.webContents.once('did-finish-load', resolve));
  await new Promise((resolve) => setTimeout(resolve, 500));
  const shotProfileIdx = process.argv.indexOf('--profile');
  if (shotProfileIdx !== -1 && process.argv[shotProfileIdx + 1]) {
    const pName = JSON.stringify(process.argv[shotProfileIdx + 1]);
    await onboardingWindow.webContents.executeJavaScript(
      `state.profile = ${pName} === 'medical' ? 'student' : ${pName}; state.clinicalMode = (${pName} === 'medical'); $('wiz-clinical').checked = state.clinicalMode; renderProfiles(); renderSourcesStep();`,
    );
  }
  await new Promise((resolve) => setTimeout(resolve, 600));
  const image = await onboardingWindow.webContents.capturePage();
  fs.writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath}`);
  app.exit(0);
}

// ---------------------------------------------------------------- tour

const TOUR_STEPS = [
  { target: 'dot', title: 'Listening & ready state', text: 'A ring means Ready; solid green means transcribing a call. Pause anytime.' },
  { target: 'sources', title: 'Persona context sources', text: 'Toggle your active sources with ⌘⌥1–4. Answers only use what is switched on.' },
  { target: 'settings-btn', title: 'Toolbar Settings', text: 'Open Settings right from here (or the menu bar) to manage sources, decks and models.' },
  { target: 'chips', title: 'One-key actions', text: '⌘⇧\\ answers from the last 90 seconds. ⌘⇧A drafts what to say, ⌘⇧E explains your screen.' },
  { target: 'ask', title: 'Ask & follow up', text: '⌘⇧↩ focuses this bar to ask anything or follow up on the current answer.' },
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
    const outPath = process.argv[shotIndex + 1];
    if (process.argv.includes('--requirements')) await captureRequirements(outPath);
    else if (process.argv.includes('--onboarding-step')) await captureOnboarding(outPath);
    else if (process.argv.includes('--settings-tab')) await captureSettingsScreenshot(outPath);
    else await captureOverlay(outPath);
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
  const needsOnboarding = !truthy(saved.ONBOARDING_COMPLETE) && !process.argv.includes('--skip-onboarding');
  if (!needsOnboarding) await ensureMicrophoneAccess();
  startApiServer();

  createOverlay();
  createTray();

  for (const [chord, handler] of [
    [HOTKEY_ASK, () => askQuestion()],
    [HOTKEY_SUGGEST, () => askQuestion('suggest')],
    [HOTKEY_EXPLAIN, () => askQuestion('explain')],
    [HOTKEY_TOGGLE, toggleOverlay],
    ['CommandOrControl+Shift+S', summariseMeeting],
    [HOTKEY_ASK_BAR, focusAskBar],
  ]) {
    if (!globalShortcut.register(chord, handler)) {
      console.error(`[hotkey] failed to register ${chord}`);
    }
  }

  // Dynamic slot hotkeys (⌘⌥1–4): always follow the active persona's hotkey_sources,
  // even after live profile changes in Settings.
  SOURCE_SLOT_ACCELERATORS.forEach((accelerator, idx) => {
    globalShortcut.register(accelerator, () => {
      const hotkeys = Array.isArray(sourceState?.hotkey_sources) ? sourceState.hotkey_sources : [];
      const target = hotkeys[idx];
      if (!target) return;
      if (overlayMode === 'hidden') setOverlayMode('pill');
      toggleSource(target);
    });
  });

  // Fast readiness wait instead of blind multi-second timers.
  await waitForApi(15000, 150);
  connectEvents();
  await Promise.all([loadSources(), loadProfile(), pollHealth()]);
  setInterval(pollHealth, 20000);

  if (needsOnboarding) {
    openOnboarding();
    return;
  }
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
ipcMain.on('overlay:height', (_event, height) => {
  if (!Number.isFinite(height)) return;
  const rounded = Math.round(height);
  if (Math.abs(rounded - cardHeight) < 2) return;
  cardHeight = rounded;
  if (overlayMode === 'card') applyBounds(false);
});
ipcMain.handle('overlay:nudge', (_event, direction) => nudgeOverlay(Math.sign(direction) || 0));
ipcMain.handle('overlay:ask', (_event, question, followUp) => {
  const text = typeof question === 'string' ? question.trim().slice(0, 500) : '';
  if (text) askQuestion('answer', text, followUp && typeof followUp === 'object' ? followUp : null);
});
ipcMain.handle('overlay:intent', (_event, intent) => {
  if (['answer', 'suggest', 'explain'].includes(intent)) askQuestion(intent);
});
ipcMain.handle('overlay:summarise', () => summariseMeeting());
ipcMain.handle('overlay:pause', () => togglePause());
ipcMain.handle('overlay:source', (_event, source) => {
  if (typeof source === 'string' && /^[a-z_]{2,20}$/.test(source)) toggleSource(source);
});
ipcMain.handle('overlay:focus-ask', () => focusAskBar());
ipcMain.handle('overlay:release-focus', () => releaseAskFocus());
ipcMain.handle('overlay:copy', (_event, text) => {
  if (typeof text === 'string') clipboard.writeText(text);
});
ipcMain.handle('overlay:toast', (_event, text) => {
  if (typeof text === 'string') send('toast:show', { text });
});
ipcMain.handle('overlay:open-citation', (_event, item) => {
  if (!item || typeof item !== 'object') return;
  if (typeof item.url === 'string') {
    try {
      const parsed = new URL(item.url);
      if (parsed.protocol === 'https:') shell.openExternal(parsed.toString());
    } catch { /* ignore invalid URL */ }
  } else if (typeof item.path === 'string' && path.isAbsolute(item.path) && fs.existsSync(item.path)) {
    shell.showItemInFolder(item.path);
  }
});

ipcMain.handle('settings:close', () => settingsWindow?.close());
ipcMain.handle('settings:get', (event) => (fromSettings(event) ? apiJson('/settings') : { error: 'forbidden' }));
ipcMain.handle('settings:profile', (event) => (fromSettings(event) ? apiJson('/profile') : { error: 'forbidden' }));
ipcMain.handle('settings:save', async (event, payload) => {
  if (!fromSettings(event)) return { error: 'forbidden' };
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return { error: 'invalid payload' };
  const res = await apiJson('/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  await Promise.all([loadProfile(), loadSources()]);
  return res;
});
ipcMain.handle('settings:save-credentials', (event, payload) => {
  if (!fromSettings(event)) return { error: 'forbidden' };
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return { error: 'invalid payload' };
  return apiJson('/credentials', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
});
ipcMain.handle('settings:providers', (event) => (fromSettings(event) ? apiJson('/providers') : { error: 'forbidden' }));
ipcMain.handle('settings:test-model', (event) => (
  fromSettings(event) ? apiJson('/providers/test', { method: 'POST' }) : { error: 'forbidden' }));
ipcMain.handle('settings:index-status', (event) => (fromSettings(event) ? apiJson('/index/status') : { error: 'forbidden' }));
ipcMain.handle('settings:run-index', (event, options) => {
  if (!fromSettings(event)) return { error: 'forbidden' };
  const body = options && typeof options === 'object' ? { refresh: Boolean(options.refresh) } : {};
  return apiJson('/index', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
});
ipcMain.handle('settings:harvest-vocab', (event) => (
  fromSettings(event) ? apiJson('/speech/harvest', { method: 'POST' }) : { error: 'forbidden' }));
ipcMain.handle('settings:request-calendar', (event) => (
  fromSettings(event) ? apiJson('/calendar/permission', { method: 'POST' }) : { error: 'forbidden' }));
ipcMain.handle('settings:pick-folder', (event) => (fromSettings(event) ? pickFolder(settingsWindow) : null));
ipcMain.handle('settings:pick-file', (event, kind) => (fromSettings(event) ? pickFile(settingsWindow, kind) : null));
ipcMain.handle('settings:logs', (event, name) => {
  if (!fromSettings(event)) return { error: 'forbidden' };
  if (!['api', 'mic', 'system'].includes(name)) return { error: 'unknown log' };
  return apiJson(`/logs/${name}`);
});
ipcMain.handle('settings:restart', (event) => {
  if (!fromSettings(event)) return;
  app.relaunch();
  app.exit(0);
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (socket) socket.close();
  if (apiProcess && !apiProcess.killed) apiProcess.kill();
});

app.on('window-all-closed', () => {});
