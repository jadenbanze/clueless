'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// The settings window never talks to the API itself: main holds the API token
// and proxies a fixed set of calls, so a compromised renderer cannot reach any
// endpoint beyond these.
contextBridge.exposeInMainWorld('settingsApi', {
  close: () => ipcRenderer.invoke('settings:close'),
  get: () => ipcRenderer.invoke('settings:get'),
  profile: () => ipcRenderer.invoke('settings:profile'),
  save: (payload) => ipcRenderer.invoke('settings:save', payload),
  providers: () => ipcRenderer.invoke('settings:providers'),
  logs: (name) => ipcRenderer.invoke('settings:logs', name),
  restart: () => ipcRenderer.invoke('settings:restart'),
  runWalkthrough: () => ipcRenderer.invoke('settings:walkthrough'),
});
