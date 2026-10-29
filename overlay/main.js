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
  Tray, Menu, dialog, nativeImage,
} = require('electron');
const { spawn } = require('child_process');
const path = require('path');

// In development __dirname is overlay/, so the project root is one level up.
// In a packaged build __dirname is inside app.asar, where '..' is meaningless —
// the helpers and server live in Resources/ instead. Getting this wrong makes
// the packaged app fail to find either, with no useful error.
const PROJECT_ROOT = app.isPackaged
  ? process.resourcesPath
  : path.join(__dirname, '..');
const API_BASE = 'http://127.0.0.1:8766';
const API_WS = 'ws://127.0.0.1:8766/events';

// Verified free: macOS has only four enabled symbolic hotkeys (79-82, the
// Mission Control space switches) and Teams does not bind either of these.
const HOTKEY_ASK = 'CommandOrControl+Shift+\\';
// Per-intent actions, following the pattern every mature Clueless-style tool uses.
const HOTKEY_SUGGEST = 'CommandOrControl+Shift+A';
const HOTKEY_EXPLAIN = 'CommandOrControl+Shift+E';
const HOTKEY_TOGGLE = 'CommandOrControl+Shift+H';

// Source toggles. Which references are relevant depends entirely on what the
// meeting is about, and no automatic relevance gate is reliable here, so the
// user decides.
const DEVELOPER_SOURCES = ['jira', 'confluence', 'code'];
// Populated once the API answers /sources, before hotkeys are bound.
let initialSources = null;

const SOURCE_HOTKEYS = {
  'CommandOrControl+Shift+1': 'jira',
  'CommandOrControl+Shift+2': 'confluence',
  'CommandOrControl+Shift+3': 'code',
  'CommandOrControl+Shift+4': 'screen',
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

function startApiServer() {
  // Prefer the bundled relocatable Python so a packaged build needs nothing
  // installed. PyInstaller was tried first and abandoned: its bootloader hung
  // before Python started, and the one-file binary was 363 MB — larger than the
  // whole app — for the same dependency set this ships directly.
  const bundled = path.join(PROJECT_ROOT, 'native', 'build', 'python', 'bin', 'python3');
  const interpreter = require('fs').existsSync(bundled) ? bundled : 'python3';

  apiProcess = spawn(interpreter, ['-m', 'server.api'], {
    cwd: PROJECT_ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  // Mirror API output to a file as well as stdout. A packaged app's stdout goes
  // nowhere useful, which made a failed server spawn impossible to diagnose.
  const logPath = path.join(require('os').tmpdir(), 'clueless-api.log');
  const logStream = require('fs').createWriteStream(logPath, { flags: 'w' });
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
    socket = new WebSocket(API_WS);
  } catch (error) {
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    console.log('[ws] connected');
    capturing = true;
    updateTray();
    send('status:update', { capturing: true });
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
    } else if (message.type === 'answer_start') {
      send('answer:start', { question: message.question });
    } else if (message.type === 'answer_delta') {
      send('answer:delta', { text: message.text });
    } else if (message.type === 'answer') {
      send('answer:result', { text: message.text, citations: message.citations });
    } else if (message.type === 'activity') {
      send('activity:update', { speaker: message.speaker, speaking: message.speaking });
    } else if (message.type === 'paused') {
      paused = Boolean(message.paused);
      updateTray();
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
      const response = await fetch(`${API_BASE}${pathname}`, { method: 'POST' });
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
    const response = await fetch(`${API_BASE}/screen`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image }),
    });
    const result = await response.json();
    console.log(`[screen] ${JSON.stringify(result).slice(0, 160)}`);
    if (!result.ok) {
      send('answer:result', {
        text:
          'Could not read the screen. Grant Screen Recording to Clueless in '
          + 'System Settings > Privacy & Security, then try again.',
        citations: [],
      });
      showOverlay();
    }
    return result;
  } catch (error) {
    console.error(`[screen] capture failed: ${error.message}`);
    return { ok: false, reason: error.message };
  }
}

async function askQuestion(intent = 'answer') {
  showOverlay();
  // The POST returns immediately; the answer arrives over the WebSocket as
  // answer_start / answer_delta / answer frames.
  try {
    const response = await fetch(`${API_BASE}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 90s of transcript is enough to answer what was just said, and keeps the
      // prompt short enough to stay responsive.
      body: JSON.stringify({ window_seconds: 90, intent }),
    });
    const result = await response.json();
    if (result.text) {
      // Non-streaming path (e.g. no audio captured yet) still returns a body.
      send('answer:result', { text: result.text, citations: result.citations ?? [] });
    }
  } catch (error) {
    send('answer:result', { text: `API unreachable: ${error.message}`, citations: [] });
  }
}

async function summariseMeeting() {
  showOverlay();
  send('answer:result', { text: 'Summarising…', citations: [] });
  try {
    const response = await fetch(`${API_BASE}/summary`, { method: 'POST' });
    const summary = await response.json();
    lastSummary = summary.text ?? null;
    updateTray();
    send('answer:result', { text: summary.text, citations: [] });
  } catch (error) {
    send('answer:result', { text: `API unreachable: ${error.message}`, citations: [] });
  }
}

async function togglePause() {
  try {
    const endpoint = paused ? '/resume' : '/pause';
    const response = await fetch(`${API_BASE}${endpoint}`, { method: 'POST' });
    const state = await response.json();
    paused = Boolean(state.paused);
    capturing = !paused && capturing;
    updateTray();
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
    width: 620,
    height: 720,
    title: 'Clueless Settings',
    resizable: true,
    minimizable: true,
    webPreferences: {
      preload: path.join(__dirname, 'settings-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
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
    ['jira', '⌘⇧1'], ['confluence', '⌘⇧2'], ['code', '⌘⇧3'], ['screen', '⌘⇧4'],
  ].map(([name, accel]) => ({
    label: `${name}  (${accel})`,
    type: 'checkbox',
    checked: Boolean(sourceState[name]),
    click: async () => {
      try {
        const response = await fetch(`${API_BASE}/sources/${name}/toggle`, { method: 'POST' });
        const state = await response.json();
        sourceState = state;
        send('sources:update', state);
        updateTray();
      } catch (error) {
        console.error(`[sources] ${error.message}`);
      }
    },
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
    { label: 'Ask  (⌘⇧\\)', click: askQuestion },
    { label: 'Summarise  (⌘⇧S)', click: summariseMeeting },
    { label: 'Save summary…', click: saveSummary, enabled: Boolean(lastSummary) },
    { type: 'separator' },
    { label: 'Context sources', submenu: sourceItems },
    { type: 'separator' },
    { label: 'Settings…', click: openSettings },
    { type: 'separator' },
    { label: 'Show overlay  (⌘⇧H)', click: showOverlay },
    { type: 'separator' },
    { label: 'Quit Clueless', role: 'quit' },
  ]));
}

/**
 * Places the panel directly under the notch, horizontally centred.
 *
 * `workArea` (not `bounds`) is what accounts for the menu bar, so on a notched
 * Mac the panel sits flush beneath the notch rather than under it. Falling back
 * to bounds keeps it on screen for displays without a menu bar inset.
 */
function notchBounds(width, height) {
  const { screen } = require('electron');
  const display = screen.getPrimaryDisplay();
  const area = display.workArea;
  // Flush against workArea.y looked cramped — the panel read as glued to the
  // menu bar. A small gap lets the notch tab breathe and looks deliberate.
  const TOP_GAP = 14;
  return {
    x: Math.round(area.x + (area.width - width) / 2),
    y: Math.round(area.y + TOP_GAP),
    width,
    height,
  };
}

function createOverlay() {
  const bounds = notchBounds(460, 380);
  overlayWindow = new BrowserWindow({
    ...bounds,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: true,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: false,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // The whole point: excluded from screen capture, so nobody you are sharing
  // your screen with can see it.
  overlayWindow.setContentProtection(true);

  // 'screen-saver' keeps it above full-screen apps, including Teams.
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  overlayWindow.loadFile(path.join(__dirname, 'index.html'));
  return overlayWindow;
}

function showOverlay() {
  if (!overlayWindow) return;
  // showInactive: never steal focus from the meeting.
  overlayWindow.showInactive();
}

function toggleOverlay() {
  if (!overlayWindow) return;
  if (overlayWindow.isVisible()) overlayWindow.hide();
  else showOverlay();
}

/** Self-test used by `npm run check-permissions`; see native/check-permissions.sh. */
async function runPermissionCheck() {
  const { execFile } = require('child_process');
  const helper = path.join(PROJECT_ROOT, 'native', 'build', 'CluelessTap.app', 'Contents', 'MacOS', 'audiotap');
  const micOK = await ensureMicrophoneAccess();

  const probe = (args) =>
    new Promise((resolve) => {
      execFile(helper, args, { timeout: 20000 }, (error, stdout, stderr) => {
        const peaks = {};
        for (const line of `${stderr}`.split('\n')) {
          const match = line.match(/PROBE \[(\w+)\].*peak=([0-9.e-]+)/);
          if (match) peaks[match[1]] = parseFloat(match[2]);
        }
        resolve({ peaks, output: `${stderr}` });
      });
    });

  console.log('[check] probing microphone (helper as child)…');
  const micProbe = await probe(['--global', '--mic', '--probe', '5']);
  console.log('[check] probing system audio (helper via LaunchServices)…');
  const sysProbe = await new Promise((resolve) => {
    const fs = require('fs');
    const logPath = path.join(require('os').tmpdir(), `mc_tap_${Date.now()}.log`);
    const child = spawn('open', ['-n', '-a', path.join(PROJECT_ROOT, 'native', 'build', 'CluelessTap.app'),
      '--args', '--global', '--probe', '5', '--log', logPath]);
    child.on('close', () => {
      const deadline = Date.now() + 14000;
      const poll = setInterval(() => {
        let text = '';
        try { text = fs.readFileSync(logPath, 'utf8'); } catch { /* not yet */ }
        if (/RESULT:/.test(text) || Date.now() > deadline) {
          clearInterval(poll);
          const peaks = {};
          for (const line of text.split('\n')) {
            const match = line.match(/PROBE \[(\w+)\].*peak=([0-9.e-]+)/);
            if (match) peaks[match[1]] = parseFloat(match[2]);
          }
          try { fs.unlinkSync(logPath); } catch { /* ignore */ }
          resolve({ peaks });
        }
      }, 400);
    });
  });

  const mic = micProbe.peaks?.mic;
  const system = sysProbe.peaks?.system;
  console.log('\n===== PERMISSION CHECK =====');
  console.log(`microphone grant: ${micOK ? 'granted' : 'NOT granted'}`);
  console.log(`mic peak (child):        ${mic === undefined ? 'absent' : mic}`);
  console.log(`system peak (LaunchServices): ${system === undefined ? 'absent' : system}`);
  const bothWork = mic > 0 && system > 0;
  console.log(bothWork ? 'RESULT: SUCCESS — both streams capturable' : 'RESULT: INCOMPLETE');
  app.exit(bothWork ? 0 : 1);
}

/**
 * Renders the overlay offscreen and writes a PNG.
 *
 * `screencapture` needs Screen Recording permission, which is not granted here,
 * and capturing the desktop would not prove what the overlay itself renders.
 * webContents.capturePage() needs no permission and captures the real DOM.
 */
async function captureOverlay(outputPath) {
  createOverlay();
  overlayWindow.showInactive();

  // Populate with representative content so the screenshot exercises the
  // transcript rows, the answer pane and the citation list.
  await new Promise((resolve) => overlayWindow.webContents.once('did-finish-load', resolve));
  overlayWindow.webContents.send('status:update', { capturing: true });
  overlayWindow.webContents.send('transcript:turn', {
    speaker: 'them',
    text: 'Are we still blocked on the Kafka authentication failure for the restaurant profile service?',
  });
  overlayWindow.webContents.send('transcript:turn', {
    speaker: 'me',
    text: 'Shweta is investigating the JWT processing error.',
  });
  overlayWindow.webContents.send('transcript:turn', {
    speaker: 'them',
    text: 'Which ticket covers the audit write path remediation?',
  });
  if (process.argv.includes('--streaming')) {
    // Mid-stream state: question line, partial answer, blinking caret, no
    // citations yet. Verifies the streaming UI rather than the settled one.
    overlayWindow.webContents.send('answer:start', {
      question: 'Which ticket covers the audit write path remediation?',
    });
    overlayWindow.webContents.send('answer:delta', {
      text: 'The ticket that covers the audit write path remediation is ',
    });
  } else {
    overlayWindow.webContents.send('answer:result', {
      text: 'PROJ-2879 covers the audit write path remediation. It is the runbook and alerts task, not the read-path spike.',
      citations: [
        'PROJ-2879 — Create audit write-path runbook and alerts',
        'example-api/docs/runbooks/audit-write-path.md',
      ],
    });
  }

  await new Promise((resolve) => setTimeout(resolve, 1200));
  const image = await overlayWindow.webContents.capturePage();
  require('fs').writeFileSync(outputPath, image.toPNG());
  console.log(`[screenshot] wrote ${outputPath} (${image.getSize().width}x${image.getSize().height})`);
  app.exit(0);
}

app.whenReady().then(async () => {
  if (app.dock) app.dock.hide();

  if (process.argv.includes('--check-permissions')) {
    await runPermissionCheck();
    return;
  }

  const shotIndex = process.argv.indexOf('--screenshot');
  if (shotIndex !== -1 && process.argv[shotIndex + 1]) {
    await captureOverlay(process.argv[shotIndex + 1]);
    return;
  }

  await ensureMicrophoneAccess();
  startApiServer();

  createOverlay();
  createTray();
  // Wait for the API to bind before connecting; connectEvents retries anyway.
  setTimeout(connectEvents, 2500);

  if (!globalShortcut.register(HOTKEY_ASK, askQuestion)) {
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

  const developerMode = Boolean(initialSources?.developer_mode);
  for (const [accelerator, source] of Object.entries(SOURCE_HOTKEYS)) {
    // Developer sources are not offered outside developer mode, so binding their
    // shortcuts would only produce a 403 and a confusing overlay.
    if (!developerMode && DEVELOPER_SOURCES.includes(source)) continue;
    const ok = globalShortcut.register(accelerator, async () => {
      try {
        const response = await fetch(`${API_BASE}/sources/${source}/toggle`, { method: 'POST' });
        const state = await response.json();
        send('sources:update', state);
        showOverlay();

        // Turning screen context on captures immediately, so the next question
        // already has it. Capture-on-enable rather than continuous capture:
        // cheaper, and it cannot quietly collect things you did not mean to share.
        if (source === 'screen' && state.screen) {
          await captureScreenContext();
        }
      } catch (error) {
        console.error(`[sources] toggle failed: ${error.message}`);
      }
    });
    if (!ok) console.error(`[hotkey] failed to register ${accelerator} (${source})`);
  }

  // Fetch the source state before binding hotkeys, so the bindings match what
  // this mode actually offers.
  for (let attempt = 0; attempt < 15; attempt += 1) {
    try {
      const response = await fetch(`${API_BASE}/sources`);
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
  console.log(`[hotkey] ask=${HOTKEY_ASK} toggle=${HOTKEY_TOGGLE} summary=CommandOrControl+Shift+S`);

  // Begin capture. The API spawns both helpers on start.
  try {
    const started = await postWithRetry('/meeting/start?with_mic=true');
    // The API arms rather than captures when auto-detection is on; saying
    // "capture started" there would be wrong and hides why nothing is recording.
    console.log(started?.auto_detect
      ? '[meeting] armed — capture begins when a call is detected'
      : '[meeting] capture started');
  } catch (error) {
    console.error(`[meeting] could not start capture: ${error.message}`);
  }
});

ipcMain.handle('overlay:hide', () => overlayWindow?.hide());
ipcMain.handle('settings:close', () => settingsWindow?.close());

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (socket) socket.close();
  if (apiProcess && !apiProcess.killed) apiProcess.kill();
});

app.on('window-all-closed', () => {});
