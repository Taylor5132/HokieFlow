"""Tiny stdlib loader for the repository-local, gitignored .env file."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ALLOWED = frozenset({
    "DEMO_MODE",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "HOKIEFLOW_AI_PROVIDER",
    "HOKIEFLOW_AI_QUESTIONS_PER_IP_HOUR",
    "HOKIEFLOW_GEMINI_CALLS_PER_DAY",
    "HOKIEFLOW_GEMINI_CALLS_PER_HOUR",
    "HOKIEFLOW_LIVE_SMOKE",
})


def load_local_env(path: str | Path) -> None:
    """Load allowlisted KEY=VALUE lines without overriding real environment.

    There is deliberately no interpolation, command substitution, or logging of
    values. The file must not be readable by group/other users on POSIX.
    """
    if "unittest" in sys.modules:
        os.environ.setdefault("DEMO_MODE", "cache")
    env_path = Path(path)
    if not env_path.exists():
        return
    try:
        if env_path.stat().st_mode & 0o077:
            raise RuntimeError(
                f"refusing {env_path}: run 'chmod 600 {env_path}' to protect secrets")
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {env_path}") from exc
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
        if key not in _ALLOWED:
            raise RuntimeError(f"unsupported .env key on line {number}: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)
