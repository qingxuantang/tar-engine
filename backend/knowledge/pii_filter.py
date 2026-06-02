"""PII filter for L3 ingest — Layer 1 (name blocklist) + Layer 2 (regex patterns).

Per L3_INGEST_SPEC.md Rule 3 (0 PII tolerance):
- Layer 1: any sentence containing a name (or alias) from the blocklist → dropped
- Layer 2: any sentence matching phone / email / wechat / telegram / amount /
  account number patterns → dropped

Both layers are deletion-only. False positives are acceptable; false negatives are not.

Mark explicitly rejected a third "Chinese-name heuristic" layer for being too aggressive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


# Layer 2 — PII regex patterns
PII_PATTERNS = [
    ("phone", re.compile(r"\b1[3-9]\d{9}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")),
    ("wechat_prefix", re.compile(r"\bwx[\w_-]{4,}\b", re.IGNORECASE)),
    ("wechat_label", re.compile(r"微信[: ：]\s*[\w_-]{4,}")),
    ("telegram", re.compile(r"@\w+")),
    ("amount_personal", re.compile(r"我[赚亏盈损][了]?\s*\d+\s*[万千百]")),
    ("account_personal", re.compile(r"我的[账号账户资金][\d.]+万")),
]


@dataclass
class FilterResult:
    """Result of PII filter — kept (None) or dropped (with reason + sample)."""

    kept: bool
    reason: Optional[str] = None
    layer: Optional[str] = None  # "L1" or "L2"


def _load_blocklist(blocklist_path: Path) -> list[str]:
    """Load + flatten name blocklist. Each row may be comma-separated aliases."""
    with open(blocklist_path) as f:
        data = yaml.safe_load(f)

    names_raw: list[str] = data.get("names", [])
    names_flat: list[str] = []
    for row in names_raw:
        # Each row may be a single name or a comma-separated alias group, e.g.
        # "Person A、Alias 1、Alias 2" or "Person A, Alias 1, Alias 2"
        # Split on Chinese 、 / English , / fullwidth ，
        parts = re.split(r"[，,、]", row)
        for p in parts:
            p = p.strip()
            if p:
                names_flat.append(p)

    # Sort longer names first so the longer alias matches before shorter substring
    names_flat.sort(key=len, reverse=True)
    return names_flat


class PIIFilter:
    """Two-layer PII filter — call .check(text) -> FilterResult."""

    def __init__(self, blocklist_path: str | Path):
        self.blocklist_path = Path(blocklist_path)
        self.names = _load_blocklist(self.blocklist_path)
        # Build a single regex with alternation for fast match
        # Escape each name (some may contain regex special chars)
        if self.names:
            pattern_str = "|".join(re.escape(n) for n in self.names)
            self.blocklist_re = re.compile(pattern_str)
        else:
            self.blocklist_re = None

    def check(self, text: str) -> FilterResult:
        """Run both layers. Returns FilterResult.kept = True if survives both."""
        # Layer 1: blocklist
        if self.blocklist_re is not None:
            m = self.blocklist_re.search(text)
            if m:
                return FilterResult(
                    kept=False,
                    reason=f"name_blocklist:{m.group(0)}",
                    layer="L1",
                )

        # Layer 2: regex patterns
        for name, pat in PII_PATTERNS:
            m = pat.search(text)
            if m:
                return FilterResult(
                    kept=False,
                    reason=f"regex_{name}:{m.group(0)}",
                    layer="L2",
                )

        return FilterResult(kept=True)
