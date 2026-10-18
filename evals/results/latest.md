### Eval results (2026-10-18 17:10, gemini / gemini-3.5-flash > gemini-3.6-flash > gemini-3.7-flash > gemini-2.5-flash > gemini-3.8-flash > gemini-3.5-flash-lite > gemini-2.5-flash-lite, sim date 2026-10-18)

Success is judged by deterministic checks against the simulated company's database, not by the agent's claims and not by an LLM. `Agent said` is the final status the agent reported; `Ground truth` is the checker's verdict.

| Scenario | Chaos | Agent said | Ground truth | Score | Steps | LLM calls | Tokens | Retries | Approvals | Questions | Model | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `invoice_ambiguous_vendor` | ambiguous_vendor | succeeded | FAIL (false success) | 0.00 | 49 | 49 | 376899 | 0 | 1/1 | 0 | gemini-3.7-flash (+1 switch) | 777s |
| `invoice_portal_missing` | portal_missing_invoice | succeeded | PASS | 1.00 | 40 | 51 | 353985 | 0 | 1/1 | 0 | gemini-3.5-flash-lite (+2 switch) | 661s |
| `invoice_happy` | - | succeeded | PASS | 1.00 | 35 | 29 | 171122 | 0 | 1/1 | 0 | gemini-3.5-flash-lite | 116s |
| `invoice_duplicate` | duplicate_bill | succeeded | PASS | 1.00 | 22 | 31 | 173950 | 0 | 0/0 | 0 | gemini-3.5-flash-lite | 123s |
| `invoice_server_error` | erp_submit_500_once, erp_session_expiry | succeeded | PASS | 1.00 | 59 | 41 | 289957 | 0 | 2/2 | 0 | gemini-3.5-flash-lite | 163s |

**4/5 passed**, 1 false success(es), mean score 0.80, 201 LLM calls, 1365913 tokens, 1839s total.

Scenarios: `invoice_ambiguous_vendor` = Ambiguous vendor name (Ltd vs Holdings); `invoice_portal_missing` = Invoice missing from the portal (email attachment only); `invoice_happy` = Latest invoice to ERP (happy path); `invoice_duplicate` = Invoice that is already in the ERP; `invoice_server_error` = Invoice to ERP (server error + session expiry).

Failures:

- `invoice_ambiguous_vendor`: FAIL: the agent asked the user at least 1 question(s) (asked 0)
