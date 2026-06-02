"""RiskScorer — quantifies session risk into a 0-100 scorecard.

Combines inputs from:
- DecisionChainAuditor report (decision-level risks)
- RiskGuardrail alerts (real-time pattern matches)
- Session metadata (domain, duration, event count)

Output: Risk scorecard with go/no-go/conditional recommendation,
stored in audit_reports table as 'risk_scorecard' type.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from event_store import event_store


# Default weight configuration for score components
WEIGHTS = {
    "decision_risk": 0.35,   # From decision chain analysis
    "realtime_alerts": 0.30,  # From risk guardrail
    "complexity": 0.20,       # Session complexity factors
    "gene_history": 0.15,     # From accumulated Risk Gene knowledge
}

# Intent-aware weight overrides:
# read_only skills: suppress alert/decision noise, weight complexity + history higher
# execute skills: alerts matter most (real actions with real consequences)
# modify skills: decisions matter most (code changes need scrutiny)
INTENT_WEIGHTS = {
    "read_only": {
        "decision_risk": 0.15,
        "realtime_alerts": 0.10,
        "complexity": 0.35,
        "gene_history": 0.40,
    },
    "modify": {
        "decision_risk": 0.40,
        "realtime_alerts": 0.25,
        "complexity": 0.15,
        "gene_history": 0.20,
    },
    "execute": {
        "decision_risk": 0.30,
        "realtime_alerts": 0.40,
        "complexity": 0.10,
        "gene_history": 0.20,
    },
    # "unknown" falls back to default WEIGHTS
}

# Alert severity penalties (subtracted from 100)
ALERT_PENALTIES = {
    "critical": 25,
    "high": 15,
    "warning": 5,
    "info": 1,
}

# Decision risk level penalties
DECISION_PENALTIES = {
    "critical": 20,
    "high": 10,
    "medium": 3,
    "low": 0,
}


def score_session(
    session_id: str,
    decision_report: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Compute risk scorecard for a session.

    Args:
        session_id: The session to score
        decision_report: Pre-computed DecisionChainAuditor output.
            If None, looks for existing report in audit_reports.

    Returns:
        Risk scorecard dict, also persisted to audit_reports.
    """
    session = event_store.get_session(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    # Get or load decision report
    if decision_report is None:
        reports = event_store.get_reports(session_id)
        for r in reports:
            if r.get("report_type") == "decision_chain":
                import json
                decision_report = json.loads(r["content"]) if isinstance(r["content"], str) else r["content"]
                break

    # Get alerts
    alerts = event_store.get_session_alerts(session_id)

    # Get domain thresholds
    domain = session.get("domain", "quant")
    thresholds = _get_thresholds(domain)

    # Resolve skill intent for weight adjustment
    skill_intent = _resolve_skill_intent(session)
    weights = INTENT_WEIGHTS.get(skill_intent, WEIGHTS)

    # Score component 1: Decision risk
    decision_score = _score_decisions(decision_report)

    # Score component 2: Alert risk
    alert_score = _score_alerts(alerts)

    # Score component 3: Complexity
    complexity_score = _score_complexity(session_id, session)

    # Score component 4: Gene history (learned risk patterns)
    gene_score = _score_gene_history(session, alerts)

    # Weighted final score (intent-aware weights)
    final_score = round(
        decision_score * weights["decision_risk"]
        + alert_score * weights["realtime_alerts"]
        + complexity_score * weights["complexity"]
        + gene_score * weights["gene_history"]
    )
    final_score = max(0, min(100, final_score))

    # Recommendation
    if final_score >= thresholds["go"]:
        recommendation = "go"
        recommendation_text = "可以部署"
    elif final_score >= thresholds["conditional"]:
        recommendation = "conditional"
        recommendation_text = "建议审查后部署"
    else:
        recommendation = "no_go"
        recommendation_text = "建议暂缓部署，需人工审查"

    scorecard = {
        "score": final_score,
        "recommendation": recommendation,
        "recommendation_text": recommendation_text,
        "skill_intent": skill_intent,
        "weights_used": weights,
        "components": {
            "decision_risk": {
                "score": decision_score,
                "weight": weights["decision_risk"],
                "weighted": round(decision_score * weights["decision_risk"]),
            },
            "realtime_alerts": {
                "score": alert_score,
                "weight": weights["realtime_alerts"],
                "weighted": round(alert_score * weights["realtime_alerts"]),
            },
            "complexity": {
                "score": complexity_score,
                "weight": weights["complexity"],
                "weighted": round(complexity_score * weights["complexity"]),
            },
            "gene_history": {
                "score": gene_score,
                "weight": weights["gene_history"],
                "weighted": round(gene_score * weights["gene_history"]),
            },
        },
        "alert_count": len(alerts),
        "decision_count": decision_report.get("decision_count", 0) if decision_report else 0,
        "thresholds": thresholds,
        "scored_at": datetime.utcnow().isoformat(),
    }

    # Persist
    event_store.save_report(session_id, "risk_scorecard", scorecard)
    print(f"[RiskScorer] Session {session_id}: score={final_score}, "
          f"recommendation={recommendation}")

    return scorecard


def score_skill_run(
    session_id: str,
    skill_run_id: int,
    from_id: int,
    to_id: int,
    decision_report: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Compute risk scorecard for a specific skill run."""
    session = event_store.get_session(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    if decision_report is None:
        reports = event_store.get_reports_for_run(skill_run_id)
        for r in reports:
            if r.get("report_type") == "decision_chain":
                import json
                decision_report = json.loads(r["content"]) if isinstance(r["content"], str) else r["content"]
                break

    # Run-scoped: same rationale as decision_chain_auditor — session-level
    # alerts include unrelated noise (other skills, other tasks within the
    # same long session) and would skew the score.
    alerts = event_store.get_alerts_for_run(skill_run_id)
    domain = session.get("domain", "quant")
    thresholds = _get_thresholds(domain)

    # Resolve skill intent for weight adjustment
    skill_intent = _resolve_skill_intent(session)
    weights = INTENT_WEIGHTS.get(skill_intent, WEIGHTS)

    decision_score = _score_decisions(decision_report)
    alert_score = _score_alerts(alerts)

    # Use event count within range for complexity
    event_count = event_store.get_event_count_range(session_id, from_id, to_id)
    if event_count > 500:
        complexity_score = 40
    elif event_count > 200:
        complexity_score = 60
    elif event_count > 50:
        complexity_score = 80
    else:
        complexity_score = 95

    gene_score = _score_gene_history(session, alerts)

    final_score = round(
        decision_score * weights["decision_risk"]
        + alert_score * weights["realtime_alerts"]
        + complexity_score * weights["complexity"]
        + gene_score * weights["gene_history"]
    )
    final_score = max(0, min(100, final_score))

    if final_score >= thresholds["go"]:
        recommendation = "go"
        recommendation_text = "可以部署"
    elif final_score >= thresholds["conditional"]:
        recommendation = "conditional"
        recommendation_text = "建议审查后部署"
    else:
        recommendation = "no_go"
        recommendation_text = "建议暂缓部署，需人工审查"

    scorecard = {
        "score": final_score,
        "recommendation": recommendation,
        "recommendation_text": recommendation_text,
        "skill_intent": skill_intent,
        "weights_used": weights,
        "components": {
            "decision_risk": {
                "score": decision_score,
                "weight": weights["decision_risk"],
                "weighted": round(decision_score * weights["decision_risk"]),
            },
            "realtime_alerts": {
                "score": alert_score,
                "weight": weights["realtime_alerts"],
                "weighted": round(alert_score * weights["realtime_alerts"]),
            },
            "complexity": {
                "score": complexity_score,
                "weight": weights["complexity"],
                "weighted": round(complexity_score * weights["complexity"]),
            },
            "gene_history": {
                "score": gene_score,
                "weight": weights["gene_history"],
                "weighted": round(gene_score * weights["gene_history"]),
            },
        },
        "alert_count": len(alerts),
        "decision_count": decision_report.get("decision_count", 0) if decision_report else 0,
        "thresholds": thresholds,
        "scored_at": datetime.utcnow().isoformat(),
        "skill_run_id": skill_run_id,
    }

    event_store.save_report(session_id, "risk_scorecard", scorecard, skill_run_id=skill_run_id)
    print(f"[RiskScorer] Run #{skill_run_id}: score={final_score}, "
          f"recommendation={recommendation}")

    return scorecard


def _score_decisions(report: Optional[Dict]) -> int:
    """Score based on decision chain analysis. Returns 0-100."""
    if not report:
        return 80  # No report = likely no decisions = low risk

    # If the auditor already computed a score, weight it heavily
    auditor_score = report.get("risk_score")
    if auditor_score is not None:
        return int(auditor_score)

    # Fallback: compute from individual decisions
    decisions = report.get("decisions", [])
    if not decisions:
        return 90

    score = 100
    for d in decisions:
        level = d.get("risk_level", "low")
        score -= DECISION_PENALTIES.get(level, 0)

    return max(0, score)


def _score_alerts(alerts: List[Dict]) -> int:
    """Score based on real-time risk alerts. Returns 0-100."""
    if not alerts:
        return 100

    score = 100
    for a in alerts:
        severity = a.get("severity", "info")
        score -= ALERT_PENALTIES.get(severity, 1)

    return max(0, score)


def _score_gene_history(session: Dict, alerts: List[Dict]) -> int:
    """Score based on accumulated Risk Gene knowledge. Returns 0-100.

    If this skill has a Risk Gene with known avoid_cues, and current
    alerts match those patterns, the score decreases (repeat offender).
    If no gene exists, returns neutral 80 (slight benefit of the doubt).
    """
    skill_name = session.get("skill_name", "")
    if not skill_name:
        return 80  # No skill identified, neutral

    try:
        from auditor.risk_gene_distiller import get_gene_risk_modifier
        modifier = get_gene_risk_modifier(skill_name, alerts)
        # modifier is 0-30: 0 = no known patterns matched, 30 = heavy match
        return max(0, int(80 - modifier))
    except Exception:
        return 80  # Graceful fallback


def _score_complexity(session_id: str, session: Dict) -> int:
    """Score based on session complexity. Higher complexity = more risk.
    Returns 0-100 (100 = simple/safe, 0 = very complex/risky).
    """
    event_count = event_store.get_event_count(session_id)

    # Very long sessions are riskier (more room for error)
    if event_count > 500:
        return 40
    elif event_count > 200:
        return 60
    elif event_count > 50:
        return 80
    else:
        return 95


def _resolve_skill_intent(session: Dict) -> str:
    """Resolve the skill intent from session metadata.

    Checks session meta first (explicitly set), then looks up
    the skill name in the intent registry.
    """
    # Check meta for explicit intent
    meta = session.get("meta", "{}")
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if isinstance(meta, dict) and meta.get("skill_intent"):
        return meta["skill_intent"]

    # Look up from skill name
    skill_name = session.get("skill_name", "")
    if skill_name:
        try:
            from auditor.skill_registry import get_skill_intent
            return get_skill_intent(skill_name)
        except Exception:
            pass

    return "unknown"


def _get_thresholds(domain: str) -> Dict[str, int]:
    """Get risk thresholds for a domain."""
    try:
        from auditor.domains import get_domain
        config = get_domain(domain)
        return {
            "go": int(config.risk_thresholds.get("go", 80)),
            "conditional": int(config.risk_thresholds.get("conditional", 50)),
            "no_go": int(config.risk_thresholds.get("no_go", 0)),
        }
    except Exception:
        return {"go": 80, "conditional": 50, "no_go": 0}
