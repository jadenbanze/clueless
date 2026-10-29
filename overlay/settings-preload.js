'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Minimal surface: the settings window talks to the local API directly over
// HTTP, so it only needs a way to close itself.
contextBridge.exposeInMainWorld('settingsApi', {
  close: () => ipcRenderer.invoke('settings:close'),
});
