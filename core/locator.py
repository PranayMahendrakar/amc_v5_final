"""
core/locator.py — find BS/PL pages in any Indian annual report.

Two-layer detection (deterministic, no LLM):

  Layer 1 — Canonical headline
    Match "Balance Sheet as at ..." or "Statement of Profit and Loss ..."
    in a headline-sized span (font_size ≥ median + 1.5pt) in the top 18%
    of the page. Reject blacklist matches: notes-to-FS, operating results,
    key ratios, cash flow, SOCE.

  Layer 2 — Structural fingerprint
    Catches headerless pages (Macobs p67 has no "Balance Sheet" headline).
    A page is a BS if it has:
      - "PARTICULARS" or "Particulars" header near the top
      - Column headers matching "As at <date>" twice
      - Body anchors: "EQUITY AND LIABILITIES" or "ASSETS"
      - 2+ numeric column clusters
    A page is a PL if it has:
      - "Particulars" header near the top
      - Column headers matching "For the year ended" twice (or "Year Ended")
      - Body anchors: "Revenue from Operations" / "Total Income" / "Profit before tax"
      - 2+ numeric column clusters

  Layer 3 — Continuation glue
    After Layer 1+2 produce candidates, scan +1/+2 pages forward for
    multi-page BS continuations (Bhilwara-style).

  Variant detection
    "Standalone" / "Consolidated" keyword in headline OR on a preceding
    section-divider page (some reports declare it once for a block).
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Set
import fitz


# ─── Canonical phrase patterns (Layer 1) ──────────────────────────────────
#
# We match these phrases anywhere in the top band of the page, NOT anchored
# at line start. Many publishers prefix the title with a page number ("78
# BALANCE SHEET AS AT…", MRF) or company short-code ("MIPL BALANCE SHEET
# AS AT…", Madhav Infra). The `^` anchor was rejecting those.
#
# To prevent false positives on body-text mentions like "as discussed in the
# balance sheet, the company…":
#   - BS title MUST be followed by "as at" or end-of-line. So
#     "BALANCE SHEET DATE" (Kalyani Directors' Report) still doesn't match.
#   - PL title is a fixed phrase ("Statement of Profit and Loss" / "Profit
#     and Loss Account") that rarely appears in flowing prose.

# The trailing context is REQUIRED, not optional. Body-text prose often
# ends a line with "balance sheet" or "statement of profit and loss" and
# would falsely match a $ (end-of-line) alternative under re.MULTILINE.
# Real titles always include the temporal qualifier on the same line or
# the immediately following line, separated only by whitespace.
BS_CANON_PATTERNS = [
    # Standard order: "Balance Sheet as at 31 March 2025"
    r"\b(standalone\s+|consolidated\s+)?balance\s+sheet\b\s*"
    r"\b(as\s+at|as\s+on)\b",
    # Reverse order: "As at 31 March 2016 ... Balance Sheet" (Bajaj Finserv
    # 2016). Requires a 4-digit year between the two phrases — real headers
    # always include the year, body-text mentions like "as on the balance
    # sheet date" don't, so this rejects accounting-policy body prose.
    # Non-greedy and bounded distance to prevent runaway matches.
    r"\b(as\s+at|as\s+on)\b[\s\S]{0,80}?\b(19|20)\d{2}\b[\s\S]{0,80}?"
    r"\b(standalone\s+|consolidated\s+)?balance\s+sheet\b",
]
PL_CANON_PATTERNS = [
    r"\b(standalone\s+|consolidated\s+)?statement\s+of\s+profit\s+(and|&)\s+loss\b\s*"
    r"\bfor\s+the\s+(year|period|quarter|half[\s-]?year)\s+ended\b",
    r"\b(standalone\s+|consolidated\s+)?profit\s+and\s+loss\s+account\b\s*"
    r"\bfor\s+the\s+(year|period|quarter|half[\s-]?year)\s+ended\b",
]
BLACKLIST_PATTERNS = [
    r"\bnotes?\s+to\s+(the\s+)?(standalone\s+|consolidated\s+)?financial",
    r"\boperating\s+results\b",
    r"\bfinancial\s+highlights\b",
    r"\bkey\s+financial\s+ratios?\b",
    r"\bcash\s+flow\s+statement\b",
    r"\bstatement\s+of\s+cash\s+flows?\b",
    r"\bcash\s+flow\s+from\s+operating\s+activities\b",
    r"\bstatement\s+of\s+changes\s+in\s+equity\b",
    r"\bnote\s+\d+\s*[:.]",                   # "Note 25:" / "Note 25." — schedules
    r"\bnote\s+\d+[a-z]?\s*\(?contd",          # "Note 28 (contd.)"
    r"\bnote\s*[–—-]\s*\d+\b",                 # "NOTE – 3 LOANS AND ADVANCES" (Regency NBFC)
    r"\bnote\s+\d+\b.*balance\s+sheet",
    r"\(contd\.?\.?\)",                        # "(Contd..)" anywhere — pages with this
                                                # are continuations of Notes schedules,
                                                # never fresh BS/PL statements
    r"^\s*annexure\b",
    r"\bcwip\s+ageing",
    r"\btrade\s+payable\s+ageing",
    r"\btrade\s+receivable\s+ageing",
    r"\bfair\s+value\s+(measurement|hierarchy)",
    r"\bfinancial\s+risk\s+management\b",
    r"\bcontingent\s+liabilities",
    r"\brelated\s+part(y|ies)\b",
    r"\bsegment\s+(report|information)",
    r"\bearnings\s+per\s+share\s+\(eps\)\s*:",   # EPS detail schedules
    # Directors' Report financial-performance summary — typically a 4-column
    # standalone+consolidated table near the start of the report. Looks
    # structurally identical to a real PL (Particulars + Year-ended × 4 +
    # Revenue from Operations + Profit before tax + Total Expenses), but is
    # NOT the audited financial statement. Akiko p.26 was the test case.
    r"\bdirector(s|s'|s’)?\s+report\b",
    r"\babstract\s+of\b.*financial\s+statement",
    r"\bextract\s+of\s+financial",
]
CONTINUATION_PATTERN = r"\(contd\.?\)?"

BS_RE = re.compile("|".join(BS_CANON_PATTERNS), re.I | re.M)
PL_RE = re.compile("|".join(PL_CANON_PATTERNS), re.I | re.M)
BLACK_RE = re.compile("|".join(BLACKLIST_PATTERNS), re.I)
CONT_RE = re.compile(CONTINUATION_PATTERN, re.I)

# Structural fingerprint patterns (Layer 2) — STRICT mode
# We require the column-header "as at"/"as on" twice AND both BS body
# anchors (equity-and-liabilities AND assets). This rejects Notes-to-FS
# schedules which typically have "Particulars" + "As at" but only ONE
# side of the BS.
BS_AS_AT_RE       = re.compile(r"\b(as\s+at|as\s+on)\b", re.I)
PL_YEAR_ENDED_RE  = re.compile(r"\b(for\s+the\s+year\s+ended|year\s+ended|"
                                r"for\s+the\s+period\s+ended)\b", re.I)
BS_EQUITY_RE      = re.compile(r"equity\s+and\s+liabilities", re.I)
BS_ASSETS_RE      = re.compile(
    r"(^|\n)\s*([a-z]\.?\s+|\([a-z0-9ivx]+\)\s+|non-?\s*current\s+)?assets\s*$",
    re.I | re.M)
PL_REVENUE_RE     = re.compile(r"revenue\s+from\s+operations?", re.I)
PL_PBT_RE         = re.compile(r"(profit\s+(before|for)\s+(the\s+year|the\s+period|tax)|"
                                r"total\s+(expenses?|comprehensive\s+income))", re.I)

# Variant
STANDALONE_RE   = re.compile(r"\bstandalone\b", re.I)
CONSOLIDATED_RE = re.compile(r"\bconsolidated\b", re.I)

NUMBER_RE = re.compile(r"^\(?-?[\d,]+\.?\d{0,4}\)?$")


@dataclass
class PageHit:
    page_idx: int                  # 0-indexed
    kind: str                      # "BS" | "PL"
    variant: str                   # "standalone" | "consolidated" | "default"
    layer: str                     # "canonical" | "structural"
    continuation_of: Optional[int] = None
    headline: str = ""

    @property
    def label(self) -> str:
        v = "" if self.variant == "default" else f"{self.variant}_"
        return f"{v}{self.kind.lower()}_p{self.page_idx + 1}"


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFKD", s)


def _headline_spans(page: fitz.Page, top_frac: float = 0.18) -> List[tuple]:
    """Return (size, text, y) for spans in the top band, larger than body
    median + 1.5pt. These are the candidate headline spans."""
    h = page.rect.height
    blocks = page.get_text("dict")["blocks"]
    sizes = []
    candidates = []
    for b in blocks:
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                sizes.append(s["size"])
                if s["bbox"][1] < h * top_frac:
                    candidates.append((s["size"], _normalize(s["text"]).strip(),
                                       s["bbox"][1]))
    if not sizes:
        return []
    sizes.sort()
    median = sizes[len(sizes) // 2]
    threshold = median + 1.5
    return [(sz, t, y) for sz, t, y in candidates if sz >= threshold and t]


def _full_top_text(page: fitz.Page, top_frac: float = 0.18) -> str:
    """All text in top band, regardless of size — used for blacklist + structural."""
    h = page.rect.height
    clip = fitz.Rect(0, 0, page.rect.width, h * top_frac)
    return _normalize(page.get_text("text", clip=clip) or "")


def _full_text(page: fitz.Page) -> str:
    return _normalize(page.get_text("text") or "")


def _numeric_columns_present(page: fitz.Page, min_clusters: int = 2,
                              min_count_per_cluster: int = 5) -> bool:
    """Quick check: does the page have >= min_clusters right-aligned numeric
    columns? Used to filter out pure-text pages from structural detection."""
    blocks = page.get_text("dict")["blocks"]
    edges = []
    for b in blocks:
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                t = _normalize(s["text"]).strip()
                if NUMBER_RE.match(t) and len(t) >= 2:
                    edges.append(round(s["bbox"][2]))
    edges.sort()
    groups = []
    for x in edges:
        if groups and abs(x - groups[-1][-1]) < 8:
            groups[-1].append(x)
        else:
            groups.append([x])
    return sum(1 for g in groups if len(g) >= min_count_per_cluster) >= min_clusters


def _detect_variant(page: fitz.Page, declared_variants: List[str]) -> str:
    """Find 'Standalone' / 'Consolidated' on the page, fallback to last
    declared variant from a prior section-divider page."""
    top = _full_top_text(page, top_frac=0.30)
    if STANDALONE_RE.search(top):
        return "standalone"
    if CONSOLIDATED_RE.search(top):
        return "consolidated"
    if declared_variants:
        return declared_variants[-1]
    return "default"


def _scan_section_divider(page: fitz.Page) -> Optional[str]:
    """Detect a section-divider page that DECLARES the variant for following
    pages. Examples:
      - A cover page reading 'STANDALONE FINANCIAL STATEMENTS' in a giant
        headline with little other content
      - A divider page with JUST 'Standalone' or 'Consolidated' as the lone
        big headline (Bajaj Finserv 2015-16 uses this pattern between its
        consolidated and standalone sections)

    Note: cannot use _headline_spans here. On very sparse divider pages,
    every span has the same size, so the median-based threshold filters
    out the headline itself. We use an absolute size threshold instead.
    """
    h = page.rect.height
    big_spans: List[tuple] = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                if (s["size"] >= 18
                        and s["bbox"][1] < h * 0.7):
                    big_spans.append((s["size"], _normalize(s["text"]).strip()))
    if not big_spans:
        return None
    combined = " ".join(t for _, t in big_spans if t).lower()
    # Pattern A: "Standalone Financial Statements" / "Consolidated Financial Statements"
    if "financial statement" in combined or "financials" in combined.split():
        if "standalone" in combined:
            return "standalone"
        if "consolidated" in combined:
            return "consolidated"
    # Pattern B: very sparse page whose entire big-text content is just
    # "Standalone" or "Consolidated" — Bajaj 2016 between section blocks.
    tokens = [tok for tok in combined.split() if len(tok) > 1]
    if len(tokens) <= 5:
        if "standalone" in tokens:
            return "standalone"
        if "consolidated" in tokens:
            return "consolidated"
    return None


def _classify_page(page: fitz.Page,
                    declared_variants: List[str]) -> Optional[PageHit]:
    """Single-page classification. Returns a PageHit or None."""
    page_idx = page.number

    # ── Layer 1: Canonical phrase ──
    # Match canonical phrases in the FULL top band, not only in
    # headline-sized spans. Several publishers (Akiko, Madhav Infra, MRF)
    # render the BS/PL title at the same font size as body text — no
    # visual emphasis. The previous headline-spans-only filter excluded
    # those pages entirely; the blacklist + strict trailing context
    # (BS requires "as at" after) keeps the precision high.
    full_top = _full_top_text(page)
    full_body = _full_text(page)

    # Blacklist check on FULL page text (not just top 18%) — some Notes
    # continuation pages (e.g. AR_2024 PPE schedule p.361) place "(Contd..)"
    # below the top band, and we'd otherwise miss the rejection signal.
    if BLACK_RE.search(full_body):
        return None

    bs_match = BS_RE.search(full_top)
    pl_match = PL_RE.search(full_top)

    if bs_match:
        return PageHit(
            page_idx=page_idx, kind="BS",
            variant=_detect_variant(page, declared_variants),
            layer="canonical",
            headline=bs_match.group(0)[:80],
        )
    if pl_match:
        return PageHit(
            page_idx=page_idx, kind="PL",
            variant=_detect_variant(page, declared_variants),
            layer="canonical",
            headline=pl_match.group(0)[:80],
        )

    # ── Layer 2: Structural fingerprint (STRICT) ──
    # Reject pages that don't have a financial-statement-density of numbers
    if not _numeric_columns_present(page, min_clusters=2, min_count_per_cluster=10):
        return None
    body = _full_text(page)
    body_lc = body.lower()

    has_particulars = "particulars" in body_lc

    # BS column-header pair signal: prefer "As at" × 2. Fall back to
    # "For the year ended" × 2 (NBFC convention, e.g. Regency Fincorp).
    bs_as_at_count   = len(BS_AS_AT_RE.findall(body))
    has_equity_liab  = bool(BS_EQUITY_RE.search(body))
    has_assets_sec   = bool(BS_ASSETS_RE.search(body))

    # PL strict: 2+ "For the year ended" + Revenue anchor + Profit/Total anchor
    pl_year_count    = len(PL_YEAR_ENDED_RE.findall(body))
    has_revenue      = bool(PL_REVENUE_RE.search(body))
    has_pbt          = bool(PL_PBT_RE.search(body))
    is_pl_shaped     = has_revenue and has_pbt

    # Standard BS: 2+ "As at" + Equity AND Assets sections
    if (has_particulars and bs_as_at_count >= 2
            and has_equity_liab and has_assets_sec
            and not is_pl_shaped):
        return PageHit(
            page_idx=page_idx, kind="BS",
            variant=_detect_variant(page, declared_variants),
            layer="structural",
            headline="(headerless BS — structural match)",
        )
    # NBFC BS: 2+ "For the year ended" column headers (yes, NBFC reports
    # use this for both BS and PL), at least ONE of equity/assets section,
    # and crucially NOT shaped like a PL (no Revenue+PBT combo). Regency
    # Fincorp p.191 is the test case — its BS starts with "A. ASSETS",
    # has "For the year ended 31.03.2025" as column header, and lacks the
    # PL signature.
    if (has_particulars and pl_year_count >= 2
            and (has_equity_liab or has_assets_sec)
            and not is_pl_shaped):
        return PageHit(
            page_idx=page_idx, kind="BS",
            variant=_detect_variant(page, declared_variants),
            layer="structural",
            headline="(NBFC-style BS — structural match)",
        )
    # Standard PL: 2+ "For the year ended" + Revenue anchor + Profit/Total anchor
    if has_particulars and pl_year_count >= 2 and is_pl_shaped:
        return PageHit(
            page_idx=page_idx, kind="PL",
            variant=_detect_variant(page, declared_variants),
            layer="structural",
            headline="(headerless PL — structural match)",
        )

    return None


def find_bs_pl_pages(pdf_path: str,
                      standalone_only: bool = True) -> List[PageHit]:
    """Locate every BS and PL page in the report.

    Args:
        pdf_path: path to the PDF.
        standalone_only: if True (per the AskMyCFO spec), drop consolidated
            variant pages — they aren't used for the template.

    Returns: list of PageHit, sorted by page index.
    """
    doc = fitz.open(pdf_path)
    hits: List[PageHit] = []
    declared_variants: List[str] = []   # running list from section-divider pages

    for page in doc:
        # Track section-divider declarations for variant inheritance
        decl = _scan_section_divider(page)
        if decl:
            declared_variants.append(decl)

        hit = _classify_page(page, declared_variants)
        if hit:
            hits.append(hit)

    # ── Layer 3: Continuation detection ──
    # Some publishers split the BS across two consecutive pages:
    #   - page N: Assets + Equity (no Total Equity and Liabilities at bottom)
    #   - page N+1: Current/Non-Current Liabilities + Total E+L (no canonical
    #     "Balance Sheet" headline)
    # Shalon Silks is a clean example. Without continuation glue, only page N
    # is captured and the Liabilities side comes back as zero, breaking
    # the balance check by exactly the missing Total CL or NCL.
    #
    # A page is a BS continuation if it satisfies ALL of:
    #   1. It immediately follows a BS hit (same kind & variant)
    #   2. It does NOT already match a canonical pattern of its own
    #   3. It has the canonical end-of-BS marker text:
    #        "total equity and liabilities" / "total liabilities and equity"
    #      OR has a one-side BS structural signature (current/non-current
    #         liabilities + numeric columns), confirming it's the rest of
    #         the same Balance Sheet.
    #   4. It is not on the blacklist (Notes/Director's report etc.)
    # Real BS continuations end at the canonical totals:
    #   "Total Equity and Liabilities" — most common
    #   "Total Liabilities and Equity" — alternate ordering
    #   "Total Liabilities"             — NBFC convention (Regency Fincorp)
    BS_CONT_TOTAL_RE = re.compile(
        r"\btotal\s+(equity\s+and\s+liabilities?|"
        r"liabilities?\s+and\s+equity|liabilities?)\b",
        re.I,
    )
    BS_CONT_SECTION_RE = re.compile(
        r"\b(current\s+liabilities|non[-\s]?current\s+liabilities|"
        r"current\s+assets|non[-\s]?current\s+assets)\b", re.I)

    located_idxs: Set[int] = set(h.page_idx for h in hits)
    continuation_hits: List[PageHit] = []
    for h in list(hits):
        if h.kind != "BS":
            continue
        # Look at next 1-2 pages
        for delta in (1, 2):
            cont_idx = h.page_idx + delta
            if cont_idx >= len(doc) or cont_idx in located_idxs:
                continue
            cont_page = doc[cont_idx]
            cont_top = _full_top_text(cont_page, top_frac=0.4)
            cont_full = _full_text(cont_page)
            # Skip if blacklisted (Notes / Cash flow / Director's report)
            if BLACK_RE.search(cont_top) or BLACK_RE.search(cont_full):
                break
            # Skip if it has its own canonical title (would have already been a hit)
            if BS_RE.search(cont_top) or PL_RE.search(cont_top):
                break
            # Reject if the page is clearly a PL or Cash Flow Statement —
            # these often come right after the BS and would otherwise satisfy
            # the loose "has section keyword + numbers" signal.
            cont_is_pl  = (PL_YEAR_ENDED_RE.search(cont_full)
                           and PL_REVENUE_RE.search(cont_full))
            cont_is_cfs = ("cash flow" in cont_full.lower()
                           and "operating activities" in cont_full.lower())
            if cont_is_pl or cont_is_cfs:
                break
            # A real BS continuation has the explicit terminator
            # "Total Equity and Liabilities" / "Total Liabilities and Equity"
            # on this very page. Section keywords alone are too weak (PL/CFS
            # pages mention "current" too).
            has_term = bool(BS_CONT_TOTAL_RE.search(cont_full))
            has_nums = _numeric_columns_present(cont_page, min_clusters=2,
                                                  min_count_per_cluster=6)
            if has_nums and has_term:
                continuation_hits.append(PageHit(
                    page_idx=cont_idx, kind="BS",
                    variant=h.variant,
                    layer="continuation",
                    continuation_of=h.page_idx,
                    headline="(BS continuation page)",
                ))
                located_idxs.add(cont_idx)
                break

    hits.extend(continuation_hits)
    hits.sort(key=lambda h: h.page_idx)

    # Tag (contd.) pages already in the canonical list (Bhilwara-style)
    for i, h in enumerate(hits):
        if h.continuation_of is not None:
            continue
        text = _full_top_text(doc[h.page_idx])
        if CONT_RE.search(text) and i > 0:
            for prev in reversed(hits[:i]):
                if prev.kind == h.kind and prev.variant == h.variant:
                    h.continuation_of = prev.page_idx
                    break

    doc.close()

    if standalone_only:
        hits = [h for h in hits if h.variant != "consolidated"]

    return hits


def summarize_hits(hits: List[PageHit]) -> str:
    """Pretty-print for logs / debugging."""
    if not hits:
        return "  (no BS/PL pages located)"
    lines = []
    for h in hits:
        cont = f" (cont. of p{h.continuation_of + 1})" if h.continuation_of else ""
        lines.append(
            f"  p{h.page_idx + 1:>3}  {h.kind:<2}  "
            f"{h.variant:<12}  [{h.layer:<10}]  "
            f"{h.headline}{cont}"
        )
    return "\n".join(lines)
