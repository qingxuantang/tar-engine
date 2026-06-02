"""Dispatcher — DEPRECATED 2026-05-10 (UX A pivot).

⚠️  This module is no longer the primary path. UX A moves skill execution
into the engine itself (see backend/cockpit/wish_runner.py +
backend/cockpit/skill_executor.py). The router no longer calls
dispatcher.dispatch(); it calls run_wish_async() directly.

This module is kept for:
  - Reference / nostalgia (the original design assumed user-IDE skill exec)
  - Possible future alternative dispatch modes (e.g., MCP push to a CC instance)

Do not add new callers. If you find yourself reaching for this, you probably
want wish_runner.run_wish_async() instead.

Original docstring follows for context:
----
Routes a Plan to the user's local IDE for execution. MVP target: reuse V1
OpenClaw gateway routes (polling + streaming) — see COCKPIT_ARCHITECTURE §4.

Day 1: in-memory queue stub. Real OpenClaw integration in Day 4 per
PLAN_COCKPIT_MVP §3.

No LLM calls here — pure routing logic.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .models import Plan, WishTask


class Dispatcher:
    """Day-1 stub dispatcher. In-memory task queue per user_id."""

    def __init__(self) -> None:
        # user_id -> queue of (task, plan) pairs awaiting pickup
        self._queues: dict[str, asyncio.Queue] = {}

    def _queue_for(self, user_id: str) -> asyncio.Queue:
        if user_id not in self._queues:
            self._queues[user_id] = asyncio.Queue()
        return self._queues[user_id]

    async def dispatch(self, task: WishTask, plan: Plan) -> None:
        """Push (task, plan) to the user's queue. Local executor will pick up."""
        await self._queue_for(task.user_id).put({"task": task.to_dict(), "plan": plan.to_dict()})

    async def next_for_user(self, user_id: str, timeout: float = 30.0) -> Optional[dict]:
        """Local executor calls this to grab the next task. Returns None on timeout."""
        try:
            return await asyncio.wait_for(self._queue_for(user_id).get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


# Module-level singleton (Day-1 stub; real version may be DI'd)
_dispatcher: Optional[Dispatcher] = None


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
    return _dispatcher
