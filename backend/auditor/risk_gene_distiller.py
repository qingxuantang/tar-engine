"""RiskGeneDistiller — distills session audit findings into compact Risk Genes.

After each session audit, compresses the decision chain report, risk alerts,
and risk scorecard into a ~200 token structured "Risk Gene" per skill.

A Risk Gene contains:
- signals_match: tool/file patterns that identify this skill's risk profile
- avoid_cues: compact failure warnings distilled from past incidents
- constraints: hard limits learned from audit history
- risk_baseline: expected score range for normal runs

Risk Genes accumulate per skill_name. Each new session either:
- Updates the existing gene (merge new findings)
- Creates a new gene (first session for this skill)

Inspired by: "From Procedural Skills to Strategy Genes" (Wang et al., 2026)
Key insight: compact structured warnings (+4.6pp) >> verbose error logs (+0.7pp)
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from event_store import event_store


def distill_risk_gene(
    session_id: str,
    decision_report: Optional[Dict] = None,
    scorecard: Optional[Dict] = None,
) -> Optional[Dict]:
    """Distill a session's audit findings into a compact Risk Gene.

    Called by AuditOrchestrator after decision audit + risk scoring.
    Returns the gene dict, also persisted to skill_risk_genes table.
    """
    session = event_store.get_session(session_id)
    if not session:
        return None

    skill_name = session.get("skill_name", "")
    if not skill_name:
        return None

    # Load existing gene for this skill (if any)
    existing_gene = event_store.get_risk_gene(skill_name)

    # Load decision report if not provided
    if decision_report is None:
        reports = event_store.get_reports(session_id)
        for r in reports:
            if r.get("report_type") == "decision_chain":
                decision_report = (
                    json.loads(r["content"])
                    if isinstance(r["content"], str)
                    else r["content"]
                )
                break

    # Load scorecard if not provided
    if scorecard is None:
        reports = event_store.get_reports(session_id)
        for r in reports:
            if r.get("report_type") == "risk_scorecard":
                scorecard = (
                    json.loads(r["content"])
                    if isinstance(r["content"], str)
                    else r["content"]
                )
                break

    # Load alerts
    alerts = event_store.get_session_alerts(session_id)

    # Extract signals
    signals = _extract_signals(session_id, decision_report, alerts)

    # Extract avoid cues (the core value — compact failure warnings)
    new_avoid_cues = _extract_avoid_cues(decision_report, alerts)

    # Extract constraints
    new_constraints = _extract_constraints(decision_report, alerts)

    # Compute risk baseline from scorecard
    score = scorecard.get("score", 75) if scorecard else 75

    if existing_gene:
        # Merge with existing gene
        gene = _merge_gene(existing_gene, signals, new_avoid_cues,
                           new_constraints, score, session_id)
    else:
        # Create new gene
        gene = {
            "skill_name": skill_name,
            "signals_match": signals,
            "avoid_cues": new_avoid_cues,
            "constraints": new_constraints,
            "risk_baseline": {
                "min_score": score,
                "max_score": score,
                "avg_score": score,
                "session_count": 1,
            },
            "version": 1,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "source_sessions": [session_id],
        }

    # Enforce compactness — trim to ~200 tokens
    gene = _enforce_compact(gene)

    # Persist
    event_store.upsert_risk_gene(skill_name, gene)

    cue_count = len(gene.get("avoid_cues", []))
    print(f"[RiskGeneDistiller] {'Updated' if existing_gene else 'Created'} "
          f"gene for '{skill_name}': {cue_count} avoid_cues, "
          f"baseline={gene['risk_baseline']['avg_score']:.0f}")

    return gene


def get_gene_risk_modifier(skill_name: str, alerts: List[Dict]) -> float:
    """Check current alerts against a skill's Risk Gene.

    Returns a modifier (0-100 scale adjustment) that RiskScorer can use.
    Positive = riskier than baseline, negative = safer.
    """
    gene = event_store.get_risk_gene(skill_name)
    if not gene:
        return 0.0

    modifier = 0.0
    avoid_cues = gene.get("avoid_cues", [])

    for alert in alerts:
        rule = alert.get("rule_name", "")
        message = alert.get("message", "").lower()
        tool = alert.get("tool_name", "")

        for cue in avoid_cues:
            # Check if this alert matches a known avoid cue
            cue_pattern = cue.get("pattern", "").lower()
            if cue_pattern and (
                cue_pattern in rule.lower()
                or cue_pattern in message
                or cue_pattern in tool.lower()
            ):
                # Known risk pattern — amplify the penalty
                modifier += cue.get("weight", 5.0)

    # Check constraints
    constraints = gene.get("constraints", [])
    for c in constraints:
        c_pattern = c.get("pattern", "").lower()
        for alert in alerts:
            if c_pattern and c_pattern in alert.get("message", "").lower():
                modifier += 10.0  # Constraint violations are serious

    return min(modifier, 30.0)  # Cap at 30 points


# ── Internal helpers ──────────────────────────────────────────────


def _extract_signals(
    session_id: str,
    decision_report: Optional[Dict],
    alerts: List[Dict],
) -> List[str]:
    """Extract tool/file patterns that characterize this skill's risk surface."""
    signals = set()

    # From decisions: extract unique tool names used
    if decision_report:
        for d in decision_report.get("decisions", []):
            tool = d.get("tool", "")
            if tool:
                signals.add(f"tool:{tool}")
            fpath = d.get("file", "")
            if fpath:
                # Extract file extension pattern
                ext = fpath.rsplit(".", 1)[-1] if "." in fpath else ""
                if ext:
                    signals.add(f"ext:{ext}")

    # From alerts: extract rule names
    for a in alerts:
        rule = a.get("rule_name", "")
        if rule:
            signals.add(f"rule:{rule}")

    return sorted(signals)[:10]  # Cap at 10 signals


def _extract_avoid_cues(
    decision_report: Optional[Dict],
    alerts: List[Dict],
) -> List[Dict]:
    """Distill failure signals into compact AVOID warnings.

    Each cue is: {pattern, warning, weight, source}
    This is the core value — paper shows +4.6pp from compact warnings.
    """
    cues = []

    # From high-risk decisions
    if decision_report:
        for d in decision_report.get("decisions", []):
            risk = d.get("risk_level", "low")
            if risk in ("high", "critical"):
                cues.append({
                    "pattern": d.get("tool", "") or d.get("file", ""),
                    "warning": d.get("risk_reason", "high-risk operation")[:80],
                    "weight": 10.0 if risk == "critical" else 5.0,
                    "source": "decision_audit",
                })

    # From alerts (deduplicate by rule_name)
    seen_rules = set()
    for a in alerts:
        rule = a.get("rule_name", "")
        severity = a.get("severity", "info")
        if rule in seen_rules:
            continue
        if severity in ("high", "critical"):
            seen_rules.add(rule)
            cues.append({
                "pattern": rule,
                "warning": a.get("message", "")[:80],
                "weight": 10.0 if severity == "critical" else 5.0,
                "source": "realtime_alert",
            })
        elif severity == "warning":
            seen_rules.add(rule)
            cues.append({
                "pattern": rule,
                "warning": a.get("message", "")[:80],
                "weight": 2.0,
                "source": "realtime_alert",
            })

    # From recommendations
    if decision_report:
        for rec in decision_report.get("recommendations", []):
            if len(rec) > 5:
                cues.append({
                    "pattern": "",
                    "warning": rec[:80],
                    "weight": 1.0,
                    "source": "recommendation",
                })

    return cues[:8]  # Cap at 8 cues for compactness


def _extract_constraints(
    decision_report: Optional[Dict],
    alerts: List[Dict],
) -> List[Dict]:
    """Extract hard constraints from audit findings."""
    constraints = []

    # Critical alerts become hard constraints
    for a in alerts:
        if a.get("severity") == "critical":
            constraints.append({
                "pattern": a.get("rule_name", ""),
                "rule": f"MUST NOT: {a.get('message', '')[:60]}",
            })

    return constraints[:5]  # Cap at 5


def _merge_gene(
    existing: Dict,
    new_signals: List[str],
    new_avoid_cues: List[Dict],
    new_constraints: List[Dict],
    new_score: int,
    session_id: str,
) -> Dict:
    """Merge new findings into an existing Risk Gene."""
    gene = dict(existing)

    # Merge signals (union, capped)
    old_signals = set(gene.get("signals_match", []))
    old_signals.update(new_signals)
    gene["signals_match"] = sorted(old_signals)[:10]

    # Merge avoid cues (deduplicate by pattern, keep higher weight)
    existing_cues = {c["pattern"]: c for c in gene.get("avoid_cues", [])}
    for cue in new_avoid_cues:
        p = cue["pattern"]
        if p in existing_cues:
            # Keep the one with higher weight
            if cue["weight"] > existing_cues[p]["weight"]:
                existing_cues[p] = cue
        else:
            existing_cues[p] = cue
    gene["avoid_cues"] = sorted(
        existing_cues.values(), key=lambda c: -c["weight"]
    )[:8]

    # Merge constraints (deduplicate by pattern)
    existing_constraints = {c["pattern"]: c for c in gene.get("constraints", [])}
    for c in new_constraints:
        existing_constraints[c["pattern"]] = c
    gene["constraints"] = list(existing_constraints.values())[:5]

    # Update risk baseline (running average)
    baseline = gene.get("risk_baseline", {})
    count = baseline.get("session_count", 0)
    avg = baseline.get("avg_score", 75)
    new_avg = (avg * count + new_score) / (count + 1)
    gene["risk_baseline"] = {
        "min_score": min(baseline.get("min_score", 100), new_score),
        "max_score": max(baseline.get("max_score", 0), new_score),
        "avg_score": round(new_avg, 1),
        "session_count": count + 1,
    }

    # Track source sessions (keep last 10)
    sources = gene.get("source_sessions", [])
    if session_id not in sources:
        sources.append(session_id)
    gene["source_sessions"] = sources[-10:]

    # Bump version
    gene["version"] = gene.get("version", 0) + 1
    gene["updated_at"] = datetime.utcnow().isoformat()

    return gene


def _enforce_compact(gene: Dict) -> Dict:
    """Ensure the gene stays compact. Trim warnings to stay under ~200 tokens."""
    # Trim avoid_cue warnings
    for cue in gene.get("avoid_cues", []):
        if len(cue.get("warning", "")) > 80:
            cue["warning"] = cue["warning"][:77] + "..."

    # Trim constraint rules
    for c in gene.get("constraints", []):
        if len(c.get("rule", "")) > 60:
            c["rule"] = c["rule"][:57] + "..."

    return gene
