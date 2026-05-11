# AskMyCFO · The Ledger

**Geometry-first PDF extraction for Indian annual reports.** Upload a PDF,
get a balanced 26-row template per the AskMyCFO spec. **Zero LLM. Zero API
cost. Fully deterministic.** Sub-second per report.

---

## Why this exists

The v2.2 pipeline used Anthropic Claude as primary mapper, OpenAI GPT-4o
as fallback, ChromaDB RAG, and sentence-transformers. ~12,000 lines of
code, ongoing API spend, non-deterministic outputs.

The actual problem the spec describes — sum line items into 26 buckets via
an alias dictionary — doesn't need any of that. It needs a layout-invariant
PDF extractor, a YAML mapping, and a balance check. That's this repo.

| | v2.2 | v3.0 (this) |
|---|---|---|
| LoC                 | ~12,000           | ~1,400 |
| API calls / report  | 5–40              | **0** |
| Cost / report       | ₹2–5              | **₹0** |
| Latency             | 30–120 s          | **< 1 s** |
| Determinism         | no                | **yes — byte-identical reruns** |
| Audit trail         | "AI said so"      | every classification cites its alias |

---

## Quickstart

```bash
pip install -r requirements.txt
python run.py
```

Opens at `http://localhost:5050`.

1. Drop a PDF on the upload card.
2. Wait for the comparison page (typically < 1 second).
3. Verify: the trimmed BS/PL PDF on the left, the computed template on
   the right. The balance check should read **✓ Balances · both years, zero gap**.

---

## Architecture

```
                     ┌────────────────┐
   annual_report.pdf │   Flask web    │
   ───────────────► │  upload route   │
                     └────────┬───────┘
                              │
                ┌─────────────▼──────────────┐
                │      core/pipeline         │  orchestrator
                └─────────────┬──────────────┘
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
  ┌──────────┐         ┌─────────────┐        ┌──────────────┐
  │ locator  │────────►│  geometry   │───────►│  classifier  │
  │  finds   │  pages  │ extracts    │  rows  │ aliases →    │
  │  BS/PL   │         │ every span  │        │  26 buckets  │
  └──────────┘         └─────────────┘        └──────┬───────┘
                                                     │
                                              ┌──────▼──────┐
                                              │ aggregator  │
                                              │ sum + bal-  │
                                              │ ance check  │
                                              └──────┬──────┘
                                                     │
                                          ┌──────────▼───────────┐
                                          │   data/jobs/<id>/    │
                                          │  source_BS_PL.pdf    │
                                          │  raw_rows.csv        │
                                          │  template.csv        │
                                          │  diagnostics.json    │
                                          └──────────────────────┘
```

### `core/locator.py` (250 LoC)
Two-layer page detection:
1. **Canonical headline** — large-font match on "Balance Sheet as at …"
   / "Statement of Profit and Loss …", with a blacklist that rejects
   Notes-to-FS schedules, EPS detail, Trade Payable Ageing, etc.
2. **Structural fingerprint** — for headerless pages (some PDFs ship the
   BS without any title). Requires `PARTICULARS` + dual `As at` column
   headers + both `EQUITY AND LIABILITIES` and `ASSETS` section anchors
   + ≥10 numeric rows. False-positive-free across our test set.

### `core/geometry.py` (370 LoC)
Layout-invariant span-level extraction.
- Right-edge clustering on numeric tokens → column positions
- Y-banding → rows
- 25%-of-page-width gap → side-by-side spread detection (split at
  physical page midpoint; **A3 landscape spreads supported**)
- Unicode NFKD normalization (handles `ﬁ` / `ﬂ` ligatures)
- Top 6% / bottom 8% body crop (filters page headers, footers, signature blocks)
- **Unlabeled subtotal rows preserved** — Schedule III convention
- Statement terminator detection (`Total Equity and Liabilities`,
  `Total Comprehensive Income`, bare `TOTAL`) — auditor sign-offs dropped

### `core/classifier.py` (200 LoC)
Section-aware dictionary lookup with fuzzy fallback.
- Label normalization: strip leading markers `(a)`, `(i)`, `1.`, `-`;
  strip trailing note refs `3(A)`, `31(d)`; preserve `(Net)`
- Section context disambiguates `Borrowings`, `Provisions`, `Loans`,
  `Investments`, `Other Financial Liabilities` between current and
  non-current
- RapidFuzz `token_set_ratio ≥ 88` fallback for label typos

### `config/mapping_bs.yaml` + `mapping_pl.yaml`
Direct transcription of the AskMyCFO Notion spec. 127 BS aliases over 28
buckets, 59 PL aliases over 7 buckets. Edit these to handle new
report styles — no code changes needed.

### `core/aggregator.py` (200 LoC)
- Sum classified rows into the 26-bucket template
- Compute totals exactly per the Notion spec:
  `Total Liabilities = Networth + CL + NCL`
- ₹1 balance tolerance
- Subtotal blacklist (explicit prefix list — **not** blanket `total *`,
  because rows like "Total outstanding dues of micro enterprises…" are
  real data)
- **Positional CY/PY extraction** based on column count: 2-col layouts
  use `[CY, PY]`, 3-col use `[Note, CY, PY]`, 4-col use
  `[Note, sub, CY, PY]` (the PL OCI sub-amount case)

---

## Verified against

| File | Pages | Layout | Result |
|---|---|---|---|
| Colgate FY24-25  | 269 | Standard separated BS/PL | ✓ balances both years |
| Kalyani Forge FY24-25 | 55  | **A3 landscape side-by-side spread** | ✓ balances both years |
| Macobs Technologies FY24-25 | 85  | **Headerless BS** + standard PL | ✓ balances both years |

---

## Project layout

```
askmycfo/
├── app.py                  Flask routes
├── run.py                  entry point
├── requirements.txt
├── README.md
├── core/
│   ├── geometry.py         layout-invariant extractor
│   ├── locator.py          BS/PL page finder
│   ├── classifier.py       alias → bucket
│   ├── aggregator.py       sum + balance check
│   └── pipeline.py         orchestrator
├── config/
│   ├── mapping_bs.yaml     BS aliases (Notion spec)
│   └── mapping_pl.yaml     P&L aliases (Notion spec)
├── templates/
│   ├── base.html
│   ├── upload.html
│   ├── compare.html        side-by-side PDF + template
│   └── jobs.html
├── static/
│   └── style.css           editorial financial-journal aesthetic
└── data/
    ├── uploads/            (gitignore)
    └── jobs/<id>/          (gitignore)
        ├── source.pdf
        ├── source_BS_PL.pdf
        ├── raw_rows.csv
        ├── template.csv
        ├── diagnostics.json
        └── meta.json
```

---

## When to edit what

- **Wrong bucket assignment** → `config/mapping_bs.yaml` or
  `mapping_pl.yaml`. Add the offending label to the right bucket's
  `aliases:` list. Restart the server. Done.
- **BS/PL pages not found** → `core/locator.py`. Most issues are
  blacklist additions or a relaxed structural rule.
- **Numeric value wrong on a row** → `core/geometry.py`. Drop into
  `proto/run_proto.py` style debugging and inspect span positions.
- **Balance check fails by large amount** → check the trace counts in the
  Template view. If `account_payable` has `×1` trace but the PDF has 2
  sub-lines, an alias is missing in the YAML.

---

## What's intentionally **not** here

- No language model. No OpenAI. No Anthropic. No sentence-transformers.
- No ChromaDB. No FAISS. No embeddings.
- No Excel. CSV is the contract; if the user wants Excel, they convert.
- No async / Celery. Pipeline runs in-process in well under a second.
- No background workers, no queue, no database. SQLite was specced in the
  Financial Health module elsewhere; this extractor has no per-report
  state to persist beyond the job directory.
