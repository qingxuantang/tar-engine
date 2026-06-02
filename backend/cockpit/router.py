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


@router.post("/audit/static")
def audit_static(body: dict):
    """Static audit of a SKILL.md text. Stateless, no LLM, returns immediately.

    Body:
        skill_text (str, required) — full SKILL.md content
        domain (str, optional) — audit domain name; defaults to "general"

    Response:
        {
            "success": true,
            "score": 0-100,
            "risk_class": "Low" | "Medium" | "High" | "Critical",
            "grade": "A" | "B" | "C" | "D" | "F",
            "findings": [
                {"severity": "...", "rule_id": "...", "message": "...", ...}
            ],
            "audit_meta": {"engine_version": "0.1.0", "domain": "general"}
        }
    """
    from .auditor_integration import _domain_config_for
    from auditor.risk_guardrail import RiskGuardrail  # type: ignore

    skill_text = (body or {}).get("skill_text")
    if not skill_text:
        raise HTTPException(400, "missing required field: skill_text")
    domain = (body or {}).get("domain", "general")

    guard = RiskGuardrail(domain=_domain_config_for(domain))
    alerts = guard.check_document(skill_text, source="audit-api")

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
        grade = "F"
    elif sev_counts["high"]:
        risk_class = "High"
        grade = "D"
    elif sev_counts["warning"]:
        risk_class = "Medium"
        grade = "C" if score < 75 else "B"
    elif score >= 90:
        risk_class = "Low"
        grade = "A"
    else:
        risk_class = "Low"
        grade = "B"

    findings = []
    for a in alerts:
        findings.append({
            "severity": a.severity,
            "rule_id": getattr(a, "rule_id", "?"),
            "message": getattr(a, "message", str(a)),
        })

    return {
        "success": True,
        "score": score,
        "risk_class": risk_class,
        "grade": grade,
        "findings": findings,
        "severity_counts": sev_counts,
        "audit_meta": {
            "engine_version": "0.1.0",
            "domain": domain,
            "rules_evaluated": len(alerts) if alerts else 0,
        },
    }
