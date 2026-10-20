"""Value provenance: every data-like value a write is about to submit must have a source in what the run observed.

Task-agnostic. The runner asks the tool for the values it would submit (the form fields of the approval card, the body of
an HTTP request), builds a ``SourceIndex`` from everything the run has read (task text, working-memory facts, tool
observations, the user's answers) and gets back the values that appear nowhere in it. Values are compared after
normalization, so pure reformatting is not a difference: dates in common formats (ISO, "12 November 2026", "12 Nov 2026",
"12/11/2026" with both day-first and month-first read as candidates, "2026-11-12T10:00"), numbers ("6,414.10 EUR" equals
"6414.1"), identifiers (case-insensitive). Free text (a message body, a note) is searched for its dates, amounts and
identifiers, which are checked one by one. A value without a digit is never data-like and is not checked.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal, InvalidOperation

from worker.agent.policy import INTERNAL_TOOLS
from worker.agent.state import RunState

# Steps whose observation is not something the run read: our own notes (they quote the rejected values), skipped calls,
# rejected finishes. A denial stays: it carries the human's note.
EXCLUDED_ERRORS = frozenset({"unsourced_values", "skipped", "finish_not_alone", "unknown_tool", "verification_failed"})
# Tools whose argument the page echoes back (a typed value shows up as ``value="..."`` in the next snapshot): the echo of
# the agent's own input is not a source.
ECHO_ARGS = {"browser_type": "text"}
MAX_SHOWN = 8
SHOWN_VALUE_LIMIT = 80

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_DMY_TEXT = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of\s+|\s+|[-./])({_MONTH})\b\.?,?[\s-]*(\d{{4}})(?!\d)", re.I)
_MDY_TEXT = re.compile(rf"\b({_MONTH})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.I)
_NUMERIC_DATE = re.compile(r"(?<![\d.])(\d{1,2})([-/.])(\d{1,2})\2(\d{4}|\d{2})(?!\d)")
_TIME = re.compile(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[AaPp][Mm])?(?:Z|[+-]\d{2}:?\d{2})?")

# A number as written: "6414", "6,414.10", "6.414,10", "6 414,10" (space-grouped), "6'414.10".
_NUMBER = re.compile(r"(?<!\d)\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?(?!\d)|\d+(?:[,.'  ]\d+)*")
_CURRENCY_SYMBOLS = re.compile(r"[€$£¥₹₩₽]")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}\b\s*|\s*\b[A-Z]{3}$")
_WHOLE_NUMBER = re.compile(r"\d+(?:[,.'  ]\d+)*|\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?")
# An identifier inside free text: starts with a letter, holds at least two digits ("CX-2026-0401", "INV/17/203", "PO4471").
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-/_.#][A-Za-z0-9]+)*(?![A-Za-z0-9])")
_VALUE_ATTR = re.compile(r'value="([^"]*)"')
_REDACTED = re.compile(r"\[REDACTED:[^\]]*\]")


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """``text`` with each span replaced by spaces (same length, so other spans stay valid)."""
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


# --------------------------------------------------------------------------- dates


def _valid(year: int, month: int, day: int) -> tuple[int, int, int] | None:
    if not 1900 <= year <= 2199:
        return None
    try:
        date(year, month, day)
    except ValueError:
        return None
    return year, month, day


def _candidates(*dates: tuple[int, int, int] | None) -> frozenset[tuple[int, int, int]]:
    return frozenset(d for d in dates if d)


def _iso(m: re.Match) -> frozenset:
    return _candidates(_valid(int(m[1]), int(m[2]), int(m[3])))


def _day_month_text(m: re.Match) -> frozenset:
    return _candidates(_valid(int(m[3]), _MONTHS[m[2].lower()], int(m[1])))


def _month_day_text(m: re.Match) -> frozenset:
    return _candidates(_valid(int(m[3]), _MONTHS[m[1].lower()], int(m[2])))


def _numeric(m: re.Match) -> frozenset:
    a, sep, b, y = int(m[1]), m[2], int(m[3]), m[4]
    if len(y) == 2:
        if sep != "/":  # "1.2.34" is more likely a version number than a date
            return frozenset()
        year = 2000 + int(y)
    else:
        year = int(y)
    return _candidates(_valid(year, b, a), _valid(year, a, b))  # day first and month first are both candidates


_DATE_PATTERNS = ((_ISO_DATE, _iso), (_DMY_TEXT, _day_month_text), (_MDY_TEXT, _month_day_text), (_NUMERIC_DATE, _numeric))


def find_dates(text: str) -> list[tuple[tuple[int, int], frozenset[tuple[int, int, int]]]]:
    """Every date written in ``text`` as ``((start, end), candidates)``; candidates are (year, month, day) tuples (two
    for a numeric date that reads both day first and month first)."""
    found: list[tuple[tuple[int, int], frozenset]] = []
    work = text
    for pattern, build in _DATE_PATTERNS:
        spans = []
        for m in pattern.finditer(work):
            candidates = build(m)
            if candidates:
                found.append((m.span(), candidates))
                spans.append(m.span())
        work = _mask(work, spans)
    found.sort()
    return found


# --------------------------------------------------------------------------- numbers


def _decimal(integer: str, fraction: str | None = None) -> str | None:
    try:
        number = Decimal(f"{integer}.{fraction}" if fraction else integer)
    except InvalidOperation:
        return None
    return format(number.normalize(), "f")


def number_candidates(token: str) -> set[str]:
    """Normalized values a written number may stand for ("6,414.10" -> {"6414.1"}; "1,234" -> {"1234", "1.234"})."""
    t = token.replace(" ", " ").replace(" ", " ").strip()
    out: set[str] = set()
    if " " in t:  # space-grouped ("6 414,10"); the pieces count too ("Item 3 100 pcs")
        grouped = re.fullmatch(r"(\d{1,3}(?: \d{3})+)(?:[.,](\d+))?", t)
        if grouped:
            out.add(_decimal(grouped[1].replace(" ", ""), grouped[2]) or "")
        for part in t.split():
            out |= number_candidates(part)
        out.discard("")
        return out
    separators = re.findall(r"[,.']", t)
    groups = re.split(r"[,.']", t)
    if not separators:
        value = _decimal(t)
        return {value} if value else out
    if all(len(g) == 3 for g in groups[1:]):  # grouping only: 1,234 / 1.234.567
        value = _decimal("".join(groups))
        if value:
            out.add(value)
    last = separators[-1]
    earlier = set(separators[:-1])
    if last != "'" and separators.count(last) == 1 and earlier <= {"," if last == "." else "."}:  # last one is the decimal point
        if all(len(g) == 3 for g in groups[1:-1]):
            value = _decimal("".join(groups[:-1]), groups[-1])
            if value:
                out.add(value)
    return out


def parse_number(text: str) -> set[str] | None:
    """The normalized candidates when the whole of ``text`` is a number (currency symbol or code, sign and % allowed)."""
    s = _CURRENCY_SYMBOLS.sub("", text).strip()
    s = _CURRENCY_CODE.sub("", s).strip().rstrip("%").strip().lstrip("+-").strip()
    if not _WHOLE_NUMBER.fullmatch(s):
        return None
    return number_candidates(s) or None


# --------------------------------------------------------------------------- the index of what the run has read


class SourceIndex:
    """Dates, numbers and text of everything the run has read, ready to look values up."""

    def __init__(self, texts: Iterable[str]):
        blob = "\n".join(texts)
        dates = find_dates(blob)
        self.dates: set[tuple[int, int, int]] = set().union(*(c for _, c in dates)) if dates else set()
        self.numbers = self._numbers(_mask(blob, [span for span, _ in dates]))
        self.loose_numbers = self._numbers(blob)  # date parts too: a year or an id number typed on its own
        self.text = _collapse(blob).lower()

    @staticmethod
    def _numbers(text: str) -> set[str]:
        out: set[str] = set()
        for m in _NUMBER.finditer(text):
            out |= number_candidates(m.group())
        return out

    def has_date(self, candidates: Iterable[tuple[int, int, int]]) -> bool:
        return any(c in self.dates for c in candidates)

    def has_number(self, candidates: Iterable[str]) -> bool:
        return any(c in self.numbers or (c.isdigit() and len(c) >= 4 and c in self.loose_numbers) for c in candidates)

    def has_literal(self, token: str) -> bool:
        """The identifier or text occurs (case-insensitive, on word boundaries; separators may differ)."""
        chunks = re.findall(r"[0-9a-z]+", token.lower())
        if not chunks:
            return False
        return re.search(r"(?<![0-9a-z])" + r"[^0-9a-z]*".join(map(re.escape, chunks)) + r"(?![0-9a-z])", self.text) is not None


def _strip_echo(text: str, typed: list[str]) -> str:
    """``text`` without the ``value="..."`` fields that merely show something the agent itself typed."""
    if not typed or "value=" not in text:
        return text

    def blank(m: re.Match) -> str:
        content = m.group(1)
        clipped = content.endswith("…")  # the snapshot clips long values to 80 characters
        for t in typed:
            if content == t or (clipped and t.startswith(content[:-1])):
                return 'value=""'
        return m.group(0)

    return _VALUE_ATTR.sub(blank, text)


def source_texts(state: RunState) -> Iterator[str]:
    """What the run has read: the task, working-memory facts, the observations of the tools that ran (pages, files,
    HTTP responses, the user's answers) and the notes of human approvals. Not the agent's own words (thoughts, plan,
    questions) and not the echo of what it typed."""
    yield state.task
    for fact in state.facts.values():
        yield fact.value
    typed: list[str] = []
    for step in state.steps:
        if step.kind == "approval":
            yield step.thought  # the approver's note
            continue
        argument = ECHO_ARGS.get(step.tool)
        if argument and step.args.get(argument) is not None:
            typed.append(_collapse(str(step.args[argument]).replace('"', "'")))
        if step.kind not in ("tool", "ask_user") or step.error in EXCLUDED_ERRORS:
            continue
        if step.tool in INTERNAL_TOOLS and step.tool != "ask_user":
            continue
        yield _strip_echo(step.observation, typed)


# --------------------------------------------------------------------------- checking values


def _unsourced_tokens(value: str, index: SourceIndex) -> list[str]:
    """The data-like parts of ``value`` that have no source in ``index`` (empty: everything is sourced or not data)."""
    text = _collapse(_REDACTED.sub(" ", value))
    if not re.search(r"\d", text):
        return []
    dates = find_dates(text)
    bad = [text[start:end] for (start, end), candidates in dates if not index.has_date(candidates)]
    rest = _mask(text, [span for span, _ in dates])
    if dates:
        rest = _TIME.sub(" ", rest)  # the time of a datetime value
    rest = _collapse(rest)
    if not re.search(r"\d", rest):
        return bad
    whole = parse_number(rest)
    if whole is not None:
        return bad if index.has_number(whole) else [*bad, rest]
    if " " not in rest and len(rest) <= 60:  # a single identifier
        return bad if index.has_literal(rest) else [*bad, rest]
    return [*bad, *_free_text_tokens(rest, index)]


def _free_text_tokens(text: str, index: SourceIndex) -> list[str]:
    """Identifiers and amounts inside free text that no source has. Small bare numbers ("step 2") are not data."""
    bad: list[str] = []
    spans = []
    for m in _IDENTIFIER.finditer(text):
        if sum(c.isdigit() for c in m.group()) >= 2:
            spans.append(m.span())
            if not index.has_literal(m.group()):
                bad.append(m.group())
    rest = _mask(text, spans)
    for m in _NUMBER.finditer(rest):
        token = m.group()
        digits = re.sub(r"\D", "", token).lstrip("0")
        currency = re.search(r"[€$£¥₹]\s*$|\b[A-Z]{3}\s*$", rest[: m.start()]) or re.match(r"\s*(?:[€$£¥₹]|[A-Z]{3}\b)", rest[m.end():])
        if len(digits) < 3 and not re.search(r"[,.']", token) and not currency:
            continue
        if not index.has_number(number_candidates(token)):
            bad.append(token)
    return bad


def find_unsourced(rows: Iterable[dict], index: SourceIndex) -> list[dict[str, str]]:
    """The ``{"label", "value"}`` rows (the values a write would submit) holding data that has no source in ``index``.
    ``value`` is the whole value, or for free text the unsourced parts only."""
    out: list[dict[str, str]] = []
    for row in rows:
        label, value = str(row.get("label") or ""), str(row.get("value") or "")
        bad = _unsourced_tokens(value, index)
        if not bad:
            continue
        whole = _collapse(value)
        shown = whole if len(bad) == 1 and (" " not in whole or bad[0] == whole) else ", ".join(dict.fromkeys(bad))
        item = {"label": label, "value": shown}
        if item not in out:
            out.append(item)
    return out


def blocked_key(item: dict) -> tuple[str, str]:
    """How the runner remembers that a value was already rejected once."""
    return _collapse(item.get("label", "")).lower(), _collapse(item.get("value", "")).lower()


def describe(items: list[dict]) -> str:
    """"Issue date '2026-09-28', Due date '2026-10-28'" (at most ``MAX_SHOWN`` entries)."""
    def one(item: dict) -> str:
        value = item["value"] if len(item["value"]) <= SHOWN_VALUE_LIMIT else item["value"][: SHOWN_VALUE_LIMIT - 3] + "..."
        return f"{item['label']} '{value}'" if item.get("label") else f"'{value}'"

    text = ", ".join(one(i) for i in items[:MAX_SHOWN])
    return text + (f" (+{len(items) - MAX_SHOWN} more)" if len(items) > MAX_SHOWN else "")
