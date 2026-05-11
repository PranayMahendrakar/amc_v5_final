"""
core/classifier.py — assign each extracted row to an output bucket.

Algorithm (deterministic, no LLM):
  1. Normalize the row label (lowercase, strip punctuation, collapse spaces)
  2. Exact lookup in the alias index → (section, bucket)
  3. If the bucket has section_required, disambiguate using the row's
     section context (from the geometry extractor's running section header)
  4. If exact lookup fails, try fuzzy (rapidfuzz token_set_ratio ≥ 88)
  5. If still no match, log to unclassified — do not guess

The alias index is built once at startup from config/mapping_bs.yaml and
config/mapping_pl.yaml.
"""
from __future__ import annotations
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from rapidfuzz import fuzz, process
    HAS_FUZZ = True
except ImportError:
    HAS_FUZZ = False


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# ─── Label normalization ──────────────────────────────────────────────────

# Strip leading bullet markers: "(a)", "(i)", "1.", "a.", "- ", "•"
LEADING_MARKER_RE = re.compile(
    r"^\s*(\([a-z0-9ivx]+\)|[a-z0-9]+\)\s|[a-z0-9]+\.\s|[-•▪◦]\s+|"
    r"[a-z]+\)\s+|i+v?\)\s+)+",
    re.I,
)
# Strip trailing note-number references: "Note 3" / "3(A)" / "31(d)"
TRAILING_NOTE_RE = re.compile(r"\s+(\d{1,3}\s*\([a-z0-9]+\)|note\s+\d+|\d{1,3})\s*$",
                                re.I)
# Strip parenthetical qualifiers but KEEP "(Net)" since it's part of the
# canonical name of "Deferred Tax Assets (Net)" etc.
PRESERVE_PARENS = ("(net)", "(at amortised cost)", "(non-current)", "(current)")
PUNCT_RE = re.compile(r"[,;:]+")
DASH_RE  = re.compile(r"[—–]+")
WHITESPACE_RE = re.compile(r"\s+")


def normalize_label(raw: str) -> str:
    """Canonical form for dictionary matching."""
    if not raw:
        return ""
    s = raw.strip().lower()
    s = DASH_RE.sub("-", s)
    s = LEADING_MARKER_RE.sub("", s)
    s = TRAILING_NOTE_RE.sub("", s)
    s = PUNCT_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s).strip()
    return s


# ─── Section normalization (from geometry) ─────────────────────────────────

def normalize_section(raw: str) -> str:
    """Map the geometry extractor's section header to one of:
       'non_current_assets', 'current_assets', 'non_current_liabilities',
       'current_liabilities', 'equity', 'pl', or '' (unknown).
    """
    if not raw:
        return ""
    s = raw.lower().strip()
    if "non-current" in s or "non current" in s or "non- current" in s:
        if "asset" in s:
            return "non_current_assets"
        if "liabilit" in s:
            return "non_current_liabilities"
    if "current" in s:
        if "asset" in s:
            return "current_assets"
        if "liabilit" in s:
            return "current_liabilities"
    if "equity" in s or "networth" in s or "shareholder" in s:
        return "equity"
    if "income" in s or "expense" in s:
        return "pl"
    return ""


# Section context constraint used by classifier
SECTION_TO_REQUIRED = {
    "non_current_assets":      "non_current",
    "current_assets":          "current",
    "non_current_liabilities": "non_current",
    "current_liabilities":     "current",
}


# ─── Mapping loader ────────────────────────────────────────────────────────

@dataclass
class BucketEntry:
    section_yaml: str           # 'networth' | 'current_liabilities' | ...
    bucket: str                 # 'share_capital' | 'account_payable' | ...
    section_required: Optional[str] = None    # 'current' | 'non_current'
    sign: int = +1


@dataclass
class CompiledMapping:
    # alias → list of candidate (BucketEntry) — multiple if alias is polysemous
    bs_index: Dict[str, List[BucketEntry]] = field(default_factory=dict)
    pl_index: Dict[str, List[BucketEntry]] = field(default_factory=dict)
    bs_buckets: List[Tuple[str, str]] = field(default_factory=list)   # (section, bucket)
    pl_buckets: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def bs_aliases(self) -> List[str]:
        return list(self.bs_index.keys())

    @property
    def pl_aliases(self) -> List[str]:
        return list(self.pl_index.keys())


def _add_aliases(index: Dict[str, List[BucketEntry]],
                  aliases: List[str], entry: BucketEntry):
    for raw in aliases:
        key = normalize_label(raw)
        if not key:
            continue
        index.setdefault(key, []).append(entry)


def load_mapping(config_dir: Path = CONFIG_DIR) -> CompiledMapping:
    """Compile both YAML files into a flat alias → bucket index."""
    cm = CompiledMapping()

    bs_doc = yaml.safe_load((config_dir / "mapping_bs.yaml").read_text())
    for section, buckets in bs_doc.items():
        for bucket, body in buckets.items():
            cm.bs_buckets.append((section, bucket))
            sec_req = body.get("section_required")
            entry = BucketEntry(
                section_yaml=section, bucket=bucket,
                section_required=sec_req, sign=+1,
            )
            _add_aliases(cm.bs_index, body.get("aliases", []), entry)

    pl_doc = yaml.safe_load((config_dir / "mapping_pl.yaml").read_text())
    for section, buckets in pl_doc.items():
        for bucket, body in buckets.items():
            cm.pl_buckets.append((section, bucket))
            if "components" in body:
                # Netting bucket — load each component separately
                for comp_name, comp_body in body["components"].items():
                    entry = BucketEntry(
                        section_yaml=section, bucket=bucket,
                        section_required=None,
                        sign=comp_body.get("sign", +1),
                    )
                    _add_aliases(cm.pl_index, comp_body.get("aliases", []), entry)
            else:
                entry = BucketEntry(
                    section_yaml=section, bucket=bucket,
                    section_required=None, sign=+1,
                )
                _add_aliases(cm.pl_index, body.get("aliases", []), entry)

    return cm


# ─── Classifier ────────────────────────────────────────────────────────────

@dataclass
class Classification:
    bucket: Optional[Tuple[str, str]]    # (section_yaml, bucket) or None
    sign: int = +1
    method: str = ""                      # "exact" | "fuzzy" | "unclassified"
    score: float = 0.0
    norm_label: str = ""


def classify_row(label: str,
                  section: str,
                  side: str,                  # "BS" or "PL"
                  mapping: CompiledMapping,
                  fuzzy_threshold: float = 88.0) -> Classification:
    """Classify a single row's label to an output bucket."""
    norm = normalize_label(label)
    if not norm:
        return Classification(bucket=None, method="unclassified", norm_label=norm)

    index = mapping.bs_index if side == "BS" else mapping.pl_index
    section_required = SECTION_TO_REQUIRED.get(normalize_section(section))

    # ── Exact match ──
    candidates = index.get(norm, [])
    if candidates:
        # Filter by section_required if disambiguator applies
        if len(candidates) > 1 and section_required:
            constrained = [c for c in candidates
                           if c.section_required == section_required
                           or c.section_required is None]
            if constrained:
                candidates = constrained
        # Prefer entries whose section_required matches the row's section
        if section_required:
            preferred = [c for c in candidates
                         if c.section_required == section_required]
            if preferred:
                candidates = preferred
        chosen = candidates[0]
        return Classification(
            bucket=(chosen.section_yaml, chosen.bucket),
            sign=chosen.sign, method="exact", score=100.0, norm_label=norm,
        )

    # ── Fuzzy fallback ──
    if HAS_FUZZ and index:
        match = process.extractOne(
            norm, list(index.keys()), scorer=fuzz.token_set_ratio,
        )
        if match and match[1] >= fuzzy_threshold:
            matched_key = match[0]
            score = match[1]
            cands = index[matched_key]
            if len(cands) > 1 and section_required:
                cands = [c for c in cands
                          if c.section_required == section_required
                          or c.section_required is None] or cands
            chosen = cands[0]
            return Classification(
                bucket=(chosen.section_yaml, chosen.bucket),
                sign=chosen.sign, method="fuzzy", score=score, norm_label=norm,
            )

    return Classification(bucket=None, method="unclassified",
                            norm_label=norm)
