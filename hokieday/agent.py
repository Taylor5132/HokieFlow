"""Provider-independent, bounded, grounded HokieFlow AI orchestration loop."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from . import config, tools
from .agent_prompts import system_prompt
from .agent_tools import AgentContext, ToolExecution, ToolRegistry
from .providers.base import (Provider, ProviderMessage, ProviderUnavailable)

MAX_INPUT_BYTES = 8_192
# Conservative defaults for Gemini's free tier: a normal request is two model
# calls (tool selection, then narration). One extra turn permits a genuinely
# compositional request without allowing an expensive open-ended loop.
MAX_TURNS = 3
MAX_TOOL_CALLS = 4
MAX_IDENTICAL_CALLS = 1
PROVIDER_TIMEOUT_S = 20.0
_CAMPUS_TZ = ZoneInfo(config.CAMPUS_TZ)


@dataclass
class AgentUnavailable(RuntimeError):
    code: str
    message: str
    detail: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {"status": "unavailable", "code": self.code,
                "message": self.message, "detail": self.detail or {}}


def _call_key(name: str, arguments: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(arguments, sort_keys=True, separators=(',', ':'), default=str)}"


_CLOCK_RE = re.compile(
    r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:(a|p)\.?\s?m\.?)?",
    re.IGNORECASE)
_CLOCK_WORDS = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?\s?m\.?)?", re.IGNORECASE)
_EMPTY_ACKNOWLEDGED = ("closed", "no menu", "no items", "no item", "nothing",
                       "none", "no event", "no departures", "no buses",
                       "not open", "no data", "unavailable", "couldn't find",
                       "could not find", "no results", "empty", "no matches",
                       "no scheduled", "no live", "no available", "wasn't able")
_CURRENCY_CLAIMS = ("right now", "currently", "current ", "live", "real-time",
                    "realtime", "at the moment", "as of now")
_STALENESS_DISCLOSURE = ("stale", "cached", "snapshot", "observed", "scheduled",
                         "as of", "replay", "replayed", "from earlier")


def _clock_values(text: str) -> set[int]:
    """Every clock time in `text` as seconds since midnight.

    A time written without AM/PM is ambiguous, so BOTH readings count: a tool's
    `15:00:00` legitimately narrates as "3:00 PM" or "15:00", and rejecting the
    reformatted form would reject correct answers.
    """
    values: set[int] = set()
    for hour_s, minute_s, second_s, meridiem in _CLOCK_RE.findall(text):
        hour, minute, second = int(hour_s), int(minute_s), int(second_s or 0)
        if hour > 23 or minute > 59 or second > 59:
            continue
        if meridiem:
            mark = meridiem.lower()
            if mark == "p" and hour < 12:
                hour += 12
            elif mark == "a" and hour == 12:
                hour = 0
            values.add(hour * 3600 + minute * 60 + second)
            continue
        values.add(hour * 3600 + minute * 60 + second)
        if hour < 12:
            values.add((hour + 12) * 3600 + minute * 60 + second)
    return values


def _split_numbers_and_times(text: str) -> tuple[set[int], set[str]]:
    """Clock values (seconds since midnight) and plain numeric tokens."""
    times = _clock_values(text)
    remainder = _CLOCK_WORDS.sub(" ", text)
    numbers = set(re.findall(r"\d+(?:\.\d+)?", remainder))
    return times, numbers


def _numbers_are_grounded(answer: str, executions: list[ToolExecution]) -> bool:
    """Reject any exact value the tool results did not contain.

    Value-preserving paraphrases are accepted: "3 PM" for a tool's "3:00 PM",
    "15:00" for the same instant, and "240" for "240.0". Nothing is invented.
    """
    times, numbers = _split_numbers_and_times(answer)
    if not times and not numbers:
        return True
    evidence = json.dumps([e.public for e in executions], default=str,
                          ensure_ascii=False)
    ev_times, ev_numbers = _split_numbers_and_times(evidence)
    ev_values = {float(value) for value in ev_numbers}
    ev_hours = {float(value // 3600) for value in ev_times}
    # Compare at MINUTE granularity: the sources emit values like 09:30:01, and
    # a model saying "9:30 AM" has preserved the value, not invented one.
    ev_minutes = {value // 60 for value in ev_times}
    for value in times:
        if value // 60 not in ev_minutes:
            return False
    for token in numbers:
        if float(token) in ev_values:
            continue
        # "3" is grounded when a tool reported a 3 o'clock time.
        if token.strip("0") and float(token) in ev_hours:
            continue
        return False
    return True


def _has_content(execution: ToolExecution) -> bool:
    """Did this tool actually return something a sentence could be built on?

    An empty-but-successful result (no matching menu items, no windows, no
    departures) is NOT evidence of availability. Without this, a model could
    answer "everything is under 500 calories" from a search that returned no
    rows at all.
    """
    data = execution.public.get("data") or {}
    name = execution.name
    if name == "find_food":
        return bool(data.get("items"))
    if name == "get_hours":
        return bool(data.get("windows"))
    if name == "get_events":
        return bool(data.get("events"))
    if name == "get_live_bus":
        return bool(data.get("count"))
    if name == "get_next_departures":
        return bool(data.get("departures"))
    if name == "get_weather":
        return data.get("status") != "unavailable"
    if name == "get_campus_schedule":
        return str(data.get("state") or "") == "ok"
    if name == "plan_day":
        # An infeasible plan is still usable: its own rationale explains why.
        return bool(data.get("itinerary")) or data.get("feasible") is False
    return True


def _is_narratable(execution: ToolExecution) -> bool:
    if not execution.public.get("ok"):
        return False
    data = execution.public.get("data") or {}
    if isinstance(data.get("clarification"), dict):
        return False
    if str(data.get("state") or "") == "clarification_needed":
        return False
    return _has_content(execution)


def _best_for_text(executions: list[ToolExecution]) -> ToolExecution | None:
    """The result the code-written fallback sentence should describe.

    Unlike the narration guard, this deliberately accepts an empty result: when
    a search legitimately finds nothing, the tool's own reason is what the
    student should hear.
    """
    if not executions:
        return None
    for execution in executions:
        if execution.name == "plan_day" and execution.public.get("ok") \
                and _has_content(execution):
            return execution
    for execution in executions:
        if execution.public.get("ok") and _has_content(execution):
            return execution
    for execution in executions:
        if execution.public.get("ok"):
            return execution
    return executions[-1]


def _primary_execution(executions: list[ToolExecution]) -> ToolExecution | None:
    """Pick the result that actually answers the question.

    plan_day is the deterministic orchestrator, so it outranks secondary
    lookups: without this, a follow-up find_food call after plan_day made the
    structured plan (and the answer) disappear from the response.
    """
    for execution in executions:
        if execution.name == "plan_day" and _is_narratable(execution):
            return execution
    for execution in executions:
        if _is_narratable(execution):
            return execution
    return None


def _has_narratable_facts(executions: list[ToolExecution]) -> bool:
    """True only when a tool returned something the model may actually narrate.

    A clarification result carries no campus facts, so narration must not be
    allowed to assert anything beyond the question itself. This also closes the
    no-digits path where a fluent but invented answer could slip past the
    numeric grounding check.
    """
    return _primary_execution(executions) is not None


def _degraded_states_are_disclosed(answer: str,
                                    executions: list[ToolExecution]) -> bool:
    """Material degradations must be spoken; minor staleness need not be.

    Missing data (`unavailable`) and incomplete coverage (`partial`) always
    require a plain-language mention. `stale` only requires one when the answer
    itself claims currency, so a slightly old forecast does not force the model
    into robotic hedging that crowds out the actual answer.
    """
    text = answer.lower()
    mandatory = {
        "unavailable": ("unavailable", "not available", "not connected",
                        "couldn't", "cannot", "can't", "won't guess", "no data"),
        "partial": ("partial", "incomplete", "may be missing", "some data",
                    "some events", "not complete", "subset"),
    }
    for execution in executions:
        data = execution.public.get("data") or {}
        states = {str(data.get("state") or "").lower(),
                  str(data.get("status") or "").lower()}
        for state, words in mandatory.items():
            if state in states and not any(word in text for word in words):
                return False
        if "stale" in states:
            claims_currency = any(word in text for word in _CURRENCY_CLAIMS)
            disclosed = any(word in text for word in _STALENESS_DISCLOSURE)
            if claims_currency and not disclosed:
                return False
    return True


def _deterministic_answer(executions: list[ToolExecution]) -> str:
    """Safe fallback when provider narration is empty or ungrounded."""
    last = _best_for_text(executions)
    if last is None:
        return "I need one more detail before I can check campus data."
    if not last.public.get("ok"):
        err = last.public.get("error") or {}
        return (f"I couldn't use that campus tool: "
                f"{err.get('message', 'it is unavailable')}")
    data = last.public.get("data") or {}
    if isinstance(data.get("clarification"), dict):
        return str(data["clarification"].get("question")
                   or "I need one more detail before I can plan.")
    if last.name == "get_campus_schedule":
        if data.get("state") != "ok":
            return str(data.get("reason") or "No personal schedule is connected.")
        events = data.get("events") or []
        windows = data.get("available_windows") or []
        day = data.get("day") or data.get("date") or "that day"
        event_text = ""
        if events:
            event_text = "; ".join(
                f"{e.get('title', 'scheduled item')} from {e.get('start_time')} to {e.get('end_time')}"
                for e in events)
        free_text = " and ".join(
            f"{w.get('start_time')} to {w.get('end_time')}" for w in windows)
        if event_text and free_text:
            return (f"On {day}, you have {event_text}. Your code-checked "
                    f"{data.get('meal', 'meal')} windows are {free_text}.")
        if free_text:
            return f"Your code-checked {data.get('meal', 'meal')} windows on {day} are {free_text}."
        return f"I couldn't find a {data.get('meal', 'meal')} window on {day} that meets the requested duration."
    if last.name == "plan_day":
        return str(data.get("rationale") or "The deterministic planner returned no rationale.")
    if last.name == "find_food":
        items = data.get("items") or []
        if items:
            item = items[0]
            safety = ("declared allergens: " + ", ".join(item.get("allergens") or [])
                      if item.get("allergens_known") else "allergen information is UNKNOWN")
            venue = item.get("location_num")
            return (f"The best matching menu item is {item.get('name')} at "
                    f"location {venue}; {safety}.")
        skipped = data.get("sources_skipped") or []
        if any(str(row.get("status")) == "nutrition_unavailable" for row in skipped):
            # A calorie ceiling can only be proven per location, so ask which one
            # instead of returning a wall of internal jargon.
            names = [row.get("location_name") for row in skipped
                     if row.get("location_name")]
            hint = ", ".join(str(n).strip() for n in names[:4])
            return ("To respect a calorie ceiling I have to check one dining "
                    f"hall's nutrition data at a time. Which one? For example: "
                    f"{hint}.")
        closed = [row for row in (data.get("statuses") or [])
                  if row.get("open_now") is False]
        if closed:
            names = ", ".join(str(row.get("name") or row.get("location_num"))
                              for row in closed[:3])
            return (f"{names} is closed right now, so there are no menu items "
                    "to offer. Ask me again while it is open, or name another "
                    "dining hall.")
        return str(data.get("reason") or "No menu item matched those constraints.")
    if last.name == "get_events":
        rows = data.get("events") or []
        degraded = data.get("state") in ("partial", "stale")
        prefix = "The event snapshot is partial, so some events may be missing. " if degraded else ""
        if rows:
            return prefix + "Matching campus events: " + "; ".join(str(e.get("title")) for e in rows[:3]) + "."
        return prefix + str(data.get("reason") or "No campus events matched.")
    if last.name == "get_weather":
        if data.get("status") == "unavailable":
            return "Campus weather data is unavailable, so I won't guess about conditions."
        if data.get("status") == "partial":
            return "Some weather data is unavailable; review the partial National Weather Service evidence before heading out."
        if data.get("status") == "stale":
            return "The National Weather Service evidence is stale or cached; verify current conditions before heading out."
        return "I checked the National Weather Service campus forecast and alerts; review the returned source details before heading out."
    if last.name == "get_live_bus":
        if data.get("stale"):
            return str(data.get("reason") or "The vehicle snapshot is stale; use schedule-only transit information, not an ETA.")
        return str(data.get("reason") or f"The latest snapshot contains {data.get('count', 0)} observed buses; it does not provide exact ETAs.")
    if last.name == "get_next_departures":
        return str(data.get("reason") or "I found service-filtered scheduled departures in the campus transit data.")
    if last.name == "get_hours":
        windows = data.get("windows") or []
        if windows:
            parts = []
            for row in windows[:3]:
                open_t = str(row.get("open_time") or "")[:5]
                close_t = str(row.get("close_time") or "")[:5]
                parts.append(f"{open_t} to {close_t}".strip())
            state = ("open now" if any(row.get("is_open_now") for row in windows)
                     else "closed right now")
            return ("Today's source-backed hours are " + ", ".join(parts)
                    + f"; it is {state}.")
        return str(data.get("reason") or "I checked the source-backed dining hours.")
    return "I checked the available campus data, but I can't safely summarize it further."


def _sources(executions: list[ToolExecution]) -> list[dict[str, Any]]:
    labels = {
        "plan_day": "HokieFlow deterministic campus planner",
        "get_campus_schedule": "Your request-scoped schedule",
        "get_events": "Virginia Tech campus events snapshot",
        "get_weather": "National Weather Service",
        "get_live_bus": "Blacksburg Transit vehicle snapshot",
        "get_next_departures": "Blacksburg Transit GTFS schedule",
        "find_food": "Virginia Tech FoodPro menu",
        "get_hours": "Virginia Tech dining hours",
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for execution in executions:
        if not execution.public.get("ok") or execution.name in seen:
            continue
        seen.add(execution.name)
        data = execution.public.get("data") or {}
        source: dict[str, Any] = {
            "file_id": execution.name,
            "title": labels.get(execution.name, execution.name),
        }
        updated = data.get("fetched_at")
        if updated:
            source["updated_at"] = updated
        state = data.get("state") or data.get("status")
        if state:
            source["state"] = state
        out.append(source)
    return out


def _envelope(answer: str, executions: list[ToolExecution], provider: Provider,
              context: AgentContext, turns: int, usage: dict[str, Any],
              grounding_replaced: bool) -> dict[str, Any]:
    raw_result = None
    primary = _primary_execution(executions)
    if primary is not None:
        raw_result = primary.raw_result
    else:
        raw_result = next((e.raw_result for e in reversed(executions)
                           if e.raw_result is not None), None)
    result = raw_result if isinstance(raw_result, dict) else None
    # A tool-level clarification (for example a missing deadline) is a
    # first-class result the UI already knows how to render.
    clarification = None
    if result:
        clarification = result.get("clarification")
    if clarification is None:
        for execution in reversed(executions):
            nested = execution.public.get("data") or {}
            if isinstance(nested.get("clarification"), dict):
                clarification = nested["clarification"]
                break
    envelope: dict[str, Any] = {
        "answer": answer,
        "result": result,
        "clarification": clarification,
        "sources": _sources(executions),
        "provenance": {
            "provider": str(provider.name),
            "model": str(provider.model),
            "tool_names": [e.name for e in executions],
            "replay": bool(context.is_replay),
            "grounding_replaced": grounding_replaced,
        },
        "agent": {"name": "HokieFlow AI", "status": "ok", "turns": turns,
                  "tool_calls": len(executions), "usage": usage},
    }
    # Preserve the legacy structured planner contract at top level.
    if result and any(k in result for k in ("itinerary", "feasible",
                                             "replan_trigger")):
        envelope.update(result)
        envelope["answer"] = answer
        envelope["result"] = result
        envelope["sources"] = _sources(executions)
        envelope["provenance"] = {
            "provider": str(provider.name), "model": str(provider.model),
            "tool_names": [e.name for e in executions],
            "replay": bool(context.is_replay),
            "grounding_replaced": grounding_replaced,
        }
        envelope["agent"] = {"name": "HokieFlow AI", "status": "ok",
                             "turns": turns, "tool_calls": len(executions),
                             "usage": usage}
    return envelope


def run_agent(text: str, *, provider: Provider, context: AgentContext,
              registry: ToolRegistry | None = None) -> dict[str, Any]:
    """One bounded, grounded agent run.

    The request clock is pinned for the whole run, so a direct tool call (food,
    hours, buses, weather) agrees with the same instant `plan_day` uses instead
    of re-reading the wall clock a few milliseconds later.
    """
    aware_now = (context.now if context.now.tzinfo is not None
                 else context.now.replace(tzinfo=_CAMPUS_TZ))
    clock_token = tools._REQUEST_NOW.set(aware_now.astimezone(_CAMPUS_TZ))
    try:
        return _run_agent_locked(text, provider=provider, context=context,
                                 registry=registry)
    finally:
        tools._REQUEST_NOW.reset(clock_token)


def _run_agent_locked(text: str, *, provider: Provider, context: AgentContext,
                      registry: ToolRegistry | None = None) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise AgentUnavailable("empty_input", "Ask a campus-life question first.")
    if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise AgentUnavailable("input_too_large", "The request is too long.")
    registry = registry or ToolRegistry()
    prompt = system_prompt(
        campus_now=context.now.astimezone(_CAMPUS_TZ).isoformat(timespec="seconds"),
        is_replay=context.is_replay,
    )
    messages = [ProviderMessage(role="user", text=raw)]
    executions: list[ToolExecution] = []
    repeats: dict[str, int] = {}
    usage: dict[str, Any] = {}

    for turn in range(1, MAX_TURNS + 1):
        final_turn = turn == MAX_TURNS
        try:
            response = provider.generate(
                system_prompt=prompt, messages=messages,
                tools=[] if final_turn else registry.declarations(),
                timeout_s=PROVIDER_TIMEOUT_S)
        except ProviderUnavailable as exc:
            raise AgentUnavailable("provider_unavailable",
                                   "HokieFlow's language provider is unavailable.",
                                   exc.to_dict()) from exc
        except TimeoutError as exc:
            raise AgentUnavailable("provider_timeout",
                                   "HokieFlow's language provider timed out.") from exc
        except Exception as exc:
            raise AgentUnavailable("provider_error",
                                   "HokieFlow's language provider failed safely.") from exc
        usage = response.usage or usage
        if response.tool_calls:
            if len(executions) + len(response.tool_calls) > MAX_TOOL_CALLS:
                raise AgentUnavailable("tool_call_limit",
                                       "HokieFlow reached its safe tool-call limit.")
            messages.append(ProviderMessage(role="assistant", text=response.text,
                                            native=response.native))
            tool_results = []
            for call in response.tool_calls:
                key = _call_key(call.name, call.arguments)
                repeats[key] = repeats.get(key, 0) + 1
                if repeats[key] > MAX_IDENTICAL_CALLS:
                    raise AgentUnavailable("repeated_tool_call",
                                           "HokieFlow stopped a repeated tool-call loop.")
                execution = registry.dispatch(call.name, call.arguments, context)
                executions.append(execution)
                tool_results.append({"id": call.call_id, "name": call.name,
                                     "response": execution.public})
            messages.append(ProviderMessage(role="tool", tool_results=tool_results))
            continue

        answer = response.text.strip()
        # A tool-free final response may ask for a missing constraint, but it
        # may not assert campus facts from model memory. This closes the gap
        # where a fluent, number-free hallucination could evade numeric checks.
        has_evidence = _has_narratable_facts(executions)
        answer_times, answer_numbers = _split_numbers_and_times(answer)
        tool_free_clarification = (not executions and answer.endswith("?")
                                   and not answer_times and not answer_numbers)
        # Nothing to narrate is not a licence to describe a campus that exists:
        # the answer must acknowledge the empty result ("D2 is closed", "no
        # departures found") or it is replaced.
        lower = answer.lower()
        empty_acknowledged = (bool(executions) and not has_evidence
                              and any(word in lower
                                      for word in _EMPTY_ACKNOWLEDGED))
        grounded = (bool(answer)
                    and (has_evidence or tool_free_clarification
                         or empty_acknowledged)
                    and _numbers_are_grounded(answer, executions)
                    and _degraded_states_are_disclosed(answer, executions))
        replaced = not grounded
        if not grounded:
            answer = _deterministic_answer(executions)
        return _envelope(answer, executions, provider, context, turn, usage,
                         replaced)

    raise AgentUnavailable("turn_limit", "HokieFlow reached its safe turn limit.")
