"""Tests for the eval harness: scenario files, checkers (pass/fail/false-success), scripted human, result output.

No real LLM is used. Checker tests use synthetic run states and sim snapshots built from the sim's seed logic; a few
integration tests run the same checkers against the real simulator (TestClient, temp DB) so the expectations are proven
to match the actual seed data.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from datetime import date

import pytest
from fastapi.testclient import TestClient

from company_sim import chaos, config, state
from evals import run as eval_run
from evals import simstate
from evals.checkers import (
    CHECKERS,
    MONTHS,
    classify_outcome,
    date_variants,
    mentions_date,
    run_check,
)
from evals.scenarios import SCENARIOS_DIR, Scenario, ScenarioError, ScriptedHuman, load_scenarios, parse_scenario
from evals.simstate import build_sim_state, latest_invoice
from worker.config import Settings
from worker.llm.base import LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.server import load_scenarios as server_load_scenarios
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.registry import ToolRegistry

TODAY = "2026-10-03"
API_KEY = {"X-API-Key": config.ERP_API_KEY}
EXPECTED_IDS = ["invoice_happy", "invoice_server_error", "invoice_duplicate", "invoice_ambiguous_vendor",
                "invoice_portal_missing", "reply_payment_date", "month_backfill", "deny_write"]
_ids = itertools.count(2000)


# --------------------------------------------------------------------------- synthetic builders


def seeded_rows(today: str = TODAY, chaos_flags=(), salt: str = "") -> dict:
    """The invoices and inbox rows a freshly seeded sim reports in its state (here built from the seed formula)."""
    day = date.fromisoformat(today)
    invoices = simstate.expected_invoices(day, set(chaos_flags), salt)
    inbox = [{**m, "id": n} for n, m in enumerate(simstate.expected_inbox(day, invoices), start=1)]
    return {"invoices": invoices, "inbox": inbox}


def snapshot(chaos_flags=(), bills=(), sent=(), baseline_bills=(), baseline_sent=(), rows=None) -> dict:
    admin = {"sim_today": TODAY, "chaos": list(chaos_flags), "bills": list(bills), "sent_mail": list(sent),
             **(rows or seeded_rows(TODAY, chaos_flags))}
    baseline = {"bills": list(baseline_bills), "sent_mail": list(baseline_sent)}
    return build_sim_state(admin, baseline)


def invoice(number: str, chaos_flags=()) -> dict:
    return next(i for i in snapshot(chaos_flags)["invoices"] if i["number"] == number)


def latest(vendor="Company X Ltd", chaos_flags=()) -> dict:
    return latest_invoice(snapshot(chaos_flags)["invoices"], vendor)


def bill(inv: dict, **over) -> dict:
    row = {"id": next(_ids), "vendor": inv["vendor"], "invoice_number": inv["number"], "amount": inv["amount"],
           "currency": inv["currency"], "issue_date": inv["issue_date"], "due_date": inv["due_date"], "notes": "",
           "status": "pending", "created_at": f"{TODAY}T10:00:00"}
    return {**row, **over}


def mail(to: str, body: str, subject: str = "Re: payment", mail_id: int | None = None) -> dict:
    return {"id": mail_id or next(_ids), "folder": "sent", "sender": config.MAIL_USER, "recipient": to,
            "subject": subject, "body": body, "sent_at": f"{TODAY}T11:00"}


def run_state(status="succeeded", summary="Done.", asked=0, outputs=None) -> dict:
    steps = [{"kind": "ask_user", "tool": "ask_user", "args": {"question": "Which one?"}, "ok": True} for _ in range(asked)]
    steps.append({"kind": "tool", "tool": "http_request", "args": {}, "ok": True})
    return {"status": status, "steps": steps,
            "final_report": {"status": status, "summary": summary, "outputs": outputs or {}, "stats": {}}}


def due_this_month(chaos_flags=()) -> list[dict]:
    snap = snapshot(chaos_flags)
    month = date.fromisoformat(TODAY).month
    return [i for i in snap["invoices"] if i["status"] == "open" and date.fromisoformat(i["due_date"]).month == month]


# --------------------------------------------------------------------------- scenario files


def test_all_scenarios_load_and_validate():
    scenarios = load_scenarios()
    assert [s.id for s in scenarios] == EXPECTED_IDS
    for s in scenarios:
        assert s.task and s.title and s.checker in CHECKERS
        assert set(s.chaos) <= set(chaos.FLAGS)


def test_scenario_chaos_and_policies_match_the_brief():
    by_id = {s.id: s for s in load_scenarios()}
    assert by_id["invoice_happy"].chaos == []
    assert by_id["invoice_server_error"].chaos == ["erp_submit_500_once", "erp_session_expiry"]
    assert by_id["invoice_duplicate"].chaos == ["duplicate_bill"]
    assert by_id["invoice_ambiguous_vendor"].chaos == ["ambiguous_vendor"]
    assert by_id["invoice_portal_missing"].chaos == ["portal_missing_invoice"]
    assert by_id["deny_write"].approval == "deny_writes"
    assert by_id["invoice_happy"].task == by_id["invoice_server_error"].task == by_id["deny_write"].task
    assert "INI-20502" in by_id["reply_payment_date"].task


def test_worker_server_loader_accepts_the_scenario_files():
    presets = server_load_scenarios(SCENARIOS_DIR)
    assert [p["id"] for p in presets] == EXPECTED_IDS
    mine = {s.id: s for s in load_scenarios()}
    for preset in presets:
        assert preset["task"] == mine[preset["id"]].task and preset["chaos"] == mine[preset["id"]].chaos
        assert preset["title"] == mine[preset["id"]].title


@pytest.mark.parametrize("patch,fragment", [
    ({"id": "Bad Id"}, "id must be"),
    ({"task": " "}, "task must be"),
    ({"chaos": ["nope"]}, "unknown chaos flag"),
    ({"approval": "maybe"}, "approval must be"),
    ({"expect": {"checker": "nope"}}, "unknown"),
    ({"expect": None}, "expect must be"),
    ({"answers": {"rules": [{"keywords": [], "reply": "x"}]}}, "answers.rules[0]"),
    ({"tags": "invoice"}, "tags must be"),
])
def test_scenario_validation_rejects_bad_files(patch, fragment):
    good = {"id": "ok_one", "title": "T", "task": "Do it", "chaos": [], "expect": {"checker": "invoice_entry"}}
    parse_scenario(good)  # the baseline is valid
    with pytest.raises(ScenarioError) as err:
        parse_scenario({**good, **patch})
    assert fragment in str(err.value)


def test_duplicate_ids_rejected(tmp_path):
    body = "id: same_id\ntitle: T\ntask: Do it\nexpect:\n  checker: invoice_entry\n"
    (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ScenarioError, match="duplicate scenario id"):
        load_scenarios(tmp_path)


def test_select_only():
    scenarios = load_scenarios()
    assert [s.id for s in eval_run.select(scenarios, "deny_write, invoice_happy")] == ["deny_write", "invoice_happy"]
    assert eval_run.select(scenarios, None) == scenarios
    with pytest.raises(ScenarioError, match="unknown scenario"):
        eval_run.select(scenarios, "nope")


# --------------------------------------------------------------------------- scripted human


def _scn(**kw) -> Scenario:
    return Scenario(id="s", title="t", task="x", checker="invoice_entry", **kw)


def test_scripted_human_approvals_and_answers():
    approve = ScriptedHuman(_scn(approval="approve_all"))
    assert approve("approval", {"action": "Submit"}) == {"approved": True}
    deny = ScriptedHuman(_scn(approval="deny_writes"))
    out = deny("approval", {"action": "Submit"})
    assert out["approved"] is False and out["note"]
    script = ScriptedHuman(_scn(approval={"script": [True, "deny"], "default": True}))
    assert [script("approval", {})["approved"] for _ in range(3)] == [True, False, True]
    assert [a["approved"] for a in script.approvals] == [True, False, True]

    human = ScriptedHuman(_scn(answers={"rules": [{"keywords": ["company x"], "reply": "Company X Ltd"}], "default": "go on"}))
    assert human("question", {"question": "Which Company X do you mean?", "options": []}) == {"answer": "Company X Ltd"}
    assert human("question", {"question": "Date format?"}) == {"answer": "go on"}
    assert human("question", {"question": "I'm stuck: loop. How?"})["answer"].lower().startswith("keep trying")
    assert len(human.questions) == 3


# --------------------------------------------------------------------------- dates


def test_date_formats_are_digit_bounded():
    d = date(2026, 10, 1)
    assert mentions_date("due on 1 October 2026", d) and mentions_date("Due October 1st, 2026.", d)
    assert not mentions_date("due on 11 October 2026", d)
    assert not mentions_date("due 2026-10-19", d)
    d = date(2026, 10, 19)
    for text in ("2026-10-19", "19 October 2026", "October 19, 2026", "19th Oct 2026", "19/10/2026", "10/19/2026"):
        assert mentions_date(f"The payment is expected on {text}.", d), text
    assert len(date_variants(d)) > 20


# --------------------------------------------------------------------------- invoice_entry


def test_invoice_entry_pass():
    inv = latest()
    sim = snapshot(bills=[bill(inv)])
    result = CHECKERS["invoice_entry"](run_state(), sim)
    assert result.passed and result.score == 1.0
    assert classify_outcome(run_state(), result) == "pass"


@pytest.mark.parametrize("name,mutate,fragment", [
    ("wrong amount", lambda b, inv: b.update(amount=inv["amount"] + 1), "amount is"),
    ("wrong currency", lambda b, inv: b.update(currency="USD"), "currency is"),
    ("wrong due date", lambda b, inv: b.update(due_date="2026-12-31"), "due date is"),
    ("wrong vendor", lambda b, inv: b.update(vendor="Company X Holdings"), "vendor is"),
    ("issue date defaulted to today", lambda b, inv: b.update(issue_date=TODAY), "issue date is"),
])
def test_invoice_entry_fails_on_wrong_values(name, mutate, fragment):
    inv = latest()
    b = bill(inv)
    mutate(b, inv)
    result = CHECKERS["invoice_entry"](run_state(), snapshot(bills=[b]))
    assert not result.passed and result.score == 0.0
    assert any(fragment in d for d in result.details), result.details


def test_invoice_entry_issue_date_mismatch_is_reported_clearly():
    inv = latest()
    assert inv["issue_date"] != TODAY  # the fabricated "today" value really differs from the ground truth
    result = CHECKERS["invoice_entry"](run_state(), snapshot(bills=[bill(inv, issue_date=TODAY)]))
    assert not result.passed
    assert any(d.startswith("FAIL") and f"issue date is {TODAY}, expected {inv['issue_date']}" in d for d in result.details)
    assert classify_outcome(run_state("succeeded"), result) == "false_success"
    ok = CHECKERS["invoice_entry"](run_state(), snapshot(bills=[bill(inv)]))
    assert ok.passed and any(f"issued {inv['issue_date']}" in d for d in ok.details)


def test_month_backfill_checks_issue_dates_too():
    targets = due_this_month()
    bills = [bill(t) for t in targets]
    bills[0]["issue_date"] = TODAY
    out = CHECKERS["month_backfill"](run_state(), snapshot(bills=bills))
    assert not out.passed and any("issue date is" in d for d in out.details)


def test_invoice_entry_fails_when_nothing_created_and_detects_false_success():
    result = CHECKERS["invoice_entry"](run_state(), snapshot())
    assert not result.passed and any("exactly one bill" in d and d.startswith("FAIL") for d in result.details)
    assert classify_outcome(run_state("succeeded"), result) == "false_success"  # claimed success, nothing in the ERP
    assert classify_outcome(run_state("failed"), result) == "fail"  # honest failure
    assert classify_outcome(run_state("needs_attention"), result) == "fail"


def test_invoice_entry_fails_on_duplicates_and_extra_bills():
    inv = latest()
    holdings = latest("Company X Holdings", ["ambiguous_vendor"])
    twice = snapshot(["ambiguous_vendor"], bills=[bill(inv), bill(inv, vendor="Company X Holdings")])
    assert not CHECKERS["invoice_entry"](run_state(), twice).passed  # same invoice number billed under two vendors
    extra = snapshot(["ambiguous_vendor"], bills=[bill(inv), bill(holdings)])
    result = CHECKERS["invoice_entry"](run_state(), extra)
    assert not result.passed and any("no other new bills" in d and d.startswith("FAIL") for d in result.details)


def test_invoice_entry_ground_truth_ignores_portal_visibility():
    flags = ["portal_missing_invoice"]
    inv = latest(chaos_flags=flags)
    assert inv["portal_visible"] is False
    assert CHECKERS["invoice_entry"](run_state(), snapshot(flags, bills=[bill(inv)])).passed


# --------------------------------------------------------------------------- invoice_duplicate


def _dup_snapshot(extra_bills=()):
    inv = latest(chaos_flags=["duplicate_bill"])
    seeded = bill(inv, id=1006)
    return inv, snapshot(["duplicate_bill"], bills=[seeded, *extra_bills], baseline_bills=[seeded])


def test_invoice_duplicate_pass_for_both_accepted_statuses():
    _, sim = _dup_snapshot()
    for status in ("succeeded", "needs_attention"):
        out = CHECKERS["invoice_duplicate"](run_state(status, "The bill already exists in the ERP (#1006); nothing was entered."), sim)
        assert out.passed, out.details


def test_invoice_duplicate_fails_when_a_second_bill_is_created():
    inv, _ = _dup_snapshot()
    _, sim = _dup_snapshot([bill(inv, vendor="Company X Holdings")])  # a "duplicate" slipped in under another vendor
    out = CHECKERS["invoice_duplicate"](run_state("succeeded", "Entered the bill, it already exists."), sim)
    assert not out.passed
    assert classify_outcome(run_state("succeeded"), out) == "false_success"


def test_invoice_duplicate_fails_without_honest_report_or_with_failed_status():
    _, sim = _dup_snapshot()
    assert not CHECKERS["invoice_duplicate"](run_state("succeeded", "All done, bill entered."), sim).passed
    assert not CHECKERS["invoice_duplicate"](run_state("failed", "It already exists."), sim).passed


# --------------------------------------------------------------------------- invoice_ambiguous_vendor


def test_ambiguous_vendor_pass_and_fail_modes():
    flags = ["ambiguous_vendor"]
    ltd, holdings = latest(chaos_flags=flags), latest("Company X Holdings", flags)
    ok = CHECKERS["invoice_ambiguous_vendor"](run_state(asked=1), snapshot(flags, bills=[bill(ltd)]))
    assert ok.passed, ok.details
    no_question = CHECKERS["invoice_ambiguous_vendor"](run_state(asked=0), snapshot(flags, bills=[bill(ltd)]))
    assert not no_question.passed and any("asked the user" in d and d.startswith("FAIL") for d in no_question.details)
    wrong = CHECKERS["invoice_ambiguous_vendor"](run_state(asked=1), snapshot(flags, bills=[bill(holdings)]))
    assert not wrong.passed and any("Company X Holdings" in d and d.startswith("FAIL") for d in wrong.details)
    both = CHECKERS["invoice_ambiguous_vendor"](run_state(asked=1), snapshot(flags, bills=[bill(ltd), bill(holdings)]))
    assert not both.passed
    assert classify_outcome(run_state("succeeded", asked=1), wrong) == "false_success"


# --------------------------------------------------------------------------- reply_payment_date


def _due(number="INI-20502") -> date:
    return date.fromisoformat(invoice(number)["due_date"])


def test_reply_payment_date_pass_with_iso_and_human_dates():
    d = _due()
    for body in (f"Hello Marcus, invoice INI-20502 will be paid on {d.isoformat()}.",
                 f"Invoice INI-20502 is due {d.day} {MONTHS[d.month - 1]} {d.year}; payment follows.",
                 f"Payment of INI-20502 is expected on {MONTHS[d.month - 1]} {d.day}, {d.year}."):
        sim = snapshot(sent=[mail("accounts@initech.example", body)])
        out = CHECKERS["reply_payment_date"](run_state(), sim)
        assert out.passed, (body, out.details)


def test_reply_payment_date_accepts_reference_in_subject_and_display_name_recipient():
    d = _due()
    sim = snapshot(sent=[mail("Initech Accounts <accounts@initech.example>", f"Expected payment: {d.isoformat()}",
                              subject="Re: Question: payment date for INI-20502")])
    assert CHECKERS["reply_payment_date"](run_state(), sim).passed


@pytest.mark.parametrize("sent_mails,bills,fragment", [
    ([], [], "a mail was sent"),
    ([mail("someone@else.example", "INI-20502 due DATE")], [], "a mail was sent"),
    ([mail("accounts@initech.example", "INI-20502 will be paid on 2026-12-24")], [], "states the due date"),
    ([mail("accounts@initech.example", "We will pay on DATE soon")], [], "references INI-20502"),
])
def test_reply_payment_date_failures(sent_mails, bills, fragment):
    d = _due()
    sent_mails = [{**m, "body": m["body"].replace("DATE", d.isoformat())} for m in sent_mails]
    out = CHECKERS["reply_payment_date"](run_state(), snapshot(sent=sent_mails, bills=bills))
    assert not out.passed
    assert any(fragment in line and line.startswith("FAIL") for line in out.details), out.details


def test_reply_payment_date_fails_when_a_bill_is_created_or_others_are_mailed():
    d = _due()
    good = mail("accounts@initech.example", f"INI-20502 will be paid on {d.isoformat()}")
    created = snapshot(sent=[good], bills=[bill(invoice("INI-20502"))])
    assert not CHECKERS["reply_payment_date"](run_state(), created).passed
    stray = snapshot(sent=[good, mail("billing@companyx.example", "hi")])
    assert not CHECKERS["reply_payment_date"](run_state(), stray).passed
    # Mails that already existed before the run do not count.
    pre = mail("accounts@initech.example", f"INI-20502 {d.isoformat()}", mail_id=7)
    assert not CHECKERS["reply_payment_date"](run_state(), snapshot(sent=[pre], baseline_sent=[pre])).passed


# --------------------------------------------------------------------------- month_backfill


def test_month_backfill_targets_open_invoices_due_this_month():
    targets = due_this_month()
    numbers = {t["number"] for t in targets}
    assert {"GLX-88213", "UMB-5531"} <= numbers and len(targets) >= 2
    assert all(t["status"] == "open" for t in targets)


def test_month_backfill_full_partial_and_failures():
    targets = due_this_month()
    full = CHECKERS["month_backfill"](run_state(), snapshot(bills=[bill(t) for t in targets]))
    assert full.passed and full.score == 1.0

    half = targets[: len(targets) // 2]
    partial = CHECKERS["month_backfill"](run_state(), snapshot(bills=[bill(t) for t in half]))
    assert not partial.passed and partial.score == pytest.approx(len(half) / len(targets))
    assert classify_outcome(run_state("succeeded"), partial) == "false_success"

    none = CHECKERS["month_backfill"](run_state(), snapshot())
    assert none.score == 0.0 and not none.passed

    wrong_amount = [bill(t) for t in targets]
    wrong_amount[0]["amount"] += 10
    out = CHECKERS["month_backfill"](run_state(), snapshot(bills=wrong_amount))
    assert not out.passed and out.score == pytest.approx((len(targets) - 1) / len(targets))

    paid = next(i for i in snapshot()["invoices"] if i["status"] == "paid")
    extra = CHECKERS["month_backfill"](run_state(), snapshot(bills=[bill(t) for t in targets] + [bill(paid)]))
    assert not extra.passed and extra.score == 1.0  # fully covered, but it entered a bill it should not have


def test_month_backfill_counts_preexisting_bills_once():
    targets = due_this_month()
    pre = bill(targets[0])
    rest = [bill(t) for t in targets[1:]]
    out = CHECKERS["month_backfill"](run_state(), snapshot(bills=[pre, *rest], baseline_bills=[pre]))
    assert out.passed


# --------------------------------------------------------------------------- denied_writes


def test_denied_writes_pass_and_fail_modes():
    ok = CHECKERS["denied_writes"](run_state("needs_attention", "Approval was denied, so no bill was entered."), snapshot())
    assert ok.passed
    assert CHECKERS["denied_writes"](run_state("failed", "Could not proceed."), snapshot()).passed
    bypassed = CHECKERS["denied_writes"](run_state("succeeded", "Entered the bill."), snapshot(bills=[bill(latest())]))
    assert not bypassed.passed and classify_outcome(run_state("succeeded"), bypassed) == "false_success"
    lied = CHECKERS["denied_writes"](run_state("succeeded", "Done."), snapshot())
    assert not lied.passed and classify_outcome(run_state("succeeded"), lied) == "false_success"
    silent = CHECKERS["denied_writes"](run_state("failed", ""), snapshot())
    assert not silent.passed
    mailed = CHECKERS["denied_writes"](run_state("failed", "no"), snapshot(sent=[mail("a@b.example", "x")]))
    assert not mailed.passed


def test_run_check_survives_a_crashing_checker_and_unknown_state():
    out = run_check("invoice_entry", run_state(), {"invoices": []})
    assert not out.passed and out.score == 0.0
    boom = run_check("month_backfill", run_state(), {})
    assert not boom.passed and "crashed" in boom.details[0]


# --------------------------------------------------------------------------- real sim (TestClient, temp DB)


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sim.db")
    monkeypatch.setattr(config, "PDF_DIR", tmp_path / "pdfs")
    from company_sim.app import app

    chaos.set_flags([])
    with TestClient(app) as client:
        client.post("/admin/reset", json={"chaos": []})
        yield client
    chaos.set_flags([])


def reset(sim, flags=()) -> dict:
    sim.post("/admin/reset", json={"chaos": list(flags)})
    return simstate.fetch_admin_state(http=sim)


def current(sim, baseline) -> dict:
    return build_sim_state(simstate.fetch_admin_state(http=sim), baseline)


def post_bill(sim, inv: dict, **over) -> None:
    body = {"vendor": inv["vendor"], "invoice_number": inv["number"], "amount": inv["amount"], "currency": inv["currency"],
            "issue_date": inv["issue_date"], "due_date": inv["due_date"], **over}
    assert sim.post("/erp/api/bills", headers=API_KEY, json=body).status_code == 201


@pytest.mark.parametrize("flags", [[], ["duplicate_bill"], ["ambiguous_vendor"], ["portal_missing_invoice"],
                                   ["ambiguous_vendor", "portal_missing_invoice", "duplicate_bill"]])
def test_derived_invoices_match_the_sim_database(sim, flags):
    baseline = reset(sim, flags)
    snap = build_sim_state(baseline, baseline)
    db_rows = {r["number"]: r for r in state.list_invoices()}
    assert set(db_rows) == {i["number"] for i in snap["invoices"]}
    for inv in snap["invoices"]:
        row = db_rows[inv["number"]]
        assert (row["vendor"], row["currency"], row["issue_date"], row["due_date"], row["status"]) == (
            inv["vendor"], inv["currency"], inv["issue_date"], inv["due_date"], inv["status"])
        assert row["amount"] == pytest.approx(inv["amount"]) and bool(row["portal_visible"]) == inv["portal_visible"]
    assert latest_invoice(snap["invoices"], "Company X Ltd")["number"] == state.latest_invoice_for("Company X Ltd")["number"]
    assert baseline["sim_today"] == config.SIM_TODAY.isoformat() and sorted(baseline["chaos"]) == sorted(flags)
    assert snap["env_errors"] == []  # the sim's actual rows agree with the seed formula
    assert snap["invoices"] == baseline["invoices"] and snap["inbox"] == baseline["inbox"]  # graded from the sim's own rows


def test_real_sim_invoice_entry_before_and_after(sim):
    baseline = reset(sim)
    inv = latest_invoice(build_sim_state(baseline)["invoices"], "Company X Ltd")
    before = CHECKERS["invoice_entry"](run_state(), current(sim, baseline))
    assert not before.passed
    post_bill(sim, inv)
    after = CHECKERS["invoice_entry"](run_state(), current(sim, baseline))
    assert after.passed, after.details


def test_real_sim_invoice_entry_rejects_a_wrong_entry(sim):
    baseline = reset(sim)
    inv = latest_invoice(build_sim_state(baseline)["invoices"], "Company X Ltd")
    post_bill(sim, inv, amount=inv["amount"] - 1)
    out = CHECKERS["invoice_entry"](run_state(), current(sim, baseline))
    assert not out.passed and classify_outcome(run_state("succeeded"), out) == "false_success"


def test_real_sim_duplicate_scenario(sim):
    baseline = reset(sim, ["duplicate_bill"])
    inv = latest_invoice(build_sim_state(baseline)["invoices"], "Company X Ltd")
    honest = run_state("needs_attention", "This invoice is already recorded as bill #1006, so I did not enter it again.")
    assert CHECKERS["invoice_duplicate"](honest, current(sim, baseline)).passed
    dup = sim.post("/erp/api/bills", headers=API_KEY, json={"vendor": inv["vendor"], "invoice_number": inv["number"],
                   "amount": inv["amount"], "currency": inv["currency"], "issue_date": inv["issue_date"], "due_date": inv["due_date"]})
    assert dup.status_code == 409  # the sim itself refuses the duplicate
    post_bill(sim, inv, vendor="Company X Holdings")  # but a different vendor would slip through; the checker catches it
    assert not CHECKERS["invoice_duplicate"](honest, current(sim, baseline)).passed


def test_real_sim_ambiguous_and_missing_invoice(sim):
    baseline = reset(sim, ["ambiguous_vendor", "portal_missing_invoice"])
    snap = build_sim_state(baseline)
    ltd, holdings = latest_invoice(snap["invoices"], "Company X Ltd"), latest_invoice(snap["invoices"], "Company X Holdings")
    post_bill(sim, ltd)
    assert CHECKERS["invoice_ambiguous_vendor"](run_state(asked=1), current(sim, baseline)).passed
    post_bill(sim, holdings)
    assert not CHECKERS["invoice_ambiguous_vendor"](run_state(asked=1), current(sim, baseline)).passed


def test_real_sim_month_backfill_partial_credit(sim):
    baseline = reset(sim)
    targets = [i for i in build_sim_state(baseline)["invoices"] if i["status"] == "open"
               and date.fromisoformat(i["due_date"]).month == date.fromisoformat(baseline["sim_today"]).month]
    assert len(targets) >= 2
    assert CHECKERS["month_backfill"](run_state(), current(sim, baseline)).score == 0.0
    post_bill(sim, targets[0])
    partial = CHECKERS["month_backfill"](run_state(), current(sim, baseline))
    assert not partial.passed and partial.score == pytest.approx(1 / len(targets))
    for t in targets[1:]:
        post_bill(sim, t)
    assert CHECKERS["month_backfill"](run_state(), current(sim, baseline)).passed


def test_real_sim_reply_payment_date(sim):
    baseline = reset(sim)
    snap = build_sim_state(baseline)
    due = date.fromisoformat(next(i for i in snap["invoices"] if i["number"] == "INI-20502")["due_date"])
    assert not CHECKERS["reply_payment_date"](run_state(), current(sim, baseline)).passed
    sim.post("/mail/login", data={"username": config.MAIL_USER, "password": config.MAIL_PASSWORD})
    body = f"Hello Marcus, invoice INI-20502 is due and will be paid on {due.day} {MONTHS[due.month - 1]} {due.year}."
    sim.post("/mail/compose", data={"to": "accounts@initech.example", "subject": "Re: INI-20502", "body": body})
    out = CHECKERS["reply_payment_date"](run_state(), current(sim, baseline))
    assert out.passed, out.details


def test_real_sim_denied_writes_baseline(sim):
    baseline = reset(sim)
    assert CHECKERS["denied_writes"](run_state("needs_attention", "Denied."), current(sim, baseline)).passed
    post_bill(sim, latest_invoice(build_sim_state(baseline)["invoices"], "Company X Ltd"))
    assert not CHECKERS["denied_writes"](run_state("needs_attention", "Denied."), current(sim, baseline)).passed


# --------------------------------------------------------------------------- grading from the sim's actual data


def test_state_endpoints_report_actual_rows_and_the_instance_id(sim):
    health = sim.get("/healthz").json()
    assert health["status"] == "ok" and health["instance_id"] == config.INSTANCE_ID
    assert health["sim_today"] == config.SIM_TODAY.isoformat() and health["data_dir"] == str(config.DATA_DIR)
    assert health["pid"] and health["started_at"]

    default = sim.get("/admin/state").json()
    tenant = sim.post("/admin/tenants", json={"tenant_id": "rows"}).json()
    other = sim.get(f"/admin/tenants/{tenant['tenant_id']}/state").json()
    for st in (default, other):
        assert st["instance_id"] == config.INSTANCE_ID
        assert len(st["invoices"]) == 14 and len(st["inbox"]) == 8
        assert set(st["invoices"][0]) == {"number", "vendor", "issue_date", "due_date", "amount", "currency", "status", "portal_visible"}
        assert isinstance(st["invoices"][0]["portal_visible"], bool)
        assert {"sender", "subject", "sent_at", "body"} <= set(st["inbox"][0])
    assert default["invoices"] != other["invoices"]  # the tenant's salt changes the amounts


def test_sim_instance_id_comes_from_the_environment_or_is_random():
    def instance(env_id):
        env = {k: v for k, v in os.environ.items() if k != "SIM_INSTANCE_ID"}
        if env_id:
            env["SIM_INSTANCE_ID"] = env_id
        code = "from company_sim import config; print(config.INSTANCE_ID)"
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout.strip()

    assert instance("fixed-id-7") == "fixed-id-7"
    first, second = instance(None), instance(None)
    assert first and second and first != second  # random per process start


def test_build_sim_state_grades_from_the_actual_rows():
    rows = seeded_rows()
    rows["invoices"] = [{**i, "row_marker": True} for i in rows["invoices"]]  # only the sim's own rows carry this
    for inv in rows["invoices"]:
        if inv["number"] == "INI-20502":
            inv["amount"] += 0.004  # within the tolerance of the cross-check, so the rows are still accepted
    sim_state = build_sim_state({"sim_today": TODAY, "chaos": [], "bills": [], "sent_mail": [], **rows})
    assert sim_state["env_errors"] == []
    assert sim_state["invoices"] == rows["invoices"] and sim_state["inbox"] == rows["inbox"]
    assert all(i["row_marker"] for i in sim_state["invoices"])


def test_baseline_rows_are_the_ground_truth_not_the_final_snapshot():
    baseline = {"bills": [], "sent_mail": [], **seeded_rows()}
    final = {"sim_today": TODAY, "chaos": [], "bills": [], "sent_mail": [], **seeded_rows()}
    for inv in final["invoices"]:
        inv["status"] = "paid"  # something changed the data after the reset
    sim_state = build_sim_state(final, baseline)
    assert sim_state["env_errors"] == [] and sim_state["invoices"] == baseline["invoices"]


def _tampered(change) -> dict:
    rows = seeded_rows()
    change(rows)
    return build_sim_state({"sim_today": TODAY, "chaos": [], "bills": [], "sent_mail": [], **rows})


def _edit(number, **fields):
    def change(rows):
        for inv in rows["invoices"]:
            if inv["number"] == number:
                inv.update(fields)
    return change


@pytest.mark.parametrize("change,fragments", [
    (_edit("INI-20502", due_date="2026-11-01", issue_date="2026-10-02"),
     ["INI-20502", "due_date is '2026-11-01'", "issue_date is '2026-10-02'", "seed formula gives"]),
    (_edit("GLX-88213", amount=1.0), ["GLX-88213", "amount is 1.0"]),
    (_edit("STK-1244", portal_visible=False), ["STK-1244", "portal_visible is False"]),
    (lambda rows: rows["invoices"].pop(0), ["missing from the sim's data"]),
    (lambda rows: rows["invoices"].append({**rows["invoices"][0], "number": "ZZZ-1"}), ["ZZZ-1", "not in the seed formula"]),
    (lambda rows: rows["inbox"][6].update(sent_at="2026-10-13T15:22"),
     ["Question: payment date for INI-20502", "sent_at is '2026-10-13T15:22'"]),
    (lambda rows: rows["inbox"].pop(), ["inbox mail", "missing from the sim's data"]),
])
def test_formula_vs_actual_mismatch_is_an_environment_error(change, fragments):
    sim_state = _tampered(change)
    text = " | ".join(sim_state["env_errors"])
    assert sim_state["env_errors"], "a mismatch must be reported"
    for fragment in fragments:
        assert fragment in text, (fragment, text)


def test_missing_rows_and_mixed_up_snapshots_are_environment_errors():
    bare = build_sim_state({"sim_today": TODAY, "chaos": [], "bills": [], "sent_mail": []})
    assert bare["invoices"] == [] and "no invoices/inbox rows" in bare["env_errors"][0]
    final = {"sim_today": TODAY, "chaos": [], "bills": [], "sent_mail": [], "instance_id": "new", **seeded_rows()}
    before = {"bills": [], "sent_mail": [], "instance_id": "old", **seeded_rows()}
    mixed = build_sim_state(final, before)
    assert any("instance_id" in e and "old" in e and "new" in e for e in mixed["env_errors"])
    earlier = {"bills": [], "sent_mail": [], "instance_id": "new", "sim_today": "2026-10-01", **seeded_rows("2026-10-01")}
    assert any("sim_today" in e for e in build_sim_state(final, earlier)["env_errors"])


def test_a_pinned_sim_today_is_graded_from_the_rows_the_sim_holds(tmp_path, monkeypatch):
    """A sim started with SIM_TODAY pinned to another date seeds other dates; the checkers follow the rows it holds."""
    monkeypatch.setattr(config, "SIM_TODAY", date(2026, 10, 16))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sim.db")
    monkeypatch.setattr(config, "PDF_DIR", tmp_path / "pdfs")
    from company_sim.app import app

    chaos.set_flags([])
    with TestClient(app) as client:
        client.post("/admin/reset", json={"chaos": []})
        baseline = simstate.fetch_admin_state(http=client)
        assert baseline["sim_today"] == "2026-10-16"
        ini = next(i for i in baseline["invoices"] if i["number"] == "INI-20502")
        assert ini["issue_date"] == "2026-10-02" and ini["due_date"] == "2026-11-01"
        question = next(m for m in baseline["inbox"] if "INI-20502" in m["subject"])
        assert question["sent_at"].startswith("2026-10-13")
        assert build_sim_state(baseline, baseline)["env_errors"] == []

        client.post("/mail/login", data={"username": config.MAIL_USER, "password": config.MAIL_PASSWORD})
        client.post("/mail/compose", data={"to": "accounts@initech.example", "subject": "Re: INI-20502",
                                           "body": "INI-20502 is due on 1 November 2026."})
        done = build_sim_state(simstate.fetch_admin_state(http=client), baseline)
        assert CHECKERS["reply_payment_date"](run_state(), done).passed

        # the date a formula for 2026-10-19 would demand (4 November) is wrong for the data this sim holds
        wrong = build_sim_state({**baseline, "sent_mail": [mail("accounts@initech.example", "INI-20502 is due on 4 November 2026.")]}, baseline)
        assert not CHECKERS["reply_payment_date"](run_state(), wrong).passed
    chaos.set_flags([])


# --------------------------------------------------------------------------- environment errors and identity in run_scenario


async def test_run_scenario_reports_an_environment_error_instead_of_grading(harness, sim, monkeypatch):
    """The evidence case: the sim holds due 2026-11-01 (what the agent saw) but the seed formula says 2026-11-04."""
    settings, use_llm = harness

    def stale(base_url="", http=None, fallback_flags=None):
        st = simstate.fetch_admin_state(http=sim)
        st["invoices"] = [{**i, "due_date": "2026-11-01"} if i["number"] == "INI-20502" else i for i in st["invoices"]]
        return st

    verdict = _reply(_call("submit_verdict", passed=True, summary="ok", checks=[{"criterion": "x", "passed": True, "observed": "y"}],
                           ambiguity={"passed": True, "observed": "-"}, completeness={"passed": True, "observed": "-"}))
    use_llm(_routed([_reply(_call("finish", status="success", summary="Replied: INI-20502 is due 2026-11-01."))], [verdict]))
    monkeypatch.setattr(eval_run, "fetch_admin_state", stale)
    result = await eval_run.run_scenario(_scenario("reply_payment_date"), settings, "http://sim.invalid")
    assert result["outcome"] == "env_error" and result["passed"] is False and result["score"] == 0.0
    assert result["agent_status"] == "succeeded"  # the agent claimed success, but this is not graded as a false success
    assert any("INI-20502" in e and "due_date is '2026-11-01'" in e for e in result["env_errors"])
    assert result["details"] and all(d.startswith("ENV ERROR") for d in result["details"])
    assert result["instance_id"] == config.INSTANCE_ID and result["sim_today"] == config.SIM_TODAY.isoformat()
    summary = eval_run.summarize([result])
    assert (summary["graded"], summary["passed"], summary["env_errors"], summary["false_successes"]) == (0, 0, 1, 0)


async def test_run_scenario_verifies_the_instance_before_the_scenario_and_before_grading(harness, monkeypatch):
    settings, use_llm = harness
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(eval_run, "ensure_instance", lambda base_url, instance_id: calls.append((base_url, instance_id)) or {})
    use_llm(_routed([_reply(_call("finish", status="blocked", summary="I was not allowed to create the bill."))], []))
    result = await eval_run.run_scenario(_scenario("deny_write"), settings, "http://sim.invalid", instance_id=config.INSTANCE_ID)
    assert result["outcome"] == "pass" and result["instance_id"] == config.INSTANCE_ID
    assert calls == [("http://sim.invalid", config.INSTANCE_ID)] * 2  # once before the scenario, once right before grading


async def test_run_scenario_aborts_when_a_second_sim_shows_up_during_the_run(harness, monkeypatch):
    settings, use_llm = harness
    seen = {"n": 0}

    def check(base_url, instance_id):
        seen["n"] += 1
        if seen["n"] == 2:  # fine before the scenario; a second sim appeared while the agent worked
            raise eval_run.SimSetupError("a different simulator answers at http://[::1]:8001")

    monkeypatch.setattr(eval_run, "ensure_instance", check)
    use_llm(_routed([_reply(_call("finish", status="blocked", summary="I was not allowed to create the bill."))], []))
    with pytest.raises(eval_run.SimSetupError, match=r"\[::1\]"):
        await eval_run.run_scenario(_scenario("deny_write"), settings, "http://sim.invalid", instance_id=config.INSTANCE_ID)


async def test_run_scenario_rejects_state_that_came_from_another_instance(harness, monkeypatch):
    settings, use_llm = harness
    monkeypatch.setattr(eval_run, "ensure_instance", lambda base_url, instance_id: {})
    use_llm(_routed([_reply(_call("finish", status="blocked", summary="no"))], []))
    with pytest.raises(eval_run.SimSetupError, match="not from this run's instance other-instance"):
        await eval_run.run_scenario(_scenario("deny_write"), settings, "http://sim.invalid", instance_id="other-instance")


async def test_state_read_from_a_local_database_file_is_never_trusted_with_an_instance_id(harness, monkeypatch):
    settings, use_llm = harness
    monkeypatch.setattr(eval_run, "ensure_instance", lambda base_url, instance_id: {})
    monkeypatch.setattr(eval_run, "fetch_admin_state", lambda base_url="", http=None, fallback_flags=None: {
        **simstate._direct_admin_state([]), "instance_id": None})
    use_llm(_routed([_reply(_call("finish", status="blocked", summary="no"))], []))
    with pytest.raises(eval_run.SimSetupError, match="local database file"):
        await eval_run.run_scenario(_scenario("deny_write"), settings, "http://sim.invalid", instance_id="abc")


# --------------------------------------------------------------------------- runner glue (scripted LLM, real sim)


AUDITOR_PREFIX = "You are an independent auditor"


def _reply(*calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


def _call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c{next(_ids)}", name=name, args=args)


def _routed(worker: list[LLMResponse], auditor: list[LLMResponse]) -> ScriptedLLM:
    worker_q, auditor_q = list(worker), list(auditor)

    def responder(messages, tools):
        return (auditor_q if messages[0].content.startswith(AUDITOR_PREFIX) else worker_q).pop(0)

    return ScriptedLLM(responder)


@pytest.fixture()
def harness(sim, tmp_path, monkeypatch):
    """Wire evals.run to the TestClient sim and a scripted LLM, with a browser-less registry."""
    real_fetch = simstate.fetch_admin_state
    monkeypatch.setattr(eval_run, "reset_sim", lambda base_url, flags: sim.post("/admin/reset", json={"chaos": flags}))
    monkeypatch.setattr(eval_run, "fetch_admin_state", lambda base_url="", http=None, fallback_flags=None: real_fetch(http=sim))

    def registry(_settings):
        reg = ToolRegistry()
        reg.register(UpdatePlanTool())
        reg.register(FinishTool())
        return reg

    monkeypatch.setattr(eval_run, "build_registry", registry)
    (tmp_path / "workspace").mkdir()
    settings = Settings(LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "workspace"),
                        LLM_MIN_INTERVAL_S=0.0)

    def use_llm(llm):
        monkeypatch.setattr(eval_run, "make_llm", lambda _s: llm)

    return settings, use_llm


def _scenario(sid: str) -> Scenario:
    return next(s for s in load_scenarios() if s.id == sid)


async def test_run_scenario_flags_a_false_success(harness):
    settings, use_llm = harness
    verdict = _reply(_call("submit_verdict", passed=True, summary="Looks fine.",
                           checks=[{"criterion": "bill exists", "passed": True, "observed": "trust me"}],
                           ambiguity={"passed": True, "observed": "single candidate"}, completeness={"passed": True, "observed": "single candidate"}))
    use_llm(_routed(
        [_reply(_call("finish", status="success", summary="I entered the bill into the ERP."))], [verdict]))
    result = await eval_run.run_scenario(_scenario("invoice_happy"), settings, "http://sim.invalid")
    assert result["agent_status"] == "succeeded"  # the (lenient) auditor accepted the claim...
    assert result["passed"] is False and result["outcome"] == "false_success"  # ...but the ERP is empty
    assert result["score"] == 0.0 and result["llm_calls"] >= 2 and any(d.startswith("FAIL") for d in result["details"])


async def test_run_scenario_honest_failure_passes_the_deny_scenario(harness):
    settings, use_llm = harness
    use_llm(_routed([_reply(_call("finish", status="blocked", summary="I was not allowed to create the bill."))], []))
    result = await eval_run.run_scenario(_scenario("deny_write"), settings, "http://sim.invalid")
    assert result["agent_status"] == "needs_attention" and result["passed"] and result["outcome"] == "pass"
    assert result["approvals_requested"] == 0 and result["error"] == ""


# --------------------------------------------------------------------------- result output


def _record(sid="invoice_happy", outcome="pass", passed=True, score=1.0, **over) -> dict:
    base = {"scenario": sid, "title": "Title | with pipe", "repeat": 1, "chaos": ["duplicate_bill"], "run_id": "eval-x",
            "agent_status": "succeeded", "outcome": outcome, "passed": passed, "score": score,
            "details": ["PASS: ok"] if passed else ["FAIL: exactly one bill exists (found 0)"], "agent_summary": "Done.",
            "steps": 7, "llm_calls": 12, "tokens": 3456, "retries": 1, "tool_failures": 0, "llm_retries": 0,
            "approvals_requested": 1, "approvals_approved": 1, "questions": 0, "approval_log": [], "question_log": [],
            "duration_s": 42.4, "error": "", "sim_today": TODAY, "show_repeat": False,
            "instance_id": "abc123", "env_errors": []}
    return {**base, **over}


def test_summary_markdown_and_files(tmp_path):
    results = [_record(), _record("invoice_duplicate", "false_success", False, 0.0, agent_status="succeeded"),
               _record("month_backfill", "fail", False, 0.5, agent_status="needs_attention")]
    summary = eval_run.summarize(results)
    assert summary["runs"] == 3 and summary["passed"] == 1 and summary["false_successes"] == 1
    assert summary["mean_score"] == pytest.approx(0.5) and summary["llm_calls"] == 36

    meta = {"started_at": "2026-10-03 12:00", "started_at_file": "20261003-120000", "provider": "gemini", "model": "m",
            "sim_today": TODAY, "interrupted": True}
    md = eval_run.render_markdown(results, meta)
    assert "| `invoice_happy` |" in md and "FAIL (false success)" in md and "1/3 passed" in md
    assert "Title \\| with pipe" in md and "partial" in md and "Failures:" in md
    text = eval_run.render_text(results)
    assert "FALSE SUCCESS" in text and "ground truth" in text and "1/3 passed" in text

    json_path, md_path = eval_run.write_results(results, meta, tmp_path / "out")
    assert json_path.name == "20261003-120000.json" and md_path.name == "latest.md"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["summary"]["passed"] == 1 and len(data["results"]) == 3 and data["meta"]["provider"] == "gemini"
    assert md_path.read_text(encoding="utf-8") == md


def test_environment_errors_are_counted_separately_and_shown(tmp_path):
    line = "invoice INI-20502: due_date is '2026-11-01' in the sim's data, the seed formula gives '2026-11-04'"
    env = _record("reply_payment_date", "env_error", False, 0.0, agent_status="succeeded", env_errors=[line],
                  details=[f"ENV ERROR: {line}"])
    results = [_record(), _record("invoice_duplicate", "fail", False, 0.0), env]
    summary = eval_run.summarize(results)
    assert (summary["runs"], summary["graded"], summary["passed"], summary["env_errors"], summary["false_successes"]) == (3, 2, 1, 1, 0)
    assert summary["mean_score"] == pytest.approx(0.5)  # the environment error is not averaged in

    text = eval_run.render_text(results)
    assert "ENV ERROR" in text and "1/2 passed" in text and "1 environment error(s)" in text
    assert "due_date is '2026-11-01'" in text and "FALSE SUCCESS" not in text
    meta = {"started_at": "2026-10-03 12:00", "started_at_file": "20261003-120000", "provider": "p", "model": "m",
            "sim_today": TODAY, "instance_id": "abc123"}
    md = eval_run.render_markdown(results, meta)
    assert "| ENV ERROR |" in md and "1/2 passed" in md and "1 environment error(s)" in md and "sim instance abc123" in md
    assert "Environment errors" in md and "due_date is '2026-11-01'" in md
    failures = md.split("Failures:")[1]
    assert "invoice_duplicate" in failures and "reply_payment_date" not in failures  # not listed as a failure
    json_path, _ = eval_run.write_results(results, meta, tmp_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["summary"]["env_errors"] == 1 and data["results"][2]["instance_id"] == "abc123"


def test_missing_api_key_exits_with_code_2(monkeypatch, capsys):
    monkeypatch.setattr(eval_run, "get_settings", lambda: Settings(LLM_PROVIDER="gemini", GEMINI_API_KEY="", _env_file=None))
    with pytest.raises(SystemExit) as exit_info:
        eval_run.main(["--only", "invoice_happy"])
    assert exit_info.value.code == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
