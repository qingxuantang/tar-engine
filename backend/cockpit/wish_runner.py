"""Wish runner — UX A pivot (2026-05-10).

Connects Planner output → skill_executor execution → run_result update.

Replaces the old dispatcher.dispatch() call which was for OpenClaw IDE-side
execution. Under UX A, the engine itself runs each skill in the plan's chain
sequentially, accumulating results.

Lifecycle:
    1. Caller submits wish (router.submit_wish)
    2. Planner generates Plan with skill_chain
    3. wish_runner.run_wish_async() launches background asyncio task
    4. For each skill in chain:
       - execute_skill(...) runs it
       - on_event callback streams progress updates (used by Telegram bot)
    5. On completion: run_result row updated with skill_results + final_output
    6. cleanup_workspace() called for §7 trust boundary

Errors are non-fatal at the chain level: a failed skill marks itself failed
in skill_results but the run continues. The chain aggregate is success only
if all skills succeeded.

Threading note: each wish runs in its own asyncio task. skill_executor uses
sync httpx (blocking calls), so we run it in a thread pool executor to avoid
blocking the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .models import LLMConfig, Plan, WishTask
from .skill_executor import (
    SkillExecutorError,
    SkillNotFound,
    cleanup_workspace,
    execute_skill,
)
from .store import get_store
from .token_budget import BudgetConfig, format_cost_summary
from .trace_writer import TraceWriter

logger = logging.getLogger("cockpit.runner")


def run_wish_sync(
    *,
    task: WishTask,
    plan: Plan,
    cfg: LLMConfig,
    on_event: Optional[Callable[[dict], None]] = None,
    budget_config: Optional[BudgetConfig] = None,
) -> dict[str, Any]:
    """Run a wish synchronously: iterate plan's skill chain.

    Returns the run result dict (also persisted to store).
    """
    store = get_store()
    store.update_wish_status(task.task_id, "running")

    # Trace writer fans out each emit so events are persisted to
    # cockpit_run_trace AND forwarded to the caller's on_event callback.
    # Internal code paths use `emit(...)` below instead of touching
    # on_event directly.
    _trace = TraceWriter(store, task.task_id, user_on_event=on_event)
    def emit(event: dict[str, Any]) -> None:
        _trace.record(event)

    emit({"type": "wish_started", "task_id": task.task_id, "chain_len": len(plan.skill_chain)})

    skill_results: list[dict[str, Any]] = []
    chain_success = True
    cumulative_context = ""
    started = time.time()

    for idx, step in enumerate(plan.skill_chain):
        skill_name = step.get("skill") or step.get("name") or ""
        sub_goal = step.get("sub_goal") or step.get("rationale") or task.wish
        if not skill_name:
            logger.warning("plan step %d has no skill name; skipping", idx)
            continue

        emit({
            "type": "skill_dispatch",
            "task_id": task.task_id,
            "step_idx": idx,
            "step_count": len(plan.skill_chain),
            "skill": skill_name,
            "sub_goal": sub_goal,
        })

        try:
            result = execute_skill(
                task=task,
                skill_name=skill_name,
                sub_goal=sub_goal,
                cfg=cfg,
                extra_context=cumulative_context if cumulative_context else None,
                on_event=emit,
                budget_config=budget_config,
            )
        except SkillNotFound as e:
            logger.warning("skill not found: %s", e)
            chain_success = False
            skill_results.append({
                "skill_name": skill_name,
                "step_idx": idx,
                "success": False,
                "error": f"skill_not_found: {e}",
                "iterations": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            })
            continue
        except SkillExecutorError as e:
            logger.warning("skill executor error: %s", e)
            chain_success = False
            skill_results.append({
                "skill_name": skill_name,
                "step_idx": idx,
                "success": False,
                "error": f"executor_error: {e}",
                "iterations": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            })
            continue

        record = result.to_dict()
        record["step_idx"] = idx
        record["sub_goal"] = sub_goal
        skill_results.append(record)

        if not result.success:
            chain_success = False
            if result.error and result.error.startswith("budget_exceeded"):
                # Budget breach is a hard stop: don't run further skills
                logger.warning("budget exceeded mid-chain; aborting remaining skills")
                break
            # Other failures: log and continue with next skill (best-effort chain)

        # Pass a brief summary of this skill's output to the next skill
        if result.final_text:
            cumulative_context += (
                f"\n\n=== PRIOR SKILL OUTPUT ({skill_name}) ===\n"
                f"{result.final_text[:1500]}\n=== END PRIOR ==="
            )

    # Build final output: concatenate all final_texts
    final_output = "\n\n".join(
        f"### {r['skill_name']}\n{r.get('final_text', '')}"
        for r in skill_results
        if r.get("success") and r.get("final_text")
    )

    # Aggregate token usage
    total_prompt = sum(int(r.get("prompt_tokens", 0)) for r in skill_results)
    total_completion = sum(int(r.get("completion_tokens", 0)) for r in skill_results)

    # Read final aggregate from store (charge_after_call has been writing it)
    wish_after = store.get_wish(task.task_id) or {}
    import json as _json
    aggregated_usage = _json.loads(wish_after.get("token_usage") or "{}")

    duration_s = round(time.time() - started, 2)

    run_result = {
        "task_id": task.task_id,
        "skill_results": skill_results,
        "final_output": final_output,
        "audit_verdicts": [],  # populated separately (planner side already did pre-run)
        "profile_updates": [],  # Profile Writer hook in future
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": duration_s,
        "token_usage": aggregated_usage,
    }
    store.save_run_result(run_result)
    store.update_wish_status(task.task_id, "done" if chain_success else "failed")

    # Per-§7 trust boundary: scrub workspace
    freed = cleanup_workspace(task.task_id)
    logger.info(
        "wish completed: task_id=%s success=%s skills=%d duration=%.2fs prompt=%d completion=%d freed=%dB",
        task.task_id, chain_success, len(skill_results),
        duration_s, total_prompt, total_completion, freed,
    )

    emit({
        "type": "wish_completed",
        "task_id": task.task_id,
        "success": chain_success,
        "duration_s": duration_s,
        "cost_summary": format_cost_summary(aggregated_usage, model=cfg.model),
        "final_output_preview": (final_output or "")[:600],
    })

    return run_result


async def run_wish_async(
    *,
    task: WishTask,
    plan: Plan,
    cfg: LLMConfig,
    on_event: Optional[Callable[[dict], None]] = None,
    budget_config: Optional[BudgetConfig] = None,
) -> dict[str, Any]:
    """Async wrapper — runs the sync wish loop in a thread pool executor.

    Use this from FastAPI handlers / background tasks so we don't block the
    event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: run_wish_sync(
            task=task, plan=plan, cfg=cfg,
            on_event=on_event, budget_config=budget_config,
        ),
    )
