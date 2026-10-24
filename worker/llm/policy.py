"""PolicyLLM: a deterministic, rule-based stand-in for a language model that drives the REAL tools end to end.

``LLM_PROVIDER=policy`` selects it. It exists for the load test (``scripts/loadtest.py``): every code path of the platform
runs for real (queue, browser pool, vault logins, approvals, events, memory, verification), but no model quota is used
and the outcome is reproducible. It is *not* a toy script of tool calls. Like a model it only ever sees the prompt the
runner builds (``context.build_messages``), and it decides each next action from what it reads there:

* the **latest observation**: the page snapshot (``[7] button "Save bill"``) is parsed into elements by role and name;
* the **working memory** shown in the state block: facts it remembered earlier. Because every decision depends on the
  latest page and those facts alone, a run resumed on another worker after a crash simply carries on.

The task it understands is the headline one, in two shapes: "Find the latest invoice from <vendor>, extract the amount and
due date, enter it into our internal system ..." and "Find invoice <NUMBER> in the vendor portal, ... enter it into
our internal system ...". The flow: log in to the vendor portal with the vault tools (``list_credentials``,
``browser_fill_secret``), pick the vendor's latest invoice by comparing issue dates on the list, read amount / due date /
issue date on the detail page (and cross-check the PDF with ``read_file``), log in to the ERP, check that no bill exists
yet, fill the new-bill form, click "Save bill" (that call is gated for human approval), confirm "Bill #N created", re-open
the bill, remember the facts plus one durable how-to note, and ``finish``. It copes with the chaos flags: the one-time
HTTP 500 of the form (re-read the page, then retry), an expired ERP session (log in again, check again), and a bill that
somebody else just created (it reports the existing one instead of creating a duplicate).

As the **auditor** (the verifier's system prompt is detected) it re-opens the bill, the portal invoice and the ERP's REST
list and submits a verdict that reports only what it observed. When a system shows its login page (a fresh browser after a
resume, an expired session) the auditor signs in with ``list_credentials`` + ``browser_fill_secret`` (authentication is the
one thing the read-only auditor may do) and opens the page again. As a fallback, if an audit still rejects the claim because
it could not see the portal, the worker opens the source again once and finishes again.

Like a real model it keeps its plan current: besides the plan it sends on the first turn, any turn after which the five
checklist items' statuses (derived from the remembered facts, the page it is on and its own decision) differ from the plan
the state block shows also carries an ``update_plan`` call. That call is bookkeeping only: it never changes the actions, and
``finish`` still goes alone (the last item is marked done on the turn before it, once the bill's values were seen to agree).

``think_ms`` simulates model latency (jittered by +-40%), and token counts are estimated from the text (about 4 characters per
token), so the shared rate limiter and the usage accounting see realistic traffic.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, quote_plus, urlparse

from worker.llm.base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec, Usage

DEFAULT_THINK_MS = 800.0
THINK_JITTER = 0.4
CHARS_PER_TOKEN = 4
MAX_SAVE_ATTEMPTS = 3
STATE_MARKER = "=== CURRENT STATE"
RESUME_MARKER = "Run resumed after an interruption"
PLAN_ITEMS = ("Log in to the vendor portal and find the invoice",
              "Read the amount, due date and issue date",
              "Check the ERP for an existing bill",
              "Enter the bill in the ERP and save it (needs approval)",
              "Re-open the bill and confirm the values")
INVOICE_FACTS = ("vendor", "invoice_number", "amount", "currency", "issue_date", "due_date")
ERP_NOTE_KEY = "erp_new_bill_form"
ERP_NOTE = ("The ERP new-bill form takes the vendor from a dropdown by name, the amount as a plain number with at most two "
            "decimals (no thousands separator) and both dates as YYYY-MM-DD; Save bill then shows a 'Bill created' notice.")


# --------------------------------------------------------------------------- reading observations


@dataclass
class Element:
    """One interactive element line of a snapshot: ``[3] combobox "Vendor" selected="X" options=[A, B]``."""

    ref: int
    role: str
    name: str
    selected: str | None = None
    options: list[str] = field(default_factory=list)
    value: str = ""
    href: str = ""


@dataclass
class Page:
    """A parsed page snapshot."""

    url: str
    title: str = ""
    status: int | None = None
    alerts: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)

    @property
    def path(self) -> str:
        return urlparse(self.url).path

    @property
    def text(self) -> str:
        return "\n".join(self.lines + self.alerts)

    def find(self, role: str, name: str) -> Element | None:
        wanted = name.lower()
        return next((e for e in self.elements if e.role == role and e.name.lower() == wanted), None)

    def labelled(self, label: str) -> str | None:
        """The line after a ``<dt>`` label line (the detail pages render ``Label`` and its value on consecutive lines)."""
        for i, line in enumerate(self.lines[:-1]):
            if line == label:
                return self.lines[i + 1]
        return None


_ELEMENT = re.compile(r'^\[(\d+)\] (\S+) "([^"]*)"(.*)$')
_FACT = re.compile(r"^- ([\w.]+): (.*?)(?:  \[[^\]]*\])?(?:  \(durable\))?$")
_PORTAL_ROW = re.compile(r"^(?P<num>[\w-]+)\[(?P<ref>\d+)\] \| (?P<vendor>.+?) \| (?P<issue>\d{4}-\d{2}-\d{2}) \| (?P<status>\w+)$")
_ERP_ROW = re.compile(r"^#(?P<id>\d+)\[(?P<ref>\d+)\] \| (?P<vendor>.+?) \| (?P<invoice>\S+) \| (?P<amount>[\d,]+\.\d{2}) \| "
                      r"(?P<currency>[A-Z]{3}) \| (?P<due>\d{4}-\d{2}-\d{2}) \| (?P<status>\w+)$")
_TASK_LATEST = re.compile(r"latest invoice (?:from|of|by)\s+(?P<vendor>.+?)(?:\s*[,;.]|\s+(?:and|then|extract|enter|into)\b|$)", re.I)
_TASK_NUMBER = re.compile(r"\binvoice\s+(?:number\s+|#\s*)?(?P<number>[A-Z]{2,5}-[A-Z0-9-]*\d[A-Z0-9-]*)", re.I)


def parse_page(text: str) -> Page | None:
    """Parse the snapshot inside a tool observation (it may follow a one-line prefix and be followed by the state block).
    None when ``text`` holds no snapshot (a failed action, a file, an API response)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("URL: ")), None)
    if start is None:
        return None
    page = Page(url=lines[start][5:].strip())
    i = start + 1
    while i < len(lines) and not lines[i].startswith("--- Page text"):
        line = lines[i]
        if line.startswith("Title: "):
            page.title = line[7:]
        elif line.startswith("HTTP status of last navigation: "):
            page.status = int(line.rsplit(" ", 1)[1])
        elif line.startswith("Alerts: "):
            page.alerts = [a.strip() for a in line[8:].split(" ; ")]
        i += 1
    i += 1
    while i < len(lines) and not lines[i].startswith("--- Interactive elements"):
        page.lines.append(lines[i])
        i += 1
    if i >= len(lines):
        return None
    for line in lines[i + 1:]:
        match = _ELEMENT.match(line)
        if match is None:
            if page.elements and not line.startswith("("):
                break  # the element list ended (blank line, then the state block)
            continue
        rest = match.group(4)
        selected = re.search(r' selected="([^"]*)"', rest)
        options = re.search(r" options=\[(.*)\]", rest)
        value = re.search(r' value="([^"]*)"', rest)
        href = re.search(r" -> (\S+)\s*$", rest)
        page.elements.append(Element(
            ref=int(match.group(1)), role=match.group(2), name=match.group(3),
            selected=selected.group(1) if selected else None,
            options=[o for o in options.group(1).split(", ") if o and not o.startswith("...(+")] if options else [],
            value=value.group(1) if value else "", href=href.group(1) if href else ""))
    return page


def parse_facts(text: str) -> dict[str, str]:
    """The remembered facts shown under ``Working memory:`` (or the auditor's ``WORKING-MEMORY FACTS``)."""
    facts: dict[str, str] = {}
    marker = re.search(r"(?:Working memory:|WORKING-MEMORY FACTS \(unverified\):)\n", text)
    if marker is None:
        return facts
    for line in text[marker.end():].splitlines():
        if not line.strip():
            break
        match = _FACT.match(line)
        if match:
            facts[match.group(1)] = match.group(2)
    return facts


@dataclass
class Target:
    """What the task asks for: the latest invoice of a vendor, or one named invoice."""

    kind: Literal["latest", "invoice"]
    vendor: str = ""
    number: str = ""


def parse_task(task: str) -> Target | None:
    number = _TASK_NUMBER.search(task)
    if number:
        return Target("invoice", number=number.group("number").upper())
    latest = _TASK_LATEST.search(task)
    return Target("latest", vendor=latest.group("vendor").strip()) if latest else None


def plain_amount(amount: str) -> str:
    """``'5,681.96 USD'`` / ``'5,681.96'`` -> ``'5681.96'``."""
    return amount.split()[0].replace(",", "") if amount.strip() else ""


def same_amount(a: str, b: str) -> bool:
    try:
        return abs(float(plain_amount(a)) - float(plain_amount(b))) < 0.005
    except ValueError:
        return False


def same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


# --------------------------------------------------------------------------- what the policy sees


@dataclass
class CallResult:
    name: str
    args: dict
    ok: bool
    content: str


@dataclass
class Situation:
    """Everything one decision may depend on, extracted from the messages the runner sent."""

    target: Target
    facts: dict[str, str]
    page: Page | None  # the browser's current page, None when unknown (nothing opened yet, just resumed, a failure)
    last: list[CallResult]  # results of the previous turn's calls, in order
    has_plan: bool
    pdf_check: bool
    plan: list[tuple[str, str]] = field(default_factory=list)  # (status, text) of the plan the state block shows


_PLAN_STATUS_OF_MARK = {"[ ]": "pending", "[>]": "in_progress", "[x]": "done", "[!]": "blocked", "[-]": "skipped"}
_PLAN_LINE = re.compile(r"^(\[.\]) \d+\. (.*?)(?:  -- .*)?$")


def parse_plan(state_text: str) -> list[tuple[str, str]]:
    """The plan checklist of the state block as ``(status, text)`` pairs (``[x] 2. Read ...  -- note``); empty when none."""
    block = state_text.rsplit(STATE_MARKER, 1)[-1]
    marker = block.find("Plan:\n")
    if marker < 0:
        return []
    plan: list[tuple[str, str]] = []
    for line in block[marker + len("Plan:\n"):].splitlines():
        match = _PLAN_LINE.match(line)
        if match is None or match.group(1) not in _PLAN_STATUS_OF_MARK:
            break
        plan.append((_PLAN_STATUS_OF_MARK[match.group(1)], match.group(2)))
    return plan


def _strip_state(content: str) -> str:
    return content.split("\n\n" + STATE_MARKER, 1)[0]


def read_situation(messages: list[Message], target: Target, *, pdf_check: bool) -> Situation:
    state_text = messages[-1].content
    last_assistant = next((i for i in range(len(messages) - 1, -1, -1)
                           if messages[i].role == "assistant" and messages[i].tool_calls), None)
    last: list[CallResult] = []
    if last_assistant is not None:
        calls = {c.id: c for c in messages[last_assistant].tool_calls}
        for message in messages[last_assistant + 1:]:
            call = calls.get(message.tool_call_id or "")
            if message.role == "tool" and call is not None:
                content = _strip_state(message.content)
                last.append(CallResult(call.name, call.args, not content.startswith("ERROR"), content))
    page = None
    if RESUME_MARKER not in state_text:  # after a resume the browser is new: whatever the transcript shows is gone
        for message in reversed(messages):
            if message.role == "tool":
                page = parse_page(_strip_state(message.content))
                if page is not None:
                    break
    return Situation(target=target, facts=parse_facts(state_text), page=page, last=last,
                     has_plan="Plan:\n(no plan yet)" not in state_text, pdf_check=pdf_check, plan=parse_plan(state_text))


@dataclass
class Decision:
    text: str
    calls: list[tuple[str, dict]] = field(default_factory=list)


def _remember(key: str, value: str, source: str, **extra: Any) -> tuple[str, dict]:
    return "remember", {"key": key, "value": value, "source": source, **extra}


def _finish(status: str, summary: str, outputs: dict[str, str] | None = None, evidence: list[tuple[str, str]] | None = None) -> Decision:
    return Decision(summary, [("finish", {
        "status": status, "summary": summary,
        "outputs": [{"name": k, "value": str(v)} for k, v in (outputs or {}).items()],
        "evidence": [{"claim": c, "source": s} for c, s in (evidence or [])]})])


def _goto(path: str, why: str) -> Decision:
    return Decision(why, [("browser_navigate", {"url": path})])


# --------------------------------------------------------------------------- the worker


def start_url(s: Situation) -> str:
    """Where the next piece of work is, according to the facts: the bill, the ERP check, or the portal."""
    facts = s.facts
    if facts.get("bill_id"):
        return f"/erp/bills/{facts['bill_id']}"
    if facts.get("invoice_number"):
        return f"/erp/bills?q={quote_plus(facts['invoice_number'])}"
    if s.target.number:
        return f"/portal/invoices/{s.target.number}"
    return f"/portal/invoices?vendor={quote_plus(facts.get('vendor_filter') or s.target.vendor)}"


def decide_worker(messages: list[Message], tool_names: set[str], *, pdf_check: bool = True) -> Decision:
    first_user = next((m.content for m in messages if m.role == "user"), "")
    task_match = re.search(r"Task from the user:\n(.*?)\n\nToday's date", first_user, re.S)
    target = parse_task(task_match.group(1) if task_match else first_user)
    if target is None:
        return _finish("blocked", "I could not tell which invoice the task is about, so I did not touch any system.")
    s = read_situation(messages, target, pdf_check=pdf_check and {"browser_download", "read_file"} <= tool_names)
    if not s.has_plan:
        return _first_turn(s)
    return _with_plan_update(s, _next_action(s))


def _next_action(s: Situation) -> Decision:
    """The real next action(s), decided from the latest page and the remembered facts alone."""
    if any(r.name == "finish" and not r.ok for r in s.last):
        return _after_failed_audit(s)
    failed = next((r for r in s.last if not r.ok), None)
    if failed is not None:
        return _recover(s, failed)
    if "pdf_check" not in s.facts and s.last:
        if s.last[-1].name == "browser_download":
            saved = re.search(r"Saved download: (\S+)", s.last[-1].content)
            return Decision("The PDF is downloaded; reading it to cross-check the invoice.",
                            [("read_file", {"path": saved.group(1) if saved else "downloads"})])
        if s.last[-1].name == "read_file":
            return _after_pdf(s, s.last[-1])
    page = s.page
    if page is None:
        return _goto(start_url(s), "Opening the page the next step needs.")
    path = page.path
    if path.endswith("/login"):
        return _at_login(s, page)
    if path == "/portal/invoices":
        return _at_portal_list(s, page)
    if path.startswith("/portal/invoices/"):
        return _at_portal_detail(s, page)
    if path == "/erp/bills/new":
        return _at_erp_form(s, page)
    if re.fullmatch(r"/erp/bills/\d+", path):
        return _at_erp_bill(s, page)
    if path == "/erp/bills":
        return _at_erp_list(s, page)
    return _goto(start_url(s), "This is not a page I need; going to the next one.")


def _first_turn(s: Situation) -> Decision:
    t = s.target
    what = f"invoice {t.number}" if t.kind == "invoice" else f"the latest invoice from {t.vendor}"
    criteria = [
        f"An ERP bill exists for {what} as shown in the vendor portal, and it is the only bill for that invoice.",
        "The bill's vendor, invoice number, amount and currency equal the vendor portal invoice.",
        "The bill's issue date and due date equal the vendor portal invoice.",
    ]
    plan = [(PLAN_ITEMS[0], "in_progress"), *((x, "pending") for x in PLAN_ITEMS[1:])]
    text = (f"Goal: record {what} as a bill in the ERP. I do not know the invoice details yet, so I check which "
            "credentials I may use and then open the vendor portal.")
    return Decision(text, [
        ("update_plan", {"goal_summary": f"Enter {what} into the ERP as an accounts-payable bill.",
                         "success_criteria": criteria, "items": [{"text": x, "status": st} for x, st in plan]}),
        ("list_credentials", {})])


def _confirmation(page: Page | None, facts: dict[str, str]) -> list[str] | None:
    """Has the stand-in seen the bill's values yet, and do they agree with the invoice? ``[]``: seen and equal; a list of
    differences: seen and different; None: not seen. The bill page shows every value; the ERP list row shows the amount,
    currency and due date."""
    bill_id = facts.get("bill_id", "")
    if page is None or not bill_id:
        return None
    if page.path == f"/erp/bills/{bill_id}":
        fields = _bill_fields(page)
        return _bill_problems(fields, facts) if all(fields.values()) else None
    row = next((r for r in _erp_rows(page) if r["id"] == bill_id), None) if page.path == "/erp/bills" else None
    if row is None:
        return None
    problems = []
    if not same_amount(row["amount"], facts.get("amount", "")) or not same(row["currency"], facts.get("currency", "")):
        problems.append(f"amount {row['amount']} {row['currency']}")
    if not same(row["due"], facts.get("due_date", "")):
        problems.append(f"due date {row['due']}")
    return problems


def derive_plan(s: Situation, decision: Decision) -> list[tuple[str, str, str]] | None:
    """The plan's ``(text, status, note)`` items as the situation (plus the facts this turn's own ``remember`` calls add)
    shows them: finished items ``done``, the first unfinished one ``in_progress``, the rest ``pending``. An item that was
    done or skipped stays so (the plan never goes backwards). The bill step is ``skipped`` when a bill for the invoice
    already existed, and the last step is ``blocked`` when the bill's values differ from the invoice. The last step is
    ``done`` as soon as the bill's values have been seen and agree, which is the turn before ``finish``. None when the
    plan shown is not the stand-in's own."""
    if [text for _, text in s.plan] != list(PLAN_ITEMS):
        return None
    facts = {**s.facts, **{a["key"]: a["value"] for name, a in decision.calls if name == "remember"}}
    page = s.page
    path = page.path if page else ""
    on_invoice = path.startswith("/portal/invoices/") and not (page and page.status and page.status >= 400)
    opens_form = any(name == "browser_navigate" and urlparse(a.get("url", "")).path == "/erp/bills/new" for name, a in decision.calls)
    bill_id, bill_status = facts.get("bill_id", ""), facts.get("bill_status", "")
    exists = bill_status == "exists"
    problems = _confirmation(page, facts)
    satisfied = [bool(facts.get("invoice_number")) or on_invoice,
                 all(facts.get(k) for k in INVOICE_FACTS),
                 bool(bill_id) or path == "/erp/bills/new" or opens_form,
                 bill_status in ("created", "exists"),
                 problems == []]
    items: list[tuple[str, str, str]] = []
    current_seen = False
    for i, text in enumerate(PLAN_ITEMS):
        before = s.plan[i][0]
        note = f"Bill #{bill_id} was already in the ERP; nothing was created." if i == 3 and exists else ""
        if before in ("done", "skipped") or (satisfied[i] and not current_seen):
            status = "skipped" if i == 3 and exists else "done"
        elif not current_seen:
            current_seen = True
            status = "blocked" if i == 4 and problems else "in_progress"
            note = f"The bill does not match the invoice: {', '.join(problems)}." if status == "blocked" else ""
        else:
            status = "pending"
        items.append((text, status, note))
    return items


def _with_plan_update(s: Situation, decision: Decision) -> Decision:
    """Add an ``update_plan`` call to this turn when the derived plan statuses differ from the plan the run shows. It goes
    first (so a failing action cannot skip it), changes nothing else, and never joins a ``finish`` (which stays alone)."""
    if any(name == "finish" for name, _ in decision.calls):
        return decision
    items = derive_plan(s, decision)
    if items is None or [status for _, status, _ in items] == [status for status, _ in s.plan]:
        return decision
    plan = [{"text": text, "status": status, **({"note": note} if note else {})} for text, status, note in items]
    return Decision(decision.text, [("update_plan", {"items": plan}), *decision.calls])


def _after_failed_audit(s: Situation) -> Decision:
    """The auditor rejected the claim. The usual cause after a restart is that it could not open the portal invoice: its
    read-only browser cannot sign in, and this run's own portal session died with the old browser. So open the invoice
    again (signing in), then finish again; if a second audit still fails, say so honestly."""
    number = s.facts.get("invoice_number") or s.target.number
    if "audit_resync" in s.facts or not number:
        return _finish("failed", "The independent audit found a problem with the bill that I cannot correct here. "
                                 "I am not claiming success.")
    return Decision("The audit could not confirm my result; my session on the vendor portal may be gone. Opening the invoice there "
                    "again, then I will finish again.",
                    [_remember("audit_resync", "reopened the portal invoice after an audit that could not see it", "independent audit feedback"),
                     ("browser_navigate", {"url": f"/portal/invoices/{number}"})])


def _recover(s: Situation, failed: CallResult) -> Decision:
    if "DENIED" in failed.content:
        return _finish("blocked", "The approver denied saving the bill, so nothing was entered in the ERP.")
    if failed.name in ("browser_download", "read_file") and "pdf_check" not in s.facts:
        return Decision("The PDF copy could not be read; the detail page is the source, so I go on without it.",
                        [_remember("pdf_check", "skipped (the PDF could not be read)", "portal invoice page"),
                         ("browser_navigate", {"url": start_url(s)})])
    if failed.name == "browser_fill_secret" and "No credential" in failed.content:
        return _finish("blocked", "The vault has no usable login for this system, so I cannot sign in.")
    return Decision(f"{failed.name} failed; re-reading the page to see where I am.", [("browser_read_page", {})])


def _at_login(s: Situation, page: Page) -> Decision:
    app = "portal" if "/portal/" in page.path else "erp" if "/erp/" in page.path else ""
    user, password, button = page.find("textbox", "Username"), page.find("textbox", "Password"), page.find("button", "Sign in")
    if "Invalid username or password" in page.text:
        return _finish("blocked", f"The stored {app} login was rejected, so I cannot sign in.")
    if not (app and user and password and button):
        return _goto(start_url(s), "The login form is not what I expected; starting that page again.")
    return Decision(f"Signing in to the {app} with the stored credential (I never see the password).", [
        ("browser_fill_secret", {"ref": user.ref, "credential": app, "field": "username"}),
        ("browser_fill_secret", {"ref": password.ref, "credential": app, "field": "password"}),
        ("browser_click", {"ref": button.ref})])


def _portal_rows(page: Page) -> list[dict]:
    return [m.groupdict() for m in (_PORTAL_ROW.match(line) for line in page.lines) if m]


def _at_portal_list(s: Situation, page: Page) -> Decision:
    target = s.target
    if target.kind == "invoice":
        return _goto(f"/portal/invoices/{target.number}", f"Opening invoice {target.number} directly.")
    query = parse_qs(urlparse(page.url).query)
    current = (query.get("vendor") or [""])[0]
    wanted = s.facts.get("vendor_filter") or target.vendor
    if not current:
        return _goto(f"/portal/invoices?vendor={quote_plus(wanted)}", f"Filtering the invoice list to {wanted}.")
    rows = [r for r in _portal_rows(page) if same(r["vendor"], current)]
    if not rows and not s.facts.get("vendor_filter"):
        combo = page.find("combobox", "Vendor")
        options = [o for o in (combo.options if combo else []) if target.vendor.lower() in o.lower() and not same(o, current)]
        if len(options) == 1:
            return Decision(f"No invoices for '{current}'; the vendor list has '{options[0]}', which must be the one.", [
                _remember("vendor_filter", options[0], "vendor dropdown of the portal list"),
                ("browser_navigate", {"url": f"/portal/invoices?vendor={quote_plus(options[0])}"})])
    best = max(rows, key=lambda r: r["issue"], default=None)
    earlier = s.facts.get("best_candidate", "")
    if earlier and (best is None or earlier.split("|")[1] > best["issue"]):
        best = {"num": earlier.split("|")[0], "issue": earlier.split("|")[1], "vendor": current}
    if best is None:
        return _finish("failed", f"The portal shows no invoice from {target.vendor}, so there is nothing to enter.")
    nxt = page.find("link", "Next")
    if nxt is not None:
        return Decision(f"The newest issue date so far is {best['issue']} ({best['num']}); checking the next page too.", [
            _remember("best_candidate", f"{best['num']}|{best['issue']}", "portal invoice list, issue dates compared"),
            ("browser_click", {"ref": nxt.ref})])
    link = page.find("link", best["num"])
    why = f"The latest {target.vendor} invoice by issue date is {best['num']} ({best['issue']}); opening it for the details."
    if link is not None:
        return Decision(why, [("browser_click", {"ref": link.ref})])
    return _goto(f"/portal/invoices/{best['num']}", why)


def _at_portal_detail(s: Situation, page: Page) -> Decision:
    if page.status and page.status >= 400:
        return _finish("failed", "The portal does not have that invoice (page not found).")
    number = page.path.rsplit("/", 1)[1]
    vendor, issue, due = page.labelled("Vendor"), page.labelled("Issue date"), page.labelled("Due date")
    amount_text = page.labelled("Amount")
    if not (vendor and issue and due and amount_text):
        return Decision("The invoice page did not show every field; re-reading it.", [("browser_read_page", {})])
    amount, _, currency = amount_text.partition(" ")
    source = f"portal invoice page {page.path}"
    wanted = {"vendor": vendor, "invoice_number": number, "issue_date": issue, "due_date": due,
              "amount": plain_amount(amount), "currency": currency.strip()}
    remembers = [_remember(k, v, source) for k, v in wanted.items() if s.facts.get(k) != v]
    if s.pdf_check and "pdf_check" not in s.facts:
        link = page.find("link", "Download PDF")
        if link is not None:
            return Decision(f"Invoice {number}: {amount_text}, issued {issue}, due {due}. Downloading the PDF to cross-check it.",
                            [*remembers, ("browser_download", {"ref": link.ref})])
    return Decision(f"Invoice {number} from {vendor}: {amount_text}, issued {issue}, due {due}. Checking the ERP for an existing bill.",
                    [*remembers, ("browser_navigate", {"url": f"/erp/bills?q={quote_plus(number)}"})])


def _after_pdf(s: Situation, read: CallResult) -> Decision:
    amount = s.facts.get("amount", "")
    try:
        shown = f"{float(amount):,.2f}"
    except ValueError:
        shown = amount
    agrees = bool(amount) and shown in read.content and s.facts.get("invoice_number", "") in read.content
    note = (f"the PDF agrees with the portal page (total {shown} {s.facts.get('currency', '')})" if agrees
            else f"WARNING: the PDF does not show the total {shown} the portal page gave; the portal page was used")
    return Decision("PDF cross-check done; moving on to the ERP." if agrees else "The PDF differs from the page; noting it and moving on.",
                    [_remember("pdf_check", note, "downloaded invoice PDF"), ("browser_navigate", {"url": start_url(s)})])


def _erp_rows(page: Page) -> list[dict]:
    return [m.groupdict() for m in (_ERP_ROW.match(line) for line in page.lines) if m]


def _at_erp_list(s: Situation, page: Page) -> Decision:
    facts = s.facts
    if facts.get("bill_id"):
        return _goto(f"/erp/bills/{facts['bill_id']}", "The bill is known; opening it.")
    number = facts.get("invoice_number")
    if not number:
        return _goto(start_url(s), "I do not have the invoice details yet; going back to the portal.")
    row = next((r for r in _erp_rows(page) if same(r["invoice"], number) and same(r["vendor"], facts.get("vendor", r["vendor"]))), None)
    if row is not None:
        link = page.find("link", f"#{row['id']}")
        calls = [_remember("bill_id", row["id"], f"ERP bill list, invoice {number}"),
                 _remember("bill_status", "exists", "ERP bill list, found before saving anything")]
        calls.append(("browser_click", {"ref": link.ref}) if link else ("browser_navigate", {"url": f"/erp/bills/{row['id']}"}))
        return Decision(f"A bill for {number} already exists (Bill #{row['id']}); I will not create a duplicate and check it instead.", calls)
    return _goto("/erp/bills/new", f"No bill for {number} yet; opening the new-bill form.")


def _at_erp_form(s: Situation, page: Page) -> Decision:
    facts = s.facts
    if not all(facts.get(k) for k in INVOICE_FACTS):
        return _goto(start_url(s), "I am missing the invoice details; reading them from the portal first.")
    text = page.text
    duplicate = re.search(r"already exists \(Bill #(\d+)\)", text)
    if duplicate:
        bill_id = duplicate.group(1)
        return Decision(f"The ERP says Bill #{bill_id} already exists for this invoice (created meanwhile); checking that one.", [
            _remember("bill_id", bill_id, "ERP duplicate warning on the new-bill form"),
            _remember("bill_status", "exists", "ERP duplicate warning on the new-bill form"),
            ("browser_navigate", {"url": f"/erp/bills/{bill_id}"})])
    server_error = "Internal Server Error" in text
    if server_error and not (s.last and s.last[-1].name == "browser_read_page"):
        return Decision("The ERP answered with a server error. I re-read the page to see whether the form is intact before retrying once.",
                        [("browser_read_page", {})])
    other_alert = next((a for a in page.alerts if a.startswith("[role=alert]") and "Internal Server Error" not in a), None)
    if other_alert:
        reason = other_alert[len("[role=alert]"):].strip(" \"")
        return _finish("failed", f"The ERP rejected the bill: {reason}")
    plan: list[tuple[str, str, str]] = [
        ("combobox", "Vendor", facts["vendor"]), ("textbox", "Invoice number", facts["invoice_number"]),
        ("textbox", "Amount", facts["amount"]), ("combobox", "Currency", facts["currency"]),
        ("textbox", "Issue date", facts["issue_date"]), ("textbox", "Due date", facts["due_date"])]
    calls: list[tuple[str, dict]] = []
    for role, label, value in plan:
        element = page.find(role, label)
        if element is None:
            return Decision(f"The form has no '{label}' field where I expected it; re-reading the page.", [("browser_read_page", {})])
        if role == "combobox":
            if not same(element.selected or "", value):
                calls.append(("browser_select", {"ref": element.ref, "option": value}))
        elif not (same_amount(element.value, value) if label == "Amount" and element.value else same(element.value, value)):
            calls.append(("browser_type", {"ref": element.ref, "text": value}))
    if calls:
        return Decision("Filling the new-bill form with the values remembered from the invoice.", calls)
    attempts = int(facts.get("save_attempts", "0") or 0)
    save = page.find("button", "Save bill")
    if attempts >= MAX_SAVE_ATTEMPTS or save is None:
        return _finish("failed", "Saving the bill did not work after several attempts; I stopped instead of guessing.")
    why = "Every field matches the invoice; saving the bill." if not server_error else "The form is intact; retrying the save once."
    return Decision(why, [_remember("save_attempts", str(attempts + 1), "ERP new-bill form"), ("browser_click", {"ref": save.ref})])


def _bill_fields(page: Page) -> dict[str, str]:
    """The labelled values of an ERP bill page (an empty string for a field the page does not show)."""
    return {"vendor": page.labelled("Vendor") or "", "invoice_number": page.labelled("Invoice number") or "",
            "amount": plain_amount(page.labelled("Amount") or ""), "currency": (page.labelled("Amount") or "").partition(" ")[2],
            "issue_date": page.labelled("Issue date") or "", "due_date": page.labelled("Due date") or ""}


def _bill_problems(fields: dict[str, str], facts: dict[str, str]) -> list[str]:
    """What differs between a bill's values and the invoice facts remembered from the portal (empty = they agree)."""
    problems = []
    if not same(fields["invoice_number"], facts.get("invoice_number", "")):
        problems.append(f"invoice number {fields['invoice_number']}")
    if not fields["vendor"].lower().startswith(facts.get("vendor", "?").lower()):
        problems.append(f"vendor {fields['vendor']}")
    if not same_amount(fields["amount"], facts.get("amount", "")) or not same(fields["currency"], facts.get("currency", "")):
        problems.append(f"amount {fields['amount']} {fields['currency']}")
    for key, label in (("issue_date", "issue date"), ("due_date", "due date")):
        if not same(fields[key], facts.get(key, "")):
            problems.append(f"{label} {fields[key]}")
    return problems


def _at_erp_bill(s: Situation, page: Page) -> Decision:
    facts = s.facts
    if page.status and page.status >= 400:
        return _finish("failed", "The ERP bill page could not be opened (not found).")
    bill_id = page.path.rsplit("/", 1)[1]
    fields = _bill_fields(page)
    if not all(fields.values()):
        return Decision("The bill page did not show every field; re-reading it.", [("browser_read_page", {})])
    source = f"ERP bill page {page.path}"
    if facts.get("bill_id") != bill_id:  # first look at this bill
        created = re.search(rf"Bill #{bill_id} created", page.text) is not None
        calls = [_remember("bill_id", bill_id, source), _remember("bill_status", "created" if created else "exists", source)]
        if created and ERP_NOTE_KEY not in facts:
            calls.append(_remember(ERP_NOTE_KEY, ERP_NOTE, "learned while entering a bill in the ERP", scope="erp", durable=True))
        return Decision(f"Bill #{bill_id} {'was created' if created else 'is shown'}; re-opening it to confirm what was stored.",
                        [*calls, ("browser_navigate", {"url": f"/erp/bills/{bill_id}"})])
    problems = _bill_problems(fields, facts)
    if problems:
        return _finish("failed", f"Bill #{bill_id} does not match the invoice: {', '.join(problems)}. I did not change it.")
    created = facts.get("bill_status") == "created"
    number, amount, currency = facts["invoice_number"], facts["amount"], facts["currency"]
    summary = (f"Entered invoice {number} from {facts['vendor']} ({amount} {currency}, issued {facts['issue_date']}, due "
               f"{facts['due_date']}) as ERP Bill #{bill_id}, and re-opened the bill: every value matches the portal invoice."
               if created else
               f"Invoice {number} from {facts['vendor']} ({amount} {currency}, due {facts['due_date']}) was already recorded as "
               f"ERP Bill #{bill_id}; its values match the portal invoice, so I did not create a duplicate.")
    outputs = {"bill_id": bill_id, "invoice_number": number, "vendor": facts["vendor"], "amount": amount, "currency": currency,
               "issue_date": facts["issue_date"], "due_date": facts["due_date"], "bill_status": facts.get("bill_status", "")}
    evidence = [(f"Bill #{bill_id} shows invoice {number}, {amount} {currency}, due {facts['due_date']}", page.url),
                (f"The portal invoice {number} shows the same amount and dates", f"/portal/invoices/{number}")]
    return _finish("success", summary, outputs, evidence)


# --------------------------------------------------------------------------- the auditor


def is_verifier(messages: list[Message]) -> bool:
    return bool(messages) and messages[0].role == "system" and "independent auditor" in messages[0].content[:200]


def _json_after(text: str, marker: str) -> Any:
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    try:
        return json.JSONDecoder().raw_decode(text[start:].lstrip())[0]
    except ValueError:
        return None


def _http_json(content: str) -> Any:
    body = content.split("\n", 1)[1] if "\n" in content else ""
    try:
        return json.loads(body)
    except ValueError:
        return None


def _login_actions(page: Page, app: str) -> list[tuple[str, dict]] | None:
    """Sign in to ``app`` with the vault credential of the same name (never seeing the values); None when the page has no
    recognisable username / password / Sign in controls."""
    user = page.find("textbox", "Username") or next((e for e in page.elements if e.role == "textbox" and re.search(r"user|e-?mail|login", e.name, re.I)), None)
    password, button = page.find("textbox", "Password"), page.find("button", "Sign in")
    if not (user and password and button and app):
        return None
    return [("browser_fill_secret", {"ref": user.ref, "credential": app, "field": "username"}),
            ("browser_fill_secret", {"ref": password.ref, "credential": app, "field": "password"}),
            ("browser_click", {"ref": button.ref})]


def decide_verifier(messages: list[Message], tool_names: set[str] | None = None) -> Decision:
    """Re-open the bill, the portal invoice and the ERP's list for that invoice, then report what was seen.

    When a system shows its login page (a fresh browser after a crash/resume, or an expired session) it signs in with
    ``list_credentials`` + ``browser_fill_secret`` (the read-only auditor may authenticate, nothing else) and opens the page
    again. It tries each system once; if signing in is impossible or fails it reports that it could not see the page."""
    names = tool_names or set()
    can_login = not names or "browser_fill_secret" in names
    briefing = next((m.content for m in messages if m.role == "user"), "")
    claim = _json_after(briefing, "WORKER'S CLAIM (unverified):\n") or {}
    outputs = claim.get("outputs") if isinstance(claim.get("outputs"), dict) else {}
    facts = parse_facts(briefing)
    pick = lambda key: str(outputs.get(key) or facts.get(key) or "")  # noqa: E731
    bill_id, invoice = pick("bill_id"), pick("invoice_number")
    criteria = re.findall(r"^- (.+)$", briefing.split("SUCCESS CRITERIA:\n", 1)[-1].split("\n\nWORKER'S CLAIM", 1)[0], re.M)

    results: list[CallResult] = []
    for i, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            by_id = {m.tool_call_id: m for m in messages[i + 1:] if m.role == "tool"}
            for call in message.tool_calls:
                reply = by_id.get(call.id)
                if reply is not None:
                    results.append(CallResult(call.name, call.args, not reply.content.startswith("ERROR"), reply.content))
    if not bill_id or not invoice:
        return _verdict(criteria, [("The claim names the bill and the invoice", False,
                                    "The worker's claim does not say which bill or invoice to check, so nothing could be verified.")],
                        "Could not verify: the claim does not identify the bill.")

    targets = [f"/erp/bills/{bill_id}", f"/portal/invoices/{invoice}"]
    seen: dict[str, Page] = {}  # target path -> the last page that really showed it
    dead: set[str] = set()  # targets given up on (could not sign in, or signing in did not help)
    tries: dict[str, int] = {}
    current: str | None = None
    page_now: Page | None = None
    origin = ""
    for r in results:
        page = parse_page(r.content) if r.ok else None
        if r.name == "browser_navigate":
            path = urlparse(str(r.args.get("url", ""))).path
            current = path if path in targets else None
        elif r.name == "browser_fill_secret":
            if r.args.get("field") == "password" and current:
                tries[current] = tries.get(current, 0) + 1
            if not r.ok and current:
                dead.add(current)
        if page is None:
            continue
        page_now = page
        parts = urlparse(page.url)
        origin = origin or f"{parts.scheme}://{parts.netloc}"
        if page.path.endswith("/login"):
            app = "portal" if "/portal/" in page.path else "erp" if "/erp/" in page.path else ""
            if r.name != "browser_fill_secret" and current and (tries.get(current, 0) >= 1 or not can_login or _login_actions(page, app) is None):
                dead.add(current)
        elif page.path in targets:
            seen[page.path] = page

    todo = [t for t in targets if t not in seen and t not in dead]
    if todo:
        target = todo[0]
        if page_now is not None and page_now.path.endswith("/login") and current == target:
            app = "portal" if "/portal/" in page_now.path else "erp"
            if not any(r.name == "list_credentials" for r in results) and (not names or "list_credentials" in names):
                return Decision(f"The {app} shows its login page. Checking which credentials I may use to sign in "
                                "(I can sign in but not change anything).", [("list_credentials", {})])
            return Decision(f"Signing in to the {app} with the stored credential to look at the record (I never see the password).",
                            _login_actions(page_now, app) or [])
        why = "Re-opening the bill in the ERP." if target == targets[0] else "Opening the invoice in the vendor portal to compare."
        return Decision(why if not tries.get(target) else "Signed in; opening the page again.", [("browser_navigate", {"url": target})])
    if not any(r.name == "http_request" for r in results) and origin:
        return Decision("Listing the ERP bills for this invoice through its REST API to look for duplicates.", [("http_request", {
            "method": "GET", "url": f"{origin}/erp/api/bills", "query": {"invoice_number": invoice}, "auth_credential": "erp_api"})])
    http = next((r for r in results if r.name == "http_request"), None)
    api = _http_json(http.content) if http is not None and http.ok else None
    return _judge(criteria, bill_id, invoice, seen.get(targets[0]), seen.get(targets[1]), api if isinstance(api, list) else None)


def _page_fields(page: Page | None, kind: str) -> dict[str, str] | None:
    """The labelled values of a bill (``kind='bill'``) or portal invoice page; None for anything else (e.g. a login page)."""
    if page is None or page.path.endswith("/login"):
        return None
    if kind == "bill" and not re.fullmatch(r"/erp/bills/\d+", page.path):
        return None
    if kind == "invoice" and not page.path.startswith("/portal/invoices/"):
        return None
    amount = page.labelled("Amount") or ""
    fields = {"vendor": page.labelled("Vendor") or "", "invoice_number": page.labelled("Invoice number") or page.path.rsplit("/", 1)[-1],
              "amount": plain_amount(amount), "currency": amount.partition(" ")[2].strip(),
              "issue_date": page.labelled("Issue date") or "", "due_date": page.labelled("Due date") or ""}
    return fields if fields["amount"] and fields["due_date"] else None


def _judge(criteria: list[str], bill_id: str, invoice: str, bill_page: Page | None, source_page: Page | None,
           api: list | None) -> Decision:
    bill = _page_fields(bill_page, "bill")
    where = "the bill page"
    if bill is None and api is not None:  # the ERP session ended: the REST list still shows the bill
        row = next((b for b in api if str(b.get("id")) == bill_id), None)
        if row is not None:
            bill = {"vendor": str(row["vendor"]), "invoice_number": str(row["invoice_number"]), "amount": f"{float(row['amount']):.2f}",
                    "currency": str(row["currency"]), "issue_date": str(row["issue_date"]), "due_date": str(row["due_date"])}
            where = "the ERP REST API"
    source = _page_fields(source_page, "invoice")
    checks: list[tuple[str, bool, str]] = []
    duplicates = [b for b in api if same(str(b.get("invoice_number", "")), invoice)] if api is not None else None
    exists = bill is not None and same(bill["invoice_number"], invoice)
    unique = duplicates is not None and len(duplicates) == 1
    checks.append((criteria[0] if criteria else "A single bill exists", exists and unique,
                   (f"Bill #{bill_id} for {invoice} is there ({where}); the ERP lists {len(duplicates)} bill(s) for that invoice number"
                    if bill is not None and duplicates is not None else
                    f"Could not see bill #{bill_id}" if bill is None else f"Saw bill #{bill_id} but could not list the ERP bills for duplicates")))
    if bill is None or source is None:
        why = "the bill" if bill is None else "the invoice in the vendor portal"
        for criterion in (criteria[1:3] or ["Values match the invoice"]):
            checks.append((criterion, False, f"Could not open {why}, so the values could not be compared."))
    else:
        values_ok = (same(bill["vendor"].split(" (")[0], source["vendor"]) and same(bill["invoice_number"], source["invoice_number"])
                     and same_amount(bill["amount"], source["amount"]) and same(bill["currency"], source["currency"]))
        dates_ok = same(bill["issue_date"], source["issue_date"]) and same(bill["due_date"], source["due_date"])
        checks.append((criteria[1] if len(criteria) > 1 else "Amount and currency match",
                       values_ok, f"Bill: {bill['vendor']}, {bill['invoice_number']}, {bill['amount']} {bill['currency']}. "
                                  f"Portal: {source['vendor']}, {source['invoice_number']}, {source['amount']} {source['currency']}."))
        checks.append((criteria[2] if len(criteria) > 2 else "Dates match",
                       dates_ok, f"Bill: issued {bill['issue_date']}, due {bill['due_date']}. "
                                 f"Portal: issued {source['issue_date']}, due {source['due_date']}."))
    for extra in criteria[3:]:
        checks.append((extra, all(ok for _, ok, _ in checks), "Judged from the checks above."))
    failed = [c for c, ok, _ in checks if not ok]
    summary = (f"Bill #{bill_id} for invoice {invoice} matches the vendor portal and is the only one." if not failed
               else f"{len(failed)} check(s) failed for bill #{bill_id}: " + "; ".join(note for _, ok, note in checks if not ok))
    vendor = (bill or source or {}).get("vendor", "").split(" (")[0]
    looked_at = ([where] + (["the vendor portal invoice page"] if source is not None else [])
                 + (["the ERP bill list for that invoice number"] if api is not None else []))
    return _verdict(criteria, checks, summary, vendor, looked_at if bill is not None or source is not None else [])


def _verdict(criteria: list[str], checks: list[tuple[str, bool, str]], summary: str, vendor: str = "",
             looked_at: list[str] | None = None) -> Decision:
    """``vendor``: the one entity this scripted auditor looked at; ``looked_at``: the sources it opened."""
    entities = [{"name": vendor, "checked": True, "candidate": ""}] if vendor else []
    seen = (f"It opened {', '.join(looked_at)}." if looked_at else "It could not open any record.")
    return Decision(summary, [("submit_verdict", {
        "passed": all(ok for _, ok, _ in checks),
        "checks": [{"criterion": c, "passed": ok, "observed": note} for c, ok, note in checks],
        # The scripted flow only re-opens the records the claim names: it lists the one vendor it saw and does not search
        # for other vendors, so it reports a single matching entity (which the runtime passes) and says what it did not do.
        "ambiguity": {"matching_entities": entities,
                      "observed": (f"Only the vendor on the records was looked at ({vendor}); no search for other entities "
                                   "with a similar name was made." if vendor else
                                   "No vendor could be read, and no search for other entities with a similar name was made.")},
        "completeness": {"passed": True, "observed": f"{seen} It did not search other sources (for example a mailbox) for a "
                                                     "newer or competing item."},
        "summary": summary})])


# --------------------------------------------------------------------------- the client


class PolicyLLM(LLMClient):
    """Deterministic ``LLMClient`` for load tests and demos (see the module docstring).

    ``think_ms``: simulated model latency per call (0 = none), jittered by +-40%. ``pdf_check``: cross-check the invoice PDF
    (needs ``browser_download`` and ``read_file``). ``seed`` makes the jitter reproducible."""

    name = "policy"

    def __init__(self, think_ms: float = DEFAULT_THINK_MS, *, pdf_check: bool = True, seed: int | None = None):
        self.think_ms = max(0.0, float(think_ms))
        self.pdf_check = pdf_check
        self._rng = random.Random(seed)
        self.calls = 0

    async def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None, *, temperature: float = 0.2,
                       tool_choice: Literal["auto", "any", "none"] = "auto") -> LLMResponse:
        if self.think_ms:
            await asyncio.sleep(self.think_ms / 1000.0 * self._rng.uniform(1 - THINK_JITTER, 1 + THINK_JITTER))
        names = {t.name for t in tools or []}
        decision = decide_verifier(messages, names) if is_verifier(messages) else decide_worker(messages, names, pdf_check=self.pdf_check)
        calls = [ToolCall(id=f"pol_{uuid.uuid4().hex[:10]}", name=name, args=args) for name, args in decision.calls
                 if not names or name in names]
        self.calls += 1
        sent = sum(len(m.content) + sum(len(json.dumps(c.args)) for c in m.tool_calls) for m in messages)
        sent += sum(len(t.description) + len(json.dumps(t.parameters)) for t in tools or [])
        returned = len(decision.text) + sum(len(json.dumps(c.args)) + len(c.name) for c in calls)
        return LLMResponse(text=decision.text, tool_calls=calls,
                           usage=Usage(input_tokens=sent // CHARS_PER_TOKEN, output_tokens=max(1, returned // CHARS_PER_TOKEN)))
