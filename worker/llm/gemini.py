"""Gemini adapter (google-genai SDK) using native function calling.

We drive our own agent loop, so automatic function calling is disabled. Newer Gemini models
return a ``thought_signature`` on function-call parts that must be echoed back on later turns;
since our neutral ``ToolCall`` has no slot for it, the client remembers signatures by call id.

Signatures are model-specific. Each ``GeminiClient`` only knows the calls *it* produced; calls from any other
model (after a failover) are sent without their id and, for Gemini 3 models (which validate signatures on
function calls strictly), with Google's documented dummy signature so the history stays valid.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from worker.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    LLMTimeoutError,
    MalformedCallError,
    Message,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitError,
    ToolCall,
    ToolSpec,
    TransientLLMError,
    Usage,
    is_network_error,
)

# Keys of the OpenAPI subset Gemini accepts in function declaration schemas.
_ALLOWED_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "properties", "required", "items",
    "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "pattern",
    "anyOf", "title", "example",
}
_TOOL_CHOICE_MODES = {"auto": "AUTO", "any": "ANY", "none": "NONE"}
# Documented placeholder for function calls that carry no signature of this model (history from another model).
DUMMY_THOUGHT_SIGNATURE = b"skip_thought_signature_validator"
# A server retry hint longer than this means a daily quota, not a per-minute limit.
DAILY_QUOTA_RETRY_S = 60.0
# The SDK's own HTTP timeout is only a backstop for the asyncio limit in GeminiClient.complete: it must fire later.
SDK_TIMEOUT_MARGIN_S = 10.0


def requires_thought_signatures(model: str) -> bool:
    """Gemini 3 models reject function calls in the current turn that lack a thought signature."""
    return model.lower().removeprefix("models/").startswith("gemini-3")


@dataclass
class CallMeta:
    """Provider-side details of a tool call that must be echoed back on later turns."""

    api_id: str | None = None
    thought_signature: bytes | None = None


_JSON_STRING_NOTE = "(JSON-encoded object)"
_ALLOWED_FORMATS = {"enum", "date-time", "int32", "int64", "float", "double"}


def sanitize_schema(schema: Any) -> Any:
    """Recursively rewrite a JSON Schema into the subset Gemini accepts.

    * unsupported keys (``additionalProperties``, ``default``, ...) are dropped;
    * ``type: [X, "null"]`` becomes ``type: X`` + ``nullable``; ``const`` becomes a one-value ``enum``;
    * an object with no ``properties`` (free-form) becomes a JSON-encoded ``string``
      (see :func:`sanitize_schema_with_paths` to learn which fields must be decoded again);
    * an array without ``items`` gets ``items: {type: string}``;
    * ``enum`` on a non-string type is converted to a string enum;
    * ``required`` entries that are not declared properties are dropped.
    """
    return sanitize_schema_with_paths(schema)[0]


def sanitize_schema_with_paths(schema: Any) -> tuple[Any, list[tuple[str, ...]]]:
    """Like :func:`sanitize_schema`, also returning the paths of free-form objects replaced by JSON strings.

    A path is a tuple of property names from the root; array items are represented by ``"*"``.
    """
    paths: list[tuple[str, ...]] = []
    return _sanitize(schema, (), paths), paths


def _sanitize(schema: Any, path: tuple[str, ...], paths: list[tuple[str, ...]]) -> Any:
    if isinstance(schema, list):
        return [_sanitize(s, path, paths) for s in schema]
    if not isinstance(schema, dict):
        return schema
    schema = dict(schema)
    if "const" in schema:
        schema.setdefault("enum", [schema.pop("const")])
    if isinstance(schema.get("type"), list):
        kinds = [t for t in schema["type"] if t != "null"]
        if len(kinds) != len(schema["type"]):
            schema["nullable"] = True
        schema["type"] = kinds[0] if kinds else "string"
    # Free-form object: Gemini rejects `type: object` without properties, so ask for a JSON string instead.
    if path and schema.get("type") == "object" and not schema.get("properties") and "anyOf" not in schema:
        paths.append(path)
        description = str(schema.get("description") or "").strip()
        out: dict[str, Any] = {"type": "string", "description": f"{description} {_JSON_STRING_NOTE}".strip()}
        if schema.get("nullable"):
            out["nullable"] = True
        return out
    if schema.get("type") == "array" and "items" not in schema and "anyOf" not in schema:
        schema["items"] = {"type": "string"}
    if "enum" in schema and isinstance(schema["enum"], list) and schema.get("type") != "string":
        if schema.get("type") in (None, "integer", "number", "boolean"):
            schema["enum"] = [str(v) for v in schema["enum"]]
            schema["type"] = "string"
    if "format" in schema and schema["format"] not in _ALLOWED_FORMATS:
        schema.pop("format")
    out = {}
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _sanitize(sub, path + (name,), paths) for name, sub in value.items()}
        elif key == "items":
            out[key] = _sanitize(value, path + ("*",), paths)
        elif key == "anyOf":
            out[key] = _sanitize(value, path, paths)
        else:
            out[key] = value
    if isinstance(out.get("required"), list) and isinstance(out.get("properties"), dict):
        out["required"] = [r for r in out["required"] if r in out["properties"]]
        if not out["required"]:
            del out["required"]
    return out


def _lenient_json(value: Any) -> Any:
    """Decode a JSON-encoded object string; return the input unchanged when it is not valid JSON."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        decoded = json.loads(text)
    except ValueError:
        return value
    return decoded if isinstance(decoded, (dict, list)) else value


def decode_json_fields(args: Any, paths: list[tuple[str, ...]]) -> Any:
    """Turn the JSON strings at ``paths`` (from :func:`sanitize_schema_with_paths`) back into dicts, in place-safe copy."""
    if not paths:
        return args
    result = args
    for path in paths:
        result = _decode_at(result, path)
    return result


def _decode_at(node: Any, path: tuple[str, ...]) -> Any:
    if not path:
        return _lenient_json(node)
    head, rest = path[0], path[1:]
    if head == "*" and isinstance(node, list):
        return [_decode_at(item, rest) for item in node]
    if isinstance(node, dict) and head in node:
        copy = dict(node)
        copy[head] = _decode_at(node[head], rest)
        return copy
    return node


def free_form_paths(spec: ToolSpec) -> list[tuple[str, ...]]:
    """Paths of the free-form object fields of ``spec`` that are sent to Gemini as JSON strings."""
    return sanitize_schema_with_paths(spec.parameters)[1]


def to_function_declaration(spec: ToolSpec) -> types.FunctionDeclaration:
    """Convert a ToolSpec to a Gemini FunctionDeclaration (parameters omitted for no-arg tools)."""
    params = sanitize_schema(spec.parameters)
    kwargs: dict[str, Any] = {"name": spec.name, "description": spec.description}
    if params.get("properties"):
        kwargs["parameters"] = params
    return types.FunctionDeclaration(**kwargs)


def to_gemini_contents(
    messages: list[Message],
    meta: Mapping[str, CallMeta] | None = None,
    *,
    foreign_signature: bytes | None = None,
) -> tuple[str | None, list[types.Content]]:
    """Convert neutral messages into ``(system_instruction, contents)``.

    Consecutive tool results are merged into one user turn of ``function_response`` parts.
    Tool names are resolved from the originating assistant call when a tool message lacks ``name``.
    Calls missing from ``meta`` were not produced by this client's model; they are sent without an API id
    and with ``foreign_signature`` (the dummy signature for models that validate signatures, else none).
    """
    meta = meta or {}
    system_parts: list[str] = []
    contents: list[types.Content] = []
    call_names: dict[str, str] = {}

    for msg in messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        elif msg.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=msg.content)]))
        elif msg.role == "assistant":
            parts: list[types.Part] = []
            if msg.content:
                parts.append(types.Part(text=msg.content))
            for call in msg.tool_calls:
                call_names[call.id] = call.name
                info = meta.get(call.id) or CallMeta(thought_signature=foreign_signature)
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(id=info.api_id, name=call.name, args=call.args),
                        thought_signature=info.thought_signature,
                    )
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        elif msg.role == "tool":
            name = msg.name or call_names.get(msg.tool_call_id or "", "tool")
            info = meta.get(msg.tool_call_id or "", CallMeta())
            part = types.Part(
                function_response=types.FunctionResponse(
                    id=info.api_id, name=name, response={"result": msg.content}
                )
            )
            last = contents[-1] if contents else None
            if last is not None and last.role == "user" and last.parts and last.parts[-1].function_response:
                last.parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))

    return ("\n\n".join(system_parts) or None), contents


def parse_response(
    resp: Any,
    meta: dict[str, CallMeta] | None = None,
    json_paths: Mapping[str, list[tuple[str, ...]]] | None = None,
) -> LLMResponse:
    """Convert a google-genai GenerateContentResponse into an LLMResponse.

    Provider call details (API id, thought signature) are recorded into ``meta`` keyed by our call id.
    ``json_paths`` maps tool name -> argument paths that were sent as JSON strings (free-form objects)
    and are decoded back into dicts here.
    """
    candidates = getattr(resp, "candidates", None) or []
    # Checked first: a malformed call usually comes back with no content at all, and is recoverable (unlike "no content").
    if candidates and str(candidates[0].finish_reason).endswith("MALFORMED_FUNCTION_CALL"):
        raise MalformedCallError("Gemini produced a malformed function call")
    if not candidates or candidates[0].content is None:
        reason = candidates[0].finish_reason if candidates else getattr(resp, "prompt_feedback", None)
        raise LLMError(f"Gemini returned no content ({reason})")
    candidate = candidates[0]

    text_parts: list[str] = []
    thought_parts: list[str] = []
    calls: list[ToolCall] = []
    for part in candidate.content.parts or []:
        if part.function_call is not None:
            fc = part.function_call
            call_id = fc.id or f"call_{uuid.uuid4().hex[:12]}"
            args = dict(fc.args or {})
            if json_paths and fc.name in json_paths:
                args = decode_json_fields(args, json_paths[fc.name])
            calls.append(ToolCall(id=call_id, name=fc.name, args=args))
            if meta is not None:
                meta[call_id] = CallMeta(api_id=fc.id, thought_signature=part.thought_signature)
        elif part.text and part.thought:
            thought_parts.append(part.text)
        elif part.text:
            text_parts.append(part.text)

    usage = getattr(resp, "usage_metadata", None)
    output_tokens = (getattr(usage, "candidates_token_count", None) or getattr(usage, "response_token_count", 0) or 0)
    output_tokens += getattr(usage, "thoughts_token_count", 0) or 0
    return LLMResponse(
        text="".join(text_parts),
        tool_calls=calls,
        usage=Usage(input_tokens=getattr(usage, "prompt_token_count", 0) or 0, output_tokens=output_tokens),
        raw=resp,
        thought="\n".join(t.strip() for t in thought_parts if t.strip()),
    )


def retry_delay_from_error(err: genai_errors.APIError) -> float | None:
    """Extract the server's retry hint (RetryInfo ``retryDelay`` or "retry in Ns" message) in seconds."""
    details = err.details if isinstance(err.details, dict) else {}
    for item in (details.get("error") or {}).get("details", []) or []:
        if str(item.get("@type", "")).endswith("RetryInfo"):
            match = re.match(r"([\d.]+)s", str(item.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    match = re.search(r"retry in ([\d.]+)s", str(err.message or ""), re.IGNORECASE)
    return float(match.group(1)) if match else None


def quota_ids_from_error(err: genai_errors.APIError) -> list[str]:
    """The ``quotaId`` of every QuotaFailure violation in a 429 body (e.g. ``GenerateRequestsPerDayPerProjectPerModel-FreeTier``)."""
    details = err.details if isinstance(err.details, dict) else {}
    ids: list[str] = []
    for item in (details.get("error") or {}).get("details", []) or []:
        for violation in item.get("violations", []) or []:
            if isinstance(violation, dict) and violation.get("quotaId"):
                ids.append(str(violation["quotaId"]))
    return ids


def is_daily_quota(err: genai_errors.APIError, retry_after: float | None) -> bool:
    """True when a 429 means a per-day quota is used up (waiting would take hours), not a per-minute limit."""
    if any("perday" in quota_id.lower() for quota_id in quota_ids_from_error(err)):
        return True
    if "perday" in str(err.message or "").lower():
        return True
    return retry_after is not None and retry_after > DAILY_QUOTA_RETRY_S


def map_error(err: Exception, model: str = "") -> LLMError:
    """Translate an SDK / network exception into our LLMError hierarchy.

    * 429 per-day quota (or retry hint > 60s) -> QuotaExhaustedError (switch model);
    * other 429 -> RateLimitError (wait ``retry_after``, same model);
    * 503 UNAVAILABLE -> TransientLLMError(unavailable=True) (one quick retry, then switch model);
    * 404 NOT_FOUND -> ModelUnavailableError (switch model);
    * connectivity errors (DNS, refused/reset, connect timeout) -> TransientLLMError(network=True): retried on a
      time budget by ResilientLLM and never a reason to fail over;
    * a call that took too long (our own limit or an HTTP read timeout) -> LLMTimeoutError, classified like a 503
      (one quick retry, then switch model; never a quota problem);
    * other 5xx / 408 -> TransientLLMError.
    """
    if isinstance(err, genai_errors.APIError):
        if err.code == 429 or err.status == "RESOURCE_EXHAUSTED":
            retry_after = retry_delay_from_error(err)
            if is_daily_quota(err, retry_after):
                return QuotaExhaustedError(
                    f"Gemini daily quota exhausted for {model or 'the model'}: {err.message}",
                    model=model,
                    retry_after=retry_after,
                )
            return RateLimitError(f"Gemini rate limit: {err.message}", retry_after=retry_after)
        if err.code == 404 or err.status == "NOT_FOUND":
            return ModelUnavailableError(f"Gemini model {model or '?'} not found: {err.message}", model=model)
        if err.code >= 500 or err.code == 408:
            return TransientLLMError(
                f"Gemini server error {err.code}: {err.message}",
                retry_after=retry_delay_from_error(err),
                unavailable=err.code == 503 or err.status == "UNAVAILABLE",
            )
        return LLMError(f"Gemini API error {err.code}: {err.message}")
    if is_network_error(err):
        return TransientLLMError(f"Gemini network error: {err}", network=True)
    if isinstance(err, (httpx.TimeoutException, TimeoutError)):
        return LLMTimeoutError(f"Gemini did not answer in time: {err}")
    if isinstance(err, (httpx.TransportError, ConnectionError)):
        return TransientLLMError(f"Gemini network error: {err}")
    return LLMError(f"Gemini call failed: {err}")


class GeminiClient(LLMClient):
    """LLMClient backed by one Gemini model (free tier friendly). Use FailoverLLM to chain several models."""

    name = "gemini"

    def __init__(
        self, api_key: str, model: str, *, include_thoughts: bool = True, client: Any = None, timeout_s: float = 0.0
    ):
        """``timeout_s``: longest one call may take (0 = no limit); a slower call raises ``LLMTimeoutError``."""
        if not api_key and client is None:
            raise LLMError("GEMINI_API_KEY is not set (get a free key at https://aistudio.google.com/apikey)")
        self.model = model
        self.timeout_s = max(0.0, timeout_s)
        if client is None:
            options = types.HttpOptions(timeout=int((self.timeout_s + SDK_TIMEOUT_MARGIN_S) * 1000)) if self.timeout_s else None
            client = genai.Client(api_key=api_key, http_options=options)
        self._client = client
        self._meta: dict[str, CallMeta] = {}
        self._include_thoughts = include_thoughts
        self._foreign_signature = DUMMY_THOUGHT_SIGNATURE if requires_thought_signatures(model) else None

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        tool_choice: Literal["auto", "any", "none"] = "auto",
    ) -> LLMResponse:
        system, contents = to_gemini_contents(messages, self._meta, foreign_signature=self._foreign_signature)
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if self._include_thoughts:
            # Summaries only (display); the thinking budget/level stays at the model default.
            config.thinking_config = types.ThinkingConfig(include_thoughts=True)
        if tools:
            config.tools = [types.Tool(function_declarations=[to_function_declaration(t) for t in tools])]
            config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=_TOOL_CHOICE_MODES[tool_choice])
            )
        try:
            # An overloaded server may hold the connection open for minutes: the limit makes that a failover trigger.
            async with asyncio.timeout(self.timeout_s or None) as limit:
                resp = await self._generate(contents, config)
        except Exception as err:  # noqa: BLE001 - mapped to the LLMError hierarchy
            if isinstance(err, TimeoutError) and limit.expired():
                raise LLMTimeoutError(f"Gemini ({self.model}) did not answer within {self.timeout_s:g}s",
                                      timeout_s=self.timeout_s) from err
            raise map_error(err, self.model) from err
        json_paths = {t.name: free_form_paths(t) for t in tools or []}
        return parse_response(resp, self._meta, json_paths)

    async def _generate(self, contents: list[types.Content], config: types.GenerateContentConfig) -> Any:
        generate = self._client.aio.models.generate_content
        try:
            return await generate(model=self.model, contents=contents, config=config)
        except genai_errors.ClientError as err:
            # A model that rejects thought summaries: stop asking for them and retry once.
            if err.code == 400 and self._include_thoughts and "thought" in str(err.message or "").lower():
                self._include_thoughts = False
                config.thinking_config = None
                return await generate(model=self.model, contents=contents, config=config)
            raise
