/*
 * ClipStage UI check for the editor's computer type. Runs the REAL index.html in jsdom twice:
 * once as a Windows browser, once as a Mac browser, and checks the staging-folder wording and the
 * "open folder" button.   node tests/ui_platform.mjs   (from the project folder; needs jsdom)
 */
import { JSDOM } from "jsdom";
import fs from "fs";
import { fileURLToPath } from "url";
const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const html = fs.readFileSync(here("../index.html"), "utf8").replace("__CLIPSTAGE_SMB_HOST__", '""');
const reply = (body) => Promise.resolve({ ok: true, status: 200, headers: new Map(), json: async () => body, text: async () => JSON.stringify(body) });
let pass = 0, fail = 0;
const check = (name, cond, extra = "") => { (cond ? pass++ : fail++); console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  -> " + extra : ""}`); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function boot(userAgent, native) {
  const clip = { text: null };
  const stub = (url) => {
    const u = String(url);
    if (u.startsWith("/config")) return reply({ smb_host: "10.1.10.5", platform: "windows", share_name: "staging" });
    if (u.startsWith("/auth/me")) return reply({ username: "e@x.com", role: "editor", valid_through: "2099-01-01", editor_name: "GOKUL" });
    if (u.startsWith("/editors")) return reply({ editors: ["GOKUL"] });
    if (u.startsWith("/stage/")) return reply({ clips: [] });
    if (u.startsWith("/search")) return reply({ hits: [], total: 0, truncated: false });
    return reply({});
  };
  const dom = new JSDOM(html, {
    runScripts: "dangerously", url: "http://localhost:8000/", pretendToBeVisual: true,
    beforeParse(w) {
      Object.defineProperty(w.navigator, "userAgent", { value: userAgent, configurable: true });
      w.Request = class Request {}; w.Headers = Headers; w.CSS = { escape: (v) => String(v) }; w.fetch = stub;
      w.confirm = () => true; w.alert = () => {}; w.prompt = () => null;
      w.localStorage.setItem("clipstage_token_exp", String(Math.floor(Date.now() / 1000) + 3600));
      w.localStorage.setItem("clipstage_token_role", "editor");
      w.localStorage.setItem("clipstage_token_valid_through", "2099-01-01");
      w.HTMLElement.prototype.scrollIntoView = () => {};
      Object.defineProperty(w.navigator, "clipboard", { value: { writeText: (t) => { clip.text = t; return Promise.resolve(); } }, configurable: true });
      if (native) w.clipstageNative = native;
    },
  });
  await wait(500);
  return { w: dom.window, clip };
}

const WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36";
const MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15";

{
  const { w, clip } = await boot(WIN);
  const btn = w.document.getElementById("openFinderBtn");
  check("Windows: button says staging folder, not Finder", btn.textContent.includes("staging folder") && !btn.textContent.includes("Finder"), btn.textContent);
  const pre = w.document.getElementById("stagingPathPrefix").textContent;
  check("Windows: folder hint is a UNC path", pre === "\\\\10.1.10.5\\staging\\", pre);
  check("Windows: hint mentions Premiere/Resolve/Avid, not FCP", /Premiere/.test(w.document.getElementById("stagingHint").textContent) && !/FCP/.test(w.document.getElementById("stagingHint").textContent));
  w.eval("currentEditor = 'GOKUL'");
  w.eval("openFinder()"); await wait(50);
  check("Windows (browser): path is copied to the clipboard", clip.text === "\\\\10.1.10.5\\staging\\GOKUL", String(clip.text));
}
{
  let opened = null;
  const { w } = await boot(WIN, { openStaging: (n) => { opened = n; } });
  w.eval("currentEditor = 'GOKUL'"); w.eval("openFinder()");
  check("Windows (desktop app): asks the app to open the folder", opened === "GOKUL", String(opened));
}
{
  const { w } = await boot(MAC);
  const btn = w.document.getElementById("openFinderBtn");
  check("Mac: button still says Finder", btn.textContent.includes("Finder"), btn.textContent);
  check("Mac: folder hint is /Volumes/staging/", w.document.getElementById("stagingPathPrefix").textContent === "/Volumes/staging/");
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
