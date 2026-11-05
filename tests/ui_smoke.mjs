/*
 * ClipStage v5.0 UI smoke test — runs the REAL index.html and indexer_status.js in jsdom with a
 * stubbed network. Checks: bulk-notes / remove send clip IDs, HTML from the NAS is escaped,
 * Remove is admin-only, every write carries the CSRF header, and the Sync button honours
 * the volume picker.
 *
 *   cd tests && npm install jsdom      (once)
 *   node tests/ui_smoke.mjs            (from the project folder)
 */
import { JSDOM } from "jsdom";
import fs from "fs";
import { fileURLToPath } from "url";
const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const html = fs.readFileSync(here("../index.html"), "utf8").replace("__CLIPSTAGE_SMB_HOST__", '""');
const calls = [];
const HITS = [
  { id: "aaaa1111aaaa1111", filename: "RAIN_1.mxf", filename_hl: "", path: "/Volumes/EDIT/x/RAIN_1.mxf", volume: "EDIT", folder: "x", category: "x", size_mb: 1, date: "2026-01-01", duration: "00:10", notes: "", use_count: 0 },
  { id: "bbbb2222bbbb2222", filename: "RAIN_2.mxf", filename_hl: "", path: "/Volumes/INGEST/y/RAIN_2.mxf", volume: "INGEST", folder: "y", category: "y", size_mb: 2, date: "2026-01-02", duration: "00:20", notes: "", use_count: 0 },
  // hostile names coming from the NAS:
  { id: "cccc3333cccc3333", filename: "<b id=bold>evil</b>.mxf", filename_hl: "", path: 'x"><img id=pwn src=x onerror="window.__pwn=1">', volume: "<i id=ital>V</i>", folder: "<u id=und>f</u>", category: "<s id=strk>c</s>", size_mb: "<em id=em>9</em>", date: "<a id=lnk>d</a>", duration: "<mark id=mk>t</mark>", notes: "", use_count: 0 },
];
function reply(body, ok = true, status = 200) { return Promise.resolve({ ok, status, headers: new Map(), json: async () => body, text: async () => JSON.stringify(body) }); }
const stub = (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const u = String(url);
  let csrf = null; try { const h = opts.headers; csrf = h && h.get ? h.get("x-clipstage-csrf") : (h || {})["X-ClipStage-CSRF"] || null; } catch (_) {}
  calls.push({ method, url: u, csrf, body: opts.body ? JSON.parse(opts.body) : null });
  if (u.startsWith("/config")) return reply({ smb_host: "" });
  if (u.startsWith("/auth/me")) return reply({ username: "admin@x.com", role: "admin", valid_through: "2099-01-01", editor_name: "" });
  if (u.startsWith("/auth/logout")) return reply({ ok: true });
  if (u.startsWith("/search")) return reply({ hits: HITS, total: 3, truncated: false });
  if (u.startsWith("/editors")) return reply({ editors: ["ARUN"] });
  if (u.startsWith("/clips/bulk-notes")) return reply({ ok: JSON.parse(opts.body).ids, failed: [], notes: JSON.parse(opts.body).notes });
  if (method === "DELETE" && u.startsWith("/clip/")) return reply({ ok: true });
  if (u.startsWith("/admin/index-status")) return reply({ state: "idle", last_finished_at: 1, last_warning: "Prune REFUSED for EDIT <img id=warnpwn src=x>" });
  if (u.startsWith("/stage/")) return reply({ clips: [] });
  if (u.startsWith("/admin/indexable-volumes")) return reply({ volumes: ["EDIT", "PLAYOUT"] });
  if (u.startsWith("/admin/run-indexer")) return reply({ started: true });
  if (u.startsWith("/facets/volumes")) return reply({ volumes: ["EDIT", "INGEST"] });
  return reply({});
};
const dom = new JSDOM(html, {
  runScripts: "dangerously", url: "http://localhost:8000/", pretendToBeVisual: true,
  beforeParse(w) {
    w.Request = class Request {}; w.Headers = Headers; w.CSS = { escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c) }; w.fetch = stub; w.confirm = () => true; w.alert = () => {};
    w.localStorage.setItem("clipstage_token_exp", String(Math.floor(Date.now() / 1000) + 3600));
    w.localStorage.setItem("clipstage_token_role", "admin");
    w.localStorage.setItem("clipstage_token_valid_through", "2099-01-01");
    w.HTMLElement.prototype.scrollIntoView = () => {};
  },
});
const w = dom.window; const $ = (s) => w.document.querySelector(s);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
await wait(400);
let pass = 0, fail = 0;
const check = (name, cond, extra = "") => { (cond ? pass++ : fail++); console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  → " + extra : ""}`); };

await w.eval("doSearch('rain')"); await wait(200);
const cards = [...w.document.querySelectorAll(".clip-card")];
check("3 result cards rendered", cards.length === 3, `${cards.length}`);

// hostile data is inert
check("no <img onerror> injected from a hostile path", !w.document.getElementById("pwn") && w.__pwn === undefined);
for (const id of ["bold", "ital", "und", "strk", "em", "lnk", "mk"]) if (w.document.getElementById(id)) check(`hostile markup '${id}' stayed text`, false);
check("hostile filename/volume/folder/category/size/date/duration all rendered as text", !["bold","ital","und","strk","em","lnk","mk"].some(id => w.document.getElementById(id)));
check("hostile text is still visible to the user (escaped, not dropped)", w.document.body.textContent.includes("<b id=bold>evil</b>.mxf"));

// select the two normal cards by clicking them, exactly as a user would
cards[0].click(); cards[1].click();
check("selection is tracked by path", w.eval("[...selected].join('|')") === "/Volumes/EDIT/x/RAIN_1.mxf|/Volumes/INGEST/y/RAIN_2.mxf");

// BULK NOTES → must send IDs
$("#bulkNotesTextarea").value = "reviewed";
await w.eval("saveBulkNotes()"); await wait(100);
const bulk = calls.find((c) => c.url.startsWith("/clips/bulk-notes"));
check("bulk-notes sends clip IDs (not paths)", bulk && JSON.stringify(bulk.body.ids) === JSON.stringify(["aaaa1111aaaa1111", "bbbb2222bbbb2222"]), bulk ? JSON.stringify(bulk.body.ids) : "no call");

check("state-changing calls carry the CSRF header", [bulk].every(c => c && c.csrf === "1"), bulk ? String(bulk.csrf) : "no call");

// REMOVE FROM INDEX → IDs in the URL, selection cleared
await w.eval("removeSelected()"); await wait(150);
const dels = calls.filter((c) => c.method === "DELETE" && c.url.startsWith("/clip/")).map((c) => c.url);
check("remove sends clip IDs in the URL", JSON.stringify(dels) === JSON.stringify(["/clip/aaaa1111aaaa1111", "/clip/bbbb2222bbbb2222"]), dels.join(" "));
check("selection is cleared after removal", w.eval("selected.size") === 0);
check("removed clips disappear from results", w.document.querySelectorAll(".clip-card").length === 1);

// REMOVE button hidden for non-admins; remove refuses
w.localStorage.setItem("clipstage_token_role", "editor");
w.eval("selected.add('/Volumes/EDIT/x/RAIN_1.mxf'); updateSelectionUI();");
check("Remove button hidden for editors", !$("#removeBtn").classList.contains("visible"));
const before = calls.length; await w.eval("removeSelected()"); await wait(50);
check("editor cannot trigger a remove request", calls.length === before);

// indexer status line escapes server text
await w.eval("loadIndexerStatus()"); await wait(100);
check("indexer warning text is escaped (no injected element)", !w.document.getElementById("warnpwn"));
check("indexer warning is surfaced", $("#indexerStatus").innerHTML.includes("warning"));

// scoped indexer run: load the REAL indexer_status.js, let it build the picker, click the REAL button
w.localStorage.setItem("clipstage_token_role", "admin");
w.eval(fs.readFileSync(here("../indexer_status.js"), "utf8"));
w.clipstageAuthenticated = true; w.clipstageStartIndexerStatus(); await wait(300);
const sel = $("#idxScope");
check("real indexer_status.js built the volume picker", !!sel && /All volumes/.test(sel.textContent), sel ? sel.textContent : "missing");
check("no 'Log in' link in the header", !/Log in/.test($(".header").textContent));
if (sel) {
  sel.click(); await wait(50);
  const labels = [...w.document.querySelectorAll("#idxScopePanel label")].map(l => l.textContent.trim());
  check("picker lists All volumes + EDIT + PLAYOUT", labels.join(",") === "All volumes,EDIT,PLAYOUT", labels.join(","));
  [...w.document.querySelectorAll("#idxScopePanel input")][2].click(); await wait(50);   // tick PLAYOUT
  calls.length = 0;
  $("#indexerBtn").click(); await wait(200);        // the real inline onclick="runIndexer()"
  const scoped = calls.filter(c => c.url.startsWith("/admin/run-indexer"));
  check("clicking Sync with PLAYOUT ticked posts ?volume=PLAYOUT", scoped.length === 1 && scoped[0].method === "POST" && scoped[0].url === "/admin/run-indexer?volume=PLAYOUT", scoped.map(c => c.method + " " + c.url).join(" | ") || "no call");
  [...w.document.querySelectorAll("#idxScopePanel input")][0].click(); await wait(50);   // back to All volumes
  calls.length = 0;
  $("#indexerBtn").click(); await wait(200);
  const full = calls.filter(c => c.url.startsWith("/admin/run-indexer"));
  check("clicking Sync with 'All volumes' posts no volume filter", full.length === 1 && full[0].url === "/admin/run-indexer", full.map(c => c.url).join(" | ") || "no call");
}

// header: signed-in email + role + Log out (index.html), admin controls follow the role
await w.eval("syncSessionFromServer()"); await wait(100);
check("header shows the signed-in email", $("#userEmail").textContent === "admin@x.com" && !$("#userBox").hidden, $("#userEmail").textContent);
check("header shows the admin role", $("#userRole").textContent === "admin");
check("admin sees Sync Index", $("#indexerBtn").style.display !== "none");
w.localStorage.setItem("clipstage_token_role", "editor"); w.clipstageRefreshIndexerAuth();
check("editor does not see Sync Index or the picker", $("#indexerBtn").style.display === "none" && $("#idxScopeWrap").style.display === "none");
w.localStorage.setItem("clipstage_token_role", "admin");
calls.length = 0;
await w.eval("clipstageLogout()"); await wait(100);
check("Log out calls /auth/logout", calls.some(c => c.method === "POST" && c.url.startsWith("/auth/logout")));
check("Log out wipes the saved session", !w.localStorage.getItem("clipstage_token_exp") && !w.localStorage.getItem("clipstage_token_role") && !w.localStorage.getItem("clipstage_token_user"));
console.log(`\n${pass} passed, ${fail} failed`); process.exit(fail ? 1 : 0);
