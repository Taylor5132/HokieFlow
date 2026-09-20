#!/usr/bin/env python3
"""Opt-in real Gemini smoke test for HokieFlow AI.

Usage (credentials are read from the protected, gitignored .env file):
  DEMO_MODE=live HOKIEFLOW_LIVE_SMOKE=1 python3 scripts/gemini_agent_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.local_env import load_local_env  # noqa: E402

load_local_env(REPO / ".env")

from app.gemini_provider import GeminiProvider  # noqa: E402
from hokieday import agent, config  # noqa: E402
from hokieday.agent_tools import AgentContext  # noqa: E402


def main() -> int:
    if os.environ.get("HOKIEFLOW_LIVE_SMOKE") != "1":
        print("refusing network call: set HOKIEFLOW_LIVE_SMOKE=1 explicitly")
        return 2
    if config.CACHE_ONLY:
        print("refusing network call: run with DEMO_MODE=live")
        return 2
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip()
    if not key or not model:
        print("set GEMINI_API_KEY and GEMINI_MODEL in the environment")
        return 2
    base_provider = GeminiProvider(key, model)
    provider = base_provider
    narration: list[str] = []
    if os.environ.get("HOKIEFLOW_SMOKE_VERBOSE") == "1":
        # Show what the model actually wrote, so a grounding replacement can be
        # diagnosed instead of guessed at. Delegates to the real provider.
        class RecordingProvider:
            name = base_provider.name
            model = base_provider.model

            def generate(self, **kwargs):
                response = base_provider.generate(**kwargs)
                if response.text:
                    narration.append(response.text)
                return response

        provider = RecordingProvider()
    now = config.now()
    # A synthetic, user-controlled schedule item proves the deterministic
    # schedule-to-agent handoff without using a real student's records.
    # Times are built in CAMPUS time; config.now() is UTC in live mode, so
    # replacing the hour on it would shift the class by the UTC offset.
    tz = ZoneInfo(config.CAMPUS_TZ)
    campus_now = now.astimezone(tz)
    monday = (campus_now +
              timedelta(days=(7 - campus_now.weekday()) % 7)).date()
    start = datetime.combine(monday, time(12, 30), tzinfo=tz)
    end = datetime.combine(monday, time(13, 45), tzinfo=tz)
    schedule = [{
        "title": "CS3114", "kind": "class", "location": "McBryde Hall",
        "start": start.isoformat(), "end": end.isoformat(),
        "repeat": "weekly",
        "repeatUntil": (monday + timedelta(days=90)).isoformat(),
    }]
    result = agent.run_agent(
        "When can I eat lunch on Monday?",
        provider=provider,
        context=AgentContext(now=now, schedule=schedule, is_replay=False),
    )
    print(json.dumps(result, indent=2, default=str))
    if narration:
        print("\n--- raw provider narration (pre-grounding) ---")
        for index, text in enumerate(narration, 1):
            print(f"[{index}] {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
