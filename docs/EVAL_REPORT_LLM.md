# Evaluation report

120 documents, 115 of them invoices. Engines used: llm (8), rules (112).

## Decisions

| Metric | Value |
|---|---|
| **False auto-pass** (bad invoice queued for payment) | **0** |
| Touchless rate (invoices auto-passed) | 61.7% |
| Touchless ceiling (invoices that *should* auto-pass) | 61.7% |
| Needless reviews (good invoice sent to a human) | 0 |
| Decision accuracy | 100.0% |
| Right decision, wrong exception type | 0 |
| Value of problem invoices stopped | $110,699.45 |

### Expected -> actual

| Expected -> actual | Documents |
|---|---|
| auto_pass -> auto_pass | 71 |
| flag -> flag | 38 |
| hold -> hold | 6 |
| not_applicable -> not_applicable | 5 |

### By scenario

| Scenario | Documents | Fully correct |
|---|---|---|
| clean | 42 | 42 |
| duplicate | 6 | 6 |
| expense_normal | 14 | 14 |
| expense_spike | 5 | 5 |
| freeform | 8 | 8 |
| math_error | 4 | 4 |
| missing_po | 5 | 5 |
| price_variance | 8 | 8 |
| price_within_tolerance | 7 | 7 |
| qty_over_receipt | 7 | 7 |
| resubmitted_new_number | 4 | 4 |
| statement | 5 | 5 |
| unknown_vendor | 5 | 5 |

## Extraction accuracy

Share of invoices the engine read where the field exactly matches ground truth. `lines` requires every line's code, quantity, price and amount to match.

| Engine | Read | vendor_name | invoice_number | invoice_date | due_date | po_number | subtotal | tax | total | lines |
|---|---|---|---|---|---|---|---|---|---|---|
| rules | 107 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| llm | 8 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |

## Work queues

| Queue | Open exceptions |
|---|---|
| purchasing | 13 |
| receiving | 11 |
| ap_review | 10 |
| vendor_onboarding | 5 |
| accounting | 5 |
