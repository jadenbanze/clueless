'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('onboardingApi', {
  state: () => ipcRenderer.invoke('onboarding:state'),
  save: (payload) => ipcRenderer.invoke('onboarding:save', payload),
  saveCredentials: (payload) => ipcRenderer.invoke('onboarding:save-credentials', payload),
  restartApi: () => ipcRenderer.invoke('onboarding:restart-api'),
  preflight: () => ipcRenderer.invoke('onboarding:preflight'),
  microphone: () => ipcRenderer.invoke('onboarding:mic'),
  systemAudio: () => ipcRenderer.invoke('onboarding:system-audio'),
  screenStatus: () => ipcRenderer.invoke('onboarding:screen-status'),
  calendarStatus: (request) => ipcRenderer.invoke('onboarding:calendar', request),
  openPrivacy: (pane) => ipcRenderer.invoke('onboarding:open-privacy', pane),
  pickFolder: () => ipcRenderer.invoke('onboarding:pick-folder'),
  pickFile: (kind) => ipcRenderer.invoke('onboarding:pick-file', kind),
  runIndex: () => ipcRenderer.invoke('onboarding:run-index'),
  providers: () => ipcRenderer.invoke('onboarding:providers'),
  testModel: () => ipcRenderer.invoke('onboarding:test-model'),
  startCapture: () => ipcRenderer.invoke('onboarding:start-capture'),
  copy: (text) => ipcRenderer.invoke('onboarding:copy', text),
  finish: (options) => ipcRenderer.invoke('onboarding:finish', options),
  onTurn: (callback) => ipcRenderer.on('onboarding:turn', (_event, turn) => callback(turn)),
  onIndexStatus: (callback) => ipcRenderer.on('onboarding:index-event', (_event, ev) => callback(ev)),
});
