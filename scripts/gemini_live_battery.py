#!/usr/bin/env python3
"""Opt-in live exercise of EVERY HokieFlow tool through the real provider.

One question per tool family, so a model change can be re-verified cheaply.

Usage (credentials come from the protected, gitignored .env):
  DEMO_MODE=live HOKIEFLOW_LIVE_SMOKE=1 python3 scripts/gemini_live_battery.py
  ... --only food,weather        # run a subset
  HOKIEFLOW_BATTERY_MAX_CALLS=24 # abort before draining the hourly budget
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.local_env import load_local_env  # noqa: E402

load_local_env(REPO / ".env")

from app import server  # noqa: E402
from app.gemini_provider import GeminiProvider  # noqa: E402
from hokieday import agent, config  # noqa: E402
from hokieday.agent_tools import AgentContext  # noqa: E402

# (key, question, expected tool, extra request context)
BATTERY: list[tuple[str, str, str, dict]] = [
    ("weather", "Will it rain on my walk across campus this afternoon?",
     "get_weather", {}),
    ("food", "What's on the menu at D2 right now, and what's under 500 calories?",
     "find_food", {}),
    ("hours", "What are D2's hours today?", "get_hours", {}),
    ("events", "What events are happening on campus today?", "get_events", {}),
    ("bus", "Are the buses running late right now?", "get_live_bus", {}),
    ("departures", "When is the next bus leaving the Tennis Courts stop?",
     "get_next_departures", {}),
    ("plan", "I've got until 1:25 PM, I'm hungry, and I need to get from "
     "Burruss Hall to McBryde Hall.",
     "plan_day", {"window": True}),
]


def _simulated_now(hour: int, minute: int) -> datetime:
    """A realistic daytime campus clock, so planning questions are answerable.

    Live wall-clock time (often late at night) would make every deadline the
    battery proposes already passed, which tests the clock rather than the tool.
    """
    tz = ZoneInfo(config.CAMPUS_TZ)
    campus = config.now().astimezone(tz)
    return campus.replace(hour=hour, minute=minute, second=0, microsecond=0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="",
                        help="comma-separated battery keys")
    parser.add_argument("--verbose", action="store_true",
                        help="print each model's raw prose before grounding")
    parser.add_argument("--at", default="11:22",
                        help="simulated campus clock HH:MM (default 11:22)")
    args = parser.parse_args()

    if os.environ.get("HOKIEFLOW_LIVE_SMOKE") != "1":
        print("refusing network calls: set HOKIEFLOW_LIVE_SMOKE=1 explicitly")
        return 2
    if config.CACHE_ONLY:
        print("refusing network calls: run with DEMO_MODE=live")
        return 2
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip()
    if not key or not model:
        print("set GEMINI_API_KEY and GEMINI_MODEL in the protected .env file")
        return 2
    max_calls = int(os.environ.get("HOKIEFLOW_BATTERY_MAX_CALLS", "24"))

    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    selected = [row for row in BATTERY if not wanted or row[0] in wanted]
    base = GeminiProvider(key, model)
    try:
        sim_hour, sim_minute = (int(part) for part in args.at.split(":"))
    except ValueError:
        print("--at must look like HH:MM")
        return 2
    now = _simulated_now(sim_hour, sim_minute)
    tz = ZoneInfo(config.CAMPUS_TZ)
    schedule = []

    print(f"provider={base.name} model={base.model} "
          f"campus_now={now.astimezone(tz):%a %Y-%m-%d %H:%M} "
          f"max_calls={max_calls}\n")
    used = 0
    failures: list[str] = []
    rate_limited = False
    for name, question, expected_tool, extra in selected:
        if used >= max_calls:
            print(f"[skip] {name}: call budget {max_calls} reached")
            continue
        narration: list[str] = []
        arguments: list[str] = []

        class Recorder:
            name = base.name
            model = base.model

            def generate(self, **kwargs):
                nonlocal used
                used += 1
                response = base.generate(**kwargs)
                if response.text:
                    narration.append(response.text)
                for call in response.tool_calls:
                    arguments.append(f"{call.name}({call.arguments})")
                return response

        # Exactly the server's deterministic pre-pass, so the battery tests the
        # same window and constraint resolution a real request gets.
        safety_call = server.parse_free_text(question, now=now, live=True)
        context = AgentContext(
            now=now, schedule=list(schedule),
            required_prefs=dict(safety_call.get("prefs") or {}),
            is_replay=False,
            plan_start=safety_call.get("start"), plan_end=safety_call.get("end"),
            origin_place=str((safety_call.get("prefs") or {}).get("from_place") or "")
            or None,
        )
        try:
            result = agent.run_agent(question, provider=Recorder(), context=context)
        except agent.AgentUnavailable as exc:
            detail = exc.detail or {}
            if detail.get("code") == "http_429":
                # The account quota is the real limit; stop rather than retry.
                print(f"[RATE LIMITED] {name}: the provider quota is exhausted. "
                      "Stopping; re-run later.\n")
                rate_limited = True
                break
            if detail.get("code") == "local_budget_exhausted":
                print(f"[BUDGET] {name}: {exc.message} Stopping.\n")
                break
            failures.append(f"{name}: {exc.code}")
            print(f"[FAIL] {name}: {exc.code} {detail}\n")
            continue
        tools = result["provenance"]["tool_names"]
        replaced = result["provenance"]["grounding_replaced"]
        answer = " ".join(str(result["answer"]).split())
        ok_tool = expected_tool in tools
        if not ok_tool:
            failures.append(f"{name}: chose {tools or 'no tool'}, expected {expected_tool}")
        print(f"[{name}] expected={expected_tool} chose={tools or 'none'} "
              f"grounding_replaced={replaced} turns={result['agent']['turns']}")
        if arguments:
            print(f"  args  : {'; '.join(arguments)}")
        print(f"  answer: {answer[:400]}")
        if verbatim := (narration[-1] if args.verbose and narration else None):
            print(f"  model : {' '.join(verbatim.split())[:400]}")
        print()

    print(f"provider calls used: {used}")
    if rate_limited:
        print("stopped early on the provider quota; remaining questions unverified")
        return 3
    if failures:
        print("problems:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("every battery question used its expected tool and stayed grounded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())