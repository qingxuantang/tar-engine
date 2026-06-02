"""Execution Trace Writer (Sprint A T1, 2026-05-19)

Captures the per-wish execution timeline into `cockpit_run_trace`.

Design from docs/10-next/PLAN_EXECUTION_TRACE_AND_RETROSPECTIVE.md:

- One row per discrete step (plan / skill_start / tool_call / tool_result /
  skill_end / audit / profile_update / error)
- Hooks onto the existing `on_event` callback contract used by
  wish_runner.py and skill_executor.py — we do NOT change the contract.
  A new TraceWriter sits alongside the caller-supplied on_event callback
  (e.g. the Telegram bot streamer) and persists each event as a trace row.
- Backwards-compatible: callers that didn't pass on_event get an
  internal-only writer; callers that did pass one still get their callback
  invoked exactly as before.

Usage:

    writer = TraceWriter(store, task_id, user_on_event=telegram_callback)
    writer.record({"type": "wish_started", "task_id": "..."})
    # Internally: append to DB + forward to telegram_callback
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("cockpit.trace_writer")


# Map on_event "type" values from wish_runner / skill_executor to the
# step_type column in cockpit_run_trace. Anything not in this map is
# recorded with its raw type as step_type.
_TYPE_TO_STEP = {
    # Wish lifecycle (emitted by wish_runner.py)
    "wish_started":     "plan",
    "wish_completed":   "plan",
    "wish_complete":    "plan",
    "wish_finished":    "plan",
    "wish_failed":      "error",
    "plan_ready":       "plan",
    "skill_dispatch":   "skill_start",
    # Skill lifecycle (emitted by skill_executor.py)
    "skill_start":      "skill_start",
    "skill_started":    "skill_start",
    "skill_end":        "skill_end",
    "skill_done":       "skill_end",
    "skill_failed":     "skill_end",
    "skill_completed":  "skill_end",
    "skill_truncated":  "skill_end",
    "skill_error":      "error",
    "skill_not_found":  "error",
    # Tool calls
    "tool_call":        "tool_call",
    "tool_result":      "tool_result",
    # Other
    "audit":            "audit",
    "audit_verdict":    "audit",
    "profile_update":   "profile_update",
    "budget_exceeded":  "error",
    "error":            "error",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_trace_id() -> str:
    return "trace_" + secrets.token_hex(8)


class TraceWriter:
    """Persists one trace row per emitted event.

    Lifecycle: created at the top of run_wish_sync, then passed to inner
    components either directly (skill_executor) or via the fan-out
    callback (existing on_event flow). Either way, every event seen
    becomes a row in cockpit_run_trace.

    The writer is intentionally tolerant of malformed events — any
    exception inside .record is caught and logged, never propagated. We
    do not want trace writing to break a wish run.
    """

    def __init__(
        self,
        store: Any,
        task_id: str,
        user_on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.store = store
        self.task_id = task_id
        self._user_on_event = user_on_event
        self._seq = 0
        # Track started_at of in-progress steps, keyed by (step_type, skill_name)
        # so we can compute duration_ms when the matching end event arrives.
        # Note: this only works if step start/end events arrive in order on
        # the same skill, which is the existing wish_runner behavior.
        self._open: dict[tuple, float] = {}

    def record(self, event: dict[str, Any]) -> None:
        """Persist one event as a trace row + forward to user callback."""
        try:
            self._persist(event)
        except Exception:
            logger.exception("trace writer persist failed; continuing")

        if self._user_on_event is not None:
            try:
                self._user_on_event(event)
            except Exception:
                logger.exception("user on_event callback failed; continuing")

    # The standalone callable form so this can be passed where a plain
    # callback is expected: pass writer.fan_out instead of writer.record.
    def fan_out(self, event: dict[str, Any]) -> None:
        self.record(event)

    # ──────────────────────────────────────────────────────────────

    def _persist(self, event: dict[str, Any]) -> None:
        raw_type = event.get("type") or "unknown"
        step_type = _TYPE_TO_STEP.get(raw_type, raw_type)
        skill = event.get("skill") or event.get("skill_name") or event.get("skill_id")
        tool = event.get("tool") or event.get("tool_name")

        # Payload: stash the whole event for forensic detail, minus the
        # fields we surface as first-class columns.
        payload = {k: v for k, v in event.items()
                   if k not in {"type", "skill", "skill_name", "skill_id",
                                "tool", "tool_name", "task_id",
                                "tokens_in", "tokens_out", "duration_ms"}}

        now = _now_iso()
        now_t = time.monotonic()

        # Track duration for paired start/end events.
        # Start and end of the same logical operation have DIFFERENT raw types
        # ("skill_start" vs "skill_end"), so pair on a synthetic kind, not on
        # step_type. Pair on (kind, identifier) — identifier is the skill or
        # tool name that disambiguates concurrent operations.
        duration_ms = None
        ended_at = None
        started_at = now

        pair_kind = None
        pair_id = None
        is_start = False
        is_end = False
        if raw_type in {"skill_start", "skill_started", "skill_dispatch"}:
            pair_kind, pair_id, is_start = "skill", skill, True
        elif raw_type in {"skill_end", "skill_done", "skill_failed",
                           "skill_completed", "skill_truncated"}:
            pair_kind, pair_id, is_end = "skill", skill, True
        elif raw_type == "tool_call":
            pair_kind, pair_id, is_start = "tool", tool, True
        elif raw_type == "tool_result":
            pair_kind, pair_id, is_end = "tool", tool, True
        elif raw_type == "wish_started":
            pair_kind, pair_id, is_start = "wish", self.task_id, True
        elif raw_type in {"wish_completed", "wish_complete", "wish_finished", "wish_failed"}:
            pair_kind, pair_id, is_end = "wish", self.task_id, True

        if pair_kind is not None:
            key = (pair_kind, pair_id)
            if is_end and key in self._open:
                start_t = self._open.pop(key)
                duration_ms = int((now_t - start_t) * 1000)
                ended_at = now
            elif is_start:
                self._open[key] = now_t

        # Explicit duration_ms in the event wins over our auto-paired one
        # (the emitter has a more authoritative clock for what it measured).
        if "duration_ms" in event and event["duration_ms"] is not None:
            try:
                duration_ms = int(event["duration_ms"])
                if ended_at is None:
                    ended_at = now
            except (ValueError, TypeError):
                pass

        self._seq += 1
        step = {
            "trace_id": _new_trace_id(),
            "task_id": self.task_id,
            "seq": self._seq,
            "step_type": step_type,
            "skill_name": skill,
            "tool_name": tool,
            "payload": payload if payload else None,
            "tokens_in": int(event.get("tokens_in", 0) or 0),
            "tokens_out": int(event.get("tokens_out", 0) or 0),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "error": event.get("error") if step_type == "error" else None,
        }
        self.store.append_trace_step(step)
