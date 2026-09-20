"""Provider-neutral types for HokieFlow's bounded campus agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    """One provider-requested allowlisted function call."""

    name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass
class ProviderMessage:
    """A normalized conversation turn.

    ``native`` preserves an assistant turn exactly as returned by a provider.
    This matters for Gemini thinking signatures. It is never exposed to users.
    """

    role: str
    text: str = ""
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    native: Any = None


@dataclass
class ProviderResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    native: Any = None


class ProviderUnavailable(RuntimeError):
    """Typed provider/API failure safe to translate into a fallback."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = str(code)
        self.safe_message = str(message)
        self.retryable = bool(retryable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.safe_message,
            "retryable": self.retryable,
        }


class Provider(Protocol):
    name: str
    model: str

    def generate(
        self,
        *,
        system_prompt: str,
        messages: list[ProviderMessage],
        tools: list[dict[str, Any]],
        timeout_s: float,
    ) -> ProviderResponse: ...
