const { contextBridge, ipcRenderer } = require('electron');

// Exposes two tiny functions to the ClipStage page.
contextBridge.exposeInMainWorld('clipstageNative', {
  startDrag: (editor, filename, targets) => ipcRenderer.send('clipstage-start-drag', editor, filename, targets),
  openStaging: (editor) => ipcRenderer.send('clipstage-open-staging', editor),
  syncLocal: (editor, clips) => ipcRenderer.invoke('clipstage-sync-local', editor, clips),
  openLocal: (editor) => ipcRenderer.send('clipstage-open-local', editor),
  startDragPaths: (paths) => ipcRenderer.send('clipstage-start-drag-paths', paths),
});
