'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('requirementsApi', {
  get: () => ipcRenderer.invoke('requirements:get'),
  copy: () => ipcRenderer.invoke('requirements:copy'),
  quit: () => ipcRenderer.invoke('requirements:quit'),
});
