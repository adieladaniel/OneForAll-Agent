"""http_request tool restricted to an allowlist of hosts."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

from worker.tools.base import Risk, Tool, ToolContext, ToolResult

MAX_BODY_CHARS = 4000
TIMEOUT_S = 20.0
# Only these are re-sent automatically after a 5xx / network error; anything else might duplicate a write.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _flatten(node: object, path: str, rows: list[dict[str, str]]) -> None:
    """Append a ``{"label": path, "value": text}`` row for every string or number inside ``node``."""
    if isinstance(node, dict):
        for key, child in node.items():
            _flatten(child, f"{path}.{key}" if path else str(key), rows)
    elif isinstance(node, list):
        for i, child in enumerate(node):
            _flatten(child, f"{path}[{i}]", rows)
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        rows.append({"label": path or "body", "value": str(node)})


class HttpRequestTool(Tool):
    """Call an HTTP API on an allowlisted host (e.g. the internal ERP's REST API)."""

    name = "http_request"
    description = (
        "Make an HTTP request to an allowed internal host. Returns the status code and the (truncated) "
        "response body. Use headers/json_body/query as needed; omit them otherwise."
    )
    parameters = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
            "url": {"type": "string", "description": "Absolute http(s) URL on an allowed host."},
            "headers": {"type": "object", "description": "Optional request headers."},
            "json_body": {"type": "object", "description": "Optional JSON request body."},
            "query": {"type": "object", "description": "Optional query-string parameters."},
            "auth_credential": {
                "type": "string",
                "description": "Optional name of a vault credential (see list_credentials). Its authentication header "
                               "is added for you; you never see the value.",
            },
        },
        "required": ["method", "url"],
    }
    risk = Risk.WRITE

    def classify(self, args: dict, ctx: ToolContext) -> Risk:
        method = str(args.get("method", "GET")).upper()
        if method in ("GET", "HEAD"):
            return Risk.READ
        if method == "DELETE":
            return Risk.IRREVERSIBLE
        return Risk.WRITE

    def retry_safe(self, args: dict, ctx: ToolContext) -> bool:
        return str(args.get("method", "GET")).upper() in IDEMPOTENT_METHODS

    def no_retry_note(self, args: dict) -> str:
        return (
            "Server error on a non-idempotent request; it was NOT retried automatically. The write may or may not "
            "have happened: check whether the record exists (GET) before retrying."
        )

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        body = f" with body {json.dumps(args['json_body'], ensure_ascii=False)}" if args.get("json_body") else ""
        auth = f" using credential {args['auth_credential']}" if args.get("auth_credential") else ""
        return f"{str(args.get('method', 'GET')).upper()} {args.get('url')}{body}{auth}"

    def submitted_values(self, args: dict, ctx: ToolContext) -> list[dict[str, str]]:
        """The leaves of the JSON body and the query of a request that changes something, labelled by their path."""
        if self.classify(args, ctx) is Risk.READ:
            return []
        rows: list[dict[str, str]] = []
        for part in ("json_body", "query"):
            _flatten(args.get(part), "" if part == "json_body" else "query", rows)
        return rows

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        method = str(args["method"]).upper()
        url = args["url"]
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host not in ctx.settings.http_allowed_hosts:
            allowed = ", ".join(sorted(ctx.settings.http_allowed_hosts))
            return ToolResult(
                ok=False,
                observation=f"Host '{host or url}' is not allowed. Allowed hosts: {allowed}.",
                error="host_not_allowed",
            )
        if ctx.extras.get("read_only") and method != "GET":
            return ToolResult(
                ok=False,
                observation=f"{method} is not permitted in read-only mode; only GET is allowed.",
                error="read_only_mode",
            )

        headers = dict(args.get("headers") or {})
        if args.get("auth_credential"):
            refusal = self._add_credential_header(headers, str(args["auth_credential"]), host, ctx)
            if refusal is not None:
                return refusal

        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
            resp = await client.request(
                method, url, headers=headers or None, json=args.get("json_body"), params=args.get("query") or None
            )

        body = self._format_body(resp)
        observation = f"HTTP {resp.status_code} {method} {url}\n{body}"
        data = {"status": resp.status_code, "url": str(resp.url)}
        if resp.status_code >= 500:
            if method not in IDEMPOTENT_METHODS:
                return ToolResult(
                    ok=False,
                    observation=f"{observation}\n{self.no_retry_note(args)}",
                    error=f"HTTP {resp.status_code}",
                    data=data,
                )
            return ToolResult(ok=False, observation=observation, error=f"HTTP {resp.status_code}", data=data, transient=True)
        if resp.status_code >= 400:
            return ToolResult(ok=False, observation=observation, error=f"HTTP {resp.status_code}", data=data)
        return ToolResult(ok=True, observation=observation, data=data)

    @staticmethod
    def _add_credential_header(headers: dict, name: str, host: str, ctx: ToolContext) -> ToolResult | None:
        """Put the vault credential's header into ``headers`` (replacing any the model set), or explain the refusal.

        The value is read here, at execution time, and never appears in an observation. A credential with a host hint
        is only sent to that host, even when the allowlist permits others."""
        scope = ctx.extras.get("vault_scope")
        credential = scope.get(name) if scope else None
        if credential is None:
            available = ", ".join(scope.names()) if scope else ""
            hint = f"Available: {available}." if available else "The vault has no credentials for this run."
            return ToolResult(ok=False, observation=f"No credential named '{name}' in the vault. {hint}",
                              error="unknown_credential")
        if not credential.allows_host(host):
            return ToolResult(
                ok=False, error="credential_host_mismatch",
                observation=f"Credential '{name}' is only for {credential.host}; it will not be sent to {host}.",
            )
        header = credential.auth_header()
        if header is None:
            return ToolResult(ok=False, observation=f"Credential '{name}' ({credential.kind}) has no authentication "
                              "header to send.", error="credential_unusable")
        for existing in [k for k in headers if k.lower() == header[0].lower()]:
            del headers[existing]
        headers[header[0]] = header[1]
        return None

    @staticmethod
    def _format_body(resp: httpx.Response) -> str:
        text = resp.text
        if "json" in resp.headers.get("content-type", ""):
            try:
                text = json.dumps(resp.json(), indent=2, ensure_ascii=False)
            except ValueError:
                pass
        if len(text) > MAX_BODY_CHARS:
            return text[:MAX_BODY_CHARS] + f"\n[truncated: showing {MAX_BODY_CHARS} of {len(text)} chars]"
        return text or "(empty body)"
