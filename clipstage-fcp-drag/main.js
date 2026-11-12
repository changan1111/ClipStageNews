const { app, BrowserWindow, ipcMain, nativeImage, shell, dialog } = require('electron');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { adapter, parseHostPath, detect } = require('./pathmap');

const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8'));
const IS_WIN = process.platform === 'win32';
const SERVER_ORIGIN = new URL(cfg.serverUrl).origin;
const SERVER_HOST = new URL(cfg.serverUrl).hostname;
const SHARE_NAME = cfg.shareName || 'staging';
// Staging share as seen on THIS (editing) computer: /Volumes/staging on a Mac, the \\HOST\staging
// network path on Windows (set "stagingMount" in config.json to use a mapped drive letter instead).
const STAGING_MOUNT = cfg.stagingMount ||
  (IS_WIN ? '\\\\' + SERVER_HOST + '\\' + SHARE_NAME : '/Volumes/' + SHARE_NAME);

// 32x32 drag icon, generated in code so no extra file is needed
const ICON_B64 = 'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAlElEQVR4nO2WMQrAIAxF09JLtIPe/1g6tNfopIOI1ORHheaNIvxnEiFEhvF3ttqhc+7WCIsxXk0BreCWyD4isEUWGPX6MmudCvQQQjinCiQJhIi4BVIR2AxwJaBDyKmGyi/oETk0BLz3z9e7UIGe4ASsBZxwIkAFuMFiAWlwgtUCVDhbAEkWqG0rWiy1kEzfCQ1jOi/ZpDcgcZsLWQAAAABJRU5ErkJggg==';
const dragIcon = nativeImage.createFromDataURL('data:image/png;base64,' + ICON_B64);

let mainWin = null;

function createWindow() {
  const win = new BrowserWindow({
    width: 1500, height: 950, title: 'ClipStage',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWin = win;
  win.loadURL(cfg.serverUrl);

  // The "Open Finder" button navigates to smb://... - hand that to the operating system instead of
  // Electron. Windows cannot open smb:// links, so they are turned into \\host\share\folder paths.
  const external = (url) => /^smb:\/\//i.test(url);
  const openSmb = (url) => {
    if (!IS_WIN) { shell.openExternal(url); return; }
    const u = new URL(url);
    const unc = '\\\\' + u.hostname + decodeURIComponent(u.pathname).replace(/\//g, '\\');
    shell.openPath(unc).then((err) => { if (err) fail('Could not open ' + unc + ': ' + err); });
  };
  win.webContents.on('will-navigate', (e, url) => {
    if (external(url)) { e.preventDefault(); openSmb(url); }
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (external(url)) { openSmb(url); return { action: 'deny' }; }
    return { action: 'allow' };
  });
}

let lastWarn = 0;
function fail(msg) {
  console.error('[ClipStage drag]', msg);
  if (Date.now() - lastWarn < 4000) return;           // don't spam popups
  lastWarn = Date.now();
  dialog.showMessageBox(mainWin, { type: 'warning', message: 'Drag to FCP failed', detail: msg });
}

// ---- real NAS paths + LOCAL STAGING FOLDER (Mac) ---------------------------------------------
// A staged item's real NAS path, as it exists on THIS computer.
// The server stores the host's spelling (/Volumes/EDIT/NEWS/a.mxf). On this computer the same share may be
// mounted under a different folder name: macOS adds a suffix when the name is already taken (EDIT -> EDIT-1,
// EDIT 2, EDIT_3). Resolution order for each path:
//   1. config.json "volumeMap" (optional manual override), e.g. { "EDIT": "/Volumes/EDIT-1" }
//   2. a folder in the volumes root with the exact name, then renamed copies (EDIT-1 ...)
// The first candidate where the FILE really exists wins, so a stale empty EDIT folder is skipped.
// Only /Volumes/<share>/... is honoured (C:\Volumes\<share>\... on Windows); anything else is ignored.
const AD = adapter(process.platform);
let _namesCache = { at: 0, names: [] };
function localVolumeNames() {
  if (Date.now() - _namesCache.at < 2000) return _namesCache.names;      // one readdir per drag, not one per clip
  let names = [];
  try {
    names = fs.readdirSync(AD.volumesRoot(), { withFileTypes: true })
      .filter((d) => d.isDirectory() || d.isSymbolicLink()).map((d) => d.name);
  } catch (e) { /* no volumes folder: nothing mounted */ }
  _namesCache = { at: Date.now(), names };
  return names;
}

// -> { path, tried[] }.  path = existing file on this computer, or null.  tried = folders looked at (for the error text).
function resolveNas(t) {
  const parsed = parseHostPath(t);
  if (!parsed) return { path: null, tried: [] };
  const roots = [];
  const override = cfg.volumeMap && typeof cfg.volumeMap === 'object'
    ? Object.keys(cfg.volumeMap).find((k) => k.toLowerCase() === parsed.volume.toLowerCase()) : null;
  if (override && typeof cfg.volumeMap[override] === 'string' && AD.isAbsolute(cfg.volumeMap[override])) {
    roots.push(cfg.volumeMap[override]);
  }
  const root = AD.volumesRoot();
  detect(parsed.volume, localVolumeNames()).candidates.forEach((n) => roots.push(AD.join(root, [n])));
  if (!roots.length) roots.push(AD.join(root, [parsed.volume]));        // nothing mounted: report the expected folder
  const tried = [];
  for (const r of [...new Set(roots)]) {
    const full = AD.join(r, parsed.rest);
    tried.push(r);
    if (fs.existsSync(full)) return { path: full, tried };
  }
  return { path: null, tried };
}

// Kept for the callers below: the local path of the real file, or null if it is not reachable from here.
function realNasPath(t) { return resolveNas(t).path; }

// Explains a failed lookup using the first clip: which share it needs and which folders were checked.
function nasHint(paths) {
  const first = (Array.isArray(paths) ? paths : [paths]).find((p) => parseHostPath(p));
  if (!first) return 'The clip path is not a /Volumes/<share>/... path.';
  const parsed = parseHostPath(first);
  const r = resolveNas(first);
  return 'Share "' + parsed.volume + '" was looked for in: ' + r.tried.join(', ') + '. ' +
    'Check that the NAS share is mounted (a renamed mount such as ' + parsed.volume + '-1 is accepted), ' +
    'or set "volumeMap" in config.json.';
}

const LOCAL_ROOT = cfg.localFolder || path.join(os.homedir(), 'ClipStage');
const isSafeName = (s) => typeof s === 'string' && s && !s.includes('/') && !s.includes('\\') && !s.includes('\0') && s !== '.' && s !== '..';
const localDirFor = (editor) => path.join(LOCAL_ROOT, editor);

// Make <LOCAL_ROOT>/<editor> hold exactly one symlink per staged clip whose real file exists on this Mac.
// Only symlinks are created/removed here - a real file in that folder is never touched.
function syncLocalLinks(editor, clips) {
  const dir = localDirFor(editor);
  fs.mkdirSync(dir, { recursive: true });
  const want = new Map();                       // clip name -> real path
  const missing = [];                           // staged, but the real file is not on this Mac
  for (const c of clips) {
    if (!c || !isSafeName(c.name) || !c.target) continue;     // no target = a real file in the share (copy mode)
    const real = realNasPath(c.target);
    if (real && fs.existsSync(real)) want.set(c.name, real); else missing.push(c.name);
  }
  for (const n of fs.readdirSync(dir)) {        // drop links that are no longer staged
    const p = path.join(dir, n);
    let st; try { st = fs.lstatSync(p); } catch (e) { continue; }
    if (st.isSymbolicLink() && !want.has(n)) { try { fs.unlinkSync(p); } catch (e) {} }
  }
  let linked = 0;
  for (const [n, real] of want) {
    const p = path.join(dir, n);
    let st = null; try { st = fs.lstatSync(p); } catch (e) {}
    if (st && !st.isSymbolicLink()) { missing.push(n); continue; }          // a real file: leave it alone
    if (st) {
      let cur = null; try { cur = fs.readlinkSync(p); } catch (e) {}
      if (cur === real) { linked++; continue; }
      try { fs.unlinkSync(p); } catch (e) {}
    }
    try { fs.symlinkSync(real, p); linked++; } catch (e) { missing.push(n); }
  }
  return { dir, linked, missing };
}

function fromServerPage(event) {
  try {
    const u = event.senderFrame ? event.senderFrame.url : event.sender.getURL();
    return new URL(u).origin === SERVER_ORIGIN;
  } catch (e) { return false; }
}

// Find the staging mount on THIS computer: configured path first, then any /Volumes/staging* (Mac)
// or the \\host\share network path (Windows) that has the editor folder.
function findEditorDir(editor) {
  const cands = [STAGING_MOUNT];
  if (IS_WIN) {
    cands.push('\\\\' + SERVER_HOST + '\\' + SHARE_NAME);
  } else {
    try {
      fs.readdirSync('/Volumes').forEach((v) => {
        if (/^staging/i.test(v)) cands.push(path.join('/Volumes', v));
      });
    } catch (e) {}
  }
  for (const c of cands) {
    const dir = path.join(c, editor);
    if (fs.existsSync(dir)) return dir;
  }
  return null;
}

// Only honour drags that come from the ClipStage page itself.
ipcMain.on('clipstage-start-drag', (event, editor, filenames, targets) => {
  try {
    const frameUrl = event.senderFrame ? event.senderFrame.url : event.sender.getURL();
    if (new URL(frameUrl).origin !== SERVER_ORIGIN) {
      return fail('Page origin ' + new URL(frameUrl).origin + ' does not match serverUrl ' + SERVER_ORIGIN + ' in config.json.');
    }
    const safe = (s) => typeof s === 'string' && s && !s.includes('/') && !s.includes('\\') && s !== '.' && s !== '..';
    const list = (Array.isArray(filenames) ? filenames : [filenames]).slice(0, 200);
    if (!safe(editor) || !list.length || !list.every(safe)) {
      return fail('Invalid editor or file name: ' + JSON.stringify([editor, list]));
    }

    // 1) preferred: the real NAS file from this computer's own NAS mount
    // 2) fallback: the file inside the staging share (works for copied files and when links do survive)
    const dir = findEditorDir(editor);
    const tl = Array.isArray(targets) ? targets : [];
    const files = [];
    list.forEach((f, i) => {
      const real = realNasPath(tl[i]);
      if (real && fs.existsSync(real)) { files.push(real); return; }
      if (dir && fs.existsSync(path.join(dir, f))) files.push(path.join(dir, f));
    });
    if (!files.length) {
      return fail('No clip file found on this computer. ' + nasHint(tl) +
        (dir ? ' Staging folder: ' + dir : ' The staging share is not mounted either.'));
    }
    if (dragIcon.isEmpty()) return fail('Drag icon failed to load.');
    event.sender.startDrag({ files, file: files[0], icon: dragIcon });
  } catch (err) {
    fail('startDrag error: ' + (err && err.message ? err.message : err));
  }
});

// "Open staging folder" button (Windows editors): open the editor's folder in Explorer.
ipcMain.on('clipstage-open-staging', (event, editor) => {
  try {
    const frameUrl = event.senderFrame ? event.senderFrame.url : event.sender.getURL();
    if (new URL(frameUrl).origin !== SERVER_ORIGIN) return;
    if (typeof editor !== 'string' || !editor || /[\\/]/.test(editor) || editor === '.' || editor === '..') return;
    const dir = findEditorDir(editor);
    if (!dir) return fail('Cannot find the staging folder for "' + editor + '". Is the staging share connected?');
    shell.openPath(dir).then((err) => { if (err) fail('Could not open ' + dir + ': ' + err); });
  } catch (err) {
    fail('open staging error: ' + (err && err.message ? err.message : err));
  }
});

// Drag search results straight into FCP: real NAS paths only, from this computer's own NAS mounts.
ipcMain.on('clipstage-start-drag-paths', (event, paths) => {
  try {
    if (!fromServerPage(event)) {
      return fail('Drag refused: the page origin does not match serverUrl in config.json.');
    }
    const list = (Array.isArray(paths) ? paths : [paths]).slice(0, 200);
    const files = [];
    for (const p of list) {
      const real = realNasPath(p);
      if (real && fs.existsSync(real)) files.push(real);
    }
    if (!files.length) {
      return fail('No clip file found on this computer. ' + nasHint(list));
    }
    if (dragIcon.isEmpty()) return fail('Drag icon failed to load.');
    event.sender.startDrag({ files, file: files[0], icon: dragIcon });
  } catch (err) {
    fail('startDragPaths error: ' + (err && err.message ? err.message : err));
  }
});

// Keep the local links folder in step with the staging panel (Mac desktop app).
ipcMain.handle('clipstage-sync-local', (event, editor, clips) => {
  try {
    if (!fromServerPage(event)) return { ok: false, reason: 'origin' };
    if (IS_WIN) return { ok: false, reason: 'windows' };
    if (!isSafeName(editor) || !Array.isArray(clips)) return { ok: false, reason: 'bad request' };
    return Object.assign({ ok: true }, syncLocalLinks(editor, clips.slice(0, 500)));
  } catch (err) {
    return { ok: false, reason: String(err && err.message ? err.message : err) };
  }
});

// "Open in Finder": open the local links folder.
ipcMain.on('clipstage-open-local', (event, editor) => {
  try {
    if (!fromServerPage(event) || IS_WIN || !isSafeName(editor)) return;
    const dir = localDirFor(editor);
    fs.mkdirSync(dir, { recursive: true });
    shell.openPath(dir).then((err) => { if (err) fail('Could not open ' + dir + ': ' + err); });
  } catch (err) {
    fail('open local folder error: ' + (err && err.message ? err.message : err));
  }
});

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
