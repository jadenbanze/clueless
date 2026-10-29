'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Minimal, explicitly enumerated surface. The renderer runs with
// contextIsolation and no Node integration, so this is its only privileged API.
contextBridge.exposeInMainWorld('copilot', {
  hide: () => ipcRenderer.invoke('overlay:hide'),
  onTranscript: (handler) =>
    ipcRenderer.on('transcript:turn', (_event, turn) => handler(turn)),
  onAnswer: (handler) => ipcRenderer.on('answer:result', (_event, answer) => handler(answer)),
  onAnswerStart: (handler) => ipcRenderer.on('answer:start', (_event, payload) => handler(payload)),
  onAnswerDelta: (handler) => ipcRenderer.on('answer:delta', (_event, payload) => handler(payload)),
  onStatus: (handler) => ipcRenderer.on('status:update', (_event, status) => handler(status)),
  onSources: (handler) => ipcRenderer.on('sources:update', (_event, state) => handler(state)),
  onActivity: (handler) => ipcRenderer.on('activity:update', (_event, a) => handler(a)),
});
