CLIPSTAGE ELECTRON APP (drag staged clips into Final Cut Pro)
=============================================================

WHO DOES WHAT
- Admin (you), once, on ONE Mac: build ClipStage.app  -> section A
- Each editor, once: copy ClipStage.app to Applications -> section B
- Editors, every day: double-click ClipStage.app (no npm, no Terminal)

-------------------------------------------------------------
A. BUILD THE .app  (admin, once; needs Node.js 18+)
-------------------------------------------------------------
1. Edit config.json
     serverUrl    : ClipStage address, e.g. http://192.168.1.20:8000
     stagingMount : where the staging share mounts on editor Macs
                    (default /Volumes/staging)
2. In Terminal:
     cd clipstage-fcp-drag
     npm install
     npx @electron/packager . ClipStage --platform=darwin --arch=arm64 --overwrite
   (use --arch=x64 for Intel Macs; build one of each if you have both)
3. Result: ClipStage-darwin-arm64/ClipStage.app
4. Zip ClipStage.app and share it with editors.

Rebuild only if config.json or the Electron files change.

-------------------------------------------------------------
B. EDITOR SETUP  (once per Mac)
-------------------------------------------------------------
1. Copy ClipStage.app to /Applications (drag to Dock if you like).
2. First launch: right-click the app > Open > Open (unsigned app, one time only).
3. FCP: Settings > Import > Files > "Leave files in place".
4. Make sure the staging share is mounted (clicking "Open Finder"
   in ClipStage mounts it).

-------------------------------------------------------------
C. DAILY USE  (editor)
-------------------------------------------------------------
1. Double-click ClipStage.app, log in, pick your name.
2. Stage clips as usual.
3. Drag a staged card from the right panel into FCP.
   Several clips: click cards to select (Shift-click for a range, or
   'Select all'), then drag any selected card - all of them move together.
Chrome still works for search/stage, but drag-to-FCP works only in this app.

-------------------------------------------------------------
D. DEVELOPER COMMANDS (no packaging; Node required)
-------------------------------------------------------------
  npm install        first time only
  npm start          run the app for this session

-------------------------------------------------------------
E. TROUBLESHOOTING
-------------------------------------------------------------
- Drag does nothing: check the share is mounted:
      ls /Volumes/staging/<YOUR-NAME>
  If it is under another name (e.g. /Volumes/staging-1), set
  stagingMount in config.json and rebuild.
- Blank window / cannot connect: serverUrl wrong, or the middle
  machine is off or on a different network.
- "App can't be opened": right-click > Open (first launch only).
- Run from Terminal to see errors:
      /Applications/ClipStage.app/Contents/MacOS/ClipStage
- Server side: the patched index.html must be on the middle machine.
