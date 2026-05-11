"""
core/aggregator.py — sum classified rows into the 26-bucket template
and compute totals + balance check exactly as the Notion spec defines.

Output format mirrors the Notion spec:

  Networth (4 buckets) + Total Networth
  Current Liabilities (5 buckets) + Total CL
  Non-Current Liabilities (4 buckets) + Total NCL
  Total Liabilities (= Networth + CL + NCL)
  Current Assets (8 buckets) + Total CA
  Non-Current Assets (7 buckets) + Total NCA
  Total Assets (= CA + NCA)
  Difference (= Total Liabilities - Total Assets)

  P&L (7 buckets)

For two-year reporting we produce CY and PY columns side by side.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.classifier import (
    CompiledMapping, Classification, classify_row, normalize_label,
)
from core.geometry import Row


# Order of buckets in the output, mirroring the Notion spec exactly.
BS_OUTPUT_ORDER = [
    # (section_label, [(bucket_yaml_key, display_label)])
    ("Equity / Networth", [
        ("share_capital",                "Share Capital"),
        ("retained_earnings",            "Retained Earnings"),
        ("general_reserve_and_surplus",  "General Reserve and Surplus"),
        ("other_equity",                 "Other Equity"),
    ]),
    ("Current Liabilities", [
        ("account_payable",              "Account Payable"),
        ("provisions",                   "Provisions"),
        ("short_term_borrowings",        "Short Term Borrowings"),
        ("other_current_liabilities",    "Other Current Liabilities"),
        ("other_financial_liabilities",  "Other Financial Liabilities"),
    ]),
    ("Non-Current Liabilities", [
        ("provisions",                   "Provisions"),
        ("long_term_borrowings",         "Long Term Borrowings"),
        ("other_non_current_liabilities","Other Non-Current Liabilities"),
        ("other_financial_liabilities",  "Other Financial Liabilities"),
    ]),
    ("Current Assets", [
        ("bank_balance",                 "Bank Balance"),
        ("cash_and_cash_equivalents",    "Cash & Cash Equivalents"),
        ("inventories",                  "Inventories"),
        ("investments",                  "Investments"),
        ("loans",                        "Loans"),
        ("account_receivables",          "Account Receivables"),
        ("other_current_assets",         "Other Current Assets"),
        ("other_financial_assets",       "Other Financial Assets"),
    ]),
    ("Non-Current Assets", [
        ("fixed_assets",                 "Fixed Assets"),
        ("investments",                  "Investments"),
        ("loans",                        "Loans"),
        ("capital_work_in_progress",     "Capital Work in Progress"),
        ("other_non_current_assets",     "Other Non-Current Assets"),
        ("other_financial_assets",       "Other Financial Assets"),
        ("deferred_tax",                 "Deferred Tax"),
    ]),
]

PL_OUTPUT_ORDER = [
    ("revenue",                            "Revenue"),
    ("cost_of_goods_sold",                 "Cost Of Goods Sold"),
    ("employee_expenses",                  "Employee Expenses"),
    ("interest",                           "Interest"),
    ("depreciation",                       "Depreciation"),
    ("other_expenses_less_other_income",   "Other Expenses less Other Income"),
    ("taxes",                              "Taxes"),
]


# Map display section label → YAML key
SECTION_TO_YAML = {
    "Equity / Networth":         "networth",
    "Current Liabilities":       "current_liabilities",
    "Non-Current Liabilities":   "non_current_liabilities",
    "Current Assets":            "current_assets",
    "Non-Current Assets":        "non_current_assets",
}


@dataclass
class TraceItem:
    """One source row contributing to a bucket."""
    label: str
    section: str
    cy: float
    py: float
    sign: int
    method: str
    score: float
    side: str            # "BS" or "PL"


@dataclass
class BucketResult:
    section: str
    bucket: str
    display: str
    cy: float = 0.0
    py: float = 0.0
    trace: List[TraceItem] = field(default_factory=list)


@dataclass
class AggregatedReport:
    bs_buckets: Dict[Tuple[str, str], BucketResult] = field(default_factory=dict)
    pl_buckets: Dict[str, BucketResult] = field(default_factory=dict)
    unclassified: List[TraceItem] = field(default_factory=list)

    # Totals (computed)
    total_networth_cy: float = 0.0
    total_networth_py: float = 0.0
    total_cl_cy: float = 0.0
    total_cl_py: float = 0.0
    total_ncl_cy: float = 0.0
    total_ncl_py: float = 0.0
    total_liabilities_cy: float = 0.0     # = NW + CL + NCL
    total_liabilities_py: float = 0.0
    total_ca_cy: float = 0.0
    total_ca_py: float = 0.0
    total_nca_cy: float = 0.0
    total_nca_py: float = 0.0
    total_assets_cy: float = 0.0
    total_assets_py: float = 0.0
    difference_cy: float = 0.0
    difference_py: float = 0.0

    balanced_cy: bool = False
    balanced_py: bool = False
    tolerance: float = 1.0


# ─── Aggregation ───────────────────────────────────────────────────────────

# Labels we ignore — these are subtotals computed by the publisher that
# would double-count if we summed them as regular line items.
# IMPORTANT: do NOT blanket-reject anything starting with "total " —
# rows like "Total outstanding dues of micro enterprises..." are real
# data lines (Trade Payables sub-breakdown).
SUBTOTAL_BLACKLIST = {
    "total", "subtotal", "sub-total", "grand total",
    "total assets", "total equity", "total liabilities",
    "total equity and liabilities", "total liabilities and equity",
    "total current assets", "total current liabilities",
    "total non-current assets", "total non current assets",
    "total non-current liabilities", "total non current liabilities",
    "total non - current assets", "total non - current liabilities",
    # NBFC subtotals (Regency Fincorp Sch III for NBFCs)
    "total financial assets", "total financial liabilities",
    "total non financial assets", "total non-financial assets",
    "total non financial liabilities", "total non-financial liabilities",
    "total income", "total revenue", "total expenses",
    "total tax expense", "total tax expenses",
    "total comprehensive income", "total other comprehensive income",
    "profit before tax", "profit for the year", "profit for the period",
    "profit/(loss) for the period", "profit/(loss) before tax",
    "earnings per equity share", "earnings per share",
    "earnings per equity share :", "(unlabeled subtotal)",
    "equity attributable to owners of the company",
    "equity attributable to owners of the company (i)",
}

# Explicit "starts-with" prefixes for canonical subtotal rows.
# These are very specific so we don't accidentally drop data rows.
SUBTOTAL_PREFIXES = (
    "total assets", "total liabilities", "total equity",
    "total current ", "total non-current ", "total non current ",
    "total non - current ",
    # NBFC: "Total Financial Liabilities" / "Total Non-Financial Assets"
    "total financial ", "total non financial ", "total non-financial ",
    "total income", "total revenue", "total expense", "total tax",
    "total comprehensive", "total other comprehensive",
    "subtotal", "sub-total", "grand total",
    "profit before ", "profit for the ", "profit/(loss) ",
)


def _is_subtotal(label: str) -> bool:
    """Drop rows that are subtotals/totals — we compute these ourselves."""
    norm = normalize_label(label)
    if not norm:
        return True
    if norm in SUBTOTAL_BLACKLIST:
        return True
    for prefix in SUBTOTAL_PREFIXES:
        if norm.startswith(prefix):
            return True
    return False


def aggregate(rows: List[Row], side: str,
               mapping: CompiledMapping) -> AggregatedReport:
    """Aggregate a list of geometry-extracted rows for ONE side (BS or PL)."""
    report = AggregatedReport()
    _init_buckets(report)

    for row in rows:
        if not row.label or _is_subtotal(row.label):
            continue
        if not row.values or all(v is None or v == 0 for v in row.values):
            continue

        # Pick CY and PY values BY COLUMN POSITION, not by "last two non-None".
        # Standard Indian financial-statement layouts:
        #   2 cols: [CY, PY]
        #   3 cols: [Note, CY, PY]                        ← most common
        #   4 cols: [Note, sub_amount, CY, PY]            ← PL with OCI sub-col
        #   5 cols: [Note, CY_detail, CY_sub, PY_detail, PY_sub]   ← Madhav PL
        #   6+ cols: same detail+subtotal pattern, with more sub levels
        #
        # For 5+ cols, "rightmost non-None per half" — a row is either a
        # detail row (value in the detail column) OR a subtotal row (value
        # in the subtotal column), never both. Picking the rightmost
        # non-None within each half gets the right value either way.
        n_cols = len(row.values)
        if n_cols == 2:
            cy = row.values[0] or 0.0
            py = row.values[1] or 0.0
        elif n_cols == 3:
            cy = row.values[1] or 0.0
            py = row.values[2] or 0.0
        elif n_cols == 4:
            cy = row.values[2] or 0.0
            py = row.values[3] or 0.0
        elif n_cols >= 5:
            half = (n_cols - 1) // 2     # number of value columns per side
            left  = [v for v in row.values[1 : 1 + half] if v is not None]
            right = [v for v in row.values[1 + half : 1 + 2 * half] if v is not None]
            cy = left[-1] if left else 0.0
            py = right[-1] if right else 0.0
        else:
            continue   # weird layout, skip

        # Skip if both columns are empty/zero
        if cy == 0.0 and py == 0.0:
            continue

        c = classify_row(row.label, row.section, side, mapping)
        trace = TraceItem(
            label=row.label, section=row.section,
            cy=cy, py=py, sign=c.sign,
            method=c.method, score=c.score, side=side,
        )

        if c.bucket is None:
            report.unclassified.append(trace)
            continue

        if side == "BS":
            key = c.bucket
            if key not in report.bs_buckets:
                # Should not happen with init_buckets, but be defensive
                report.bs_buckets[key] = BucketResult(
                    section=key[0], bucket=key[1], display=key[1].title(),
                )
            report.bs_buckets[key].cy += c.sign * cy
            report.bs_buckets[key].py += c.sign * py
            report.bs_buckets[key].trace.append(trace)
        else:   # PL
            _, bucket_name = c.bucket
            if bucket_name not in report.pl_buckets:
                report.pl_buckets[bucket_name] = BucketResult(
                    section="profit_and_loss",
                    bucket=bucket_name, display=bucket_name.title(),
                )
            report.pl_buckets[bucket_name].cy += c.sign * cy
            report.pl_buckets[bucket_name].py += c.sign * py
            report.pl_buckets[bucket_name].trace.append(trace)

    return report


def _init_buckets(report: AggregatedReport):
    """Pre-create every output bucket so we always emit the full template
    shape even when a bucket has zero contributions."""
    for section_label, buckets in BS_OUTPUT_ORDER:
        section_yaml = SECTION_TO_YAML[section_label]
        for bucket_key, display in buckets:
            report.bs_buckets[(section_yaml, bucket_key)] = BucketResult(
                section=section_yaml, bucket=bucket_key, display=display,
            )
    for bucket_key, display in PL_OUTPUT_ORDER:
        report.pl_buckets[bucket_key] = BucketResult(
            section="profit_and_loss", bucket=bucket_key, display=display,
        )


def compute_totals(report: AggregatedReport, tolerance: float = 1.0):
    """Compute section totals and the balance check as per the Notion spec."""
    def sum_section(section_yaml: str) -> Tuple[float, float]:
        cy = sum(b.cy for k, b in report.bs_buckets.items() if k[0] == section_yaml)
        py = sum(b.py for k, b in report.bs_buckets.items() if k[0] == section_yaml)
        return cy, py

    report.total_networth_cy, report.total_networth_py = sum_section("networth")
    report.total_cl_cy,       report.total_cl_py       = sum_section("current_liabilities")
    report.total_ncl_cy,      report.total_ncl_py      = sum_section("non_current_liabilities")
    report.total_ca_cy,       report.total_ca_py       = sum_section("current_assets")
    report.total_nca_cy,      report.total_nca_py      = sum_section("non_current_assets")

    # Per Notion spec: "Total Liabilities = Total Networth + Total CL + Total NCL"
    report.total_liabilities_cy = (report.total_networth_cy
                                    + report.total_cl_cy
                                    + report.total_ncl_cy)
    report.total_liabilities_py = (report.total_networth_py
                                    + report.total_cl_py
                                    + report.total_ncl_py)
    report.total_assets_cy = report.total_ca_cy + report.total_nca_cy
    report.total_assets_py = report.total_ca_py + report.total_nca_py
    report.difference_cy = report.total_liabilities_cy - report.total_assets_cy
    report.difference_py = report.total_liabilities_py - report.total_assets_py

    report.balanced_cy = abs(report.difference_cy) <= tolerance
    report.balanced_py = abs(report.difference_py) <= tolerance
    report.tolerance = tolerance


# ─── Merge BS + PL into a single report (the orchestrator calls this) ─────

def merge(bs_report: AggregatedReport,
            pl_report: AggregatedReport,
            tolerance: float = 1.0) -> AggregatedReport:
    """Combine BS-side aggregation and PL-side aggregation into one report."""
    merged = AggregatedReport()
    merged.bs_buckets = bs_report.bs_buckets
    merged.pl_buckets = pl_report.pl_buckets
    merged.unclassified = bs_report.unclassified + pl_report.unclassified
    compute_totals(merged, tolerance)
    return merged
