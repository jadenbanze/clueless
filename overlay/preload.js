'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Minimal, explicitly enumerated surface. The renderer runs with
// contextIsolation and no Node integration, so this is its only privileged API.
const on = (channel) => (handler) => ipcRenderer.on(channel, (_event, payload) => handler(payload ?? {}));

contextBridge.exposeInMainWorld('copilot', {
  // Actions
  hide: () => ipcRenderer.invoke('overlay:hide'),
  setMode: (mode) => ipcRenderer.invoke('overlay:mode', mode),
  reportHeight: (height) => ipcRenderer.send('overlay:height', height),
  nudge: (direction) => ipcRenderer.invoke('overlay:nudge', direction),
  ask: (question) => ipcRenderer.invoke('overlay:ask', question),
  runIntent: (intent) => ipcRenderer.invoke('overlay:intent', intent),
  summarise: () => ipcRenderer.invoke('overlay:summarise'),
  togglePause: () => ipcRenderer.invoke('overlay:pause'),
  toggleSource: (name) => ipcRenderer.invoke('overlay:source', name),
  focusAskBar: () => ipcRenderer.invoke('overlay:focus-ask'),
  releaseFocus: () => ipcRenderer.invoke('overlay:release-focus'),
  copy: (text) => ipcRenderer.invoke('overlay:copy', text),
  toast: (text) => ipcRenderer.invoke('overlay:toast', text),
  openSettings: () => ipcRenderer.invoke('overlay:open-settings'),

  // Events
  onTranscript: on('transcript:turn'),
  onAnswer: on('answer:result'),
  onAnswerStart: on('answer:start'),
  onAnswerDelta: on('answer:delta'),
  onStatus: on('status:update'),
  onSources: on('sources:update'),
  onActivity: on('activity:update'),
  onMode: on('mode:set'),
  onConfig: on('overlay:config'),
  onToast: on('toast:show'),
  onAskFocus: on('ask:focus'),
  onAskBlur: on('ask:blur'),
  onHealth: on('health:update'),
  onProfile: on('profile:update'),
  onTour: on('tour:step'),
  tourNext: () => ipcRenderer.invoke('overlay:tour-next'),
  tourEnd: () => ipcRenderer.invoke('overlay:tour-end'),
});
