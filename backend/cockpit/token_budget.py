"""Token budget enforcer — UX A pivot (2026-05-10).

Why it exists: under UX A the engine pays the LLM cost via user's BYOK key.
Without budget caps, a runaway tool-use loop or a verbose Planner could
silently incur big charges on the user's API account.

Two layers of cap:
  1. Per-wish cap: each individual wish has a hard ceiling on total tokens
     (and approximate USD cost). Hitting the cap aborts further LLM calls.
  2. Per-day per-user cap: across all wishes a user runs in a day, total cost
     can't exceed their daily ceiling. Used to prevent prompt-injection attacks
     or accidental abuse.

Both caps are post-call checks — i.e. we record the usage we already incurred,
then reject further calls if we're over. This means we can overshoot by ONE
call. For MVP that's acceptable; pre-call estimation is future work.

Hooks into skill_executor.execute_skill via charge_after_call().
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .store import CockpitStore, get_store

logger = logging.getLogger("cockpit.budget")


# ---------------------------------------------------------------------------
# Pricing table (USD per 1M tokens)
# ---------------------------------------------------------------------------
# Verify against provider docs occasionally; treat as approximate.
# Caller can override via COCKPIT_PRICING env var (JSON map).

_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o":              (2.50, 10.00),
    "gpt-4o-mini":         (0.15, 0.60),
    "gpt-4-turbo":         (10.00, 30.00),
    "gpt-3.5-turbo":       (0.50, 1.50),
    "o1-preview":          (15.00, 60.00),
    "o1-mini":             (3.00, 12.00),
    # Anthropic (via OpenAI-compat proxies)
    "claude-opus":         (15.00, 75.00),
    "claude-opus-4-7":     (15.00, 75.00),
    "claude-sonnet":       (3.00, 15.00),
    "claude-sonnet-4-6":   (3.00, 15.00),
    "claude-haiku":        (0.80, 4.00),
    "claude-haiku-4-5":    (0.80, 4.00),
    # DeepSeek
    "deepseek-chat":       (0.14, 0.28),
    "deepseek-reasoner":   (0.55, 2.19),
    # Doubao (approximate, ¥ converted)
    "doubao-pro":          (0.40, 1.00),
    # Qwen
    "qwen-max":            (0.55, 2.20),
    "qwen-plus":           (0.11, 0.28),
    # Default fallback if unknown
    "_default":            (1.00, 3.00),
}


def _load_pricing() -> dict[str, tuple[float, float]]:
    raw = os.environ.get("COCKPIT_PRICING")
    if not raw:
        return _DEFAULT_PRICING
    try:
        import json
        override = json.loads(raw)
        out = dict(_DEFAULT_PRICING)
        for k, v in override.items():
            if isinstance(v, list) and len(v) == 2:
                out[k] = (float(v[0]), float(v[1]))
        return out
    except Exception:
        logger.warning("invalid COCKPIT_PRICING env var; using defaults")
        return _DEFAULT_PRICING


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Best-effort USD cost estimate. Falls back to default rates for unknown models."""
    pricing = _load_pricing()
    rates = pricing.get(model) or pricing.get("_default") or (1.0, 3.0)
    in_rate, out_rate = rates
    return round(
        (prompt_tokens / 1_000_000) * in_rate +
        (completion_tokens / 1_000_000) * out_rate,
        6,
    )


# ---------------------------------------------------------------------------
# Budget config
# ---------------------------------------------------------------------------


@dataclass
class BudgetConfig:
    """Hard caps for token + cost."""

    # Per-wish caps
    max_tokens_per_wish: int = 200_000           # ~$0.50-$2 on most models
    max_cost_usd_per_wish: float = 5.0           # absolute USD ceiling per wish

    # Per-day caps
    max_tokens_per_user_per_day: int = 2_000_000  # ~$5-$20 on most models
    max_cost_usd_per_user_per_day: float = 50.0

    @classmethod
    def from_env(cls) -> "BudgetConfig":
        return cls(
            max_tokens_per_wish=int(os.environ.get("COCKPIT_MAX_TOKENS_PER_WISH", "200000")),
            max_cost_usd_per_wish=float(os.environ.get("COCKPIT_MAX_COST_PER_WISH", "5.0")),
            max_tokens_per_user_per_day=int(
                os.environ.get("COCKPIT_MAX_TOKENS_PER_DAY", "2000000")
            ),
            max_cost_usd_per_user_per_day=float(
                os.environ.get("COCKPIT_MAX_COST_PER_DAY", "50.0")
            ),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Raised when a wish or user has hit its budget cap.

    Carries the budget kind ('wish_tokens' / 'wish_cost' / 'day_tokens' / 'day_cost')
    and the offending number for logging / Telegram-side reporting.
    """

    def __init__(self, kind: str, current: float, limit: float, detail: str = ""):
        self.kind = kind
        self.current = current
        self.limit = limit
        super().__init__(
            f"budget exceeded ({kind}): current={current} > limit={limit}"
            + (f"; {detail}" if detail else "")
        )


# ---------------------------------------------------------------------------
# Charge + check
# ---------------------------------------------------------------------------


def _today_start_iso(now: Optional[datetime] = None) -> str:
    n = now or datetime.now(timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def charge_after_call(
    *,
    task_id: str,
    user_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    skill: str = "",
    config: Optional[BudgetConfig] = None,
    store: Optional[CockpitStore] = None,
) -> dict[str, float]:
    """Record token usage for a wish and check if any cap was exceeded.

    Call this AFTER each LLM call returns successfully. If a cap was exceeded
    AS A RESULT of this charge, raises BudgetExceeded — the caller should
    abort further LLM calls but the just-completed call's data is preserved.

    Returns a summary dict with the new running totals (useful for logging).
    """
    cfg = config or BudgetConfig.from_env()
    s = store or get_store()

    cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)
    new_total = s.add_wish_token_usage(
        task_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
        skill=skill,
        estimated_cost_usd=cost,
    )

    # Check per-wish caps
    if new_total["total_tokens"] > cfg.max_tokens_per_wish:
        raise BudgetExceeded(
            "wish_tokens",
            new_total["total_tokens"],
            cfg.max_tokens_per_wish,
            f"task_id={task_id}",
        )
    if new_total["estimated_cost_usd"] > cfg.max_cost_usd_per_wish:
        raise BudgetExceeded(
            "wish_cost",
            new_total["estimated_cost_usd"],
            cfg.max_cost_usd_per_wish,
            f"task_id={task_id}",
        )

    # Check per-day caps. Note: this counts only completed wishes today,
    # NOT the current in-flight wish. We add it explicitly.
    today = s.sum_user_tokens_since(user_id, _today_start_iso())
    daily_total_tokens = today["total_tokens"] + new_total["total_tokens"]
    daily_total_cost = today["estimated_cost_usd"] + new_total["estimated_cost_usd"]

    if daily_total_tokens > cfg.max_tokens_per_user_per_day:
        raise BudgetExceeded(
            "day_tokens",
            daily_total_tokens,
            cfg.max_tokens_per_user_per_day,
            f"user_id={user_id}",
        )
    if daily_total_cost > cfg.max_cost_usd_per_user_per_day:
        raise BudgetExceeded(
            "day_cost",
            daily_total_cost,
            cfg.max_cost_usd_per_user_per_day,
            f"user_id={user_id}",
        )

    return {
        "wish_total_tokens": new_total["total_tokens"],
        "wish_total_cost_usd": new_total["estimated_cost_usd"],
        "day_total_tokens": daily_total_tokens,
        "day_total_cost_usd": daily_total_cost,
    }


def format_cost_summary(usage: dict, model: str = "") -> str:
    """Render a compact human-readable cost line.

    Used by Telegram completion message + web UI inline display.
    """
    pt = usage.get("prompt_tokens", 0)
    ct = usage.get("completion_tokens", 0)
    total = pt + ct
    cost = usage.get("estimated_cost_usd", 0.0)
    line = f"{total:,} tokens (in={pt:,} out={ct:,})"
    if cost > 0:
        line += f" ≈ ${cost:.4f}"
    if model:
        line += f" via {model}"
    return line
