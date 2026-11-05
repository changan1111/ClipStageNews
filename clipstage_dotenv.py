"""
ClipStage - safe .env loader for the Python tools (macOS and Windows).

Importing this module (first import in api.py / indexer.py / generate_thumbs.py / store.py ...)
reads KEY=value lines from the .env file next to the code.  Same rules as clipstage_env.sh:

  * nothing in the file is ever executed - it is parsed, line by line
  * values may contain spaces ("SHARE FOLDER"); surrounding quotes are removed
  * a variable that is already set (and not empty) in the environment WINS over the file

That is why `python indexer.py` now works without a wrapper script, which is what lets the
Windows scheduled tasks start the Python tools directly.
"""
import os
import re
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z0-9_]+$")


def parse_env_text(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip("\r\n")
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not _KEY.match(key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def load_env(path=None) -> int:
    """Load .env into os.environ (never overriding a non-empty existing value). Returns count set."""
    f = Path(path) if path else Path(__file__).resolve().parent / ".env"
    try:
        text = f.read_text(encoding="utf-8-sig")
    except OSError:
        return 0
    n = 0
    for k, v in parse_env_text(text).items():
        if not os.environ.get(k):
            os.environ[k] = v
            n += 1
    return n


load_env()
