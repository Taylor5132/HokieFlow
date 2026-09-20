"""Offline test suite; pin replay mode before app modules load local .env."""
import os

# setdefault lets an explicit DEMO_MODE=cache remain visible while preventing a
# developer's gitignored `.env` (normally live) from burning provider quota when
# they run `python3 -m unittest` without a prefix.
os.environ.setdefault("DEMO_MODE", "cache")
