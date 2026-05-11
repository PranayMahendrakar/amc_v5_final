"""
core/pipeline.py — orchestrate the full extraction pipeline.

    PDF in
      ↓
    [1] Locator        find every BS/PL page (canonical + structural)
      ↓
    [2] Geometry       per-page span-level row extraction (handles
                       side-by-side spreads, multi-page continuation,
                       unlabeled subtotals, ligatures)
      ↓
    [3] Classifier     normalize labels, section-aware dict lookup,
                       fuzzy fallback. Unmatched rows logged.
      ↓
    [4] Aggregator     sum into 26-bucket template, compute totals,
                       run balance check
      ↓
    Outputs (in jobs/<id>/):
        source_BS_PL.pdf   — trimmed PDF, only the located pages
        raw_rows.csv       — every row the geometry extractor emitted
        template.csv       — 26-bucket spec template + totals
        diagnostics.json   — coverage, balance check, unclassified list
"""
from __future__ import annotations
import csv
import json
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Dict, List, Optional

import fitz

from core.geometry import extract_page, Row, ExtractionResult
from core.locator import find_bs_pl_pages, PageHit, summarize_hits
from core.classifier import load_mapping, CompiledMapping
from core.aggregator import (
    aggregate, merge, AggregatedReport, BucketResult, TraceItem,
    BS_OUTPUT_ORDER, PL_OUTPUT_ORDER, SECTION_TO_YAML,
)


# ─── Helpers ───────────────────────────────────────────────────────────────

def _write_trimmed_pdf(src_pdf: str, page_idxs: List[int],
                        dst_path: Path) -> None:
    """Save a new PDF containing only the listed pages, in order."""
    src = fitz.open(src_pdf)
    dst = fitz.open()
    for idx in page_idxs:
        if 0 <= idx < len(src):
            dst.insert_pdf(src, from_page=idx, to_page=idx)
    dst.save(str(dst_path))
    dst.close()
    src.close()


def _write_raw_csv(per_page_extractions: List[tuple], dst_path: Path) -> None:
    """Write every raw row from every BS/PL page to one CSV.

    Each tuple is (page_idx, kind, variant, ExtractionResult).
    Side-by-side pages produce multiple sub-extractions (LEFT, RIGHT)."""
    with dst_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page", "side", "variant", "half", "section",
                    "label", "col_1", "col_2", "col_3", "col_4"])

        for page_idx, kind, variant, result in per_page_extractions:
            if result.side_by_side:
                for half_name, sub in zip(("LEFT", "RIGHT"),
                                           result.sub_extractions):
                    for r in sub.rows:
                        _emit_row(w, page_idx + 1, kind, variant, half_name, r)
            else:
                for r in result.rows:
                    _emit_row(w, page_idx + 1, kind, variant, "", r)


def _emit_row(w, page, side, variant, half, r: Row):
    vals = ["" if v is None else f"{v:.2f}" for v in r.values]
    # pad to 4 columns
    vals = (vals + ["", "", "", ""])[:4]
    w.writerow([page, side, variant, half, r.section, r.label, *vals])


def _write_template_csv(report: AggregatedReport, dst_path: Path) -> None:
    """Write the 26-bucket template per the Notion spec, plus totals + balance."""
    with dst_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "parameter", "current_year", "prior_year"])

        # ── Balance Sheet ──
        for section_label, buckets in BS_OUTPUT_ORDER:
            section_yaml = SECTION_TO_YAML[section_label]
            for bucket_key, display in buckets:
                br = report.bs_buckets[(section_yaml, bucket_key)]
                w.writerow([section_label, display,
                             f"{br.cy:.2f}", f"{br.py:.2f}"])

            # Section totals
            if section_label == "Equity / Networth":
                w.writerow([section_label, "TOTAL NETWORTH",
                             f"{report.total_networth_cy:.2f}",
                             f"{report.total_networth_py:.2f}"])
            elif section_label == "Current Liabilities":
                w.writerow([section_label, "TOTAL CURRENT LIABILITIES",
                             f"{report.total_cl_cy:.2f}",
                             f"{report.total_cl_py:.2f}"])
            elif section_label == "Non-Current Liabilities":
                w.writerow([section_label, "TOTAL NON-CURRENT LIABILITIES",
                             f"{report.total_ncl_cy:.2f}",
                             f"{report.total_ncl_py:.2f}"])
            elif section_label == "Current Assets":
                w.writerow([section_label, "TOTAL CURRENT ASSETS",
                             f"{report.total_ca_cy:.2f}",
                             f"{report.total_ca_py:.2f}"])
            elif section_label == "Non-Current Assets":
                w.writerow([section_label, "TOTAL NON-CURRENT ASSETS",
                             f"{report.total_nca_cy:.2f}",
                             f"{report.total_nca_py:.2f}"])

        # Grand totals
        w.writerow(["Totals", "TOTAL LIABILITIES (NW + CL + NCL)",
                     f"{report.total_liabilities_cy:.2f}",
                     f"{report.total_liabilities_py:.2f}"])
        w.writerow(["Totals", "TOTAL ASSETS (CA + NCA)",
                     f"{report.total_assets_cy:.2f}",
                     f"{report.total_assets_py:.2f}"])
        w.writerow(["Totals", "DIFFERENCE (L - A)",
                     f"{report.difference_cy:.2f}",
                     f"{report.difference_py:.2f}"])
        w.writerow(["Totals", "BALANCED",
                     "YES" if report.balanced_cy else "NO",
                     "YES" if report.balanced_py else "NO"])

        # ── P&L ──
        w.writerow([])
        for bucket_key, display in PL_OUTPUT_ORDER:
            br = report.pl_buckets[bucket_key]
            w.writerow(["P&L", display, f"{br.cy:.2f}", f"{br.py:.2f}"])


def _serialize_diagnostics(report: AggregatedReport,
                            hits: List[PageHit],
                            timing: Dict[str, float]) -> dict:
    """Build a JSON-serializable diagnostic dict."""
    return {
        "pages_located": [
            {"page": h.page_idx + 1, "kind": h.kind, "variant": h.variant,
             "layer": h.layer, "headline": h.headline}
            for h in hits
        ],
        "timing_seconds": timing,
        "totals": {
            "total_networth":     [report.total_networth_cy, report.total_networth_py],
            "total_cl":           [report.total_cl_cy,       report.total_cl_py],
            "total_ncl":          [report.total_ncl_cy,      report.total_ncl_py],
            "total_liabilities":  [report.total_liabilities_cy, report.total_liabilities_py],
            "total_ca":           [report.total_ca_cy,       report.total_ca_py],
            "total_nca":          [report.total_nca_cy,      report.total_nca_py],
            "total_assets":       [report.total_assets_cy,   report.total_assets_py],
            "difference":         [report.difference_cy,     report.difference_py],
        },
        "balance_check": {
            "tolerance":    report.tolerance,
            "balanced_cy":  report.balanced_cy,
            "balanced_py":  report.balanced_py,
        },
        "unclassified_count": len(report.unclassified),
        "unclassified": [
            {"label": t.label, "section": t.section, "side": t.side,
             "cy": t.cy, "py": t.py, "norm": ""}
            for t in report.unclassified[:200]   # cap to avoid huge JSON
        ],
        "buckets": {
            "bs": [
                {"section": br.section, "bucket": br.bucket, "display": br.display,
                 "cy": br.cy, "py": br.py,
                 "trace_count": len(br.trace),
                 "trace": [
                     {"label": t.label, "cy": t.cy, "py": t.py,
                      "sign": t.sign, "method": t.method, "score": t.score}
                     for t in br.trace
                 ]}
                for br in report.bs_buckets.values()
            ],
            "pl": [
                {"section": br.section, "bucket": br.bucket, "display": br.display,
                 "cy": br.cy, "py": br.py,
                 "trace_count": len(br.trace),
                 "trace": [
                     {"label": t.label, "cy": t.cy, "py": t.py,
                      "sign": t.sign, "method": t.method, "score": t.score}
                     for t in br.trace
                 ]}
                for br in report.pl_buckets.values()
            ],
        },
    }


# ─── Public pipeline entry point ──────────────────────────────────────────

def run_pipeline(pdf_path: str, output_dir: Path,
                  standalone_only: bool = True,
                  tolerance: float = 1.0,
                  mapping: Optional[CompiledMapping] = None) -> dict:
    """Run the full pipeline on one PDF.

    Returns: dict of output filenames + diagnostics summary.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping = mapping or load_mapping()

    timing = {}

    # 1. Locate
    t = time.time()
    try:
        hits = find_bs_pl_pages(pdf_path, standalone_only=standalone_only)
    except fitz.FileDataError as e:
        # Corrupted, empty, or non-PDF file — return a clean error instead
        # of letting the bare exception bubble up to Flask.
        return {
            "ok": False,
            "error": f"Could not open PDF (file may be corrupted or empty): {e}",
            "diagnostics": {"pages_located": [], "timing_seconds": {}},
        }
    except RuntimeError as e:
        # PyMuPDF can also raise generic RuntimeError on malformed input
        return {
            "ok": False,
            "error": f"PDF could not be parsed: {e}",
            "diagnostics": {"pages_located": [], "timing_seconds": {}},
        }
    timing["locate"] = round(time.time() - t, 3)

    if not hits:
        return {
            "ok": False,
            "error": "No BS/PL pages located in the PDF",
            "diagnostics": {"pages_located": [], "timing_seconds": timing},
        }

    # 2. Extract geometry per page
    t = time.time()
    doc = fitz.open(pdf_path)
    per_page = []      # list of (page_idx, kind, variant, ExtractionResult)
    bs_rows: List[Row] = []
    pl_rows: List[Row] = []

    for hit in hits:
        page = doc[hit.page_idx]
        result = extract_page(page)
        per_page.append((hit.page_idx, hit.kind, hit.variant, result))

        # Flatten side-by-side results into rows assigned to BS or PL.
        # If the locator says this page is BS but geometry split it into
        # two halves (Kalyani A3 spread = BS|PL), we keep the LEFT half as
        # BS and treat the RIGHT half as PL.
        if result.side_by_side:
            left_rows  = result.sub_extractions[0].rows
            right_rows = result.sub_extractions[1].rows
            if hit.kind == "BS":
                bs_rows.extend(left_rows)
                pl_rows.extend(right_rows)
            else:
                pl_rows.extend(left_rows)
                bs_rows.extend(right_rows)
        else:
            if hit.kind == "BS":
                bs_rows.extend(result.rows)
            else:
                pl_rows.extend(result.rows)
    doc.close()
    timing["extract"] = round(time.time() - t, 3)

    # 3. Trim PDF
    pdf_out = output_dir / "source_BS_PL.pdf"
    _write_trimmed_pdf(pdf_path, [h.page_idx for h in hits], pdf_out)

    # 4. Raw rows CSV
    raw_csv = output_dir / "raw_rows.csv"
    _write_raw_csv(per_page, raw_csv)

    # 5. Classify + aggregate
    t = time.time()
    bs_report = aggregate(bs_rows, "BS", mapping)
    pl_report = aggregate(pl_rows, "PL", mapping)
    report = merge(bs_report, pl_report, tolerance=tolerance)
    timing["classify_aggregate"] = round(time.time() - t, 3)

    # 6. Template CSV
    template_csv = output_dir / "template.csv"
    _write_template_csv(report, template_csv)

    # 7. Diagnostics JSON
    diag = _serialize_diagnostics(report, hits, timing)
    (output_dir / "diagnostics.json").write_text(
        json.dumps(diag, indent=2, default=str), encoding="utf-8"
    )

    return {
        "ok": True,
        "files": {
            "trimmed_pdf": str(pdf_out),
            "raw_csv":     str(raw_csv),
            "template_csv": str(template_csv),
            "diagnostics": str(output_dir / "diagnostics.json"),
        },
        "diagnostics": diag,
        "report": report,
    }
