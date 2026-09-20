"""Tiny stdlib loader for the repository-local, gitignored `.env` file.

Hand-rolled rather than adding python-dotenv as a hard dependency, because the
offline demo and the whole test suite must run on the stdlib alone (NFR-1). The
optional auth stack imports python-dotenv when it is installed; this loader
exists so the rest of the app works without it.

Deliberately narrow:
  * `KEY=VALUE` only -- no `${}` interpolation, no command substitution, no
    `export` semantics beyond a tolerated prefix, no include directives;
  * a real process environment variable ALWAYS wins, so a container or platform
    setting cannot be clobbered by a stray file;
  * values are never logged, echoed, or written anywhere;
  * the file is refused if group/other can read it, since it holds secrets.

Any well-formed key is loaded. An earlier version rejected keys outside a fixed
allowlist, which broke the moment the app grew a second integration: a `.env`
containing SUPABASE_URL raised at import and took the server down. The file is
the operator's own configuration, so the loader's job is to parse it safely, not
to police which settings exist.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_local_env(path: str | Path) -> dict[str, str]:
    """Load `path` into os.environ without overriding existing values.

    Returns the keys it actually set, so a caller can report which local
    settings were applied without printing any value.
    """
    if "unittest" in sys.modules:
        # The offline suite must never be talked into live mode by a developer's
        # .env; an explicit DEMO_MODE still wins over this default.
        os.environ.setdefault("DEMO_MODE", "cache")
    env_path = Path(path)
    if not env_path.exists():
        return {}
    try:
        mode = env_path.stat().st_mode
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {env_path}") from exc
    if mode & 0o077:
        raise RuntimeError(
            f"refusing {env_path}: run 'chmod 600 {env_path}' to protect secrets")

    applied: dict[str, str] = {}
    for number, raw in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f"invalid .env syntax on line {number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _KEY_RE.fullmatch(key):
            raise RuntimeError(f"invalid .env key on line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in os.environ:
            continue                      # a real environment variable wins
        os.environ[key] = value
        applied[key] = value
    return applied