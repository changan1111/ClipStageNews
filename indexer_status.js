/*
 * ClipStage — Indexer live status (drop-in)
 *
 * Add ONE line to server/static/index.html, just before </body>:
 *     <script src="/static/indexer_status.js"></script>
 *
 * It:
 *   • takes over the existing #indexerBtn (starts the indexer via POST /admin/run-indexer)
 *   • adds a volume picker (populated from GET /admin/indexable-volumes) next to the
 *     button — "All volumes" runs the normal full delta index; tick one or several
 *     volumes (e.g. EDIT2 + PLAYOUT) to scope that one run to just those
 *     (POST /admin/run-indexer?volume=EDIT2,PLAYOUT)
 *   • shows a live bar under the header while indexing: phase, progress,
 *     per-volume counts, current scope and RUNNING TIME
 *   • shows "Last run: <date> · took <duration> · <n> clips" next to the button
 *   • picks up a run already in progress (page reload, another tab, CLI)
 *   • refreshes the current search when a run finishes
 *
 * It uses the theme CSS variables already in index.html (light / dark / midnight).
 */
(function () {
  'use strict';

  var btn = document.getElementById('indexerBtn');
  var header = document.querySelector('.header');
  if (!btn || !header) { console.warn('[indexer_status] #indexerBtn or .header not found'); return; }

  /* ── styles ─────────────────────────────────────────────── */
  var css = document.createElement('style');
  css.textContent =
    '#idxBar{display:none;flex-shrink:0;padding:7px 20px;background:var(--accent-dim);' +
    'border-bottom:1px solid var(--accent-border);color:var(--text);' +
    "font-family:'Space Mono',monospace;font-size:11px;}" +
    '#idxBar.error{background:rgba(214,67,63,.10);border-bottom-color:var(--red);}' +
    '#idxBar .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;}' +
    '#idxBar .spin{width:11px;height:11px;border:2px solid var(--border);border-top-color:var(--accent);' +
    'border-radius:50%;animation:idxspin .7s linear infinite;flex-shrink:0;}' +
    '#idxBar .grow{flex:1;min-width:120px;}' +
    '#idxBar .muted{color:var(--muted);}' +
    '#idxBar .time{color:var(--accent);font-weight:700;}' +
    '#idxTrack{height:3px;background:var(--border);margin-top:6px;border-radius:2px;overflow:hidden;position:relative;}' +
    '#idxFill{height:3px;width:0;background:var(--accent);transition:width .5s;}' +
    '#idxFill.indet{width:30%!important;position:absolute;animation:idxslide 1.2s ease-in-out infinite;}' +
    '#idxStop{font-family:\'Space Mono\',monospace;font-size:11px;padding:2px 10px;cursor:pointer;' +
    'border:1px solid var(--red);border-radius:4px;background:transparent;color:var(--red);flex-shrink:0;}' +
    '#idxStop:hover:not(:disabled){background:var(--red);color:#fff;}' +
    '#idxStop:disabled{opacity:.6;cursor:not-allowed;}' +
    '#idxLast{font-family:\'Space Mono\',monospace;font-size:10px;color:var(--muted);white-space:nowrap;}' +
    '@keyframes idxspin{to{transform:rotate(360deg)}}' +
    '@keyframes idxslide{0%{left:-30%}100%{left:100%}}' +
    '#indexerBtn:disabled,#idxScope:disabled{opacity:.5;cursor:not-allowed;}' +
    '#idxScopeWrap{display:inline-block;}' +
    '#idxScopePanel{display:none;position:fixed;z-index:3000;min-width:190px;max-height:320px;overflow:auto;' +
    'background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;' +
    'box-shadow:0 8px 24px rgba(0,0,0,.35);padding:4px;}' +
    '#idxScopePanel label{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:4px;' +
    "cursor:pointer;font-family:'Space Mono',monospace;font-size:11px;user-select:none;}" +
    '#idxScopePanel label:hover{background:var(--accent-dim);}' +
    '#idxScopePanel .sep{height:1px;background:var(--border);margin:4px 2px;}' +
    '#idxScopePanel input{accent-color:var(--accent);margin:0;}' +
    '#idxScope{font-family:\'Space Mono\',monospace;font-size:11px;padding:3px 8px;cursor:pointer;' +
    'border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);}' +
    '#idxLastWrap{display:flex;flex-direction:column;align-items:flex-end;' +
    "font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);line-height:1.5;white-space:nowrap;}";
  document.head.appendChild(css);

  /* ── DOM ────────────────────────────────────────────────── */
  var bar = document.createElement('div');
  bar.id = 'idxBar';
  bar.innerHTML =
    '<div class="row"><span class="spin" id="idxSpin"></span>' +
    '<span id="idxPhase" class="grow"></span>' +
    '<span class="muted">Running</span><span class="time" id="idxTime">0s</span>' +
    '<button id="idxStop" type="button" style="display:none" ' +
    'title="Stop this index run. Nothing is deleted - the next run carries on from the current index.">■ Stop</button></div>' +
    '<div id="idxVols" class="muted" style="margin-top:3px"></div>' +
    '<div id="idxTrack"><div id="idxFill"></div></div>';
  header.parentNode.insertBefore(bar, header.nextSibling);

  // Who is signed in + Log out are rendered by index.html (#userBox). This script only shows
  // the admin-only controls (volume picker + Sync Index) and the live status.

  // Volume picker — "All volumes" (default, full delta run) or any combination of
  // volumes/folders (tick one, two, three…) so a quick run only scans those trees.
  // Hidden unless logged in as admin (see renderAuth).
  var scopeWrap = document.createElement('span');
  scopeWrap.id = 'idxScopeWrap';
  var scopeSel = document.createElement('button');      // the dropdown button
  scopeSel.id = 'idxScope';
  scopeSel.type = 'button';
  scopeSel.title = 'Tick the volumes to index, or leave on All volumes for the normal run';
  var scopePanel = document.createElement('div');
  scopePanel.id = 'idxScopePanel';
  scopeWrap.appendChild(scopeSel);
  document.body.appendChild(scopePanel);                // fixed-position, so no header clipping
  btn.parentNode.insertBefore(scopeWrap, btn);

  var volumeList = [];          // all indexable volumes
  var picked = [];              // ticked volumes; empty = All volumes
  var volumesLoaded = false;

  function selectedVolumes() { return picked.slice(); }
  window.clipstageSelectedVolumes = selectedVolumes;   // used by Sync Index in index.html

  function scopeLabel() {
    if (!picked.length) return 'All volumes ▾';
    if (picked.length <= 2) return picked.join(' + ') + ' ▾';
    return picked.length + ' volumes ▾';
  }

  function buildPanel() {
    scopePanel.innerHTML = '';
    function row(label, checked, onChange) {
      var l = document.createElement('label');
      var c = document.createElement('input');
      c.type = 'checkbox'; c.checked = checked;
      c.addEventListener('change', function () { onChange(c.checked); });
      l.appendChild(c);
      l.appendChild(document.createTextNode(label));
      scopePanel.appendChild(l);
    }
    row('All volumes', picked.length === 0, function () { picked = []; refreshScope(); });
    var sep = document.createElement('div'); sep.className = 'sep'; scopePanel.appendChild(sep);
    volumeList.forEach(function (v) {
      row(v, picked.indexOf(v) !== -1, function (on) {
        picked = picked.filter(function (x) { return x !== v; });
        if (on) picked.push(v);
        // keep canonical order; ticking every volume is the same as All volumes
        picked = volumeList.filter(function (x) { return picked.indexOf(x) !== -1; });
        if (picked.length === volumeList.length) picked = [];
        refreshScope();
      });
    });
  }

  function refreshScope() {
    scopeSel.textContent = scopeLabel();
    buildPanel();
    renderLast(last);
  }

  function openPanel() {
    if (scopeSel.disabled) return;
    buildPanel();
    var r = scopeSel.getBoundingClientRect();
    scopePanel.style.display = 'block';
    scopePanel.style.top = (r.bottom + 4) + 'px';
    scopePanel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - scopePanel.offsetWidth - 8)) + 'px';
  }
  function closePanel() { scopePanel.style.display = 'none'; }

  scopeSel.addEventListener('click', function (e) {
    e.stopPropagation();
    if (scopePanel.style.display === 'block') closePanel(); else openPanel();
  });
  document.addEventListener('click', function (e) {
    if (!scopePanel.contains(e.target) && e.target !== scopeSel) closePanel();
  });
  window.addEventListener('resize', closePanel);

  function loadVolumes() {
    if (volumesLoaded) return;
    volumesLoaded = true;
    fetch('/admin/indexable-volumes', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        volumeList = d.volumes || [];
        picked = picked.filter(function (x) { return volumeList.indexOf(x) !== -1; });
        refreshScope();
      })
      .catch(function () { volumesLoaded = false; });
  }
  scopeSel.textContent = scopeLabel();

  var lastWrap = document.createElement('div');
  lastWrap.id = 'idxLastWrap';
  lastWrap.innerHTML = '<span id="idxLastFull"></span><span id="idxLastPartial"></span>';
  btn.parentNode.insertBefore(lastWrap, btn);

  var $ = function (id) { return document.getElementById(id); };

  /* ── helpers ────────────────────────────────────────────── */
  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + 'h ' : '') + (h || m ? m + 'm ' : '') + s + 's';
  }
  function n(x) { return (x || 0).toLocaleString(); }
  function toast(msg, type) {
    var f = window.showToast || window.toast;
    if (typeof f === 'function') { try { f(msg, type); return; } catch (e) {} }
    console.log('[indexer]', msg);
  }

  /* ── auth (named admin / editor accounts) ──────────────────
     Editors can log in too (so the UI shows who's logged in), but only an
     admin session unlocks the run-indexer controls — see require_admin in
     api.py. */
  var TOKEN_KEY = 'clipstage_token';
  var TOKEN_EXP_KEY = 'clipstage_token_exp';
  var TOKEN_USER_KEY = 'clipstage_token_user';
  var TOKEN_ROLE_KEY = 'clipstage_token_role';

  function getSession() {
    var exp = parseInt(localStorage.getItem(TOKEN_EXP_KEY) || '0', 10);
    var validThrough = localStorage.getItem('clipstage_token_valid_through');
    if (!exp || exp * 1000 < Date.now()) return null;
    var role = localStorage.getItem(TOKEN_ROLE_KEY);
    if (!validThrough || (role !== 'admin' && role !== 'editor')) return null;
    return { username: localStorage.getItem(TOKEN_USER_KEY), role: role, validThrough: validThrough };
  }
  function setSession(d) {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.setItem(TOKEN_EXP_KEY, String(Math.floor(Date.now() / 1000) + d.expires_in));
    localStorage.setItem(TOKEN_USER_KEY, d.username);
    localStorage.setItem(TOKEN_ROLE_KEY, d.role);
    localStorage.setItem('clipstage_token_valid_through', d.valid_through || '');
  }
  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_EXP_KEY);
    localStorage.removeItem(TOKEN_USER_KEY);
    localStorage.removeItem(TOKEN_ROLE_KEY);
    localStorage.removeItem('clipstage_token_valid_through');
  }
  function getToken() {
    return getSession() ? 'cookie-session' : null;
  }

  function renderAuth() {
    // Admin session -> picker + Sync Index. Anyone else -> hidden. No login link: the page's own
    // sign-in screen appears whenever there is no session.
    var s = getSession();
    var admin = !!(s && s.role === 'admin');
    scopeWrap.style.display = admin ? '' : 'none';
    btn.style.display = admin ? '' : 'none';
    if (!admin) closePanel();
  }

  function doLogin() {
    if (window.clipstageShowLogin) window.clipstageShowLogin();
  }

  function escapeHtml(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : s;
    return d.innerHTML;
  }

  renderAuth();   // logged out / editor: hidden controls + login link. Admin: full controls.
  window.clipstageRefreshIndexerAuth = renderAuth;

  window.clipstageStartIndexerStatus = function () {
    renderAuth();
    loadVolumes();
    poll();
  };
  window.clipstageStopIndexerStatus = function () {
    clearTimeout(pollT);
    if (tickT) clearInterval(tickT);
    pollT = tickT = null;
  };
  if (window.clipstageAuthenticated) window.clipstageStartIndexerStatus();

  /* ── state ──────────────────────────────────────────────── */
  var last = null;          // last status from server
  var offset = 0;           // serverNow - clientNow (seconds)
  var wasRunning = false;
  var stopRequested = false;   // Stop was clicked for the current run
  var pollT = null, tickT = null;

  function fmtWhen(ts, dur, total) {
    var d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) +
      ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) +
      (dur != null ? ' · ' + fmtDur(dur) : '') +
      (total != null ? ' · ' + n(total) + ' clips' : '');
  }

  // Two lines, visible to everyone (editors included) — the full/nightly
  // history, and (when a specific volume is picked in the dropdown) that
  // volume's own last scoped run, independently of the full-run history.
  function renderLast(s) {
    var fullEl = $('idxLastFull'), partEl = $('idxLastPartial');
    if (!fullEl) return;

    if (s && s.last_full_finished_at) {
      fullEl.textContent = 'Last full run: ' + fmtWhen(s.last_full_finished_at, s.last_full_duration_s, s.last_full_total);
    } else if (s && s.last_finished_at) {
      // Falls back to the generic "last run" field for status files written
      // before full/partial history was tracked separately.
      fullEl.textContent = 'Last run: ' + fmtWhen(s.last_finished_at, s.last_duration_s, s.last_total);
    } else {
      fullEl.textContent = 'Full run: never';
    }

    var vols = selectedVolumes();
    if (vols.length) {
      partEl.innerHTML = '';
      vols.forEach(function (vol) {
        var p = s && s.last_partial ? s.last_partial[vol] : null;
        var line = document.createElement('span');
        line.textContent = p
          ? (vol + ' only — last run: ' + fmtWhen(p.finished_at, p.duration_s, p.total))
          : (vol + ' only — never run separately');
        partEl.appendChild(line);
      });
      partEl.style.display = 'flex';
      partEl.style.flexDirection = 'column';
      partEl.style.alignItems = 'flex-end';
    } else {
      partEl.style.display = 'none';
    }
  }

  function render() {
    if (!last) return;
    var s = last;
    var fill = $('idxFill');

    if (s.state === 'running') {
      bar.className = ''; bar.style.display = 'block';
      $('idxSpin').style.display = '';
      btn.disabled = true;
      scopeSel.disabled = true;

      var elapsed = (Date.now() / 1000 + offset) - (s.started_at || 0);
      $('idxTime').textContent = fmtDur(elapsed);

      var isAdmin = !!(getSession() && getSession().role === 'admin');
      var stopping = stopRequested || s.phase === 'stopping';
      var stopBtn = $('idxStop');
      stopBtn.style.display = isAdmin ? '' : 'none';
      stopBtn.disabled = stopping;
      stopBtn.textContent = stopping ? 'Stopping…' : '■ Stop';

      var msg, pct = null;
      if (stopping) {
        msg = 'Stopping — finishing the current step…';
      } else if (s.phase === 'probing' && s.to_probe) {
        msg = 'Reading clip durations ' + n(s.probed) + ' / ' + n(s.to_probe);
        pct = 100 * (s.probed || 0) / s.to_probe;
      } else if (s.phase === 'saving' && s.to_save) {
        msg = 'Saving to index ' + n(s.saved) + ' / ' + n(s.to_save);
        pct = 100 * (s.saved || 0) / s.to_save;
      } else if (s.phase === 'pruning') {
        msg = 'Removing deleted files from index…';
      } else if (s.phase === 'scanning') {
        msg = 'Scanning volumes — ' + n(s.scanned) + ' clips found (' + n(s.unchanged) + ' unchanged)';
      } else {
        msg = 'Starting indexer…';
      }
      var scopeTxt = (s.scope && s.scope !== 'All volumes') ? ' [' + s.scope + ' only]' : '';
      $('idxPhase').textContent = '⟳ Indexing' + scopeTxt + ' · ' + msg;

      var v = s.volumes || {}, parts = [];
      Object.keys(v).forEach(function (k) { parts.push(k + ': ' + n(v[k])); });
      $('idxVols').textContent = (s.phase === 'scanning' && parts.length) ? parts.join('   ') : '';
      $('idxVols').style.display = $('idxVols').textContent ? '' : 'none';

      if (pct === null) { fill.className = 'indet'; fill.style.width = ''; }
      else { fill.className = ''; fill.style.width = pct.toFixed(1) + '%'; }
    } else if (s.state === 'error' || s.state === 'stalled') {
      bar.className = 'error'; bar.style.display = 'block';
      $('idxSpin').style.display = 'none';
      btn.disabled = false;
      scopeSel.disabled = false;
      $('idxPhase').textContent = '⚠ Indexer ' + (s.state === 'stalled' ? 'appears stalled' : 'failed') +
        (s.message ? ' — ' + s.message : '');
      var canStop = s.state === 'stalled' && !!(getSession() && getSession().role === 'admin');
      $('idxStop').style.display = canStop ? '' : 'none';
      $('idxStop').disabled = stopRequested;
      $('idxTime').textContent = fmtDur((s.updated_at || 0) - (s.started_at || 0));
      $('idxVols').style.display = 'none';
      fill.className = ''; fill.style.width = '0';
    } else {
      bar.style.display = 'none';
      $('idxStop').style.display = 'none';
      btn.disabled = false;
      scopeSel.disabled = false;
    }
  }

  function schedule() {
    clearTimeout(pollT);
    var running = last && last.state === 'running';
    pollT = setTimeout(poll, running ? 2000 : 15000);
    if (running && !tickT) tickT = setInterval(render, 1000);
    if (!running && tickT) { clearInterval(tickT); tickT = null; }
  }

  function poll() {
    fetch('/admin/index-status', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        offset = (s.now || Date.now() / 1000) - Date.now() / 1000;
        var running = s.state === 'running';
        if (!running && s.state !== 'stalled') stopRequested = false;
        if (wasRunning && !running) {
          if (s.state === 'stopped') {
            toast('■ Indexing stopped — nothing was deleted; the next run carries on from here');
          } else if (s.state === 'idle') {
            toast('✓ Indexing finished in ' + fmtDur(s.last_duration_s) + ' — ' + n(s.last_total) + ' clips', 'success');
            var si = document.getElementById('searchInput');
            if (si && si.value && typeof window.onSearch === 'function') {
              try { window.onSearch(si.value); } catch (e) {}
            }
          } else {
            toast('Indexer ' + s.state + (s.message ? ': ' + s.message : ''), 'error');
          }
        }
        wasRunning = running;
        last = s;
        renderLast(s);
        render();
        schedule();
      })
      .catch(function () { schedule(); });
  }

  function start() {
  if (btn.disabled) return;

  // No login pre-check: the user is already signed in; the server (require_admin) decides.

  btn.disabled = true;
  scopeSel.disabled = true;

  var vols = selectedVolumes();
  var vol = vols.join(', ');
  var url = '/admin/run-indexer' + (vols.length ? '?volume=' + encodeURIComponent(vols.join(',')) : '');

  fetch(url, {
    method: 'POST',
  })
    .then(function (r) {
      if (r.status === 401) {
        clearSession();
        renderAuth();
        throw new Error('Session expired — please log in again');
      }

      if (r.status === 409) {
        toast('Indexer is already running');
        return;
      }

      if (r.status === 400) {
        return r.json().then(function (d) {
          throw new Error(d.detail || 'Bad volume');
        });
      }

      if (!r.ok) {
        throw new Error('HTTP ' + r.status);
      }

      toast(
        vol
          ? 'Indexer started — ' + vol + ' only'
          : 'Indexer started — all volumes'
      );
    })
    .catch(function (e) {
      toast('Could not start indexer: ' + e.message, 'error');
    })
    .then(function () {
      btn.disabled = false;
      scopeSel.disabled = false;
      poll();
        });
  }
  /* ── Stop button (admin only; the server enforces that too) ─────────────── */
  $('idxStop').addEventListener('click', function () {
    var b = this;
    if (b.disabled) return;
    if (!window.confirm('Stop the running index?\n\nNothing is deleted. The next run continues from what is already indexed.')) return;
    stopRequested = true;
    b.disabled = true;
    b.textContent = 'Stopping…';
    fetch('/admin/stop-indexer', { method: 'POST' })
      .then(function (r) {
        if (r.status === 401) { clearSession(); renderAuth(); throw new Error('Session expired — please log in again'); }
        if (r.status === 409) { stopRequested = false; toast('No index is running'); return; }
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        toast('Stopping the index…');
      })
      .catch(function (e) {
        stopRequested = false;
        toast('Could not stop the index: ' + e.message, 'error');
      })
      .then(function () { poll(); });
  });
})();