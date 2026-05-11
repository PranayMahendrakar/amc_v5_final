"""
core/geometry.py — layout-invariant geometric row extraction.

Approach:
  1. Get every text span from PyMuPDF: (text, x0, y0, x1, y1, font_size)
  2. Numeric tokens (right-aligned in financial statements) form X-clusters
     on their right-edge. DBSCAN-style proximity grouping. Each cluster = one
     value column.
  3. If we detect 4+ clusters with a wide horizontal gap, the page is
     side-by-side — split it in half at the midpoint and recurse on each half.
  4. Y-cluster ALL spans into row bands. For each row:
       label  = spans NOT in any numeric cluster, joined by x0 order
       values = one float per numeric cluster (0.0 if absent)
  5. Wrap-continuation: a row with empty label but non-zero values is merged
     into the previous row's label (PDF text wraps).
  6. Section headers: a row with only label text and no values (and few words)
     becomes the running "section" context attached to subsequent rows.

No LLM. No table detection. No grid lines required. Works on PDFs with rules,
without rules, with merged cells, with side-by-side spreads.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
import fitz


# Numeric token: optional minus or parens, digits with optional Indian/Western
# comma separators, optional decimal up to 4 places. "1,23,45,678.90" works
# (Indian lakh-crore comma format) — pyramid commas are fine because we strip
# them before float-parsing.
NUMBER_RE = re.compile(r"^\(?-?[\d,]+\.?\d{0,4}\)?$")
DASH_AS_ZERO = {"-", "–", "—", "–", "—"}      # em/en dashes used as "zero" placeholder


def normalize_ligatures(s: str) -> str:
    """Collapse PDF ligatures (ﬁ, ﬂ) to plain ASCII. Without this, regex
    like r'profit' won't match 'proﬁt' that PDFs commonly produce."""
    return unicodedata.normalize("NFKD", s)


def parse_number(tok: str) -> Optional[float]:
    """Parse a numeric token to float. Returns None if not a number.
    '(123.45)' → -123.45, '1,234.5' → 1234.5, '-' → 0.0
    """
    t = tok.strip()
    if not t:
        return None
    if t in DASH_AS_ZERO:
        return 0.0
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.startswith("-"):
        neg, t = True, t[1:]
    t = t.replace(",", "").strip()
    if not t:
        return None
    try:
        v = float(t)
        return -v if neg else v
    except ValueError:
        return None


@dataclass
class Span:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


def split_multi_number_span(span: Span) -> List[Span]:
    """Split spans where PyMuPDF merged multiple visual numbers into one
    text run, e.g. '15,88,770.64  11,50,952.16' or 'TOTAL 30,72,114.43 27,88,831.05'.

    Without this, NUMBER_RE rejects the multi-token string and the values
    silently fall into label_parts — losing every subtotal row whose numbers
    were rendered as one span.

    Sub-spans inherit y-bounds and get x-bounds proportional to character
    offsets within the parent's bbox (mono-spaced approximation). That's
    accurate enough for column assignment because right-edge alignment is
    what matters and adjacent characters compose linearly.
    """
    txt = span.text
    if " " not in txt and " " not in txt:
        return [span]
    tokens = [t for t in re.split(r"[\s ]+", txt) if t]
    if len(tokens) < 2:
        return [span]
    # Only split if at least one token looks numeric — avoids fragmenting
    # ordinary multi-word labels like "Trade payables".
    if not any(NUMBER_RE.match(t) or t in DASH_AS_ZERO for t in tokens):
        return [span]
    span_w = max(span.x1 - span.x0, 1.0)
    char_w = span_w / max(len(txt), 1)
    out: List[Span] = []
    cursor = 0
    for tok in tokens:
        idx = txt.find(tok, cursor)
        if idx < 0:
            idx = cursor
        sub_x0 = span.x0 + idx * char_w
        sub_x1 = span.x0 + (idx + len(tok)) * char_w
        out.append(Span(tok, sub_x0, span.y0, sub_x1, span.y1, span.size))
        cursor = idx + len(tok)
    return out


def page_spans(page: fitz.Page,
                top_crop: float = 0.06,
                bottom_crop: float = 0.08) -> List[Span]:
    """Pull every text span in the page BODY (drop top/bottom margins).

    Cropping fixes three real problems we observed:
      - Colgate BS top rows polluted with "Corporate Sustainability..." nav bar
      - Colgate BS bottom rows polluted with auditor signatures + DINs
      - Colgate PL falsely flagged as side-by-side because footer DINs
        ("324982E", "118746", "07645510") created phantom right-edge clusters
    """
    h = page.rect.height
    y_min, y_max = h * top_crop, h * (1 - bottom_crop)
    out: List[Span] = []
    blocks = page.get_text("dict")["blocks"]
    for b in blocks:
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                txt = normalize_ligatures(s["text"]).strip()
                if not txt:
                    continue
                x0, y0, x1, y1 = s["bbox"]
                yc = (y0 + y1) / 2
                if yc < y_min or yc > y_max:
                    continue   # crop top/bottom
                out.extend(split_multi_number_span(
                    Span(txt, x0, y0, x1, y1, s["size"])
                ))
    return out


def cluster_1d(values: List[float], eps: float) -> List[List[float]]:
    """Sort values and group consecutive ones within eps. Simple, deterministic,
    no scikit-learn needed."""
    if not values:
        return []
    sv = sorted(values)
    groups: List[List[float]] = [[sv[0]]]
    for v in sv[1:]:
        if v - groups[-1][-1] <= eps:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def numeric_column_clusters(spans: List[Span], eps_x: float = 8.0,
                              min_count: int = 8) -> List[Tuple[float, float]]:
    """Find right-edge X positions of numeric columns.
    Returns list of (x_min, x_max) per cluster, in left-to-right order.
    Only clusters with >= min_count numeric tokens are kept.

    min_count=8: a real financial-statement column always has 15+ values.
    Lower thresholds (4-5) catch spurious sparse clusters like Virinchi's
    section markers '(1)', '(2)', '(3)' (parsed as -1, -2, -3) at far-left,
    which then trip the side-by-side spread detector.
    """
    right_edges = [s.x1 for s in spans if NUMBER_RE.match(s.text) and len(s.text) >= 2]
    groups = cluster_1d(right_edges, eps_x)
    return [(min(g), max(g)) for g in groups if len(g) >= min_count]


def split_side_by_side(page: fitz.Page, spans: List[Span],
                        cols: List[Tuple[float, float]]) -> Optional[float]:
    """If page has a side-by-side spread layout, return the midpoint of the
    inter-page gap. Otherwise return None.

    Side-by-side spreads happen when a publisher fits two logical pages onto
    one physical PDF page — almost always an A3 landscape printout
    (~1190×842pt). The pages share a centerline; columns on each side mirror.

    Detection requires THREE things:
      1. LANDSCAPE orientation. A portrait page is, by physical necessity,
         not a two-page spread — it would only fit one logical page. Some
         publishers (Virinchi, Shalon, half the NBFC reports) use a wide
         gap between the LABEL column and the NUMERIC columns on a single
         portrait page; without this gate, the splitter cuts the page in
         half and produces a LEFT half of labels-only + a RIGHT half of
         numbers-with-empty-labels, destroying every row.
      2. 4+ numeric clusters. Each side of a true spread has its own
         Note/CY/PY columns, so a spread has 4 minimum (often 6).
      3. Centroid gap > 25% of page width. Kalyani A3 spread = 37%; the
         Colgate PL OCI sub-amount layout (a single page) = 14.5%.

    Empirical: with portrait gating, Virinchi (595×842) and Shalon (612×810)
    correctly bypass; Kalyani (1190×842 landscape A3) still triggers.
    """
    # Gate 1: landscape orientation
    if page.rect.width <= page.rect.height:
        return None
    # Gate 2: column count
    if len(cols) < 4:
        return None
    # Gate 3: gap threshold
    centroids = sorted([(a + b) / 2 for a, b in cols])
    page_w = page.rect.width
    for i in range(1, len(centroids)):
        gap = centroids[i] - centroids[i - 1]
        if gap > page_w * 0.25:
            return (centroids[i - 1] + centroids[i]) / 2
    return None


def split_spans_by_x(spans: List[Span], split_x: float
                     ) -> Tuple[List[Span], List[Span]]:
    """Partition spans into left-of-split and right-of-split."""
    left = [s for s in spans if s.xc < split_x]
    right = [s for s in spans if s.xc >= split_x]
    return left, right


@dataclass
class Row:
    section: str
    label: str
    values: List[Optional[float]]   # one per numeric column, None if absent
    y: float                         # for ordering / debugging


def extract_rows(spans: List[Span],
                 cols: List[Tuple[float, float]],
                 row_eps_y: float = 4.0) -> List[Row]:
    """Group spans into rows by Y centroid, then assign each row's tokens
    to label vs value-columns.

    Wrap-merge: if a row has empty label but non-empty values, OR has only
    a label that ends in a wrap-suffix word, fold it into the previous row.

    Section context: a row with label-only and a short, capitalized form
    becomes the running section header attached to subsequent rows.
    """
    if not spans:
        return []

    # Y-cluster
    ys = sorted(set(round(s.yc, 1) for s in spans))
    bands: List[List[float]] = [[ys[0]]]
    for y in ys[1:]:
        if y - bands[-1][-1] <= row_eps_y:
            bands[-1].append(y)
        else:
            bands.append([y])
    band_ranges = [(min(b) - row_eps_y, max(b) + row_eps_y) for b in bands]

    rows: List[Row] = []
    current_section = ""

    for y_lo, y_hi in band_ranges:
        row_spans = [s for s in spans if y_lo <= s.yc <= y_hi]
        if not row_spans:
            continue
        row_spans.sort(key=lambda s: s.x0)

        # Bucket spans into label vs each column
        label_parts: List[str] = []
        col_values: List[Optional[float]] = [None] * len(cols)
        for s in row_spans:
            assigned = False
            for i, (cx0, cx1) in enumerate(cols):
                # Numeric token's right-edge falls in this column
                if cx0 - 4 <= s.x1 <= cx1 + 4 and (
                        NUMBER_RE.match(s.text) or s.text in DASH_AS_ZERO):
                    v = parse_number(s.text)
                    if v is not None:
                        col_values[i] = v
                        assigned = True
                        break
            if not assigned:
                # Skip note-number-like tokens that happen to sit between cols
                # (a bare 1-2 digit integer not aligned to any column = note ref)
                if NUMBER_RE.match(s.text) and len(s.text) <= 3:
                    continue
                label_parts.append(s.text)

        label = " ".join(label_parts).strip()
        # Drop pure-noise rows
        if not label and all(v is None for v in col_values):
            continue

        # Section header: label only, no values, looks like a header
        # CRITICAL: labels beginning with "Total" / "Sub-total" / "Subtotal"
        # / "TOTAL" are subtotal data rows, NEVER section headers — even if
        # they happen to contain words like "Non-Current" or "Current".
        # Without this guard, "Total Non-Current Assets" would be swallowed
        # as a new section header and its values would orphan.
        label_lc = label.lower()
        is_total_label = (label_lc.startswith("total")
                          or label_lc.startswith("sub-total")
                          or label_lc.startswith("subtotal")
                          or label_lc.startswith("grand total"))
        is_section = (label and all(v is None for v in col_values)
                       and len(label.split()) <= 6
                       and any(c.isalpha() for c in label)
                       and not is_total_label)

        if is_section:
            # Heuristic: short, mostly-uppercase OR contains classic anchors.
            # Anchor match is case-insensitive — Cholamandalam BS uses
            # "Non-current assets" (lowercase 'c'), and the previous case-
            # sensitive check missed it, leaving every NCA row mislabeled
            # with the parent "ASSETS" section. That broke section_required
            # disambiguation in the classifier and routed non-current
            # receivables into current_assets.
            anchors = ("assets", "equity and liabilities", "liabilities",
                        "non-current", "non current", "current",
                        "income", "expenses", "shareholders")
            ll = label.lower()
            if (label.isupper() or any(a in ll for a in anchors)):
                current_section = label
                continue
            # Else treat as a regular label-only row (e.g., a subtotal row
            # that we don't yet have values for; might be wrap continuation)

        # Wrap-merge: a row with no label but with values is EITHER
        #   (a) a true continuation — the previous row had ONLY a label and
        #       its values wrap onto this line (multi-line label case), OR
        #   (b) an UNLABELED SUBTOTAL ROW — common in Indian Schedule III
        #       statements where sub-section totals appear in a styled band
        #       with no label text (Colgate p202: "Total Non-Current Assets"
        #       renders only as 1,25,584 / 1,29,272 with no label).
        # We must distinguish: if the previous row ALREADY has values, this
        # is case (b) — keep it as its own row with empty label so it's not
        # silently destroyed. Only merge when the previous row has no values.
        if not label:
            if rows and all(v is None for v in rows[-1].values):
                # Previous row is label-only → attach orphan values to it
                for i, v in enumerate(col_values):
                    if v is not None:
                        rows[-1].values[i] = v
                continue
            # Otherwise treat as unlabeled subtotal row — keep it
            if any(v is not None for v in col_values):
                rows.append(Row(section=current_section,
                                  label="(unlabeled subtotal)",
                                  values=col_values,
                                  y=(y_lo + y_hi) / 2))
            continue

        # Wrap-merge: previous row has values but empty/short label and
        # current row has no values → label continuation of previous
        # (we don't apply this aggressively; keep simple for now)

        rows.append(Row(section=current_section, label=label,
                         values=col_values, y=(y_lo + y_hi) / 2))

    return rows


# ─────────────────────────────────────────────────────────────────────
# Public façade
# ─────────────────────────────────────────────────────────────────────

def truncate_at_terminator(rows: List[Row]) -> List[Row]:
    """Indian financial statements have canonical end-of-statement markers.
    Everything after these is sign-off / auditor / signature boilerplate —
    drop it so the CSV is purely financial data.

    BS terminators: 'Total Equity and Liabilities', 'Total Liabilities and Equity'
    PL terminators: 'Total Comprehensive Income for the (year|period)', 'Earnings per Equity Share'
    """
    TERMINATORS = (
        r"total\s+equity\s+and\s+liabilities",
        r"total\s+liabilities\s+and\s+equity",
        r"total\s+comprehensive\s+income\s+for\s+the\s+(year|period)",
        r"earnings\s+per\s+(equity\s+)?share",
        r"^total$",      # Macobs uses bare "TOTAL" as both-side terminator
    )
    pat = re.compile("|".join(TERMINATORS), re.I)
    last_term_idx = -1
    for i, r in enumerate(rows):
        if pat.search(r.label):
            last_term_idx = i
    if last_term_idx >= 0:
        return rows[:last_term_idx + 1]
    return rows


@dataclass
class ExtractionResult:
    rows: List[Row]
    cols: List[Tuple[float, float]]  # column x-ranges
    side_by_side: bool = False
    sub_extractions: List["ExtractionResult"] = field(default_factory=list)


def extract_page(page: fitz.Page) -> ExtractionResult:
    """Run the full extractor on one page. If side-by-side detected,
    recurse on left and right halves split at the PHYSICAL page midpoint."""
    spans = page_spans(page)
    cols = numeric_column_clusters(spans)
    gap_midpoint = split_side_by_side(page, spans, cols)
    if gap_midpoint is not None:
        page_mid = page.rect.width / 2
        left_spans = [s for s in spans if s.x0 < page_mid]
        right_spans = [s for s in spans if s.x0 >= page_mid]
        left_cols = numeric_column_clusters(left_spans)
        right_cols = numeric_column_clusters(right_spans)
        left_rows = truncate_at_terminator(extract_rows(left_spans, left_cols))
        right_rows = truncate_at_terminator(extract_rows(right_spans, right_cols))
        return ExtractionResult(
            rows=[], cols=cols, side_by_side=True,
            sub_extractions=[
                ExtractionResult(rows=left_rows, cols=left_cols),
                ExtractionResult(rows=right_rows, cols=right_cols),
            ],
        )
    rows = truncate_at_terminator(extract_rows(spans, cols))
    return ExtractionResult(rows=rows, cols=cols, side_by_side=False)
