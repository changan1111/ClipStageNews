"""
ClipStage v5.0 - shared configuration (single source of truth).

api.py, indexer.py and generate_thumbs.py all import from here, so the volume list,
video extensions, clip-ID hash, collection name and paths can never drift apart.
Everything is overridable with environment variables (see .env.sample).
"""

import clipstage_dotenv  # noqa: F401  (loads .env first, so every tool sees the same settings)

import hashlib
import os
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# -- Platform --------------------------------------------------------------------
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _norm_windows(p) -> str:
    """Canonical text form of a media path on Windows: forward slashes, no extended-length
    prefix, upper-case drive letter. The clip ID is md5(path), so the indexer, the API and the
    staged links must all agree on ONE spelling. os.readlink() on Windows returns paths with the
    extended-length prefix (backslash-backslash-question-backslash); this undoes that."""
    s = str(p)
    if s.startswith("\\\\?\\UNC\\"):          # \\?\UNC\server\share  ->  \\server\share
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):                # \\?\C:\dir  ->  C:\dir
        s = s[4:]
    s = s.replace("\\", "/")
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        s = s[0].upper() + s[1:]
    return s


def norm_path(p) -> str:
    """Path as stored in the index. Identity on macOS/Linux; forward-slash form on Windows."""
    return _norm_windows(p) if IS_WINDOWS else str(p)


def pid_alive(pid) -> bool:
    """True if a process with this id is running. NEVER uses os.kill(pid, 0) on Windows, where
    that call would terminate the process instead of probing it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5             # access denied = it exists
        try:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259           # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


# A file whose existence means "the running index should stop" (written by the Stop button / CLI,
# checked by the indexer; works the same on macOS and Windows - no signals needed).
INDEX_STOP_FILE = BASE_DIR / "index_stop.flag"


def process_cmdline(pid) -> str:
    """Command line of a running process, or '' when it cannot be determined."""
    import subprocess
    try:
        pid = int(pid)
        if IS_WINDOWS:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20, creationflags=0x08000000)
        else:
            r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                               capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def terminate_process_tree(pid, force: bool = False) -> bool:
    """Stop a process and its children (ffprobe ...). Returns True if the request was delivered."""
    import signal
    import subprocess
    try:
        pid = int(pid)
        if pid <= 0 or pid == os.getpid():
            return False
        if IS_WINDOWS:
            args = ["taskkill.exe", "/PID", str(pid), "/T", "/F"]       # without /F a console process ignores it
            r = subprocess.run(args, capture_output=True, timeout=30, creationflags=0x08000000)
            return r.returncode == 0
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            if os.getpgid(pid) == pid:          # it leads its own process group -> take the children too
                os.killpg(pid, sig)
                return True
        except (ProcessLookupError, PermissionError):
            pass
        os.kill(pid, sig)
        return True
    except Exception:
        return False


def detached_popen_kwargs() -> dict:
    """subprocess.Popen options that let a child outlive the API process."""
    if IS_WINDOWS:
        return {"creationflags": 0x00000200 | 0x08000000}   # NEW_PROCESS_GROUP | NO_WINDOW
    return {"start_new_session": True}


def _find_tool(env_name: str, exe: str, extra=()):
    """Explicit override (.env) -> PATH -> well-known install folders."""
    override = os.environ.get(env_name, "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which(exe)
    if found:
        return found
    for cand in extra:
        if Path(cand).exists():
            return str(cand)
    return None


_MAC_BIN = ("/opt/homebrew/bin", "/usr/local/bin")
FFMPEG = _find_tool("CLIPSTAGE_FFMPEG", "ffmpeg", [f"{d}/ffmpeg" for d in _MAC_BIN])
FFPROBE = _find_tool("CLIPSTAGE_FFPROBE", "ffprobe", [f"{d}/ffprobe" for d in _MAC_BIN])

# -- Search collection ---------------------------------------------------------
# "clips_v2" = the schema with token_separators (word-by-word search).
COLLECTION = os.environ.get("CLIPSTAGE_COLLECTION", "clips_v2")

# -- Volumes -------------------------------------------------------------------
# macOS mounts NAS shares under /Volumes/<share>.  On Windows the installer makes C:/Volumes/<share>
# (a folder link to \\\\NAS\\<share>) so the very same layout - and the same staged links - work.
VOLUMES_ROOT = Path(os.environ.get("CLIPSTAGE_VOLUMES_ROOT", "C:/Volumes" if IS_WINDOWS else "/Volumes"))

DEFAULT_SCAN_VOLUMES = [
    "EDIT", "EDIT2", "INGEST", "PLAYOUT", "DIGITAL", "SHARE FOLDER", "TRANSCODER",
]
_scan_env = os.environ.get("CLIPSTAGE_SCAN_VOLUMES", "")
SCAN_VOLUMES = (
    [v.strip() for v in _scan_env.split(",") if v.strip()]
    if _scan_env.strip() else list(DEFAULT_SCAN_VOLUMES)
)

# -- Media rules ---------------------------------------------------------------
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v",
                    ".mpg", ".mpeg", ".r3d", ".braw"}

# Folder names skipped at ANY depth (case-insensitive exact match).
EXCLUDE_FOLDERS = {
    "@recycle", "#recycle", "@recently-snapshot", ".trashes",
    ".temporaryitems", "lost+found", "$recycle.bin", "system volume information",
}

# -- Paths ---------------------------------------------------------------------
STAGING_PATH = os.environ.get("CLIPSTAGE_STAGING_PATH",
                              "C:/ClipStage-Staging" if IS_WINDOWS else "/Users/Shared/staging")
# How a staged clip is placed in the editor's folder: symlink (default) | hardlink | copy
LINK_MODE = os.environ.get("CLIPSTAGE_LINK_MODE", "symlink").strip().lower()
if LINK_MODE not in ("symlink", "hardlink", "copy"):
    LINK_MODE = "symlink"
STATIC_DIR = BASE_DIR / "static"
THUMB_DIR = Path(os.environ.get("CLIPSTAGE_THUMB_DIR", str(STATIC_DIR / "thumbs")))


def clip_uid(path: str) -> str:
    """Stable clip ID = first 16 hex chars of md5(path). The UI uses the same value for
    /thumb/<id> and /stream/<id>. Moving or renaming a file therefore creates a new clip."""
    return hashlib.md5(path.encode()).hexdigest()[:16]


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
