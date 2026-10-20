"""Generic browser tools for an LLM agent, built on Playwright (async, Chromium).

Site-agnostic: every action returns a compact text snapshot of the page (see ``snapshot.py``) in which
visible interactive elements are numbered (``[7] button "Save bill"``). The agent refers to elements by
those numbers. Refs are re-assigned on every snapshot, so a stale ref produces a clear error.

One ``BrowserSession`` (an isolated browser context + active page, taken from the shared ``BrowserPool``) lives
per run in ``ctx.extras["browser"]``. The tool instances themselves are stateless and can be shared by any number
of concurrent runs: ``classify`` / ``describe_action`` receive the calling run's ``ToolContext`` and look up the
element behind a ref in *that run's* session.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from worker.tools.base import Risk, Tool, ToolContext, ToolResult
from worker.tools.browser_pool import POOL_KEY, BrowserPool, BrowserPoolError, get_pool
from worker.tools.registry import ToolRegistry
from worker.tools.snapshot import (
    DEFAULT_MAX_ELEMENTS,
    DEFAULT_MAX_TEXT,
    SNAPSHOT_JS,
    find_in_page,
    format_snapshot,
)

ACTION_TIMEOUT_MS = 5000
NAV_TIMEOUT_MS = 20000
DOWNLOAD_WAIT_S = 10.0
SESSION_KEY = "browser"

# --------------------------------------------------------------------------- risk classification
_IRREVERSIBLE_RE = re.compile(r"\b(delete|remove|pay|payment|approve|transfer)\b", re.I)
_WRITE_RE = re.compile(
    r"\b(submit|save|create|confirm|pay|payment|send|post|approve|delete|remove|transfer|sign|reply)\b"
    r"|place order|cancel order",
    re.I,
)
# Names that look like write words but are not business writes (login/logout, searching).
_BENIGN_RE = re.compile(r"\b(sign|log)[ -]?(in|out)\b|\blog(in|out)\b|\bsearch\b|\bfilter\b|\bfind\b", re.I)
# Submit-type buttons with these names are navigation/filtering, not commits.
_READISH_SUBMIT_RE = re.compile(r"\b(next|previous|prev|back|apply|reset|clear|show|view|go|load more)\b", re.I)
# Links are navigation unless their label is clearly an action ("Delete", "Approve", "Pay now").
_LINK_ACTION_RE = re.compile(
    r"\b(delete|remove|pay|approve|transfer|confirm|submit)\b|place order|cancel order", re.I
)

# Evaluated with the ref of the clicked/focused element (or null). Collects the labelled value of every visible
# field of that element's form, or of all forms when it is not inside one.
FORM_VALUES_JS = """(ref) => {
  const norm = (s) => (s == null ? '' : String(s)).replace(/\\s+/g, ' ').trim();
  const target = ref == null ? null : document.querySelector('[data-agent-ref="' + ref + '"]');
  const own = target && (target.form || target.closest('form'));
  const forms = own ? [own] : Array.from(document.forms);
  const out = [], seen = new Set();
  for (const form of forms) {
    for (const el of form.elements) {
      const tag = el.tagName.toLowerCase();
      if (!/^(input|select|textarea)$/.test(tag) || seen.has(el) || el.disabled) continue;
      const type = (el.type || '').toLowerCase();
      if (/^(hidden|submit|button|reset|image|file)$/.test(type)) continue;
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0) && !/^(checkbox|radio)$/.test(type)) continue;
      seen.add(el);
      const labels = el.labels ? Array.from(el.labels).map((l) => norm(l.textContent)).join(' ') : '';
      const label = norm(labels || el.getAttribute('aria-label') || el.placeholder || el.name || el.id || tag);
      let value;
      if (type === 'checkbox') value = el.checked ? 'checked' : 'unchecked';
      else if (type === 'radio') { if (!el.checked) continue; value = norm(el.value) || 'selected'; }
      else if (tag === 'select') value = Array.from(el.selectedOptions).map((o) => norm(o.label || o.text)).join(', ');
      else if (type === 'password') value = el.value ? '••••' : '';
      else value = norm(el.value);
      out.push({ label: label.slice(0, 80), value: value.slice(0, 200) });
    }
  }
  return out.slice(0, 40);
}"""

_RISK_ORDER = {Risk.READ: 0, Risk.WRITE: 1, Risk.IRREVERSIBLE: 2}


def _max_risk(*risks: Risk) -> Risk:
    return max(risks, key=lambda r: _RISK_ORDER[r], default=Risk.READ)


def classify_element(name: str, role: str, *, submit: bool = False) -> Risk:
    """Risk of clicking an element with accessible ``name``/``role``. ``submit`` marks form-submitting buttons."""
    name = name or ""
    if role == "link":
        if _IRREVERSIBLE_RE.search(name):
            return Risk.IRREVERSIBLE
        return Risk.WRITE if _LINK_ACTION_RE.search(name) else Risk.READ
    if role in ("textbox", "checkbox", "radio", "combobox", "listbox", "tab", "slider", "file"):
        return Risk.READ
    if _IRREVERSIBLE_RE.search(name):
        return Risk.IRREVERSIBLE
    if _BENIGN_RE.search(name):
        return Risk.READ
    if _WRITE_RE.search(name):
        return Risk.WRITE
    if submit and not _READISH_SUBMIT_RE.search(name):
        return Risk.WRITE
    return Risk.READ


# Authentication only (read-only mode). A "login form" is a form whose visible submit controls are all READ (sign in /
# log in / search ...) and that either holds a password input or has a sign-in button. The username field is recognised by
# its accessible name (or type=email).
_SIGN_IN_RE = re.compile(r"\b(sign|log)[ -]?in\b|\blogin\b", re.I)
_LOGIN_USER_RE = re.compile(r"user|e-?mail|login", re.I)
_LOGIN_FIELD_TYPES = frozenset({"", "text", "email", "password"})


def _is_login_form_field(session: Any, ref: Any, *, username_only: bool = False) -> bool:
    """Is element ``ref`` (in ``session``'s latest snapshot) a text/password input of a login form?

    The single rule that lets the read-only verifier authenticate without being able to change anything:

    * the element is an ``<input>`` of type text / email / password, inside a form;
    * every visible submit control of that form is classified READ by ``classify_element`` (so "Sign in", "Log in" or
      "Search" qualify; "Save", "Sign up", "Submit" or an unnamed submit do not), and there is at least one;
    * the form holds a password input or one of its submit controls is a sign-in / log-in button (a plain search box
      is therefore not a login form);
    * with ``username_only``: the field is the username / email / login one (its type is email or its name matches).

    Unknown elements (not in the snapshot cache) are never login fields."""
    info = session.info(ref)
    if not info or info.get("tag") != "input":
        return False
    itype = (info.get("type") or "text").lower()
    if itype not in _LOGIN_FIELD_TYPES or (username_only and itype == "password"):
        return False
    submits = [str(n) for n in info.get("formSubmits") or []]
    if not submits or any(classify_element(n, "button", submit=True) is not Risk.READ for n in submits):
        return False
    if not (info.get("formHasPassword") or any(_SIGN_IN_RE.search(n) for n in submits)):
        return False
    if username_only:
        return itype == "email" or bool(_LOGIN_USER_RE.search(str(info.get("name") or "")))
    return True


def _is_enter(key: str) -> bool:
    return key.strip().split("+")[-1].lower() in ("enter", "return")


class StaleRefError(Exception):
    """The requested element ref is not present on the current page."""


class BrowserToolError(Exception):
    """A semantic (non-transient) failure with a message meant for the agent."""


# --------------------------------------------------------------------------- session
def _safe_name(raw: str, default: str = "download") -> str:
    name = Path((raw or "").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name or default


class BrowserSession:
    """One run's isolated browser context (taken from a ``BrowserPool``) and its active page.

    Also holds the run's element-ref cache from the latest snapshot, which the stateless tools read to classify
    and describe actions."""

    def __init__(
        self,
        ctx: ToolContext,
        pool: BrowserPool | None = None,
        *,
        max_text: int = DEFAULT_MAX_TEXT,
        max_elements: int = DEFAULT_MAX_ELEMENTS,
    ) -> None:
        self.ctx = ctx
        self._pool = pool
        self.max_text = max_text
        self.max_elements = max_elements
        self._context: Any = None
        self._context_closed = False
        self._page: Any = None
        self._notes: list[str] = []
        self._step = 0
        # download bookkeeping
        self.downloads: list[str] = []
        self._reported_downloads = 0
        self._dl_started = 0
        self._dl_done = 0
        # latest snapshot knowledge
        self.refs: dict[int, dict] = {}
        self.focus: int | None = None
        self.url: str = ""
        self.view_page = 0
        self.n_pages = 1
        self.last_nav: dict | None = None
        self._launched_before = False

    # ---- lifecycle
    @property
    def pool(self) -> BrowserPool:
        """The injected pool (``ctx.extras['browser_pool']`` or the constructor argument), else the process default."""
        return self._pool or self.ctx.extras.get(POOL_KEY) or get_pool(self.ctx.settings)

    @property
    def started(self) -> bool:
        """Is there a live context (its context open and the Chromium behind it still connected)?"""
        context = self._context
        if context is None or self._context_closed:
            return False
        browser = context.browser
        return browser is None or browser.is_connected()

    @property
    def downloads_dir(self) -> Path:
        return self.ctx.run_dir / "downloads"

    @property
    def page(self) -> Any:
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in (self._context.pages if self._context else []) if not p.is_closed()]
            if not open_pages:
                raise PlaywrightError("Target page, context or browser has been closed")
            self._page = open_pages[-1]
        return self._page

    async def start(self) -> None:
        """Get a browser context from the pool if there is none (idempotent). After a crash or reset the new
        context is empty and the agent is told the previous page is gone."""
        if self.started:
            if not any(not p.is_closed() for p in self._context.pages):
                await self._context.new_page()
            return
        await self._teardown()
        try:
            self._context = await self.pool.acquire_context(headless=bool(self.ctx.settings.HEADLESS))
        except BrowserPoolError as exc:
            raise BrowserToolError(str(exc)) from exc
        try:
            self._context_closed = False
            self._context.set_default_timeout(ACTION_TIMEOUT_MS)
            self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            self._context.on("page", self._on_page)
            self._context.on("close", self._on_context_closed)
            await self._apply_init_cookies()
            await self._context.new_page()
        except BaseException:
            await self._teardown()
            raise
        self.refs, self.focus, self.url = {}, None, ""
        if self._launched_before:
            self._notes.append("The browser was restarted; the previous page and its state are gone. Navigate again.")
        self._launched_before = True

    async def _apply_init_cookies(self) -> None:
        """Add the cookies in ``ctx.extras["browser_init_cookies"]`` (Playwright cookie dicts, each with ``url`` or
        ``domain``) to the fresh context, before any page exists. The platform uses this to pin a run to its
        organization's simulated company; it also applies again after a browser restart."""
        cookies = self.ctx.extras.get("browser_init_cookies")
        if cookies:
            await self._context.add_cookies([dict(c) for c in cookies])

    async def _teardown(self) -> None:
        """Give this run's context back to the pool (closes only that context, never the browser)."""
        context, self._context, self._page = self._context, None, None
        if context is not None:
            await self.pool.release(context)

    def _on_context_closed(self, context: Any) -> None:
        if context is self._context:
            self._context_closed = True

    async def close(self) -> None:
        """Release the run's browser context."""
        await self._teardown()

    async def reset(self) -> None:
        """Drop a broken context so the next ``start`` takes a fresh one (the pool relaunches Chromium if it died)."""
        await self._teardown()

    # ---- page event handlers
    def _on_page(self, page: Any) -> None:
        if self._page is not None and not self._page.is_closed():
            self._notes.append("A new tab opened; now viewing it.")
        self._page = page
        page.on("dialog", self._on_dialog)
        page.on("response", self._on_response)
        page.on("download", self._on_download)
        page.on("close", self._on_page_closed)

    def _on_page_closed(self, page: Any) -> None:
        if page is self._page and self._context is not None:
            open_pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else None
            if open_pages:
                self._notes.append("A tab was closed; switched back to the previous tab.")

    async def _on_dialog(self, dialog: Any) -> None:
        self._notes.append(f"Dialog ({dialog.type}) auto-accepted: \"{' '.join(dialog.message.split())[:200]}\"")
        try:
            await dialog.accept()
        except PlaywrightError:
            pass

    def _on_response(self, response: Any) -> None:
        try:
            if response.request.is_navigation_request() and response.frame.parent_frame is None:
                self.last_nav = {
                    "url": response.url,
                    "status": response.status,
                    "ctype": response.headers.get("content-type", ""),
                    "disposition": response.headers.get("content-disposition", ""),
                }
        except PlaywrightError:
            pass

    async def _on_download(self, download: Any) -> None:
        self._dl_started += 1
        try:
            dest = self._unique_download_path(_safe_name(download.suggested_filename))
            await download.save_as(dest)
            self.downloads.append(f"downloads/{dest.name}")
        except Exception as exc:  # noqa: BLE001
            self._notes.append(f"Download failed: {exc}")
        finally:
            self._dl_done += 1

    def _unique_download_path(self, name: str) -> Path:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        dest = self.downloads_dir / name
        stem, suffix, n = dest.stem, dest.suffix, 1
        while dest.exists():
            dest = self.downloads_dir / f"{stem}_{n}{suffix}"
            n += 1
        return dest

    async def wait_for_download(
        self, started_before: int, timeout: float = DOWNLOAD_WAIT_S, start_timeout: float | None = None
    ) -> bool:
        """Wait until a download that began after ``started_before`` has been saved.

        With ``start_timeout`` set, give up early (False) if no download has even started by then."""
        begin = time.monotonic()
        deadline = begin + timeout
        while time.monotonic() < deadline:
            if self._dl_started > started_before:
                if self._dl_done >= self._dl_started:
                    return True
            elif start_timeout is not None and time.monotonic() - begin > start_timeout:
                return False
            await asyncio.sleep(0.1)
        return False

    @property
    def downloads_started(self) -> int:
        return self._dl_started

    async def save_response_download(self, url: str) -> str:
        """Fetch ``url`` with the browser context's cookies and save it into downloads/."""
        response = await self._context.request.get(url, timeout=15000)
        if not response.ok:
            raise BrowserToolError(f"Fetching {url} returned HTTP {response.status}")
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
        name = match.group(1) if match else Path(urlparse(url).path).name
        if not Path(name).suffix and "pdf" in response.headers.get("content-type", ""):
            name = (name or "download") + ".pdf"
        dest = self._unique_download_path(_safe_name(name))
        dest.write_bytes(await response.body())
        self.downloads.append(f"downloads/{dest.name}")
        return f"downloads/{dest.name}"

    # ---- waiting / settling
    async def settle(self) -> None:
        """Wait (with short timeouts) for a click/navigation to finish loading."""
        await asyncio.sleep(0.25)
        try:
            page = self.page
        except PlaywrightError:
            return
        for state, timeout in (("domcontentloaded", 8000), ("load", 5000), ("networkidle", 1500)):
            try:
                await page.wait_for_load_state(state, timeout=timeout)
            except PlaywrightTimeoutError:
                pass
            except PlaywrightError:
                break
        if self._dl_started > self._dl_done:  # a download is in flight
            await self.wait_for_download(self._dl_started - 1, timeout=DOWNLOAD_WAIT_S)

    # ---- snapshots
    async def _snapshot_data(self) -> dict:
        for attempt in range(3):
            try:
                return await self.page.evaluate(SNAPSHOT_JS, {"maxElements": 600})
            except PlaywrightError as exc:
                if attempt == 2 or not re.search(r"context was destroyed|navigat|Cannot find context", str(exc), re.I):
                    raise
                await self.settle()
        raise RuntimeError("unreachable")

    def _update_cache(self, data: dict) -> None:
        if data["url"] != self.url:
            self.view_page = 0
        self.url = data["url"]
        self.refs = {e["ref"]: e for e in data["elements"]}
        self.focus = data.get("focus")

    def drain_notes(self) -> list[str]:
        """Notes produced since the last observation (dialogs, new tabs, downloads)."""
        notes, self._notes = self._notes, []
        new = self.downloads[self._reported_downloads :]
        self._reported_downloads = len(self.downloads)
        notes.extend(f"Downloaded: {path}" for path in new)
        return notes

    def _status(self) -> int | None:
        return self.last_nav["status"] if self.last_nav else None

    async def observe(self, *, find: str | None = None, boost: float = 1.0) -> str:
        """Snapshot the current page and return the compact text observation."""
        data = await self._snapshot_data()
        self._update_cache(data)
        notes = self.drain_notes()
        if find:
            return find_in_page(data, find, status=self._status(), notes=notes)
        view = format_snapshot(
            data,
            page=self.view_page,
            max_text=int(self.max_text * boost),
            max_elements=int(self.max_elements * boost),
            status=self._status(),
            notes=notes,
        )
        self.view_page, self.n_pages = view.page, view.pages
        return view.text

    async def brief(self) -> str:
        """URL/title only (cheap), plus pending notes."""
        notes = self.drain_notes()
        return "\n".join([f"URL: {self.page.url}", f"Title: {await self.page.title()}", *notes])

    # ---- elements
    async def locate(self, ref: Any) -> Any:
        """Locator for ``ref`` or StaleRefError if it is no longer on the page."""
        try:
            number = int(float(ref))
        except (TypeError, ValueError):
            raise BrowserToolError(f"ref must be a number like 7, got {ref!r}") from None
        if not self.refs:
            raise StaleRefError("No page snapshot yet. Call browser_navigate or browser_read_page first to get element refs.")
        locator = self.page.locator(f'[data-agent-ref="{number}"]')
        if number not in self.refs or await locator.count() != 1:
            raise StaleRefError(
                f"Element [{number}] is not on the current page (refs are re-numbered on every page read and the page "
                "may have changed). Call browser_read_page to get fresh refs, then retry."
            )
        return locator

    def info(self, ref: Any) -> dict | None:
        try:
            return self.refs.get(int(float(ref)))
        except (TypeError, ValueError):
            return None

    async def form_values(self, ref: Any = None) -> list[dict[str, str]]:
        """Label + current value of the fields of the form around element ``ref`` (default: all visible forms).

        Read-only: evaluates the live DOM, so it shows what would be submitted right now. Password fields are
        masked, empty fields are reported as an empty string, and nothing is returned for pages without forms."""
        try:
            number = int(float(ref)) if ref is not None else None
        except (TypeError, ValueError):
            number = None
        rows = await self.page.evaluate(FORM_VALUES_JS, number)
        return [{"label": str(r.get("label") or ""), "value": str(r.get("value") or "")} for r in rows or []]

    # ---- risk
    @property
    def page_path(self) -> str:
        """Path (and query) of the page in the latest snapshot, for human-readable action descriptions."""
        parsed = urlparse(self.url)
        return (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "") if self.url else "the current page"

    def classify_click(self, ref: Any) -> Risk:
        """Risk of clicking ``ref``. An element missing from this session's cache can't be shown harmless, so it
        is WRITE: unknown never downgrades to READ."""
        info = self.info(ref)
        if info is None:
            return Risk.WRITE
        return classify_element(info.get("name", ""), info.get("role", ""), submit=bool(info.get("submit")))

    def classify_enter_on(self, info: dict | None) -> Risk:
        """Risk of pressing Enter while ``info`` has focus; ``None`` (focused element unknown) is WRITE."""
        if info is None:
            return Risk.WRITE
        if info.get("tag") == "textarea":
            return Risk.READ
        if info.get("role") in ("button", "link"):
            return classify_element(info.get("name", ""), info.get("role", ""), submit=bool(info.get("submit")))
        return _max_risk(*(classify_element(n, "button", submit=True) for n in info.get("formSubmits") or []))

    def classify_press(self, key: str) -> Risk:
        """Risk of pressing ``key``. Only Enter/Space can activate something. With nothing focused they do
        nothing; a focused element missing from the cache is unknown, hence WRITE."""
        if not _is_enter(key) and key.strip() != "Space":
            return Risk.READ
        if not self.focus:
            return Risk.READ
        return self.classify_enter_on(self.refs.get(self.focus))

    # ---- evidence
    async def screenshot(self, label: str = "shot") -> str:
        """Save a screenshot; return its path relative to the run dir."""
        self._step += 1
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label or "shot").strip("_")[:40] or "shot"
        rel = f"screenshots/{self._step:03d}_{safe}.png"
        path = self.ctx.run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path))
        return rel


def peek_session(ctx: ToolContext) -> BrowserSession | None:
    """The run's browser session if it has one (never creates it)."""
    return ctx.extras.get(SESSION_KEY)


def session_for(ctx: ToolContext) -> BrowserSession:
    """The run's browser session, created (not yet started) on first use."""
    session = ctx.extras.get(SESSION_KEY)
    if session is None:
        session = BrowserSession(ctx)
        ctx.extras[SESSION_KEY] = session
    return session


async def close_browser(ctx: ToolContext) -> None:
    """Close the run's browser session, if any (releases its context; the shared Chromium keeps running)."""
    session = ctx.extras.pop(SESSION_KEY, None)
    if session is not None:
        await session.close()


# --------------------------------------------------------------------------- tools
_CLOSED_RE = re.compile(r"target (page, context or browser )?(has been )?closed|browser has been closed|connection closed|browser closed", re.I)
_TRANSIENT_RE = re.compile(r"timeout|timed out|detached|net::err_|execution context was destroyed", re.I)


def _first_lines(message: str, n: int = 2, limit: int = 300) -> str:
    lines = [ln.strip() for ln in message.strip().splitlines() if ln.strip()]
    return " ".join(lines[:n])[:limit]


class BrowserTool(Tool):
    """Base class: session lookup, read-only enforcement and Playwright error mapping.

    Instances hold no state, so one set of browser tools can serve any number of concurrent runs; everything
    run-specific lives in the ``BrowserSession`` in ``ctx.extras``."""

    max_retries = 2

    # -- hooks for subclasses
    def refusal(self, args: dict, session: BrowserSession) -> str | None:
        return None

    async def execute(self, args: dict, ctx: ToolContext, session: BrowserSession) -> ToolResult:
        raise NotImplementedError

    # -- retry policy: only read-level actions are re-run automatically
    def retry_safe(self, args: dict, ctx: ToolContext) -> bool:
        return self.classify(args, ctx) == Risk.READ

    def no_retry_note(self, args: dict) -> str:
        return (
            "[NOT retried automatically: this action changes state and may or may not have taken effect. "
            "Call browser_read_page and check whether it worked before repeating it.]"
        )

    async def approval_preview(self, args: dict, ctx: ToolContext) -> list[dict[str, str]] | None:
        """For state-changing actions (submitting a form): the form's current field values."""
        session = peek_session(ctx)
        if session is None or not session.started or self.classify(args, ctx) == Risk.READ:
            return None
        ref = args.get("ref") if args.get("ref") is not None else session.focus
        rows = await session.form_values(ref)
        redactor = ctx.extras.get("redactor")  # a field holding a vault secret (e.g. an API key in a text input)
        if redactor:
            rows = [{**row, "value": redactor.redact(row["value"])} for row in rows]
        return rows or None

    # -- shared plumbing
    @staticmethod
    def _element_desc(args: dict, ctx: ToolContext | None) -> str:
        session = peek_session(ctx) if ctx else None
        info = session.info(args.get("ref")) if session else None
        if info is None:
            return f"element [{args.get('ref')}]"
        name = info.get("name") or ""
        return f'{info.get("role", "element")} "{name}"' if name else info.get("role", "element")

    @staticmethod
    def _page_path(ctx: ToolContext | None) -> str:
        session = peek_session(ctx) if ctx else None
        return session.page_path if session else "the current page"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        session = session_for(ctx)
        try:
            await session.start()
            if ctx.extras.get("read_only"):
                refused = self.refusal(args, session)
                if refused:
                    return ToolResult(
                        ok=False,
                        observation=f"Refused: read-only mode (verification) does not allow this action. {refused}",
                        error="read_only_refused",
                    )
            return await self.execute(args, ctx, session)
        except StaleRefError as exc:
            return ToolResult(ok=False, observation=str(exc), error="stale_ref")
        except BrowserToolError as exc:
            return ToolResult(ok=False, observation=str(exc), error="browser_error")
        except PlaywrightError as exc:
            return await self._map_playwright_error(exc, session)

    async def _map_playwright_error(self, exc: PlaywrightError, session: BrowserSession) -> ToolResult:
        message = str(exc)
        short = _first_lines(message)
        if _CLOSED_RE.search(message):
            await session.reset()
            return ToolResult(
                ok=False,
                observation=f"Browser action failed (browser was closed, it will be restarted): {short}",
                error=short,
                transient=True,
            )
        transient = isinstance(exc, PlaywrightTimeoutError) or bool(_TRANSIENT_RE.search(message))
        hint = ""
        if re.search(r"intercepts pointer events|not visible|not enabled|outside of the viewport", message, re.I):
            hint = " The element may be covered, hidden or disabled; try browser_read_page, scroll, or another element."
        return ToolResult(
            ok=False,
            observation=f"Browser action failed: {short}{hint}",
            error=short,
            transient=transient,
        )

    async def finish(
        self,
        session: BrowserSession,
        *,
        evidence_label: str | None = None,
        prefix: str = "",
        settle: bool = True,
        view_reset: bool = False,
    ) -> ToolResult:
        """Settle, snapshot, and optionally attach a screenshot."""
        if settle:
            await session.settle()
        if view_reset:
            session.view_page = 0
        observation = await session.observe()
        if prefix:
            observation = prefix + "\n" + observation
        evidence = [await session.screenshot(evidence_label)] if evidence_label else []
        return ToolResult(ok=True, observation=observation, evidence=evidence, data={"url": session.url})


def _ref_schema(description: str = "Element number from the latest page snapshot, e.g. 7 for '[7] button ...'.") -> dict:
    return {"type": "integer", "description": description}


class NavigateTool(BrowserTool):
    name = "browser_navigate"
    description = (
        "Open a URL in the browser (http/https; a path like '/erp' is resolved against the company environment). "
        "Returns a snapshot of the page: text, alerts and numbered interactive elements."
    )
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute URL or a path starting with '/'."}},
        "required": ["url"],
    }

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Navigate to {args.get('url')}"

    async def execute(self, args, ctx, session):
        raw = str(args["url"]).strip()
        if raw.startswith("/"):
            raw = urljoin(ctx.settings.SIM_BASE_URL.rstrip("/") + "/", raw.lstrip("/"))
        elif not re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I):
            raw = "http://" + raw
        if urlparse(raw).scheme not in ("http", "https"):
            raise BrowserToolError("Only http and https URLs can be opened.")
        started = session.downloads_started
        session.view_page = 0
        try:
            await session.page.goto(raw, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            if "Download is starting" not in str(exc):
                raise
            await session.wait_for_download(started)
            return await self.finish(session, prefix=f"Navigation to {raw} started a file download.", settle=False)
        return await self.finish(session)


class ClickTool(BrowserTool):
    name = "browser_click"
    description = (
        "Click an element by its ref number (links, buttons, checkboxes, tabs...). Returns a fresh page snapshot. "
        "Clicking buttons that submit or change data may require user approval."
    )
    parameters = {"type": "object", "properties": {"ref": _ref_schema()}, "required": ["ref"]}
    max_retries = 1
    risk = Risk.WRITE  # fallback if classification itself fails: never assume a click is harmless

    def classify(self, args: dict, ctx: ToolContext) -> Risk:
        """Risk of clicking ``args['ref']`` according to *this run's* last snapshot. Without a session, or when the
        ref is not in its cache, the element is unknown and the click is WRITE: unknown is never downgraded to READ."""
        session = peek_session(ctx)
        return session.classify_click(args.get("ref")) if session else Risk.WRITE

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Click {self._element_desc(args, ctx)} on {self._page_path(ctx)}"

    def refusal(self, args, session):
        risk = session.classify_click(args.get("ref"))
        if risk != Risk.READ:
            return f"Clicking {self._element_desc(args, session.ctx)} is classified as {risk.value}."
        return None

    async def execute(self, args, ctx, session):
        locator = await session.locate(args["ref"])
        info = session.info(args["ref"]) or {}
        started = session.downloads_started
        try:
            await locator.click(timeout=ACTION_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            if info.get("role") not in ("checkbox", "radio"):
                raise
            await locator.click(force=True, timeout=ACTION_TIMEOUT_MS)  # custom-styled hidden input
        await session.settle()
        if session.downloads_started > started:
            await session.wait_for_download(started)
        return await self.finish(session, evidence_label="click", settle=False)




class TypeTool(BrowserTool):
    name = "browser_type"
    description = (
        "Type text into a text field/textarea by ref. By default replaces the existing value (clear=true). "
        "submit=true presses Enter afterwards (submits the form). For dropdowns use browser_select, "
        "for checkboxes use browser_click."
    )
    parameters = {
        "type": "object",
        "properties": {
            "ref": _ref_schema(),
            "text": {"type": "string", "description": "Text to enter."},
            "clear": {"type": "boolean", "description": "Replace existing content (default true); false appends."},
            "submit": {"type": "boolean", "description": "Press Enter after typing (default false)."},
        },
        "required": ["ref", "text"],
    }
    read_only_safe = False
    max_retries = 1
    risk = Risk.WRITE  # fallback if classification itself fails

    def classify(self, args: dict, ctx: ToolContext) -> Risk:
        """Typing alone is READ; with ``submit`` (Enter) the risk is that of the form the field belongs to, per
        this run's last snapshot. An unknown field (no session / not cached) with ``submit`` is WRITE."""
        if not args.get("submit"):
            return Risk.READ
        session = peek_session(ctx)
        if session is None:
            return Risk.WRITE
        return session.classify_enter_on(session.info(args.get("ref")))

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        session = peek_session(ctx) if ctx else None
        info = session.info(args.get("ref")) if session else None
        secret = bool(info and info.get("type") == "password")
        shown = "••••" if secret else str(args.get("text", ""))
        shown = shown if len(shown) <= 60 else shown[:57] + "..."
        tail = " and press Enter" if args.get("submit") else ""
        return f'Type "{shown}" into {self._element_desc(args, ctx)} on {self._page_path(ctx)}{tail}'

    def submitted_values(self, args: dict, ctx: ToolContext) -> list[dict[str, str]]:
        """With submit=true the typed text goes out with the form (the form's other fields are in the approval preview)."""
        if not args.get("submit"):
            return []
        session = peek_session(ctx)
        info = session.info(args.get("ref")) if session else None
        if info and info.get("type") == "password":
            return []
        label = str((info or {}).get("name") or "") or "Typed text"
        return [{"label": label, "value": str(args.get("text", ""))}]

    def refusal(self, args, session):
        if args.get("submit"):
            return "Typing with submit=true is not allowed in read-only mode."
        if not _is_login_form_field(session, args.get("ref"), username_only=True):
            return ("Typing is not allowed in read-only mode, except the username/email field of a login form "
                    "(and then without submit).")
        return None

    async def execute(self, args, ctx, session):
        locator = await session.locate(args["ref"])
        info = session.info(args["ref"]) or {}
        role, tag, itype = info.get("role"), info.get("tag"), info.get("type")
        if tag == "select" or role in ("combobox", "listbox"):
            raise BrowserToolError(f"[{args['ref']}] is a dropdown; use browser_select instead.")
        if role in ("checkbox", "radio", "button", "link"):
            raise BrowserToolError(f"[{args['ref']}] is a {role}, not a text field; use browser_click instead.")
        if itype == "file":
            raise BrowserToolError("File upload fields are not supported by this tool.")
        text = str(args["text"])
        try:
            if args.get("clear", True):
                await locator.fill(text, timeout=ACTION_TIMEOUT_MS)
            else:
                await locator.focus(timeout=ACTION_TIMEOUT_MS)
                await session.page.keyboard.press("Control+End")  # caret to the end, then append
                await session.page.keyboard.type(text)
        except PlaywrightTimeoutError:
            raise
        except PlaywrightError as exc:
            msg = str(exc)
            if "Malformed value" in msg:
                raise BrowserToolError(
                    f"Field [{args['ref']}] is type={itype}; the value {text!r} has the wrong format "
                    "(dates must be YYYY-MM-DD, times HH:MM, numbers plain digits)."
                ) from exc
            if "cannot be filled" in msg or "not an <input>" in msg or "Element is not" in msg:
                raise BrowserToolError(f"[{args['ref']}] cannot be typed into: {_first_lines(msg, 1)}") from exc
            raise
        if args.get("submit"):
            await session.page.keyboard.press("Enter")
            return await self.finish(session, evidence_label="type_submit", view_reset=False)
        observation = await session.observe()
        return ToolResult(ok=True, observation=observation, data={"url": session.url})


class SelectTool(BrowserTool):
    name = "browser_select"
    description = (
        "Choose an option in a <select> dropdown by ref. 'option' matches the visible label or the value, "
        "case-insensitively. If it does not match, the error lists the valid options."
    )
    parameters = {
        "type": "object",
        "properties": {"ref": _ref_schema(), "option": {"type": "string", "description": "Option label or value."}},
        "required": ["ref", "option"],
    }
    read_only_safe = False
    max_retries = 1

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f'Select "{args.get("option")}" in {self._element_desc(args, ctx)} on {self._page_path(ctx)}'

    def refusal(self, args, session):
        return "Selecting options is not allowed in read-only mode."

    async def execute(self, args, ctx, session):
        locator = await session.locate(args["ref"])
        options = await locator.evaluate(
            "el => el.tagName === 'SELECT' ? Array.from(el.options).map((o, i) => "
            "({i, label: (o.label || o.text || '').trim(), value: o.value, disabled: o.disabled})) : null"
        )
        if options is None:
            raise BrowserToolError(
                f"[{args['ref']}] is not a <select> element. For custom dropdowns, click it, then click the option."
            )
        wanted = str(args["option"]).strip().lower()
        usable = [o for o in options if not o["disabled"]]
        match = [o for o in usable if o["label"].lower() == wanted] or [o for o in usable if o["value"].lower() == wanted]
        if not match:
            partial = [o for o in usable if wanted and wanted in o["label"].lower()]
            if len(partial) == 1:
                match = partial
            elif len(partial) > 1:
                raise BrowserToolError(
                    f"'{args['option']}' matches several options: {', '.join(repr(o['label']) for o in partial)}. "
                    "Use the exact label."
                )
        if not match:
            listing = ", ".join(repr(o["label"]) for o in usable[:40])
            more = f" (+{len(usable) - 40} more)" if len(usable) > 40 else ""
            raise BrowserToolError(f"No option '{args['option']}' in [{args['ref']}]. Valid options: {listing}{more}")
        await locator.select_option(index=match[0]["i"], timeout=ACTION_TIMEOUT_MS)
        prefix = f"Selected '{match[0]['label']}'."
        return await self.finish(session, evidence_label="select", prefix=prefix, settle=True)


class PressTool(BrowserTool):
    name = "browser_press"
    description = (
        "Press a keyboard key on the focused element, e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown', 'Control+A'. "
        "Enter inside a form submits it."
    )
    parameters = {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "Key name as used by Playwright, e.g. 'Enter'."}},
        "required": ["key"],
    }
    read_only_safe = False
    risk = Risk.WRITE  # fallback if classification itself fails

    def classify(self, args: dict, ctx: ToolContext) -> Risk:
        """Enter/Space is judged by the focused element in this run's last snapshot; other keys are READ. Without a
        session nothing can be focused or submitted, so that is READ too."""
        session = peek_session(ctx)
        return session.classify_press(str(args.get("key", ""))) if session else Risk.READ

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        session = peek_session(ctx) if ctx else None
        focused = session.info(session.focus) if session and session.focus else None
        where = ""
        if focused:
            where = f' on {focused.get("role")} "{focused.get("name", "")}"'
        return f"Press {args.get('key')}{where} on {self._page_path(ctx)}"

    def refusal(self, args, session):
        risk = session.classify_press(str(args.get("key", "")))
        return f"Pressing {args.get('key')} here is classified as {risk.value}." if risk != Risk.READ else None

    async def execute(self, args, ctx, session):
        key = str(args["key"])
        await session.page.keyboard.press(key)
        return await self.finish(session, evidence_label="press" if _is_enter(key) else None)


class ScrollTool(BrowserTool):
    name = "browser_scroll"
    description = (
        "Scroll the page ('down', 'up', 'top', 'bottom'). Also pages through long text/element lists when a snapshot "
        "says '(truncated ...)', and loads lazy content."
    )
    parameters = {
        "type": "object",
        "properties": {"direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]}},
    }

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Scroll {args.get('direction', 'down')} on {self._page_path(ctx)}"

    async def execute(self, args, ctx, session):
        direction = str(args.get("direction") or "down").lower()
        if direction not in ("down", "up", "top", "bottom"):
            raise BrowserToolError("direction must be one of: down, up, top, bottom")
        script = {
            "down": "window.scrollBy(0, Math.round(window.innerHeight * 0.8))",
            "up": "window.scrollBy(0, -Math.round(window.innerHeight * 0.8))",
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.documentElement.scrollHeight)",
        }[direction]
        await session.page.evaluate(script)
        if direction == "down":
            session.view_page = min(session.view_page + 1, max(session.n_pages - 1, 0))
        elif direction == "up":
            session.view_page = max(session.view_page - 1, 0)
        elif direction == "top":
            session.view_page = 0
        else:
            session.view_page = max(session.n_pages - 1, 0)
        await asyncio.sleep(0.3)  # let lazy content load
        return await self.finish(session, settle=False)


class BackTool(BrowserTool):
    name = "browser_back"
    description = "Go back one page in the browser history."
    parameters = {"type": "object", "properties": {}}

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return "Go back in browser history"

    async def execute(self, args, ctx, session):
        before = session.page.url
        response = await session.page.go_back(wait_until="domcontentloaded")
        session.view_page = 0
        if response is None and session.page.url == before:
            raise BrowserToolError("There is no earlier page in the history.")
        return await self.finish(session)


class ReadPageTool(BrowserTool):
    name = "browser_read_page"
    description = (
        "Re-read the current page: text, alerts and numbered interactive elements (refs are refreshed). "
        "Pass `find` to return only the lines mentioning that text (with neighbours) - use it on long pages."
    )
    parameters = {
        "type": "object",
        "properties": {"find": {"type": "string", "description": "Optional text to search for (case-insensitive)."}},
    }

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        find = args.get("find")
        return f'Read page{f" searching for {find!r}" if find else ""}'

    async def execute(self, args, ctx, session):
        find = (args.get("find") or "").strip() or None
        await session.settle()
        observation = await session.observe(find=find, boost=2.0 if not find else 1.0)
        return ToolResult(ok=True, observation=observation, data={"url": session.url})


class ScreenshotTool(BrowserTool):
    name = "browser_screenshot"
    description = "Save a screenshot of the current page as evidence (it is stored, not shown to you)."
    parameters = {
        "type": "object",
        "properties": {"label": {"type": "string", "description": "Short label for the file name, e.g. 'bill_saved'."}},
    }

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Take screenshot '{args.get('label', 'shot')}'"

    async def execute(self, args, ctx, session):
        rel = await session.screenshot(str(args.get("label") or "shot"))
        brief = await session.brief()
        return ToolResult(ok=True, observation=f"Screenshot saved: {rel}\n{brief}", evidence=[rel])


class DownloadTool(BrowserTool):
    name = "browser_download"
    description = (
        "Click a download link/button by ref and save the file. Returns the saved path (e.g. 'downloads/invoice.pdf'), "
        "readable with read_file. Also handles links that open a PDF inline."
    )
    parameters = {"type": "object", "properties": {"ref": _ref_schema()}, "required": ["ref"]}
    max_retries = 1

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Download file via {self._element_desc(args, ctx)} on {self._page_path(ctx)}"

    async def execute(self, args, ctx, session):
        locator = await session.locate(args["ref"])
        info = session.info(args["ref"]) or {}
        started = session.downloads_started
        count_before = len(session.downloads)
        session.last_nav = None
        await locator.click(timeout=ACTION_TIMEOUT_MS)
        saved = await session.wait_for_download(started, start_timeout=2.5)
        if not saved and len(session.downloads) == count_before:
            # No download event: the link may have opened a file (e.g. PDF) inline. Fetch it with the session cookies.
            await session.settle()
            if len(session.downloads) == count_before:
                nav = session.last_nav or {}
                target = None
                if "pdf" in nav.get("ctype", "").lower() or nav.get("disposition"):
                    target = nav.get("url")
                elif info.get("href"):
                    target = urljoin(session.page.url, info["href"])
                if not target:
                    raise BrowserToolError(
                        f"Clicking [{args['ref']}] did not start a download. Use browser_read_page to find the download link."
                    )
                await session.save_response_download(target)
        saved_path = session.downloads[-1]
        observation = await session.observe()
        return ToolResult(
            ok=True,
            observation=f"Saved download: {saved_path}\n" + observation,
            data={"path": saved_path, "url": session.url},
        )


def browser_tools() -> list[BrowserTool]:
    """One instance of every browser tool. They are stateless, so a single set can be shared by all runs."""
    return [
        cls()
        for cls in (
            NavigateTool,
            ClickTool,
            TypeTool,
            SelectTool,
            PressTool,
            ScrollTool,
            BackTool,
            ReadPageTool,
            ScreenshotTool,
            DownloadTool,
        )
    ]


def register_browser_tools(registry: ToolRegistry) -> list[BrowserTool]:
    """Register all browser tools in ``registry`` and return them."""
    return [registry.register(tool) for tool in browser_tools()]
