'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const on = (channel) => (handler) => ipcRenderer.on(channel, (_event, payload) => handler(payload ?? {}));

contextBridge.exposeInMainWorld('settingsApi', {
  close: () => ipcRenderer.invoke('settings:close'),
  get: () => ipcRenderer.invoke('settings:get'),
  profile: () => ipcRenderer.invoke('settings:profile'),
  save: (payload) => ipcRenderer.invoke('settings:save', payload),
  saveCredentials: (payload) => ipcRenderer.invoke('settings:save-credentials', payload),
  providers: () => ipcRenderer.invoke('settings:providers'),
  testModel: () => ipcRenderer.invoke('settings:test-model'),
  indexStatus: () => ipcRenderer.invoke('settings:index-status'),
  runIndex: (options) => ipcRenderer.invoke('settings:run-index', options),
  harvestVocab: () => ipcRenderer.invoke('settings:harvest-vocab'),
  requestCalendar: () => ipcRenderer.invoke('settings:request-calendar'),
  pickFolder: () => ipcRenderer.invoke('settings:pick-folder'),
  pickFile: (kind) => ipcRenderer.invoke('settings:pick-file', kind),
  logs: (name) => ipcRenderer.invoke('settings:logs', name),
  restart: () => ipcRenderer.invoke('settings:restart'),
  runWalkthrough: () => ipcRenderer.invoke('settings:walkthrough'),
  onIndexStatus: on('settings:index-event'),
});
