/*
 * ClipStage UI check for the Stop button on the live index bar. Runs the REAL index.html +
 * indexer_status.js in jsdom with a stubbed network.   node tests/ui_stop.mjs   (needs jsdom)
 */
import { JSDOM } from "jsdom";
import fs from "fs";
import { fileURLToPath } from "url";
const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const html = fs.readFileSync(here("../index.html"), "utf8").replace("__CLIPSTAGE_SMB_HOST__", '""');
const reply = (body, ok = true, status = 200) => Promise.resolve({ ok, status, headers: new Map(), json: async () => body, text: async () => JSON.stringify(body) });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let pass = 0, fail = 0;
const check = (name, cond, extra = "") => { (cond ? pass++ : fail++); console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  -> " + extra : ""}`); };

async function boot(role, { stopStatus = 200 } = {}) {
  const calls = [];
  const now = () => Date.now() / 1000;
  const st = { state: "running", phase: "scanning", started_at: now() - 5, updated_at: now(), scanned: 10, unchanged: 0, volumes: { EDIT: 10 }, scope: "All volumes" };
  const stub = (url, opts = {}) => {
    const u = String(url), method = (opts.method || "GET").toUpperCase();
    calls.push({ method, url: u });
    if (u.startsWith("/config")) return reply({ smb_host: "" });
    if (u.startsWith("/auth/me")) return reply({ username: "u@x.com", role, valid_through: "2099-01-01", editor_name: "" });
    if (u.startsWith("/admin/index-status")) return reply({ ...st, now: now() });
    if (u.startsWith("/admin/stop-indexer")) {
      if (stopStatus === 200) { st.phase = "stopping"; }
      return reply({ stopping: true }, stopStatus === 200, stopStatus);
    }
    if (u.startsWith("/admin/indexable-volumes")) return reply({ volumes: ["EDIT"] });
    if (u.startsWith("/editors")) return reply({ editors: [] });
    if (u.startsWith("/stage/")) return reply({ clips: [] });
    if (u.startsWith("/search")) return reply({ hits: [], total: 0, truncated: false });
    return reply({});
  };
  const asked = [];
  const dom = new JSDOM(html, {
    runScripts: "dangerously", url: "http://localhost:8000/", pretendToBeVisual: true,
    beforeParse(w) {
      w.Request = class Request {}; w.Headers = Headers; w.CSS = { escape: (v) => String(v) }; w.fetch = stub;
      w.confirm = (m) => { asked.push(m); return true; }; w.alert = () => {};
      w.localStorage.setItem("clipstage_token_exp", String(Math.floor(Date.now() / 1000) + 3600));
      w.localStorage.setItem("clipstage_token_role", role);
      w.localStorage.setItem("clipstage_token_valid_through", "2099-01-01");
      w.HTMLElement.prototype.scrollIntoView = () => {};
    },
  });
  await wait(500);
  dom.window.eval(fs.readFileSync(here("../indexer_status.js"), "utf8"));      // like ui_smoke: load the REAL widget
  dom.window.eval("window.clipstageStartIndexerStatus && window.clipstageStartIndexerStatus()");
  await wait(500);
  return { w: dom.window, calls, st, asked };
}

{
  const { w, calls, st, asked } = await boot("admin");
  const btn = w.document.getElementById("idxStop");
  check("admin: Stop button is visible while an index runs", btn && btn.style.display !== "none" && /Stop/.test(btn.textContent), btn && btn.textContent);
  btn.click(); await wait(300);
  check("admin: asks for confirmation first", asked.length === 1 && /nothing is deleted/i.test(asked[0]), asked[0]);
  const post = calls.find((c) => c.url.startsWith("/admin/stop-indexer"));
  check("admin: POSTs /admin/stop-indexer", post && post.method === "POST");
  check("admin: button shows Stopping… and is disabled", btn.disabled && /Stopping/.test(btn.textContent), btn.textContent);
  check("admin: bar text says it is stopping", /Stopping/.test(w.document.getElementById("idxPhase").textContent), w.document.getElementById("idxPhase").textContent);
  st.state = "stopped"; st.phase = "stopped"; st.message = "Stopped by user";
  await wait(2700);                                   // the widget re-polls every 2 s while running
  check("after the stop: the bar is hidden and Sync is usable again", w.document.getElementById("idxBar").style.display === "none" && !w.document.getElementById("indexerBtn").disabled);
}
{
  const { w } = await boot("editor");
  const btn = w.document.getElementById("idxStop");
  check("editor: can watch progress but sees no Stop button", w.document.getElementById("idxBar").style.display === "block" && btn.style.display === "none");
}
{
  const { w, calls } = await boot("admin", { stopStatus: 409 });
  w.document.getElementById("idxStop").click(); await wait(300);
  check("409 (already finished): button comes back, no crash", !w.document.getElementById("idxStop").disabled);
  check("409: request was still sent once", calls.filter((c) => c.url.startsWith("/admin/stop-indexer")).length === 1);
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
