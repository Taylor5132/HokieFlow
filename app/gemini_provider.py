"""Minimal stdlib Gemini GenerateContent adapter.

The deterministic core does not import a provider SDK. This adapter uses only
Google's fixed Gemini API origin, sends the key in a header (never a URL/log),
and preserves model parts verbatim so Gemini thought signatures survive tool
round-trips.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from hokieday.providers.base import (ProviderMessage, ProviderResponse, ProviderUnavailable,
                                      ToolCall)

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_MAX_RESPONSE_BYTES = 2_000_000
_BUDGET_LOCK = threading.Lock()

# Local ledger caps. Sized so the per-IP question guard trips first and Google's
# account quota stays the real ceiling: the old 30/120 answered a busy demo with
# local_budget_exhausted while the account was nowhere near its own limit. Both
# are env-overridable (HOKIEFLOW_GEMINI_CALLS_PER_HOUR / _PER_DAY).
DEFAULT_CALLS_PER_HOUR = 600
DEFAULT_CALLS_PER_DAY = 3000


def _bounded_env_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(max(value, 1), maximum)


def _budget_state_path():
    """Where the call ledger lives.

    Deliberately NOT `config.CACHE_DIR`: in DEMO_MODE=cache that points at the
    committed `fixtures/` replay store, and runtime bookkeeping must never be
    written into a frozen fixture (it would show up as a repo change).
    """
    try:
        from hokieday import config
        return Path(config.REPO_DIR) / "cache" / "ai_provider_calls.json"
    except Exception:                                        # noqa: BLE001
        return None


def _load_budget_state() -> list[float]:
    """Real call times, so the guard survives a server restart.

    An in-memory counter is useless against the realistic failure mode: a demo
    restarts the server a few times and each process gets a fresh allowance
    while the ACCOUNT quota keeps draining.
    """
    path = _budget_state_path()
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    stamps = []
    for value in data if isinstance(data, list) else []:
        try:
            stamps.append(float(value))
        except (TypeError, ValueError):
            continue
    return sorted(stamps)


def _save_budget_state(stamps: list[float]) -> None:
    path = _budget_state_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(stamps[-500:]), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _budget_limits() -> tuple[int, int]:
    """(calls per hour, calls per day) for the local ledger."""
    return (
        _bounded_env_int("HOKIEFLOW_GEMINI_CALLS_PER_HOUR",
                         DEFAULT_CALLS_PER_HOUR, 10_000),
        _bounded_env_int("HOKIEFLOW_GEMINI_CALLS_PER_DAY",
                         DEFAULT_CALLS_PER_DAY, 100_000),
    )


def _limit_source(name: str) -> str:
    """Whether a limit came from the environment or from the code default.

    A deployment can pin the old small values as app settings, which would leave
    a new default with no effect -- and nothing in the response would say so.
    """
    return "env" if (os.environ.get(name) or "").strip() else "default"


def budget_snapshot() -> dict:
    """How much of the local call budget is left, without spending any of it.

    Published on /api/status so a rate-limited answer can be told apart from a
    dead provider at a glance: a demo that suddenly answers nothing is usually
    this guard or the per-client question throttle, not the Gemini key.
    """
    now = time.time()
    hourly, daily = _budget_limits()
    stamps = [s for s in _load_budget_state() if now - s < 86_400]
    in_hour = sum(1 for stamp in stamps if now - stamp < 3_600)
    return {
        "calls_last_hour": in_hour,
        "calls_last_hour_limit": hourly,
        "calls_today": len(stamps),
        "calls_today_limit": daily,
        "remaining_this_hour": max(0, hourly - in_hour),
        "remaining_today": max(0, daily - len(stamps)),
        "limits_source": {
            "calls_per_hour": _limit_source("HOKIEFLOW_GEMINI_CALLS_PER_HOUR"),
            "calls_per_day": _limit_source("HOKIEFLOW_GEMINI_CALLS_PER_DAY"),
        },
    }


def _reserve_budget() -> None:
    """Guard the shared account quota, across processes and restarts."""
    now = time.time()
    hourly, daily = _budget_limits()
    with _BUDGET_LOCK:
        stamps = [s for s in _load_budget_state() if now - s < 86_400]
        in_hour = sum(1 for stamp in stamps if now - stamp < 3_600)
        if in_hour >= hourly or len(stamps) >= daily:
            raise ProviderUnavailable(
                "local_budget_exhausted",
                ("The local Gemini usage budget is exhausted "
                 f"({in_hour}/{hourly} this hour, {len(stamps)}/{daily} today); "
                 "try again after the quota window resets."),
                retryable=True,
            )
        stamps.append(now)
        _save_budget_state(stamps)


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, *,
                 opener: Callable[..., Any] | None = None) -> None:
        key = str(api_key or "").strip()
        model = str(model or "").strip().removeprefix("models/")
        if not key:
            raise ValueError("Gemini API key is required")
        if not _MODEL_RE.fullmatch(model):
            raise ValueError("invalid Gemini model id")
        self._api_key = key
        self.model = model
        self._opener = opener or urllib.request.urlopen

    def _contents(self, messages: list[ProviderMessage]) -> list[dict]:
        contents: list[dict] = []
        for message in messages:
            if message.native is not None and message.role == "assistant":
                # Preserve all provider-returned parts, including opaque thought
                # signatures required by Gemini 3 thinking models.
                contents.append(message.native)
                continue
            if message.tool_results:
                parts = []
                for item in message.tool_results:
                    response = {
                        "name": str(item.get("name") or ""),
                        "response": item.get("response") or {},
                    }
                    if item.get("id"):
                        response["id"] = str(item["id"])
                    parts.append({"functionResponse": response})
                contents.append({"role": "user", "parts": parts})
                continue
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role,
                             "parts": [{"text": str(message.text)}]})
        return contents

    def generate(self, *, system_prompt: str,
                 messages: list[ProviderMessage], tools: list[dict],
                 timeout_s: float) -> ProviderResponse:
        declarations = [{
            "name": t["name"],
            "description": t["description"],
            "parametersJsonSchema": t["parameters"],
        } for t in tools]
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": self._contents(messages),
        }
        if declarations:
            payload["tools"] = [{"functionDeclarations": declarations}]
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        payload.update({
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 512,
            },
            # Do not opt into provider-side conversation storage.
            "store": False,
        })
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{_API_ROOT}/models/{self.model}:generateContent",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
                "User-Agent": "HokieFlow/1.0",
            },
            method="POST",
        )
        _reserve_budget()
        try:
            with self._opener(request, timeout=float(timeout_s)) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in (408, 429, 500, 502, 503, 504)
            try:
                exc.close()
            except Exception:  # pragma: no cover - close is best effort
                pass
            raise ProviderUnavailable(
                f"http_{exc.code}",
                "Gemini is temporarily unavailable" if retryable
                else "Gemini rejected the request",
                retryable=retryable,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderUnavailable(
                "network_error", "Gemini could not be reached", retryable=True
            ) from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderUnavailable("response_too_large",
                                      "Gemini returned an oversized response")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderUnavailable("invalid_response",
                                      "Gemini returned invalid JSON") from exc

        candidates = data.get("candidates") or []
        if not candidates:
            reason = ((data.get("promptFeedback") or {}).get("blockReason")
                      or "no_candidate")
            raise ProviderUnavailable("blocked_or_empty",
                                      f"Gemini returned no answer ({reason})")
        candidate = candidates[0]
        content = candidate.get("content") or {"role": "model", "parts": []}
        parts = content.get("parts") or []
        texts: list[str] = []
        calls: list[ToolCall] = []
        for index, part in enumerate(parts):
            if isinstance(part.get("text"), str) and not part.get("thought"):
                texts.append(part["text"])
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                args = fc.get("args")
                calls.append(ToolCall(
                    name=str(fc.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                    # Omit FunctionResponse.id when Gemini omitted the matching
                    # FunctionCall.id; inventing one violates the API contract.
                    call_id=str(fc.get("id") or ""),
                ))
        return ProviderResponse(
            text="\n".join(t.strip() for t in texts if t.strip()).strip(),
            tool_calls=calls,
            finish_reason=str(candidate.get("finishReason") or ""),
            usage=data.get("usageMetadata") or {},
            native=content,
        )
