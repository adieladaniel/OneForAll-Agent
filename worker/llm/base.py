"""Neutral message / tool-call types and the abstract LLM client."""
from __future__ import annotations

import errno
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx


@dataclass
class ToolSpec:
    """A tool the model may call. ``parameters`` is a JSON Schema with ``type: object``."""

    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    args: dict


@dataclass
class Message:
    """One conversation turn. ``tool_calls`` is for assistant turns; ``tool_call_id``/``name`` for tool turns."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class Usage:
    """Token accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    """The model's reply: free text and/or tool calls."""

    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw: Any = None
    thought: str = ""  # provider-side reasoning summary (display only; never fed back into the conversation)


class LLMError(Exception):
    """Base class for LLM failures."""


class RateLimitError(LLMError):
    """The provider rejected the call for quota reasons. ``retry_after`` is the server's hint in seconds."""

    def __init__(self, message: str = "rate limited", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TransientLLMError(LLMError):
    """A retryable failure: 5xx responses or network errors.

    ``unavailable`` marks "this model is overloaded/unavailable right now" (HTTP 503): a failover client
    retries once quickly and then switches to the next model instead of waiting.

    ``network`` marks a connectivity failure (DNS, refused/reset connection, connect timeout): the provider was
    never reached, so it says nothing about the model. ResilientLLM keeps retrying those on a time budget and
    a failover client must not mark any model unusable because of them.
    """

    def __init__(
        self, message: str, retry_after: float | None = None, unavailable: bool = False, network: bool = False
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.unavailable = unavailable
        self.network = network


class LLMTimeoutError(TransientLLMError):
    """The provider did not answer within ``LLM_CALL_TIMEOUT_S`` (an overloaded server holding the connection open).

    Classified like a 503 (``unavailable=True``): a failover client retries once quickly and then moves on to the next
    model for this call, marking the timed-out model temporarily unavailable (never quota-exhausted)."""

    def __init__(self, message: str = "the model did not answer in time", timeout_s: float | None = None):
        super().__init__(message, unavailable=True)
        self.timeout_s = timeout_s


class MalformedCallError(TransientLLMError):
    """The model answered with a broken function call (Gemini's ``MALFORMED_FUNCTION_CALL``).

    A per-response glitch, not an outage: a failover client tries the next model for the same call *without* marking
    the first one unusable, ResilientLLM re-asks a couple of times with a slightly higher temperature, and the agent
    runner treats one that still escapes as a failed turn (corrective note, failure guard) instead of ending the run."""

    def __init__(self, message: str = "The model produced a malformed function call"):
        super().__init__(message)


_NETWORK_ERRNOS = {
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTUNREACH,
    11001, 11002, 11003, 11004,  # WSAHOST_NOT_FOUND / TRY_AGAIN / NO_RECOVERY / NO_DATA (Windows DNS)
    10050, 10051, 10052, 10053, 10054, 10060, 10061, 10065,  # Winsock: net down/unreachable, reset, timeout, refused
}
_NETWORK_MARKERS = (
    "getaddrinfo failed", "name or service not known", "temporary failure in name resolution",
    "nodename nor servname", "no address associated with hostname", "network is unreachable",
    "connection refused", "connection reset", "connection aborted", "no route to host",
    "failed to establish a new connection", "name resolution",
)


def is_network_error(err: BaseException) -> bool:
    """True for connectivity failures (DNS, refused/reset connections, connect timeouts), looking through ``__cause__``.

    Plain read timeouts and HTTP error statuses are *not* connectivity errors: the server was reached.
    """
    seen: set[int] = set()
    node: BaseException | None = err
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError, socket.gaierror,
                             socket.herror, ConnectionError)):
            return True
        if isinstance(node, OSError) and not isinstance(node, TimeoutError):
            if node.errno in _NETWORK_ERRNOS or getattr(node, "winerror", None) in _NETWORK_ERRNOS:
                return True
        text = str(node).lower()
        if isinstance(node, (OSError, httpx.TransportError)) and any(m in text for m in _NETWORK_MARKERS):
            return True
        node = node.__cause__ or node.__context__
    return False


class QuotaExhaustedError(LLMError):
    """A per-day (or otherwise long-lived) quota is used up for ``model``; waiting is pointless, switch models."""

    def __init__(self, message: str = "quota exhausted", model: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.model = model
        self.retry_after = retry_after


class QuotaExceededError(LLMError):
    """The organization's own daily LLM budget (tokens or calls, see ``worker/usage.py``) is used up.

    Unlike ``QuotaExhaustedError`` (a provider quota for one model) this is a policy decision of the deployment: no
    model or key switch helps, so the run ends as needs_attention with this message until the budget resets."""


class ModelUnavailableError(LLMError):
    """``model`` cannot serve requests: unknown (404, ``temporary=False``) or persistently overloaded (503)."""

    def __init__(self, message: str = "model unavailable", model: str = "", temporary: bool = False):
        super().__init__(message)
        self.model = model
        self.temporary = temporary


class LLMClient(ABC):
    """Abstract chat client with native tool calling."""

    name: str

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        tool_choice: Literal["auto", "any", "none"] = "auto",
    ) -> LLMResponse:
        """Run one completion. Raises RateLimitError / TransientLLMError / LLMError on failure."""
