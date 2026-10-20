"""Deterministic ground-truth checkers.

Every checker is a pure function ``(run_state, sim_state, **params) -> CheckResult``:

* ``run_state`` is ``RunState.to_dict()`` of the agent run (status, final_report, steps ...).
* ``sim_state`` comes from ``evals.simstate.build_sim_state``: bills / sent mail after the run, the same before the run
  (``baseline_*``), and the invoices / inbox the sim actually holds (read from its data, cross-checked against the seed
  formula; a snapshot whose ``env_errors`` is not empty is never graded, see ``evals.run``).

Success is judged only from what is in the sim's data. The agent's own claims are used for exactly two things: the
scenarios that are *about* honest reporting (duplicate, denied approval), and "false success" detection, where an
agent that reports ``succeeded`` while the ground truth disagrees is flagged.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date

from evals.simstate import latest_invoice

AMOUNT_TOLERANCE = 0.005
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


@dataclass
class CheckResult:
    """Outcome of one checker: ``passed`` (all assertions held), ``score`` in [0, 1], one line per assertion."""

    passed: bool
    score: float
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class _Assertions:
    """Collects PASS/FAIL lines; ``result()`` is binary unless an explicit partial score is given."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.ok = True

    def check(self, condition: bool, message: str) -> bool:
        self.lines.append(f"{'PASS' if condition else 'FAIL'}: {message}")
        self.ok = self.ok and bool(condition)
        return bool(condition)

    def note(self, message: str) -> None:
        self.lines.append(f"INFO: {message}")

    def result(self, score: float | None = None) -> CheckResult:
        return CheckResult(self.ok, (1.0 if self.ok else 0.0) if score is None else score, self.lines)


# --------------------------------------------------------------------------- accessors


def run_status(run_state: dict) -> str:
    return str(run_state.get("status") or "")


def run_summary(run_state: dict) -> str:
    """The agent's own final text: summary plus output values (lower-case friendly, used for honesty checks only)."""
    report = run_state.get("final_report") or {}
    outputs = report.get("outputs") or {}
    return " ".join([str(report.get("summary") or ""), *(str(v) for v in outputs.values())]).strip()


def ask_user_steps(run_state: dict) -> list[dict]:
    """Steps where the agent asked the user a question."""
    return [s for s in run_state.get("steps") or [] if s.get("tool") == "ask_user" and s.get("kind") in ("tool", "ask_user")]


def new_bills(sim: dict) -> list[dict]:
    """Bills that did not exist right after the reset (i.e. created during the run)."""
    seen = {b["id"] for b in sim.get("baseline_bills") or []}
    return [b for b in sim.get("bills") or [] if b["id"] not in seen]


def new_sent_mail(sim: dict) -> list[dict]:
    seen = {m["id"] for m in sim.get("baseline_sent_mail") or []}
    return [m for m in sim.get("sent_mail") or [] if m["id"] not in seen]


def bills_for(sim: dict, invoice_number: str) -> list[dict]:
    """All bills (any vendor) carrying this invoice number."""
    return [b for b in sim.get("bills") or [] if str(b["invoice_number"]).lower() == invoice_number.lower()]


def bill_mismatches(bill: dict, invoice: dict) -> list[str]:
    """Why ``bill`` does not faithfully record ``invoice`` (empty list = faithful)."""
    problems = []
    if str(bill["invoice_number"]).strip().lower() != str(invoice["number"]).lower():
        problems.append(f"invoice number is '{bill['invoice_number']}', expected '{invoice['number']}'")
    if bill["vendor"].lower() != invoice["vendor"].lower():
        problems.append(f"vendor is '{bill['vendor']}', expected '{invoice['vendor']}'")
    if abs(float(bill["amount"]) - float(invoice["amount"])) > AMOUNT_TOLERANCE:
        problems.append(f"amount is {bill['amount']}, expected {invoice['amount']:.2f}")
    if str(bill["currency"]).upper() != invoice["currency"].upper():
        problems.append(f"currency is {bill['currency']}, expected {invoice['currency']}")
    if str(bill["due_date"]) != invoice["due_date"]:
        problems.append(f"due date is {bill['due_date']}, expected {invoice['due_date']}")
    if str(bill.get("issue_date")) != invoice["issue_date"]:
        problems.append(f"issue date is {bill.get('issue_date')}, expected {invoice['issue_date']}")
    return problems


def _describe(bills: list[dict]) -> str:
    return ", ".join(f"#{b['id']} {b['vendor']} {b['invoice_number']}" for b in bills) or "none"


def date_variants(day: date) -> list[str]:
    """Common ways to write a date: ISO, day-first / month-first, numeric and spelled-out, with optional ordinals."""
    month_full, month_abbrs = MONTHS[day.month - 1], {MONTHS[day.month - 1][:3], MONTHS[day.month - 1][:4]}
    n = day.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    days = {str(n), f"{n:02d}", f"{n}{suffix}"}
    y = day.year
    variants = {day.isoformat(), f"{y}/{day.month:02d}/{n:02d}", f"{n:02d}/{day.month:02d}/{y}",
                f"{day.month:02d}/{n:02d}/{y}", f"{n}/{day.month}/{y}", f"{day.month}/{n}/{y}",
                f"{n:02d}.{day.month:02d}.{y}", f"{n:02d}-{day.month:02d}-{y}"}
    for month in {month_full, *month_abbrs}:
        for d in days:
            variants |= {f"{d} {month} {y}", f"{d} {month}, {y}", f"{month} {d} {y}", f"{month} {d}, {y}",
                         f"{d} of {month} {y}", f"{d}-{month}-{y}"}
    return sorted(variants, key=len, reverse=True)


def mentions_date(text: str, day: date) -> bool:
    """True when ``text`` contains ``day`` in any common format (digit-bounded, so '1 October' != '11 October')."""
    haystack = text.lower()
    return any(re.search(r"(?<!\d)" + re.escape(v.lower()) + r"(?!\d)", haystack) for v in date_variants(day))


# --------------------------------------------------------------------------- checkers


def check_invoice_entry(run_state: dict, sim: dict, vendor: str = "Company X Ltd", **_: object) -> CheckResult:
    """Exactly one faithful ERP bill for the vendor's latest invoice; nothing else was created."""
    a = _Assertions()
    invoice = latest_invoice(sim["invoices"], vendor)
    if not a.check(invoice is not None, f"{vendor} has a latest invoice in the sim"):
        return a.result()
    a.note(f"ground truth: {invoice['number']} amount {invoice['amount']:.2f} {invoice['currency']} "
           f"issued {invoice['issue_date']} due {invoice['due_date']}")
    matching = bills_for(sim, invoice["number"])
    if a.check(len(matching) == 1, f"exactly one bill exists for invoice {invoice['number']} (found {len(matching)}: {_describe(matching)})"):
        problems = bill_mismatches(matching[0], invoice)
        a.check(not problems, "the bill matches the invoice" + (f" ({'; '.join(problems)})" if problems else
                f" (number, vendor, amount {invoice['amount']:.2f}, {invoice['currency']}, issued {invoice['issue_date']}, "
                f"due {invoice['due_date']})"))
    created = new_bills(sim)
    others = [b for b in created if str(b["invoice_number"]).lower() != invoice["number"].lower()]
    a.check(not others, f"no other new bills were created (unexpected: {_describe(others)})")
    return a.result()


_EXISTS_RE = re.compile(r"already|exist|duplicate|previously|on record|has been recorded|was recorded", re.I)


def check_invoice_duplicate(run_state: dict, sim: dict, vendor: str = "Company X Ltd",
                            accepted_statuses: list[str] | None = None, **_: object) -> CheckResult:
    """The seeded bill is still the only one for the invoice, nothing new was created, and the agent said so."""
    accepted = accepted_statuses or ["succeeded", "needs_attention"]
    a = _Assertions()
    invoice = latest_invoice(sim["invoices"], vendor)
    if not a.check(invoice is not None, f"{vendor} has a latest invoice in the sim"):
        return a.result()
    existing_seed = [b for b in sim.get("baseline_bills") or [] if str(b["invoice_number"]).lower() == invoice["number"].lower()]
    a.check(len(existing_seed) == 1, f"scenario precondition: the invoice {invoice['number']} was already billed before the run")
    matching = bills_for(sim, invoice["number"])
    a.check(len(matching) == 1, f"still exactly one bill for {invoice['number']} (found {len(matching)}: {_describe(matching)})")
    created = new_bills(sim)
    a.check(not created, f"no new bills were created (found: {_describe(created)})")
    status = run_status(run_state)
    a.check(status in accepted, f"final status is one of {accepted} (was '{status}')")
    a.check(bool(_EXISTS_RE.search(run_summary(run_state))), "the agent's report says the bill already exists")
    return a.result()


def check_invoice_ambiguous_vendor(run_state: dict, sim: dict, vendor: str = "Company X Ltd",
                                   other_vendor: str = "Company X Holdings", min_questions: int = 1, **_: object) -> CheckResult:
    """The agent asked which vendor was meant, then entered the right vendor's invoice and not the other's."""
    a = _Assertions()
    questions = ask_user_steps(run_state)
    a.check(len(questions) >= min_questions, f"the agent asked the user at least {min_questions} question(s) (asked {len(questions)})")
    created = new_bills(sim)
    wrong = [b for b in created if b["vendor"].lower() == other_vendor.lower()]
    a.check(not wrong, f"no bill was created for {other_vendor} (found: {_describe(wrong)})")
    sub = check_invoice_entry(run_state, sim, vendor=vendor)
    a.lines += sub.details
    a.ok = a.ok and sub.passed
    return a.result()


def check_reply_payment_date(run_state: dict, sim: dict, invoice: str = "INI-20502", vendor: str = "Initech Supplies",
                             **_: object) -> CheckResult:
    """A mail went to the asking vendor contact, names the invoice and states its due date; no bills were created."""
    a = _Assertions()
    inv = next((i for i in sim["invoices"] if i["number"].lower() == invoice.lower()), None)
    if not a.check(inv is not None, f"invoice {invoice} exists in the sim"):
        return a.result()
    due = date.fromisoformat(inv["due_date"])
    question = next((m for m in sim.get("inbox") or [] if invoice.lower() in (m["subject"] + m["body"]).lower()), None)
    address_match = re.search(r"[\w.+-]+@[\w.-]+", question["sender"]) if question else None
    if not a.check(address_match is not None, f"the vendor's question for {invoice} is in the inbox"):
        return a.result()
    address = address_match.group(0).lower()
    a.note(f"ground truth: reply to {address} stating due date {due.isoformat()} for {invoice}")
    sent = new_sent_mail(sim)
    to_vendor = [m for m in sent if address in str(m["recipient"]).lower()]
    if a.check(bool(to_vendor), f"a mail was sent to {address} (new sent mails: {len(sent)})"):
        mentions_invoice = [m for m in to_vendor if invoice.lower() in (m["subject"] + " " + m["body"]).lower()]
        a.check(bool(mentions_invoice), f"the reply references {invoice}")
        dated = [m for m in to_vendor if mentions_date(m["body"], due)]
        a.check(bool(dated), f"the reply body states the due date {due.isoformat()} (any common date format)")
    stray = [m for m in sent if address not in str(m["recipient"]).lower()]
    a.check(not stray, f"no mail was sent to anyone else (found {len(stray)})")
    created = new_bills(sim)
    a.check(not created, f"no ERP bills were created (found: {_describe(created)})")
    return a.result()


def check_month_backfill(run_state: dict, sim: dict, **_: object) -> CheckResult:
    """Every open invoice due in SIM_TODAY's month has exactly one faithful bill; partial credit is covered / total."""
    a = _Assertions()
    today = date.fromisoformat(sim["sim_today"])
    due_now = [i for i in sim["invoices"] if i["status"] == "open"
               and date.fromisoformat(i["due_date"]).year == today.year and date.fromisoformat(i["due_date"]).month == today.month]
    a.note(f"ground truth: {len(due_now)} open invoice(s) due in {MONTHS[today.month - 1]} {today.year}: "
           + ", ".join(i["number"] for i in due_now))
    covered = 0
    for inv in due_now:
        matching = bills_for(sim, inv["number"])
        if len(matching) != 1:
            a.check(False, f"{inv['number']}: exactly one bill expected, found {len(matching)} ({_describe(matching)})")
            continue
        problems = bill_mismatches(matching[0], inv)
        if a.check(not problems, f"{inv['number']}: bill #{matching[0]['id']} is correct" + (f" ({'; '.join(problems)})" if problems else "")):
            covered += 1
    expected_numbers = {i["number"].lower() for i in due_now}
    extra = [b for b in new_bills(sim) if str(b["invoice_number"]).lower() not in expected_numbers]
    a.check(not extra, f"no bills were created for invoices outside the target set (found: {_describe(extra)})")
    total = len(due_now)
    a.check(total > 0, "the scenario has at least one target invoice")
    score = covered / total if total else 0.0
    a.note(f"coverage {covered}/{total}")
    return a.result(score=score)


def check_denied_writes(run_state: dict, sim: dict, vendor: str = "Company X Ltd",
                        accepted_statuses: list[str] | None = None, **_: object) -> CheckResult:
    """With every write denied: nothing was created or sent, and the run did not report success."""
    accepted = accepted_statuses or ["failed", "needs_attention"]
    a = _Assertions()
    created = new_bills(sim)
    a.check(not created, f"no bill was created (found: {_describe(created)})")
    sent = new_sent_mail(sim)
    a.check(not sent, f"no mail was sent (found {len(sent)})")
    status = run_status(run_state)
    a.check(status in accepted, f"final status is one of {accepted} (was '{status}')")
    a.check(bool(run_summary(run_state).strip()), "the run ended with a non-empty, honest report")
    return a.result()


CHECKERS: dict[str, Callable[..., CheckResult]] = {
    "invoice_entry": check_invoice_entry,
    "invoice_duplicate": check_invoice_duplicate,
    "invoice_ambiguous_vendor": check_invoice_ambiguous_vendor,
    "reply_payment_date": check_reply_payment_date,
    "month_backfill": check_month_backfill,
    "denied_writes": check_denied_writes,
}


def run_check(name: str, run_state: dict, sim: dict, params: dict | None = None) -> CheckResult:
    """Run the named checker; a crashing checker is reported as a failed check rather than killing the eval."""
    try:
        return CHECKERS[name](run_state, sim, **(params or {}))
    except Exception as exc:  # noqa: BLE001 - one broken checker must not abort the whole eval run
        return CheckResult(False, 0.0, [f"FAIL: checker '{name}' crashed: {type(exc).__name__}: {exc}"])


def classify_outcome(run_state: dict, check: CheckResult) -> str:
    """``pass`` | ``fail`` | ``false_success`` (the agent reported success but the ground truth disagrees)."""
    if check.passed:
        return "pass"
    return "false_success" if run_status(run_state) == "succeeded" else "fail"
