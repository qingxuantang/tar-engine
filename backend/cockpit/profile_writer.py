"""Profile Writer (P-Write) — SQLite-backed.

Reads / writes user profile from cockpit.db. Memory-Palace-inspired schema:
  factor_preferences / risk_thresholds / parameter_search_history / resolved_conflicts

Day-3+: append-only history list for parameter searches; conflict surfacing in Day 4.
LLM summarization (PLAN_OSS_STRATEGY §12 Phase 2-3) is later.
"""

from __future__ import annotations

from typing import Any, Optional

from .store import CockpitStore, get_store


class ProfileWriter:
    """SQLite-backed profile per user."""

    def __init__(self, store: Optional[CockpitStore] = None) -> None:
        self._store = store

    def _s(self) -> CockpitStore:
        return self._store if self._store else get_store()

    def get(self, user_id: str) -> dict[str, Any]:
        return self._s().get_profile(user_id)

    def update(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        return self._s().update_profile(user_id, updates)

    def append_param_search(self, user_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        prof = self.get(user_id)
        history = list(prof.get("parameter_search_history") or [])
        history.append(entry)
        return self.update(user_id, {"parameter_search_history": history})

    def update_factor_preferences(
        self, user_id: str, frequently_used: Optional[list] = None,
        explicitly_disliked: Optional[list] = None,
    ) -> dict[str, Any]:
        prof = self.get(user_id)
        fp = dict(prof.get("factor_preferences") or {})
        if frequently_used is not None:
            fp["frequently_used"] = frequently_used
        if explicitly_disliked is not None:
            fp["explicitly_disliked"] = explicitly_disliked
        return self.update(user_id, {"factor_preferences": fp})


_pw: Optional[ProfileWriter] = None


def get_profile_writer(store: Optional[CockpitStore] = None) -> ProfileWriter:
    global _pw
    if _pw is None or store is not None:
        _pw = ProfileWriter(store=store)
    return _pw


def reset_profile_writer_for_tests() -> None:
    global _pw
    _pw = None
