"""Cockpit data models.

LLMConfig — per-request LLM endpoint config. **NOT persisted on Engine.**
  Comes from request headers X-LLM-Base-Url / X-LLM-Api-Key / X-LLM-Model,
  lives in memory for the duration of a single wish, dropped after use.
  Cf. PLAN_OSS_STRATEGY §3 / COCKPIT_ARCHITECTURE §3 (BYOK).

WishTask — represents one user wish + its lifecycle.
Plan — Planner's decomposition + skill chain output.
RunResult — final result after IDE executes the plan.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class LLMConfig:
    """User LLM endpoint config — passed per request, never persisted on Engine."""

    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> Optional["LLMConfig"]:
        """Build from request headers. Returns None if any required header missing."""
        # Header lookup is case-insensitive in HTTP
        h = {k.lower(): v for k, v in headers.items()}
        base_url = h.get("x-llm-base-url")
        api_key = h.get("x-llm-api-key")
        model = h.get("x-llm-model")
        if not (base_url and api_key and model):
            return None
        return cls(base_url=base_url.strip(), api_key=api_key.strip(), model=model.strip())

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def __repr__(self) -> str:  # never log the key
        return f"LLMConfig(base_url={self.base_url!r}, model={self.model!r}, api_key=***)"


@dataclass
class WishTask:
    """A single wish from user. ID generated server-side; user never authors it."""

    wish: str
    user_id: str
    context: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: f"tsk_{uuid.uuid4().hex[:16]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "pending"  # pending | planning | dispatched | running | done | failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "wish": self.wish,
            "context": self.context,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Plan:
    """Output of Planner — list of skills to invoke + structured intent."""

    task_id: str
    sub_questions: list[dict[str, Any]] = field(default_factory=list)
    skill_chain: list[dict[str, Any]] = field(default_factory=list)
    raw_llm_response: Optional[str] = None  # for debug/audit, may be redacted

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "sub_questions": self.sub_questions,
            "skill_chain": self.skill_chain,
        }


@dataclass
class RunResult:
    """Final result for a wish — populated as events flow back from user IDE."""

    task_id: str
    skill_results: list[dict[str, Any]] = field(default_factory=list)
    final_output: Optional[str] = None
    audit_verdicts: list[dict[str, Any]] = field(default_factory=list)
    profile_updates: list[dict[str, Any]] = field(default_factory=list)
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "skill_results": self.skill_results,
            "final_output": self.final_output,
            "audit_verdicts": self.audit_verdicts,
            "profile_updates": self.profile_updates,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
