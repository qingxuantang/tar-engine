"""Auditor L1 + L2 integration for cockpit.

For each skill in a plan, audit the skill's SKILL.md against TAR Engine V2's
existing RiskGuardrail (L1 — 17 universal rules + 18 quant-domain rules) and
capability metadata (L2 — declared vs effective bitmap).

Day 4 scope:
  L1: ✅ run RiskGuardrail.check_document on each SKILL.md
  L2: ⏳ stub — capability bitmap diff requires runtime trace; record placeholder
  L3: out of scope for cockpit MVP (PLAN_OSS_STRATEGY §12 Phase 1+)

Module layout: this is a thin shim. The heavy auditor code lives in
backend/auditor/ — we import + call.

Skill name → SKILL.md path mapping: assumes skill bundles at ~/.claude/skills/<name>/SKILL.md.
The path is configurable via env COCKPIT_SKILLS_DIR.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from auditor.domain_config import DomainConfig  # noqa: E402  (sys.path set in router)

logger = logging.getLogger("cockpit.audit")


SKILLS_DIR = Path(os.environ.get("COCKPIT_SKILLS_DIR", "/root/.claude/skills"))


def _resolve_skill_md(skill_name: str) -> Optional[Path]:
    """Map skill display name → SKILL.md path.

    Delegates to skill_executor.find_skill_path so the pre-run auditor sees
    skills from both the Claude Code skills dir (CC_SKILLS_DIR) and any
    installed packs (PACKS_DIR). Without this, the auditor would only see
    /root/.claude/skills/* and report `not_found` for every pack skill.
    """
    from .skill_executor import find_skill_path
    try:
        skill_dir = find_skill_path(skill_name)
    except Exception as e:
        logger.warning("skill resolve: find_skill_path raised for %s: %s", skill_name, e)
        return None
    if skill_dir is None:
        return None
    md_path = skill_dir / "SKILL.md"
    return md_path if md_path.exists() else None


def _domain_config_for(domain: str) -> DomainConfig:
    """Map domain string → DomainConfig instance from auditor.domains.<name>.

    OSS only ships the `general` domain. Curated paid packs (quant, content, ...)
    register their own DomainConfig by extending auditor.domains.
    """
    from auditor.domains.general import GENERAL_CONFIG  # type: ignore
    # Future: dynamic discovery of installed domain packs via entry_points
    return GENERAL_CONFIG


def _audit_skill_md(skill_name: str, md_path: Path, domain: str = "general") -> dict[str, Any]:
    """Run L1 static audit on a single SKILL.md. Returns a verdict dict."""
    from auditor.risk_guardrail import RiskGuardrail  # type: ignore

    text = md_path.read_text(encoding="utf-8")
    guard = RiskGuardrail(domain=_domain_config_for(domain))
    alerts = guard.check_document(text, source=str(md_path))

    sev_counts = {"critical": 0, "high": 0, "warning": 0, "info": 0}
    for a in alerts:
        sev_counts[a.severity] = sev_counts.get(a.severity, 0) + 1

    score = 100
    score -= 20 * sev_counts["critical"]
    score -= 10 * sev_counts["high"]
    score -= 5 * sev_counts["warning"]
    score -= 1 * sev_counts["info"]
    score = max(0, score)

    if sev_counts["critical"]:
        risk_class = "Critical"
    elif sev_counts["high"]:
        risk_class = "High"
    elif sev_counts["warning"]:
        risk_class = "Medium"
    else:
        risk_class = "Low"

    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"

    return {
        "skill": skill_name,
        "skill_md_path": str(md_path),
        "rules_evaluated": len(guard.rules),
        "alerts": [
            {
                "rule_name": a.rule_name,
                "severity": a.severity,
                "message": a.message,
            }
            for a in alerts
        ],
        "alerts_by_severity": sev_counts,
        "score": score,
        "grade": grade,
        "risk_class": risk_class,
        "audit_layer": "L1_static",
        "domain": domain,
    }


def audit_skill_chain(
    skill_chain: list[dict[str, Any]],
    domain: str = "general",
) -> list[dict[str, Any]]:
    """Audit each skill in a plan's chain. Returns list of verdicts (one per step).

    For skills whose SKILL.md is missing, returns a verdict with status="not_found".
    Skills appearing more than once: cached after first audit.
    """
    cache: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []

    for i, step in enumerate(skill_chain):
        skill_name = step.get("skill", "")
        if not skill_name:
            out.append({"step_index": i, "skill": "", "status": "missing_skill_field"})
            continue

        if skill_name in cache:
            verdict = dict(cache[skill_name])
            verdict["step_index"] = i
            verdict["cached"] = True
            out.append(verdict)
            continue

        md_path = _resolve_skill_md(skill_name)
        if md_path is None:
            verdict = {
                "step_index": i,
                "skill": skill_name,
                "status": "not_found",
                "message": f"SKILL.md not found for {skill_name!r} under {SKILLS_DIR}",
            }
        else:
            try:
                verdict = _audit_skill_md(skill_name, md_path, domain=domain)
                verdict["step_index"] = i
                verdict["status"] = "audited"
            except Exception as e:
                logger.exception("audit failed for %s: %s", skill_name, e)
                verdict = {
                    "step_index": i,
                    "skill": skill_name,
                    "status": "audit_error",
                    "message": str(e),
                }

        cache[skill_name] = verdict
        out.append(verdict)

    return out


def chain_aggregate_verdict(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-skill verdicts into a chain-level summary."""
    audited = [v for v in verdicts if v.get("status") == "audited"]
    missing = sum(1 for v in verdicts if v.get("status") == "not_found")
    errored = sum(1 for v in verdicts if v.get("status") == "audit_error")

    if not audited:
        return {
            "chain_score": None,
            "chain_risk_class": "Unknown",
            "audited_skills": 0,
            "missing_skills": missing,
            "errored_skills": errored,
        }

    # Chain risk = worst of any step
    risk_priority = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}
    worst = max(audited, key=lambda v: risk_priority.get(v.get("risk_class", "Low"), 0))
    avg_score = sum(v["score"] for v in audited) / len(audited)

    return {
        "chain_score": round(avg_score, 1),
        "chain_risk_class": worst.get("risk_class", "Low"),
        "audited_skills": len(audited),
        "missing_skills": missing,
        "errored_skills": errored,
    }
