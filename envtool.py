#!/usr/bin/env python3
"""
ClipStage - tiny .env editor used by the installers (macOS and Windows).

    python envtool.py get KEY [--default X]       print a value (empty if unset)
    python envtool.py set KEY VALUE [KEY VALUE..] create/update lines, keep everything else
    python envtool.py init                        create .env from .env.sample if it does not exist

Values are written verbatim (spaces are fine, no quoting). The file is always written with LF
line endings and no BOM, and on macOS/Linux is chmod 600.  Run with --file PATH to edit a
different file.  Nothing in the file is ever executed.
"""
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _read(path: Path) -> list:
    try:
        return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").split("\n")
    except OSError:
        return []


def get(path: Path, key: str, default: str = "") -> str:
    for line in _read(path):
        if line.startswith(key + "="):
            val = line[len(key) + 1:].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            return val
    return default


def set_values(path: Path, pairs: dict) -> None:
    lines = _read(path)
    while lines and lines[-1] == "":
        lines.pop()
    for key, val in pairs.items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", key):
            raise SystemExit(f"bad key: {key!r}")
        if "\n" in val or "\r" in val:
            raise SystemExit(f"value for {key} contains a line break")
        new = f"{key}={val}"
        for i, line in enumerate(lines):
            if line.startswith(key + "="):
                lines[i] = new
                break
        else:
            lines.append(new)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if os.name != "nt":
        os.chmod(path, 0o600)


def main(argv: list) -> int:
    path = HERE / ".env"
    if "--file" in argv:
        i = argv.index("--file")
        path = Path(argv[i + 1])
        del argv[i:i + 2]
    default = ""
    if "--default" in argv:
        i = argv.index("--default")
        default = argv[i + 1]
        del argv[i:i + 2]
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "get" and len(rest) == 1:
        print(get(path, rest[0], default))
        return 0
    if cmd == "set" and rest and len(rest) % 2 == 0:
        set_values(path, dict(zip(rest[0::2], rest[1::2])))
        return 0
    if cmd == "init":
        if not path.exists():
            shutil.copyfile(HERE / ".env.sample", path)
            if os.name != "nt":
                os.chmod(path, 0o600)
            print("created", path)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
