"""Audit baseline distiller — same-skill historical comparison for static audits.

Adapted from V3's RiskGeneDistiller (which operates on run-level audits).
The static-audit version is simpler because the input is just a score + a
list of fired rule_ids; we don't have decision chains or run-time traces.

What we keep:
- Mean / stddev / min / max of past scores → "expected normal range"
- Recurring rule_ids → "this skill's typical risk profile"
- Trend (improved / stable / regressed) vs the most recent prior audit

What we drop (vs V3 Risk Gene):
- avoid_cues / signals_match / constraints — these need decision_chain data
- LLM distillation — static audit doesn't have an LLM in-loop

The output is a `historical_baseline` dict the audit endpoint adds to its
response and the report formatter renders into a "Historical baseline" section.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any


def compute_skill_hash(skill_name: str, frontmatter: dict[str, Any]) -> str:
    """Stable identity for a skill across body edits.

    Hash inputs: skill_name + frontmatter.description (first 200 chars).
    A skill that gets renamed or has its description rewritten is treated as
    a new skill (which is usually what you want — the identity meaningfully
    changed). Pure body edits keep the same hash so the baseline keeps
    accumulating.
    """
    desc = (frontmatter.get("description") or "")[:200].strip()
    payload = f"name:{skill_name}\ndesc:{desc}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _classify_trend(current_score: int, prior_scores: list[int]) -> str:
    """Compare current vs the most recent prior. Trend bands:
       improved  : >= +5 vs prior, OR vs mean (whichever larger)
       regressed : <= -5 vs prior, OR vs mean (whichever larger)
       stable    : otherwise
       first_audit : no priors at all
    """
    if not prior_scores:
        return "first_audit"
    last_prior = prior_scores[0]
    delta = current_score - last_prior
    if delta >= 5:
        return "improved"
    if delta <= -5:
        return "regressed"
    return "stable"


def _stddev(values: list[int]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def _top_recurring_rules(history: list[dict[str, Any]], k: int = 5) -> list[dict]:
    """Across the audit history, which rule_ids keep firing? Returns the
    top k by hit_count, each annotated with the % of audits that hit it.
    """
    n = len(history)
    if n == 0:
        return []
    counts: dict[str, int] = {}
    for h in history:
        rids = h.get("finding_rule_ids") or []
        for rid in rids:
            counts[rid] = counts.get(rid, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: -x[1])[:k]
    return [
        {"rule_id": rid, "hit_count": ct, "hit_rate_pct": round(100 * ct / n, 1)}
        for rid, ct in ranked
    ]


def compute_baseline(history: list[dict[str, Any]], current_score: int) -> dict[str, Any]:
    """Distill a same-skill audit history into a comparable baseline.

    Args:
        history: list of past audit records (newest first), already filtered
                 to this skill_hash. May be empty.
        current_score: the score from the audit we just ran (NOT yet in history).

    Returns a dict ready to drop into the audit response. Shape:
        {
            "n_prior_audits": int,
            "trend": "first_audit" | "stable" | "improved" | "regressed",
            "delta_vs_last": int or null,
            "score_stats": {"mean", "stddev", "min", "max"} or null,
            "in_normal_band": bool or null,    # current within mean ± stddev
            "top_recurring_rules": [{"rule_id", "hit_count", "hit_rate_pct"}, ...],
            "first_audit_at": iso or null,
            "last_prior_audit_at": iso or null,
        }
    """
    prior_scores = [int(h["score"]) for h in history]
    trend = _classify_trend(current_score, prior_scores)

    if not prior_scores:
        return {
            "n_prior_audits": 0,
            "trend": trend,
            "delta_vs_last": None,
            "score_stats": None,
            "in_normal_band": None,
            "top_recurring_rules": [],
            "first_audit_at": None,
            "last_prior_audit_at": None,
        }

    mean = sum(prior_scores) / len(prior_scores)
    sd = _stddev(prior_scores)
    # Normal band: mean ± max(stddev, 3). If n < 3 the stddev is meaningless;
    # widen the band so we don't flag tiny samples as "out of band" noise.
    band_width = max(sd, 3.0) if len(prior_scores) >= 3 else max(sd, 10.0)
    in_band = (mean - band_width) <= current_score <= (mean + band_width)

    return {
        "n_prior_audits": len(prior_scores),
        "trend": trend,
        "delta_vs_last": current_score - prior_scores[0],
        "score_stats": {
            "mean": round(mean, 1),
            "stddev": round(sd, 1),
            "min": min(prior_scores),
            "max": max(prior_scores),
        },
        "in_normal_band": bool(in_band),
        "top_recurring_rules": _top_recurring_rules(history),
        "first_audit_at": history[-1].get("audited_at") if history else None,
        "last_prior_audit_at": history[0].get("audited_at") if history else None,
    }
