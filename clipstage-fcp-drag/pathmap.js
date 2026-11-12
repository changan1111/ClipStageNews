'use strict';
// ClipStage path map + platform adapter (v7).
//
// The server stores ONE spelling of every clip path: the host's (e.g. /Volumes/EDIT/NEWS/a.mxf, or
// C:/Volumes/EDIT/NEWS/a.mxf when the host is Windows). Each editor machine keeps a tiny map of
// host volume name -> the folder where that share is mounted HERE, e.g.  EDIT -> /Volumes/EDIT-1.
// Everything that knows how a path looks on a given OS lives in this file (the "platform adapter").
// No Electron here, so it can be unit-tested with plain Node.

const nodePath = require('path');
const fs = require('fs');

function adapter(platform) {
  const win = platform === 'win32';
  const p = win ? nodePath.win32 : nodePath.posix;
  return {
    win,
    join: (root, parts) => p.join(root, ...parts),
    isAbsolute: (x) => p.isAbsolute(x),
    // The folder that normally holds mounted shares on this OS (Windows: C:\Volumes, as the installer makes it).
    volumesRoot: () => (win ? 'C:\\Volumes' : '/Volumes'),
    sep: p.sep,
  };
}

// "/Volumes/EDIT/NEWS/a.mxf"  or  "C:\Volumes\EDIT\NEWS\a.mxf"  ->  { volume:'EDIT', rest:['NEWS','a.mxf'] }
// Anything else (no /Volumes/<share>/<file>, "." or ".." segments, NUL) -> null.
function parseHostPath(t) {
  if (typeof t !== 'string' || !t || t.includes('\0')) return null;
  const parts = t.replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length && /^[A-Za-z]:$/.test(parts[0])) parts.shift();
  if (parts.length < 3 || parts[0].toLowerCase() !== 'volumes') return null;
  if (parts.some((s) => s === '.' || s === '..')) return null;
  return { volume: parts[1], rest: parts.slice(2) };
}

// Volume names are matched case-insensitively (the indexer and fix_volume_case.py do the same).
const sameVol = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();

// A mounted share that macOS/Windows renamed because the name was taken: "EDIT-1", "EDIT 2", "EDIT_3".
function isDuplicateOf(local, host) {
  const l = String(local).toLowerCase(), h = String(host).toLowerCase();
  if (l === h || !l.startsWith(h)) return false;
  return /^[-_ ]\d+$/.test(l.slice(h.length));
}

// Which local folders could be the host volume `hostVol`?  names = folder names in the volumes root.
//  - exactly one exact match            -> { kind:'exact',   candidates:[it] }
//  - no exact, one "EDIT-1" style name  -> { kind:'renamed', candidates:[it] }
//  - several (EDIT and EDIT-1 mounted)  -> { kind:'ambiguous', candidates:[...] }  (exact first)
//  - nothing                            -> { kind:'none',   candidates:[] }
function detect(hostVol, names) {
  const exact = names.filter((n) => sameVol(n, hostVol));
  const dup = names.filter((n) => isDuplicateOf(n, hostVol)).sort();
  if (exact.length === 1 && !dup.length) return { kind: 'exact', candidates: exact };
  if (!exact.length && dup.length === 1) return { kind: 'renamed', candidates: dup };
  if (exact.length || dup.length) return { kind: 'ambiguous', candidates: [...exact, ...dup] };
  return { kind: 'none', candidates: [] };
}

class PathMap {
  // file: JSON file in the app's data folder. platform: process.platform (injectable for tests).
  constructor(file, platform, fsImpl) {
    this.file = file;
    this.ad = adapter(platform || process.platform);
    this.fs = fsImpl || fs;
    this.data = { version: 1, pairs: [] };       // pairs: [{ host:'EDIT', local:'/Volumes/EDIT-1', confirmed:true }]
    this.load();
  }

  load() {
    try {
      const j = JSON.parse(this.fs.readFileSync(this.file, 'utf8'));
      if (j && Array.isArray(j.pairs)) {
        this.data = { version: 1, pairs: j.pairs.filter((x) => x && typeof x.host === 'string' && typeof x.local === 'string') };
      }
    } catch (e) { /* first run: empty map */ }
  }

  save() {
    try {
      this.fs.mkdirSync(nodePath.dirname(this.file), { recursive: true });
      this.fs.writeFileSync(this.file, JSON.stringify(this.data, null, 2));
    } catch (e) { /* a read-only profile must not break drag */ }
  }

  pairFor(hostVol) { return this.data.pairs.find((x) => sameVol(x.host, hostVol)) || null; }

  // The editor confirmed (or picked) where `hostVol` lives on this machine.
  set(hostVol, localRoot) {
    if (!hostVol || typeof localRoot !== 'string' || !this.ad.isAbsolute(localRoot)) return false;
    const cur = this.pairFor(hostVol);
    if (cur) { cur.local = localRoot; cur.confirmed = true; } else this.data.pairs.push({ host: hostVol, local: localRoot, confirmed: true });
    this.save();
    return true;
  }

  remove(hostVol) {
    this.data.pairs = this.data.pairs.filter((x) => !sameVol(x.host, hostVol));
    this.save();
  }

  // Folder names currently under this OS's volumes root.
  localNames() {
    try {
      return this.fs.readdirSync(this.ad.volumesRoot(), { withFileTypes: true })
        .filter((d) => d.isDirectory() || d.isSymbolicLink()).map((d) => d.name);
    } catch (e) { return []; }
  }

  // For each host volume seen so far: saved pair, or a proposal that still needs ONE confirm click.
  status(hostVols) {
    const names = this.localNames();
    const root = this.ad.volumesRoot();
    return [...new Set(hostVols)].map((h) => {
      const pair = this.pairFor(h);
      if (pair && pair.confirmed) return { host: h, local: pair.local, confirmed: true, kind: 'saved', candidates: [] };
      const d = detect(h, names);
      const cands = d.candidates.map((n) => this.ad.join(root, [n]));
      return { host: h, local: cands.length === 1 ? cands[0] : '', confirmed: false, kind: d.kind, candidates: cands };
    });
  }

  // Resolve one host path for THIS machine. NEVER guesses: an unconfirmed volume is 'not_mapped'.
  // -> { state:'ok'|'bad_path'|'not_mapped', local?, host?, volume? }  (file existence is checked separately)
  resolve(hostPath) {
    const parsed = parseHostPath(hostPath);
    if (!parsed) return { state: 'bad_path', reason: 'Not a /Volumes/<share>/... path' };
    const pair = this.pairFor(parsed.volume);
    if (!pair || !pair.confirmed) return { state: 'not_mapped', volume: parsed.volume, reason: `${parsed.volume} is not set up on this computer` };
    const local = this.ad.join(pair.local, parsed.rest);
    // belt and braces: the joined path must stay under the mapped folder
    const rel = (this.ad.win ? nodePath.win32 : nodePath.posix).relative(pair.local, local);
    if (rel.startsWith('..') || this.ad.isAbsolute(rel)) return { state: 'bad_path', reason: 'Path leaves the mapped volume' };
    return { state: 'ok', local, volume: parsed.volume };
  }
}

// Check a resolved local path: ok | not_mounted (volume folder missing) | no_access | missing.
async function checkLocal(fsp, pm, hostPath) {
  const r = pm.resolve(hostPath);
  if (r.state !== 'ok') return r;
  const pair = pm.pairFor(r.volume);
  try { await fsp.access(pair.local); } catch (e) {
    return { state: 'not_mounted', volume: r.volume, local: r.local, reason: `${r.volume} is not mounted (${pair.local})` };
  }
  try { await fsp.access(r.local, fs.constants.R_OK); return { state: 'ok', local: r.local, volume: r.volume }; } catch (e) {
    if (e && (e.code === 'EACCES' || e.code === 'EPERM')) return { state: 'no_access', volume: r.volume, local: r.local, reason: 'No permission to read this file' };
    return { state: 'missing', volume: r.volume, local: r.local, reason: 'File not found on this computer' };
  }
}

module.exports = { adapter, parseHostPath, sameVol, isDuplicateOf, detect, PathMap, checkLocal };
