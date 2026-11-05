"""Tests for the macOS + Windows installers and the cross-platform helpers."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------- path spelling (Windows) ---------------------------------------------------------
def test_windows_path_normalisation():
    import clipstage_config as c
    f = c._norm_windows
    assert f(r"\\?\c:\Volumes\EDIT\a b\x.mxf") == "C:/Volumes/EDIT/a b/x.mxf"      # os.readlink() spelling
    assert f(r"\\?\UNC\nas\EDIT\x.mxf") == "//nas/EDIT/x.mxf"
    assert f("C:/Volumes\\EDIT/x") == "C:/Volumes/EDIT/x"                          # mixed separators from scandir
    assert f(r"c:\Volumes") == "C:/Volumes"
    assert f("C:/Volumes/EDIT/x") == "C:/Volumes/EDIT/x"                           # idempotent


def test_norm_path_is_identity_on_posix():
    import clipstage_config as c
    if not c.IS_WINDOWS:
        assert c.norm_path("/Volumes/EDIT/a b.mxf") == "/Volumes/EDIT/a b.mxf"
        assert c.norm_path(Path("/Volumes/EDIT/x")) == "/Volumes/EDIT/x"


def test_clip_id_is_stable_across_spellings():
    import clipstage_config as c
    a = c.clip_uid(c._norm_windows(r"C:\Volumes\EDIT\x.mxf"))
    b = c.clip_uid(c._norm_windows(r"\\?\C:\Volumes\EDIT\x.mxf"))
    assert a == b


def test_pid_alive_never_kills():
    import clipstage_config as c
    assert c.pid_alive(os.getpid()) is True
    assert c.pid_alive(0) is False and c.pid_alive(-5) is False and c.pid_alive("junk") is False
    assert c.pid_alive(4_000_000) is False
    assert c.pid_alive(os.getpid()) is True            # still alive after probing ourselves


def test_detached_popen_kwargs():
    import clipstage_config as c
    kw = c.detached_popen_kwargs()
    assert ("creationflags" in kw) if c.IS_WINDOWS else (kw == {"start_new_session": True})


def test_api_link_helpers_roundtrip(tmp_path):
    if os.name == "nt":
        pytest.skip("uses POSIX symlinks")
    import api
    src = tmp_path / "vol" / "clip.mxf"
    src.parent.mkdir()
    src.write_bytes(b"x")
    dst = tmp_path / "stage" / "clip.mxf"
    dst.parent.mkdir()
    os.symlink(str(src), str(dst))
    assert api._read_link(dst) == str(src)
    assert api._link_points_to(dst, src)
    assert not api._link_points_to(dst, tmp_path / "other.mxf")
    assert not api._link_points_to(tmp_path / "missing", src)


def test_volume_of_uses_volumes_root(monkeypatch):
    import api
    monkeypatch.setattr(api, "VOLUMES_ROOT", Path("/Volumes"))
    assert api._volume_of(Path("/Volumes/EDIT/a/b.mxf")) == "EDIT"
    assert api._volume_of(Path("/elsewhere/EDIT/a.mxf")) == "EDIT"      # fallback keeps the old behaviour


# ---------- .env handling ---------------------------------------------------------------------
def test_dotenv_parser_rules(tmp_path, monkeypatch):
    import clipstage_dotenv as d
    f = tmp_path / ".env"
    f.write_text('# c\nA=x y\nB="quoted value"\nC=p=ss"w\\o rd&^%\nbad key=1\nEMPTY=\n', encoding="utf-8")
    got = d.parse_env_text(f.read_text())
    assert got == {"A": "x y", "B": "quoted value", "C": 'p=ss"w\\o rd&^%', "EMPTY": ""}
    monkeypatch.setenv("A", "from-shell")
    monkeypatch.delenv("B", raising=False)
    d.load_env(f)
    assert os.environ["A"] == "from-shell"          # an existing value always wins
    assert os.environ["B"] == "quoted value"
    monkeypatch.delenv("B", raising=False)


def test_dotenv_never_executes(tmp_path):
    import clipstage_dotenv as d
    marker = tmp_path / "pwned"
    got = d.parse_env_text(f"X=$(touch {marker})\n`touch {marker}`=1\n")
    assert not marker.exists() and got == {"X": f"$(touch {marker})"}


def test_envtool_set_get(tmp_path):
    f = tmp_path / "e.env"
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "envtool.py"), "--file", str(f), *a],
                                    capture_output=True, text=True, check=True).stdout.strip()
    run("set", "A", "x y", "B", "2")
    run("set", "A", "z w")
    assert run("get", "A") == "z w" and run("get", "B") == "2" and run("get", "NOPE", "--default", "d") == "d"
    assert f.read_text() == "A=z w\nB=2\n"
    if os.name != "nt":
        assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_backup_db_makes_dated_copy_and_prunes(tmp_path, monkeypatch):
    import sqlite3
    import backup_db
    import store
    db = tmp_path / "c.db"
    con = sqlite3.connect(db); con.execute("create table t(x)"); con.execute("insert into t values (1)"); con.commit(); con.close()
    monkeypatch.setattr(store, "DB_PATH", db)
    out = tmp_path / "out"
    out.mkdir()
    for i in range(20):
        (out / f"clipstage-2020-01-{i + 1:02d}.db").write_bytes(b"old")
    monkeypatch.setenv("CLIPSTAGE_BACKUP_DIR", str(out))
    monkeypatch.setenv("CLIPSTAGE_BACKUP_KEEP", "5")
    assert backup_db.main() == 0
    files = sorted(out.glob("clipstage-*.db"))
    assert len(files) == 5
    newest = files[-1]
    assert sqlite3.connect(newest).execute("select x from t").fetchone() == (1,)


# ---------- installer scripts -------------------------------------------------------------------
SH = ["install-mac-host.sh", "install-mac-editor.sh", "clipstage.sh", "mount_volumes.sh"]
PS = ["install-win-host.ps1", "install-win-editor.ps1", "clipstage.ps1", "clipstage_env.ps1"]


@pytest.mark.parametrize("name", SH)
def test_shell_scripts_parse(name):
    subprocess.run(["bash", "-n", str(ROOT / name)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("name", SH)
def test_shell_scripts_shellcheck(name):
    r = subprocess.run(["shellcheck", "-S", "warning", str(ROOT / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
@pytest.mark.parametrize("name", PS)
def test_powershell_scripts_parse(name):
    code = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{ROOT / name}',[ref]$t,[ref]$e); if($e){{$e|%{{$_.Message}}; exit 1}}")
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_scripts_use_lf_and_no_secrets():
    for name in SH + PS + ["clipstage_dotenv.py", "envtool.py"]:
        raw = (ROOT / name).read_bytes()
        if name.endswith(".sh"):
            assert b"\r\n" not in raw, f"{name} must use LF line endings (bash)"
        assert b"supabase.co/" not in raw.replace(b"https://supabase.com", b""), name


def test_windows_defaults_are_sane():
    text = (ROOT / "clipstage_config.py").read_text()
    assert '"C:/Volumes" if IS_WINDOWS' in text and "C:/ClipStage-Staging" in text


def test_editor_app_cross_platform():
    js = (ROOT / "clipstage-fcp-drag" / "main.js").read_text()
    assert "process.platform === 'win32'" in js and "clipstage-open-staging" in js
    cfg = json.loads((ROOT / "clipstage-fcp-drag" / "config.json").read_text())
    assert "serverUrl" in cfg


def test_powershell_files_are_ascii():
    """Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI: any non-ASCII character would turn into junk."""
    for name in PS:
        (ROOT / name).read_bytes().decode("ascii")


@pytest.mark.parametrize("name", ["install-mac-host.sh", "install-mac-editor.sh", "install-win-host.ps1", "install-win-editor.ps1"])
def test_installers_show_a_plan_and_offer_confirm_or_edit(name):
    text = (ROOT / name).read_text()
    assert "What this installer will do" in text, f"{name}: no up-front plan"
    assert "Is this correct?" in text and "E = edit" in text, f"{name}: no confirm/edit step"


def test_mac_host_explains_and_asks_before_installing_homebrew():
    text = (ROOT / "install-mac-host.sh").read_text()
    assert "Homebrew is required" in text and 'yesno "Continue and install everything above?"' in text
    # Homebrew is only installed when something is actually missing
    assert 'NEED_BREW=1' in text and 'if [ "$NEED_BREW" -eq 1 ]; then' in text


def test_prompts_never_loop_forever_when_input_closes():
    for name in ("install-mac-host.sh", "install-mac-editor.sh"):
        text = (ROOT / name).read_text()
        assert text.count("Input ended") >= 3, f"{name}: read must stop on end of input"
    win = (ROOT / "install-win-host.ps1").read_text()
    assert "Too many invalid entries" in win
