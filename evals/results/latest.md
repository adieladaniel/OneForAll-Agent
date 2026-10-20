### Eval results (2026-10-20 13:28, gemini / gemini-2.5-flash > gemini-3.5-flash-lite > gemini-2.5-flash-lite, sim date 2026-10-20, sim instance c9e6ff0a3d43)

Success is judged by deterministic checks against the simulated company's database, not by the agent's claims and not by an LLM. `Agent said` is the final status the agent reported; `Ground truth` is the checker's verdict.

| Scenario | Chaos | Agent said | Ground truth | Score | Steps | LLM calls | Tokens | Retries | Approvals | Questions | Model | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `month_backfill` | - | needs_attention | FAIL | 0.33 | 72 | 87 | 736523 | 0 | 1/1 | 0 | gemini-3.5-flash-lite (+1 switch) | 622s |
| `reply_payment_date` | - | needs_attention | FAIL | 0.00 | 67 | 57 | 491215 | 0 | 1/1 | 0 | gemini-3.5-flash-lite | 231s |

**0/2 passed**, 0 false success(es), mean score 0.17, 144 LLM calls, 1227738 tokens, 853s total.

Scenarios: `month_backfill` = Record every open invoice due this month; `reply_payment_date` = Reply to a vendor payment-date question.

Failures:

- `month_backfill`: FAIL: GLX-88213: exactly one bill expected, found 0 (none) \| FAIL: UMB-5531: exactly one bill expected, found 0 (none)
- `reply_payment_date`: FAIL: a mail was sent to accounts@initech.example (new sent mails: 0)
