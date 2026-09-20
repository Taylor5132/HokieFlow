from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from app import server
from app.gemini_provider import GeminiProvider
from app.local_env import load_local_env
from hokieday import agent, agent_tools, config
from hokieday.agent_tools import AgentContext, ToolRegistry
from hokieday.providers.base import (ProviderMessage, ProviderResponse,
                                      ProviderUnavailable, ToolCall)

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 19, 11, 22, tzinfo=TZ)


class FakeProvider:
    name = "fake"
    model = "fake-grounded-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.requests = []

    def generate(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(*, text="", calls=()):
    return ProviderResponse(text=text, tool_calls=list(calls),
                            finish_reason="STOP", native={
                                "role": "model", "parts": ([{"text": text}]
                                if text else [])})


class AgentToolTests(unittest.TestCase):
    def test_unknown_and_invalid_tools_are_rejected(self):
        registry = ToolRegistry()
        context = AgentContext(now=NOW)
        unknown = registry.dispatch("run_shell", {"command": "rm -rf /"}, context)
        self.assertFalse(unknown.public["ok"])
        self.assertEqual(unknown.public["error"]["code"], "unknown_tool")
        invalid = registry.dispatch("get_weather", {"hours": 1000}, context)
        self.assertFalse(invalid.public["ok"])
        self.assertEqual(invalid.public["error"]["code"], "invalid_arguments")
        extra = registry.dispatch("get_weather", {"url": "https://evil.example"}, context)
        self.assertFalse(extra.public["ok"])
        oversized = registry.dispatch(
            "get_events", {"tags": ["x" * 9000]}, context)
        self.assertFalse(oversized.public["ok"])
        self.assertEqual(oversized.public["error"]["code"], "invalid_arguments")

    def test_schedule_computes_realistic_monday_lunch_windows(self):
        schedule = [{
            "title": "CS3114", "kind": "class", "location": "McBryde 100",
            "start": "2026-09-21T12:30:00-04:00",
            "end": "2026-09-21T13:45:00-04:00",
            "repeat": "weekly", "repeatUntil": "2026-12-09",
        }]
        execution = ToolRegistry().dispatch(
            "get_campus_schedule",
            {"day": "monday", "meal": "lunch", "minimum_minutes": 20},
            AgentContext(now=NOW, schedule=schedule),
        )
        self.assertTrue(execution.public["ok"])
        data = execution.public["data"]
        self.assertEqual(data["events"][0]["start_time"], "12:30 PM")
        self.assertEqual(data["events"][0]["end_time"], "1:45 PM")
        self.assertEqual(
            [(w["start_time"], w["end_time"]) for w in data["available_windows"]],
            [("11:00 AM", "12:30 PM"), ("1:45 PM", "3:00 PM")],
        )

    def test_schedule_absence_is_unknown_not_free(self):
        execution = ToolRegistry().dispatch(
            "get_campus_schedule", {"day": "monday"}, AgentContext(now=NOW))
        self.assertEqual(execution.public["data"]["state"], "unavailable")
        self.assertEqual(execution.public["data"]["available_windows"], [])

    def test_device_coordinates_are_removed_from_model_result(self):
        dynamic = config.register_dynamic_place(37.22925, -80.42396)
        safe = ToolRegistry().dispatch(
            "plan_day", {"start": "11:22", "end": "13:25", "prefs": {}},
            AgentContext(now=NOW, origin_place=dynamic),
        )
        rendered = json.dumps(safe.public)
        self.assertNotIn("from_coords", rendered)
        self.assertNotIn("to_coords", rendered)
        self.assertNotIn("student_ref", rendered)
        self.assertNotIn("37.22925", rendered)
        self.assertNotIn("-80.42396", rendered)
        self.assertIn("your location", rendered)

    def test_required_allergen_survives_model_omission(self):
        execution = ToolRegistry().dispatch(
            "plan_day", {"start": "11:22", "end": "13:25", "prefs": {}},
            AgentContext(now=NOW, required_prefs={"avoid": ["Peanuts"]}),
        )
        self.assertTrue(execution.public["ok"])
        self.assertIn("Peanuts", execution.raw_result["constraints"]["avoid"])

    def test_text_origin_is_preserved_without_device_origin(self):
        execution = ToolRegistry().dispatch(
            "plan_day", {"start": "11:22", "end": "13:25", "prefs": {}},
            AgentContext(now=NOW, required_prefs={"from_place": "Hahn Hall"}),
        )
        self.assertEqual(execution.raw_result["constraints"]["from_place"],
                         "Hahn Hall")

    def test_device_origin_takes_precedence_over_text_origin(self):
        execution = ToolRegistry().dispatch(
            "plan_day", {"start": "11:22", "end": "13:25", "prefs": {}},
            AgentContext(now=NOW, origin_place="McBryde Hall",
                         required_prefs={"from_place": "Hahn Hall"}),
        )
        self.assertEqual(execution.raw_result["constraints"]["from_place"],
                         "McBryde Hall")

    def test_calorie_ceiling_is_extracted_and_cannot_be_weakened(self):
        required = server._agent_hard_constraints(
            "Keep lunch under 600 calories, actually no more than 550 kcal.")
        self.assertEqual(required, {"max_kcal": 550.0})
        execution = ToolRegistry().dispatch(
            "plan_day", {"start": "11:22", "end": "13:25",
                         "prefs": {"max_kcal": 900}},
            AgentContext(now=NOW, required_prefs=required),
        )
        self.assertEqual(execution.raw_result["constraints"]["max_kcal"], 550.0)


class AgentLoopTests(unittest.TestCase):
    def test_request_clock_is_pinned_for_direct_tool_calls(self):
        """A direct tool call must use the request instant, not the wall clock."""
        pinned = datetime(2026, 9, 19, 11, 22, tzinfo=TZ)
        provider = FakeProvider([
            response(calls=[ToolCall("get_hours", {"location": "D2"}, "hours")]),
            response(text="D2's hours are listed above."),
        ])
        result = agent.run_agent("D2 hours?", provider=provider,
                                 context=AgentContext(now=pinned))
        windows = result["result"]["windows"]
        self.assertTrue(windows)
        # The pinned 11:22 clock sits inside the first window, so is_open_now is
        # only True if the request clock reached the tool.
        self.assertTrue(windows[0]["is_open_now"])

    def _context(self, schedule=None):
        return AgentContext(now=NOW, schedule=schedule or [])

    def test_realistic_schedule_question_is_grounded(self):
        schedule = [{
            "title": "CS3114", "kind": "class", "location": "McBryde 100",
            "start": "2026-09-21T12:30:00-04:00",
            "end": "2026-09-21T13:45:00-04:00",
            "repeat": "weekly", "repeatUntil": "2026-12-09",
        }]
        provider = FakeProvider([
            response(calls=[ToolCall("get_campus_schedule", {
                "day": "monday", "meal": "lunch", "minimum_minutes": 20,
            }, "call-1")]),
            response(text=("You have CS3114 from 12:30 PM to 1:45 PM on Monday, "
                           "so lunch fits from 11:00 AM to 12:30 PM or from "
                           "1:45 PM to 3:00 PM.")),
        ])
        result = agent.run_agent("When can I eat lunch on Monday?",
                                 provider=provider,
                                 context=self._context(schedule))
        self.assertEqual(provider.calls, 2)
        self.assertIn("CS3114", result["answer"])
        self.assertIn("12:30 PM", result["answer"])
        self.assertEqual(result["provenance"]["tool_names"],
                         ["get_campus_schedule"])
        self.assertFalse(result["provenance"]["grounding_replaced"])

    def test_unavailable_schedule_cannot_be_narrated_as_free_time(self):
        provider = FakeProvider([
            response(calls=[ToolCall("get_campus_schedule", {
                "day": "monday", "meal": "lunch"}, "call-1")]),
            response(text="You are free for lunch all day."),
        ])
        result = agent.run_agent("When can I eat lunch?", provider=provider,
                                 context=self._context())
        self.assertNotIn("free for lunch", result["answer"])
        self.assertIn("No personal schedule", result["answer"])
        self.assertTrue(result["provenance"]["grounding_replaced"])

    def test_grounding_accepts_value_preserving_paraphrases(self):
        with mock.patch.object(agent, "_deterministic_answer",
                               return_value="fallback"):
            provider = FakeProvider([
                response(calls=[ToolCall("get_campus_schedule", {
                    "day": "monday", "meal": "lunch"}, "call-1")]),
                # "12:30 PM" -> "12:30 pm", "1 hour" style totals are not used.
                response(text="Lunch fits until 12:30 pm on Monday."),
            ])
            schedule = [{
                "title": "CS3114", "kind": "class", "location": "McBryde",
                "start": "2026-09-21T12:30:00-04:00",
                "end": "2026-09-21T13:45:00-04:00", "repeat": "none",
            }]
            result = agent.run_agent(
                "When can I eat lunch on Monday?", provider=provider,
                context=self._context(schedule))
        self.assertFalse(result["provenance"]["grounding_replaced"])
        self.assertIn("12:30 pm", result["answer"])

    def test_grounding_rejects_a_number_that_no_tool_returned(self):
        provider = FakeProvider([
            response(calls=[ToolCall("get_campus_schedule", {
                "day": "monday", "meal": "lunch"}, "call-1")]),
            response(text="You can eat at 9:15 pm on Monday."),
        ])
        schedule = [{
            "title": "CS3114", "kind": "class", "location": "McBryde",
            "start": "2026-09-21T12:30:00-04:00",
            "end": "2026-09-21T13:45:00-04:00", "repeat": "none",
        }]
        result = agent.run_agent(
            "When can I eat lunch on Monday?", provider=provider,
            context=self._context(schedule))
        self.assertTrue(result["provenance"]["grounding_replaced"])
        self.assertNotIn("9:15", result["answer"])

    def test_request_window_wins_over_a_model_supplied_one(self):
        execution = ToolRegistry().dispatch(
            "plan_day", {"start": "03:00", "end": "03:05", "prefs": {}},
            AgentContext(now=NOW, plan_start="2026-09-19T11:22:00-04:00",
                         plan_end="2026-09-19T13:25:00-04:00"),
        )
        self.assertTrue(execution.public["ok"])
        self.assertEqual(execution.raw_result["constraints"]["window"]["end"][:16]
                         if isinstance(execution.raw_result["constraints"].get("window"), dict)
                         else execution.raw_result["itinerary"]["window_end"][:16],
                         "2026-09-19T13:25")

    def test_missing_deadline_asks_instead_of_inventing_one(self):
        execution = ToolRegistry().dispatch(
            "plan_day", {"start": "2026-09-19T11:22:00-04:00", "prefs": {}},
            AgentContext(now=NOW))
        data = execution.public["data"]
        self.assertEqual(data["state"], "clarification_needed")
        self.assertEqual(data["clarification"]["kind"], "need_deadline")
        self.assertEqual(execution.raw_result["state"], "clarification_needed")

    def test_missing_deadline_envelope_reaches_the_ui_contract(self):
        provider = FakeProvider([
            response(calls=[ToolCall("plan_day", {"prefs": {}}, "call-1")]),
            response(text="Sure, lunch is at noon."),
        ])
        result = agent.run_agent("I'm hungry", provider=provider,
                                 context=AgentContext(now=NOW))
        self.assertEqual(result["clarification"]["kind"], "need_deadline")
        self.assertNotIn("noon", result["answer"])

    def test_dining_names_resolve_to_location_numbers(self):
        self.assertEqual(agent_tools._resolve_dining_location("D2"), "15")
        self.assertEqual(agent_tools._resolve_dining_location("d2"), "15")
        self.assertEqual(agent_tools._resolve_dining_location("D2 at Dietrick Hall"), "15")
        self.assertEqual(agent_tools._resolve_dining_location("Owens Food Court"), "39")
        self.assertEqual(agent_tools._resolve_dining_location("Owens"), "39")
        self.assertIsNone(agent_tools._resolve_dining_location("Narnia Cafe"))

    def test_unknown_dining_name_is_typed_not_guessed(self):
        execution = ToolRegistry().dispatch(
            "get_hours", {"location": "Narnia Cafe"}, AgentContext(now=NOW))
        data = execution.public["data"]
        self.assertEqual(data["state"], "unknown_location")
        self.assertTrue(data["known_locations"])

    def test_food_by_student_facing_name(self):
        execution = ToolRegistry().dispatch(
            "find_food", {"location": "D2", "max_kcal": 500},
            AgentContext(now=NOW))
        self.assertTrue(execution.public["ok"])
        self.assertEqual(execution.raw_result["location_num"], "15")

    def test_plan_result_outranks_a_secondary_lookup(self):
        provider = FakeProvider([
            response(calls=[ToolCall("plan_day", {"prefs": {}}, "plan")]),
            response(calls=[ToolCall("find_food", {"location": "D2"}, "food")]),
            response(text="Buffalo Chicken Ranch Wrap is available."),
        ])
        result = agent.run_agent(
            "I'm hungry and need to be at McBryde by 1:25 PM", provider=provider,
            context=AgentContext(now=NOW, plan_start="2026-09-19T11:22:00-04:00",
                                 plan_end="2026-09-19T13:25:00-04:00"))
        # The structured plan must survive a follow-up lookup.
        self.assertIsInstance(result["result"], dict)
        self.assertIn("feasible", result["result"])
        self.assertIn("itinerary", result["result"])
        self.assertEqual(result["provenance"]["tool_names"],
                         ["plan_day", "find_food"])

    def test_kcal_ceiling_campus_wide_asks_which_hall(self):
        provider = FakeProvider([
            response(calls=[ToolCall("find_food", {"max_kcal": 500}, "food")]),
            response(text="Everything is under 500 calories everywhere."),
        ])
        result = agent.run_agent("What's under 500 calories?", provider=provider,
                                 context=AgentContext(now=NOW))
        self.assertNotIn("Everything is under 500", result["answer"])
        self.assertIn("dining hall", result["answer"])

    def test_stop_names_and_ids_both_resolve(self):
        for value in ("1125", "Tennis Courts", "tennis courts"):
            execution = ToolRegistry().dispatch(
                "get_next_departures", {"stop": value, "horizon_min": 180},
                AgentContext(now=NOW))
            self.assertTrue(execution.public["ok"], value)
            self.assertEqual(execution.public["data"]["stop_id"], "1125", value)

    def test_unknown_stop_is_typed_not_guessed(self):
        execution = ToolRegistry().dispatch(
            "get_next_departures", {"stop": "Platform 9 3/4"},
            AgentContext(now=NOW))
        data = execution.public["data"]
        self.assertEqual(data["state"], "unknown_stop")
        self.assertTrue(data["known_stops"])

    def test_food_status_agrees_with_the_request_clock(self):
        """A plan and its dining status must not disagree about 'open now'."""
        execution = ToolRegistry().dispatch(
            "find_food", {"location": "D2", "open_only": True},
            AgentContext(now=NOW))
        status = (execution.public["data"].get("statuses") or [{}])[0]
        self.assertTrue(status.get("open_now"))
        self.assertTrue(execution.public["data"]["items"])

    def test_unsupported_number_is_replaced_by_code_answer(self):
        schedule = [{
            "title": "CS3114", "kind": "class", "location": "McBryde",
            "start": "2026-09-21T12:30:00-04:00",
            "end": "2026-09-21T13:45:00-04:00", "repeat": "none",
        }]
        provider = FakeProvider([
            response(calls=[ToolCall("get_campus_schedule", {
                "day": "monday", "meal": "lunch"}, "call-1")]),
            response(text="You have 99 minutes for lunch."),
        ])
        result = agent.run_agent("When is lunch?", provider=provider,
                                 context=self._context(schedule))
        self.assertNotIn("99", result["answer"])
        self.assertTrue(result["provenance"]["grounding_replaced"])
        self.assertIn("12:30 PM", result["answer"])

    def test_repeated_call_loop_is_stopped(self):
        call = ToolCall("get_weather", {"hours": 1}, "same")
        provider = FakeProvider([response(calls=[call]), response(calls=[call]),
                                 response(calls=[call])])
        with self.assertRaises(agent.AgentUnavailable) as caught:
            agent.run_agent("Weather?", provider=provider,
                            context=self._context())
        self.assertEqual(caught.exception.code, "repeated_tool_call")

    def test_tool_free_campus_claim_is_not_returned(self):
        provider = FakeProvider([response(text="D2 is open now.")])
        result = agent.run_agent("Is D2 open?", provider=provider,
                                 context=self._context())
        self.assertNotIn("open now", result["answer"])
        self.assertTrue(result["provenance"]["grounding_replaced"])

    def test_tool_free_clarification_is_allowed(self):
        provider = FakeProvider([response(text="Which day should I check?")])
        result = agent.run_agent("When can I eat?", provider=provider,
                                 context=self._context())
        self.assertEqual(result["answer"], "Which day should I check?")
        self.assertFalse(result["provenance"]["grounding_replaced"])

    def test_turn_limit_stops_compositional_loop(self):
        provider = FakeProvider([
            response(calls=[ToolCall("get_weather", {"hours": 1}, "one")]),
            response(calls=[ToolCall("get_weather", {"hours": 2}, "two")]),
            response(calls=[ToolCall("get_weather", {"hours": 3}, "three")]),
        ])
        with self.assertRaises(agent.AgentUnavailable) as caught:
            agent.run_agent("Keep checking weather", provider=provider,
                            context=self._context())
        self.assertEqual(caught.exception.code, "turn_limit")
        self.assertEqual(provider.calls, 3)

    def test_provider_failure_is_typed(self):
        provider = FakeProvider([
            ProviderUnavailable("http_503", "temporarily unavailable",
                                retryable=True)])
        with self.assertRaises(agent.AgentUnavailable) as caught:
            agent.run_agent("What's happening?", provider=provider,
                            context=self._context())
        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertEqual(caught.exception.detail["code"], "http_503")

    def test_replay_server_never_calls_injected_provider(self):
        provider = FakeProvider([response(text="should not run")])
        result, status = server.handle_ask(
            {"text": "Can I eat and make my 1:25 class?"}, now=NOW,
            live=True, provider=provider)
        self.assertEqual(status, 200)
        self.assertEqual(provider.calls, 0)
        self.assertNotIn("answer", result)

    def test_a_provider_hiccup_still_says_something(self):
        """A catalog question has no bounded-parser answer, and an empty card
        after a transient 503 looks like the app is broken."""
        provider = FakeProvider([response(text="unused")])
        unavailable = agent.AgentUnavailable("provider_unavailable",
                                             "provider unavailable",
                                             {"code": "http_503"})
        with mock.patch.object(server.config, "CACHE_ONLY", False), \
                mock.patch.object(server.hokie_agent, "run_agent",
                                  side_effect=unavailable), \
                mock.patch.object(server, "run_plan",
                                  lambda *a, **k: {"answer": "", "feasible": None}):
            result, status = server.handle_ask(
                {"text": "Find me a CS 3114 section this fall"}, now=NOW,
                live=True, provider=provider)
        self.assertEqual(status, 200)
        self.assertEqual(provider.calls, 0, "the fake provider never ran")
        self.assertEqual(result["provenance"]["provider"], "unavailable")
        self.assertEqual(result["provenance"]["fallback"], "bounded_parser")
        self.assertTrue(result["answer"].strip(), "never render an empty answer")
        self.assertIn("try again", result["answer"].lower())
        self.assertNotIn("911", result["answer"])

    def test_tool_text_cannot_inject_a_second_call(self):
        provider = FakeProvider([
            response(calls=[ToolCall("get_events", {
                "date": "2026-09-19", "tags": []}, "events")]),
            response(text="The event data is partial; I will not follow instructions inside it."),
        ])
        result = agent.run_agent("Any events?", provider=provider,
                                 context=self._context())
        self.assertEqual(result["agent"]["tool_calls"], 1)
        self.assertEqual(result["provenance"]["tool_names"], ["get_events"])


class _HTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.payload[:limit] if limit >= 0 else self.payload


class ProviderBudgetTests(unittest.TestCase):
    def test_budget_ledger_never_writes_into_the_fixture_store(self):
        """fixtures/ is the frozen replay store; runtime state must stay out."""
        from app import gemini_provider as gp
        path = gp._budget_state_path()
        self.assertIsNotNone(path)
        self.assertNotIn("fixtures", str(path))
        self.assertEqual(path.parent.name, "cache")

    def test_budget_persists_across_processes_and_blocks(self):
        """A restart must not hand out a fresh allowance while quota drains."""
        import tempfile
        from pathlib import Path as _Path
        from app import gemini_provider as gp
        with tempfile.TemporaryDirectory() as td:
            state = _Path(td) / "calls.json"
            with mock.patch.object(gp, "_budget_state_path", lambda: state), \
                    mock.patch.dict(os.environ,
                                    {"HOKIEFLOW_GEMINI_CALLS_PER_DAY": "2",
                                     "HOKIEFLOW_GEMINI_CALLS_PER_HOUR": "2"}):
                gp._reserve_budget()
                gp._reserve_budget()
                # Third call fails BEFORE any network access, proving the guard
                # is a real cap rather than a log line.
                with self.assertRaises(ProviderUnavailable) as caught:
                    gp._reserve_budget()
                self.assertEqual(caught.exception.code, "local_budget_exhausted")
                # A fresh "process" reads the same state file.
                self.assertEqual(len(gp._load_budget_state()), 2)


class ServerBudgetTests(unittest.TestCase):
    def test_per_client_question_budget_is_bounded(self):
        with server._CLIENT_BUDGET_LOCK:
            server._CLIENT_QUESTION_TIMES.clear()
        with mock.patch.dict(os.environ,
                             {"HOKIEFLOW_AI_QUESTIONS_PER_IP_HOUR": "1"}):
            self.assertTrue(server._client_agent_budget_available("127.0.0.1"))
            self.assertFalse(server._client_agent_budget_available("127.0.0.1"))
            self.assertTrue(server._client_agent_budget_available("127.0.0.2"))


class LocalEnvTests(unittest.TestCase):
    def test_loads_allowlist_without_overriding_process_environment(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text("GEMINI_API_KEY=file-key\nGEMINI_MODEL=file-model\n")
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "process-key"},
                                 clear=False):
                os.environ.pop("GEMINI_MODEL", None)
                load_local_env(path)
                self.assertEqual(os.environ["GEMINI_API_KEY"], "process-key")
                self.assertEqual(os.environ["GEMINI_MODEL"], "file-model")
                os.environ.pop("GEMINI_MODEL", None)

    def test_loads_keys_the_loader_does_not_know_about(self):
        """A .env may carry settings for other integrations (e.g. Supabase).

        Rejecting unknown keys made the server fail to import the moment the
        accounts integration was merged, so unknown keys are loaded, not policed.
        """
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text("SUPABASE_URL=https://example.supabase.co\n"
                            "APP_ENV=production\n")
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SUPABASE_URL", None)
                os.environ.pop("APP_ENV", None)
                applied = load_local_env(path)
                self.assertEqual(os.environ["SUPABASE_URL"],
                                 "https://example.supabase.co")
                self.assertIn("SUPABASE_URL", applied)
                os.environ.pop("SUPABASE_URL", None)
                os.environ.pop("APP_ENV", None)

    def test_refuses_a_world_readable_secret_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text("GEMINI_API_KEY=secret\n")
            path.chmod(0o644)
            with self.assertRaises(RuntimeError):
                load_local_env(path)

    def test_rejects_malformed_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text("this is not an assignment\n")
            path.chmod(0o600)
            with self.assertRaises(RuntimeError):
                load_local_env(path)


class GeminiAdapterTests(unittest.TestCase):
    def setUp(self):
        """Isolate the persisted call ledger.

        The adapter guards the shared account quota through a JSON ledger in
        cache/, so without this the tests depend on how much live testing this
        machine has already done and fail with local_budget_exhausted instead of
        exercising the HTTP paths they are about.
        """
        import tempfile
        from pathlib import Path
        from app import gemini_provider
        self._tmp = tempfile.TemporaryDirectory()
        self._ledger = Path(self._tmp.name) / "ai_provider_calls.json"
        self._patch = mock.patch.object(gemini_provider, "_budget_state_path",
                                        lambda: self._ledger)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_function_call_parsing_and_secret_header(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _HTTPResponse({
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"role": "model", "parts": [{
                        "functionCall": {"id": "abc", "name": "get_weather",
                                         "args": {"hours": 3}},
                        "thoughtSignature": "opaque",
                    }, {
                        "functionCall": {"name": "get_events",
                                         "args": {"date": "today"}},
                    }]},
                }],
                "usageMetadata": {"totalTokenCount": 7},
            })

        provider = GeminiProvider("super-secret", "gemini-test", opener=opener)
        result = provider.generate(
            system_prompt="policy", messages=[ProviderMessage("user", "weather")],
            tools=ToolRegistry().declarations(), timeout_s=4)
        self.assertEqual(result.tool_calls[0].name, "get_weather")
        self.assertEqual(result.tool_calls[0].arguments, {"hours": 3})
        self.assertEqual(result.tool_calls[0].call_id, "abc")
        self.assertEqual(result.tool_calls[1].call_id, "")
        self.assertNotIn("super-secret", captured["request"].full_url)
        self.assertEqual(captured["request"].headers["X-goog-api-key"],
                         "super-secret")
        self.assertEqual(result.native["parts"][0]["thoughtSignature"], "opaque")

    def test_http_error_is_typed_without_body_leak(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {},
                                         io.BytesIO(b'{}'))

        provider = GeminiProvider("secret", "gemini-test", opener=opener)
        with self.assertRaises(ProviderUnavailable) as caught:
            provider.generate(system_prompt="p",
                              messages=[ProviderMessage("user", "x")],
                              tools=ToolRegistry().declarations(), timeout_s=1)
        self.assertEqual(caught.exception.code, "http_429")
        self.assertTrue(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
