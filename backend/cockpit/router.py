"""Cockpit FastAPI router — Day 3 (SQLite-persisted).

Routes:
  POST /api/cockpit/wish              submit a wish, returns task_id + plan
  GET  /api/cockpit/wish/{task_id}    get task state (wish + plan + run result)
  GET  /api/cockpit/wishes            list recent wishes (?user_id=...)
  GET  /api/cockpit/profile/{user_id} get current profile
  GET  /api/cockpit/health             cockpit subsystem health
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

import asyncio

from .auditor_integration import audit_skill_chain, chain_aggregate_verdict
from .models import LLMConfig, WishTask
from .planner import Planner
from .profile_writer import get_profile_writer
from .store import get_store
from .wish_runner import run_wish_async

logger = logging.getLogger("cockpit")

router = APIRouter(prefix="/api/cockpit", tags=["cockpit"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "subsystem": "cockpit", "version": "0.0.3-day3-store"}


@router.post("/wish")
async def submit_wish(request: Request) -> dict[str, Any]:
    """Submit a wish for orchestration.

    Body: { "wish": str, "user_id": str, "context"?: dict }
    Headers: X-LLM-Base-Url, X-LLM-Api-Key, X-LLM-Model (BYOK)
    Returns: { "task_id": str, "stream_url": str, "plan": {...} }
    """
    body = await request.json()
    wish = (body.get("wish") or "").strip()
    user_id = (body.get("user_id") or "").strip()
    context = body.get("context") or {}

    if not wish:
        raise HTTPException(status_code=400, detail="wish is required")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    llm_config = LLMConfig.from_headers(dict(request.headers))

    task = WishTask(wish=wish, user_id=user_id, context=context)
    store = get_store()
    store.save_wish(task.to_dict())

    profile = get_profile_writer().get(user_id)
    planner = Planner(llm_config=llm_config)
    store.update_wish_status(task.task_id, "planning")
    plan = planner.plan(task, profile=profile)

    plan_dict = plan.to_dict()
    plan_dict["intent"] = plan_dict.get("intent")  # not yet propagated; future
    store.save_plan(plan_dict, raw_llm_response=plan.raw_llm_response or "")

    # Auditor L1+L2: pre-run static audit of each skill in the chain
    audit_verdicts = audit_skill_chain(plan.skill_chain, domain="quant")
    chain_summary = chain_aggregate_verdict(audit_verdicts)

    # Persist verdicts as part of run_result (final_output empty until skills actually run)
    store.save_run_result({
        "task_id": task.task_id,
        "skill_results": [],
        "final_output": None,
        "audit_verdicts": audit_verdicts,
        "profile_updates": [],
        "completed_at": None,
    })

    # UX A (2026-05-10): instead of dispatching to user IDE, the engine itself
    # runs the skill chain. Fire-and-forget background task — caller polls
    # /wish/{task_id} or subscribes via Telegram bot for progress.
    if llm_config and llm_config.is_configured():
        asyncio.create_task(
            run_wish_async(task=task, plan=plan, cfg=llm_config),
        )
        store.update_wish_status(task.task_id, "running")
    else:
        # Without an LLM config we can plan but cannot execute. Status stays "planning".
        logger.warning(
            "wish %s planned but LLM config missing; skipping execution",
            task.task_id,
        )

    logger.info(
        "cockpit.wish accepted: task_id=%s user_id=%s wish=%r chain_len=%d llm=%s audited=%d",
        task.task_id, user_id, wish[:80],
        len(plan.skill_chain),
        "configured" if llm_config and llm_config.is_configured() else "missing",
        sum(1 for v in audit_verdicts if v.get("status") == "audited"),
    )

    return {
        "task_id": task.task_id,
        "stream_url": f"/api/cockpit/wish/{task.task_id}",
        "plan": plan.to_dict(),
        "audit_verdicts": audit_verdicts,
        "chain_summary": chain_summary,
    }


@router.get("/wish/{task_id}")
async def get_wish(task_id: str) -> dict[str, Any]:
    """Get current state of a wish (wish + plan + result if exists)."""
    store = get_store()
    wish = store.get_wish(task_id)
    if not wish:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    plan = store.get_plan(task_id)
    result = store.get_run_result(task_id)
    return {"wish": wish, "plan": plan, "result": result}


@router.get("/wishes")
async def list_wishes(
    user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    return get_store().list_wishes(user_id=user_id, limit=limit)


@router.get("/profile/{user_id}")
async def get_profile(user_id: str) -> dict[str, Any]:
    return get_profile_writer().get(user_id)


@router.get("/wish/{task_id}/trace")
async def get_wish_trace(
    task_id: str,
    step_type: Optional[str] = Query(None, description="Filter by step_type"),
    skill: Optional[str] = Query(None, description="Filter by skill_name"),
) -> dict[str, Any]:
    """Get the execution trace for a wish.

    Returns the full step-by-step record persisted in cockpit_run_trace
    (plan / skill_start / skill_end / tool_call / tool_result / audit /
    error). Optional `step_type` / `skill` filters.

    The wish must exist; otherwise 404. An empty `steps` array means the
    wish ran on an older build before the trace feature shipped (or trace
    write failed silently — see logs).
    """
    store = get_store()
    wish = store.get_wish(task_id)
    if not wish:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    steps = store.get_trace(task_id)
    if step_type:
        steps = [s for s in steps if s.get("step_type") == step_type]
    if skill:
        steps = [s for s in steps if s.get("skill_name") == skill]

    # Roll up totals at the top level for quick at-a-glance use
    total_tokens_in = sum(int(s.get("tokens_in") or 0) for s in steps)
    total_tokens_out = sum(int(s.get("tokens_out") or 0) for s in steps)
    total_duration_ms = sum(int(s.get("duration_ms") or 0) for s in steps if s.get("duration_ms"))
    step_counts: dict[str, int] = {}
    for s in steps:
        st = s.get("step_type") or "unknown"
        step_counts[st] = step_counts.get(st, 0) + 1

    return {
        "task_id": task_id,
        "wish_status": wish.get("status"),
        "step_count": len(steps),
        "summary": {
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "duration_ms": total_duration_ms,
            "by_step_type": step_counts,
        },
        "steps": steps,
    }


ENGINE_VERSION = "0.2.0"


def _severity_deduction(severity: str) -> int:
    return {"critical": 20, "high": 10, "warning": 5, "info": 1}.get(severity, 0)


def _grade_from_score_and_severity(score: int, sev_counts: dict) -> tuple[str, str]:
    """Return (risk_class, letter_grade) given total score and severity tallies.

    Critical findings short-circuit to F regardless of score. High caps the grade
    at D. Warning caps at C. Otherwise A/B by score band.
    """
    if sev_counts.get("critical"):
        return "Critical", "F"
    if sev_counts.get("high"):
        return "High", "D"
    if sev_counts.get("warning"):
        return "Medium", "C" if score < 75 else "B"
    if score >= 90:
        return "Low", "A"
    return "Low", "B"


def _git_commit_sha() -> str:
    """Best-effort short commit SHA so reports can be traced to source code.

    Order of preference:
      1. TAR_ENGINE_COMMIT_SHA env var (set by compose / CI / build script)
      2. `git rev-parse --short HEAD` (only works if repo is mounted into the
         container — typical for dev mode)
      3. "unknown" sentinel
    """
    import os
    import subprocess
    from pathlib import Path
    if os.environ.get("TAR_ENGINE_COMMIT_SHA"):
        return os.environ["TAR_ENGINE_COMMIT_SHA"][:12]
    try:
        repo_root = Path(__file__).resolve().parents[2]
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


@router.post("/audit/static")
def audit_static(body: dict):
    """Static audit of a SKILL.md text. Stateless, no LLM, returns immediately.

    Body:
        skill_text (str, required) — full SKILL.md content
        domain (str, optional) — audit domain name; defaults to "general"
        skill_name (str, optional) — override; otherwise parsed from frontmatter
        no_history (bool, optional) — when true, skip historical baseline
                                       lookup AND skip recording this audit
                                       (useful for stateless one-off queries)

    Response shape (v0.2):
        {
            "success": true,
            "score": 0-100,
            "risk_class": "...",
            "grade": "A".."F",
            "severity_counts": {"critical", "high", "warning", "info"},
            "score_breakdown_by_category": {
                "<category>": {"score": 0-100, "rules_evaluated": N,
                               "findings": M, "max_severity": "..."}
            },
            "findings": [
                {"rule_id", "rule_name", "category", "severity", "message",
                 "description", "fix_template", "match_count",
                 "hits": [{"line_number", "line_text", "excerpt"}]}
            ],
            "rules_applied": [...],   # full rule registry that was active
            "audit_meta": {
                "engine_version", "rule_set_version", "rule_count",
                "domain", "commit_sha", "audited_at"
            }
        }
    """
    import re
    import uuid
    from datetime import datetime
    from .auditor_integration import _domain_config_for
    from .store import get_store
    from auditor.risk_guardrail import (  # type: ignore
        RiskGuardrail,
        UNIVERSAL_RULES,
        RULE_CATEGORIES,
        CATEGORY_DISPLAY_NAMES,
        RULE_SET_VERSION,
    )
    from auditor.audit_baseline import (  # type: ignore
        compute_skill_hash,
        compute_baseline,
    )

    skill_text = (body or {}).get("skill_text")
    if not skill_text:
        raise HTTPException(400, "missing required field: skill_text")
    domain = (body or {}).get("domain", "general")
    skill_name_override = (body or {}).get("skill_name")
    no_history = bool((body or {}).get("no_history"))

    domain_cfg = _domain_config_for(domain)
    guard = RiskGuardrail(domain=domain_cfg)
    alerts = guard.check_document(skill_text, source="audit-api")

    # ── Tally severity counts ──
    sev_counts = {"critical": 0, "high": 0, "warning": 0, "info": 0}
    for a in alerts:
        sev_counts[a.severity] = sev_counts.get(a.severity, 0) + 1

    # ── Total score ──
    score = 100
    for sev, n in sev_counts.items():
        score -= _severity_deduction(sev) * n
    score = max(0, score)
    risk_class, grade = _grade_from_score_and_severity(score, sev_counts)

    # ── Build full rule registry (universal + domain-specific) so the report
    #    can show "this category had N rules, all passed" instead of staying
    #    silent on clean categories ──
    domain_rules = list(getattr(domain_cfg, "realtime_rules", []) or [])
    all_static_rules = [
        r for r in (UNIVERSAL_RULES + domain_rules)
        if r.match_scope in ("static", "both")
    ]

    # Merge category lists: universal + per-domain. Domain categories come
    # from DomainConfig.categories (declared by the domain author).
    domain_categories = dict(getattr(domain_cfg, "categories", {}) or {})
    ordered_categories = list(RULE_CATEGORIES) + [
        c for c in domain_categories.keys() if c not in RULE_CATEGORIES
    ]
    display_names = dict(CATEGORY_DISPLAY_NAMES)
    display_names.update(domain_categories)

    rules_by_category: dict[str, list] = {c: [] for c in ordered_categories}
    for r in all_static_rules:
        rules_by_category.setdefault(r.category, []).append(r)
    # Promote any orphan categories surfaced by rules but not declared
    # anywhere to the ordered list, so they still show up in the breakdown.
    for cat in list(rules_by_category.keys()):
        if cat not in ordered_categories:
            ordered_categories.append(cat)
            display_names.setdefault(cat, cat.replace("_", " ").title())

    rules_applied = [
        {
            "rule_id": r.rule_id,
            "rule_name": r.name,
            "category": r.category,
            "severity": r.severity,
            "description": r.description,
        }
        for r in all_static_rules
    ]

    # ── Findings list (rich, ordered by severity desc then category) ──
    severity_order = {"critical": 0, "high": 1, "warning": 2, "info": 3}
    findings = []
    for a in alerts:
        details = a.details or {}
        cat = details.get("category") or "uncategorized"
        findings.append({
            "rule_id": details.get("rule_id") or "?",
            "rule_name": a.rule_name,
            "category": cat,
            "category_display": display_names.get(cat, cat.replace("_", " ").title()),
            "severity": a.severity,
            "message": a.message,
            "description": details.get("description") or "",
            "fix_template": details.get("fix_template") or "",
            "match_count": details.get("match_count", 0),
            "hits": details.get("hits", []),
        })
    findings.sort(key=lambda f: (severity_order.get(f["severity"], 9), f["category"]))

    # ── Per-category sub-scores ──
    score_breakdown_by_category = {}
    for category in ordered_categories:
        cat_findings = [f for f in findings if f["category"] == category]
        sub_sev_counts = {"critical": 0, "high": 0, "warning": 0, "info": 0}
        for f in cat_findings:
            sub_sev_counts[f["severity"]] = sub_sev_counts.get(f["severity"], 0) + 1
        sub_score = 100
        for sev, n in sub_sev_counts.items():
            sub_score -= _severity_deduction(sev) * n
        sub_score = max(0, sub_score)
        max_sev = "none"
        for level in ("critical", "high", "warning", "info"):
            if sub_sev_counts[level]:
                max_sev = level
                break
        score_breakdown_by_category[category] = {
            "category_display": display_names.get(category, category),
            "score": sub_score,
            "rules_evaluated": len(rules_by_category.get(category, [])),
            "findings_count": len(cat_findings),
            "max_severity": max_sev,
            "severity_counts": sub_sev_counts,
        }

    # ── Same-skill historical baseline (C1) ──
    # Parse skill_name + description from frontmatter to get a stable identity.
    fm_match = re.search(r"^---\s*\n(.*?)\n---", skill_text, re.MULTILINE | re.DOTALL)
    frontmatter: dict[str, str] = {}
    if fm_match:
        for line in fm_match.group(1).splitlines():
            kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
            if kv:
                val = kv.group(2)
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                frontmatter[kv.group(1)] = val
    skill_name = skill_name_override or frontmatter.get("name") or "unnamed-skill"
    skill_hash = compute_skill_hash(skill_name, frontmatter)
    finding_rule_ids = [f["rule_id"] for f in findings if f.get("rule_id") and f["rule_id"] != "?"]

    audited_at = datetime.utcnow().isoformat() + "Z"
    historical_baseline = None
    if not no_history:
        store = get_store()
        try:
            history = store.get_audit_history(skill_hash, limit=50)
            historical_baseline = compute_baseline(history, current_score=score)
            # Record AFTER computing baseline (so this audit doesn't influence itself)
            store.record_audit(
                audit_id=str(uuid.uuid4()),
                skill_hash=skill_hash,
                skill_name=skill_name,
                score=score,
                grade=grade,
                sev_counts=sev_counts,
                finding_rule_ids=finding_rule_ids,
                domain=domain,
                engine_version=ENGINE_VERSION,
                rule_set_version=RULE_SET_VERSION,
                audited_at=audited_at,
            )
        except Exception as e:
            # History is a nice-to-have. If the store is unavailable (e.g. tests
            # without a tmp DB), continue without it — never block the audit.
            logger.warning("audit history unavailable: %s", e)
            historical_baseline = None

    return {
        "success": True,
        "score": score,
        "risk_class": risk_class,
        "grade": grade,
        "severity_counts": sev_counts,
        "score_breakdown_by_category": score_breakdown_by_category,
        "findings": findings,
        "rules_applied": rules_applied,
        "historical_baseline": historical_baseline,
        "audit_meta": {
            "engine_version": ENGINE_VERSION,
            "rule_set_version": RULE_SET_VERSION,
            "rule_count": len(all_static_rules),
            "universal_rule_count": len([r for r in UNIVERSAL_RULES if r.match_scope in ("static", "both")]),
            "domain_rule_count": len([r for r in domain_rules if r.match_scope in ("static", "both")]),
            "domain": domain,
            "commit_sha": _git_commit_sha(),
            "audited_at": audited_at,
            "skill_hash": skill_hash,
            "skill_name": skill_name,
        },
    }
