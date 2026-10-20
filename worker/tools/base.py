"""Tool framework base types."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from worker.config import Settings
from worker.llm.base import ToolSpec


class Risk(str, Enum):
    """How consequential a tool call is; drives the approval policy."""

    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"


@dataclass
class ToolResult:
    """Outcome of a tool call. ``transient`` marks errors eligible for automatic retry."""

    ok: bool
    observation: str
    error: str | None = None
    evidence: list[str] = field(default_factory=list)  # paths relative to the run dir
    data: dict | None = None  # structured payload for UI / verifier
    transient: bool = False


@dataclass
class ToolContext:
    """Per-run resources handed to every tool."""

    run_id: str
    run_dir: Path
    workspace: Path
    settings: Settings
    memory: Any = None  # set by the agent runtime
    emit: Callable[[str, dict], Awaitable[None]] | None = None  # event callback
    extras: dict = field(default_factory=dict)  # per-run resources, e.g. the browser session


class Tool(ABC):
    """A capability the agent can invoke. Subclasses set the class attributes and implement ``run``."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict]  # JSON Schema, type=object
    risk: ClassVar[Risk] = Risk.READ
    read_only_safe: ClassVar[bool] = True  # may be given to the verifier
    # Offered to the verifier although not read_only_safe: the tool enforces its own narrow read-only rule
    # (``BrowserTool.refusal``). Kept separate so the approval policy still treats the tool as not read-only-safe.
    verifier_allowed: ClassVar[bool] = False
    max_retries: ClassVar[int] = 2  # auto-retries for transient failures

    def classify(self, args: dict, ctx: ToolContext) -> Risk:
        """Risk of this specific call; override when it depends on the arguments or on the run's own state.

        ``ctx`` is the calling run's context. Tool instances are shared and stateless: anything a classification
        needs beyond ``args`` (e.g. the page the browser is on) must be read from ``ctx``, never from the tool."""
        return self.risk

    def retry_safe(self, args: dict, ctx: ToolContext) -> bool:
        """May this specific call be re-run automatically after a transient failure? Override to return False
        for state-changing calls, where a failure may have happened after a partial write."""
        return True

    def no_retry_note(self, args: dict) -> str:
        """Guidance appended to a transient failure of a call that ``retry_safe`` ruled out."""
        return "[Not retried automatically: the action may or may not have taken effect. Verify before repeating it.]"

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        """One-line human-readable description for approval cards and the timeline. ``ctx`` (the calling run's
        context) lets a tool enrich the text with run state; it may be None for a purely static description."""
        shown = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        return f"{self.name}({shown})"

    async def approval_preview(self, args: dict, ctx: ToolContext) -> list[dict[str, str]] | None:
        """Optional ``[{"label", "value"}]`` rows shown to the human approver ("values to be submitted").
        Must be read-only and cheap; the runner treats any failure as "no preview"."""
        return None

    def submitted_values(self, args: dict, ctx: ToolContext) -> list[dict[str, str]]:
        """Optional ``[{"label", "value"}]`` rows of data this call sends, beyond ``approval_preview`` (for example the
        body of an HTTP request). The runner checks both against what the run has read (``worker/agent/provenance.py``)
        before a write runs or is shown to a human. Must be synchronous, read-only and cheap."""
        return []

    @abstractmethod
    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        """Execute the tool. May raise; ``execute_with_retry`` converts exceptions to results."""

    def spec(self) -> ToolSpec:
        """The model-facing declaration of this tool."""
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


def _short(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."
