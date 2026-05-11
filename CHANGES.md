# Changes — May 11 patch

## Test results

Every PDF in the user's job archive now extracts and balances:

| Report | Pages located | Total Assets CY | Balance |
| --- | --- | --- | --- |
| Virinchi FY2425 | 111, 112 | 62,506 | ✓ |
| Shalon Silks FY2425 | 93, 94, 95 | 28,261 | ✓ |
| Akiko FY2425 | 71, 72 | 5,694 | ✓ |
| Regency Fincorp FY2425 (NBFC) | 191, 192, 193 | 22,911 | ✓ |
| Madhav Infra FY2223 | 45, 46 | 47,001 | ✓ |
| MRF AR_2024 | 93, 94 | 26,415 | ✓ |
| MRF AR_2025 | 91, 92 | 29,096 | ✓ |
| Bajaj Finserv AR_2016 | 55, 173 | 2,747 | ✓ |
| Chola AR_2017 (×2 jobs) | 109, 110 | 3,072,114 | ✓ |
| AR_2018 | 52, 143, 144 | 131,426 | ✓ |
| AVG Logistics FY2425 | 71 | 49,558 | ✓ |
| AR_2024 (3 variants) | – | – | ✓ |

**Regression set:**

| Report | Result |
| --- | --- |
| Colgate FY2425 | ✓ TA=301,855, balanced |
| Kalyani Forge FY2425 (A3 spread) | ✓ TA=23,028, balanced |
| Macobs Technologies FY2425 | ✓ TA=3,159, balanced |

**Total: 18/18 balance correctly.**

## Files changed

### `core/geometry.py`
- `numeric_column_clusters`: raised `min_count` from 4 to 8. Filters out sparse
  spurious column-clusters like Virinchi's section markers "(1)", "(2)", "(3)"
  parsed as `-1, -2, -3` at far-left x≈52.
- `split_side_by_side`: now requires `page.rect.width > page.rect.height`.
  A true two-page A3 landscape spread is landscape by physical necessity.
  Without this gate, portrait A4 pages with a wide gap between the label
  column and numeric columns (Virinchi, Shalon) were sliced in half,
  producing a LEFT half of labels-only + a RIGHT half of values with no
  labels — destroying every row.

### `core/locator.py`
- **Canonical phrases now match against full top-of-page text** (~25%) rather
  than headline-sized spans only. Many publishers (Akiko, Madhav Infra, MRF)
  render the BS/PL title at body text size — no visual emphasis.
- BS regex matches anywhere "balance sheet" + "as at"/"as on" appear (catches
  title prefixes like "78 BALANCE SHEET AS AT" — MRF — and "MIPL BALANCE
  SHEET AS AT" — Madhav).
- **NEW: reverse-order BS regex** for "As at 31 March 2016 ... Balance Sheet"
  layouts (Bajaj Finserv 2015-16). Requires a 4-digit year between the two
  phrases so body-text mentions like "as on the balance sheet date" do not
  match.
- PL regex requires "for the year/period/quarter/half-year ended" right after
  the title — body-text mentions of "Statement of Profit and Loss" in
  accounting policy paragraphs (Madhav pp.50-54) no longer match.
- Layer 2 BS now accepts NBFC convention: when "For the year ended" replaces
  "As at" as the BS column header (Regency Fincorp) and the page is not
  PL-shaped (no Revenue + PBT combo).
- `BS_AS_AT_RE` matches "as at" OR "as on" (AR_2018 j32epor7 reverse layout).
- `BS_ASSETS_RE` relaxed to match "A. ASSETS" / "(i) ASSETS" prefix forms
  (NBFC enumeration).
- **`_scan_section_divider` no longer depends on relative font-size threshold.**
  On a sparse divider page where every span has the same large size (Bajaj
  Finserv's "Standalone" cover at p.166, where both spans are size 40),
  the old median-based filter rejected the headline itself. Now uses an
  absolute size threshold (≥18pt) and accepts "Standalone" / "Consolidated"
  alone as the divider keyword (not just "Standalone Financial Statements").
- **Multi-page BS continuation glue** (Layer 3): scans +1/+2 pages after a
  BS hit; includes as continuation if the next page has numeric columns, a
  Total terminator ("Total Equity and Liabilities" / "Total Liabilities and
  Equity" / "Total Liabilities"), is not blacklisted, not PL-shaped, and
  not a CFS page. Captures Shalon's p.94, Regency's p.192.
- **Blacklist applies to FULL body text, not just top 18%** — some Notes
  continuations (e.g., AR_2024 PPE schedule p.361) have their "(Contd..)"
  marker below the top band.
- New blacklist patterns:
  - `\bdirector(s|s'|s')?\s+report\b` (Akiko false PL on p.26)
  - `\bnote\s*[–—-]\s*\d+\b` (Regency "NOTE – 3 LOANS AND ADVANCES")
  - `\(contd\.?\.?\)` (any Notes schedule continuation marker)
  - `\bstatement\s+of\s+cash\s+flows?\b` (alternate CFS naming)
  - `\bcash\s+flow\s+from\s+operating\s+activities\b` (AVG p.72 CFS body)

### `core/aggregator.py`
- **5+ column layout support** for PL: Madhav's `[Note, CY_detail,
  CY_subtotal, PY_detail, PY_subtotal]` layout. Detail rows put values in
  the detail columns, subtotal rows put values in the subtotal columns —
  never both. The previous `n_cols >= 4 → cy=-2, py=-1` rule misread
  these as PY-side only. New rule: for `n_cols ≥ 5`, take the rightmost
  non-None value within the LEFT half as CY and within the RIGHT half as
  PY. Madhav now correctly extracts both CY 2022-23 and PY 2021-22.
- New subtotal entries for NBFC: "Total Financial Liabilities",
  "Total Financial Assets", "Total Non-Financial Liabilities", "Total
  Non-Financial Assets". Previously these were fuzzy-matched into
  `other_financial_liabilities` and double-counted with the individual
  rows summing to them.

### `core/pipeline.py`
- Catch `fitz.FileDataError` / `RuntimeError` on PDF open and return a clean
  `{ok: False, error: "Could not open PDF..."}` result instead of letting
  the bare exception bubble up to Flask.

### `config/mapping_bs.yaml`
- `bank_balance`: singular variants "Bank Balance other than (ii) above"
  (Shalon), "Balances with banks", "Other balances with banks" (Madhav).
- `other_equity`: "equity warrants", "share warrants" (Madhav PY).
- `long_term_borrowings`: NBFC variants "Borrowings (other than debt
  securities)", "Debt Securities", "Subordinated Liabilities" (Regency Sch
  III for NBFCs).

### `config/mapping_pl.yaml`
- `revenue`: NBFC line items — "Interest Income", "Fees and commission
  Income", "Net gain on fair value changes", "Net gain on derecognition of
  financial instruments", "Rental Income", "Dividend Income", "Other
  operating income", "Total Revenue from operations".
- `cost_of_goods_sold`: "Construction Expenses", "Changes in Construction
  Work in Progress" (Madhav — construction company), "Fees and commission
  expense" (NBFC).
- `taxes`: "Earlier years tax", "Earlier years' tax", "Tax adjustment for
  earlier years", "Adjustment of tax relating to earlier periods".

