"""Tests for cockpit/store.py — SQLite persistence."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_store():
    """Fresh CockpitStore on a tmp DB; cleaned up after."""
    from cockpit.store import CockpitStore, reset_store_for_tests

    reset_store_for_tests()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        s = CockpitStore(db_path=Path(path))
        # populate the connection
        s._c()
        yield s
    finally:
        if s._conn:
            s._conn.close()
        os.unlink(path)
        reset_store_for_tests()


def test_wish_save_get_round_trip(tmp_store):
    tmp_store.save_wish({
        "task_id": "tsk_aaa",
        "user_id": "u1",
        "wish": "find me the best params",
        "context": {"strategy": "OnlyBTC"},
        "status": "pending",
    })
    w = tmp_store.get_wish("tsk_aaa")
    assert w is not None
    assert w["wish"] == "find me the best params"
    assert w["context"] == {"strategy": "OnlyBTC"}
    assert w["status"] == "pending"


def test_wish_status_update(tmp_store):
    tmp_store.save_wish({
        "task_id": "tsk_b", "user_id": "u1", "wish": "x", "status": "pending",
    })
    tmp_store.update_wish_status("tsk_b", "dispatched")
    assert tmp_store.get_wish("tsk_b")["status"] == "dispatched"


def test_list_wishes_filter_by_user(tmp_store):
    for i in range(5):
        tmp_store.save_wish({
            "task_id": f"tsk_{i}", "user_id": ("u1" if i < 3 else "u2"),
            "wish": f"w{i}", "status": "pending",
        })
    assert len(tmp_store.list_wishes(user_id="u1")) == 3
    assert len(tmp_store.list_wishes(user_id="u2")) == 2
    assert len(tmp_store.list_wishes()) == 5


def test_plan_save_and_get(tmp_store):
    tmp_store.save_wish({
        "task_id": "tsk_p", "user_id": "u1", "wish": "x", "status": "pending",
    })
    tmp_store.save_plan({
        "task_id": "tsk_p",
        "intent": "parameter_search",
        "sub_questions": [{"id": "q1", "question": "ok"}],
        "skill_chain": [{"skill": "数据更新"}, {"skill": "自动迭代"}],
        "clarifications_needed": [],
    }, raw_llm_response="tokens=100")
    p = tmp_store.get_plan("tsk_p")
    assert p["intent"] == "parameter_search"
    assert len(p["skill_chain"]) == 2
    assert p["raw_llm_response"] == "tokens=100"


def test_run_result_save_and_get(tmp_store):
    tmp_store.save_wish({
        "task_id": "tsk_r", "user_id": "u1", "wish": "x", "status": "pending",
    })
    tmp_store.save_run_result({
        "task_id": "tsk_r",
        "skill_results": [{"skill": "数据更新", "ok": True}],
        "final_output": "all done",
        "audit_verdicts": [{"rule": "test", "severity": "info"}],
        "profile_updates": [{"factor_preferences": {"frequently_used": ["MA34"]}}],
        "completed_at": "2026-05-07T14:00:00+00:00",
    })
    r = tmp_store.get_run_result("tsk_r")
    assert r["final_output"] == "all done"
    assert r["skill_results"][0]["ok"] is True
    assert r["audit_verdicts"][0]["severity"] == "info"


def test_profile_default_shape(tmp_store):
    p = tmp_store.get_profile("u1")
    assert p["user_id"] == "u1"
    assert p["factor_preferences"] == {}
    assert p["risk_thresholds"] == {}
    assert p["parameter_search_history"] == []


def test_profile_persists_across_get_calls(tmp_store):
    tmp_store.save_profile("u1", {
        "version": "v0",
        "factor_preferences": {"frequently_used": ["MA34", "MA377"]},
        "risk_thresholds": {"max_drawdown": 0.15},
        "parameter_search_history": [],
        "resolved_conflicts": [],
    })
    p1 = tmp_store.get_profile("u1")
    p2 = tmp_store.get_profile("u1")
    assert p1["factor_preferences"]["frequently_used"] == ["MA34", "MA377"]
    assert p1 == p2


def test_profile_update_merges(tmp_store):
    p1 = tmp_store.update_profile("u1", {
        "factor_preferences": {"frequently_used": ["MA34"]},
    })
    assert p1["factor_preferences"]["frequently_used"] == ["MA34"]
    # update again — risk_thresholds should be set, factor_preferences should remain
    p2 = tmp_store.update_profile("u1", {
        "risk_thresholds": {"max_drawdown": 0.2},
    })
    assert p2["factor_preferences"]["frequently_used"] == ["MA34"]
    assert p2["risk_thresholds"]["max_drawdown"] == 0.2


def test_persistence_across_connection(tmp_store):
    """Open a 2nd connection to the same db file and verify the data is there."""
    from cockpit.store import CockpitStore

    tmp_store.save_wish({
        "task_id": "tsk_persist", "user_id": "u1", "wish": "test", "status": "pending",
    })
    # close the first connection
    tmp_store._conn.close()
    tmp_store._conn = None
    # open new
    s2 = CockpitStore(db_path=tmp_store.db_path)
    s2._c()
    assert s2.get_wish("tsk_persist") is not None
    s2._conn.close()


def test_profile_writer_uses_store(tmp_store):
    from cockpit.profile_writer import ProfileWriter

    pw = ProfileWriter(store=tmp_store)
    pw.append_param_search("u1", {"strategy": "A", "best": {"p1": 1}})
    pw.append_param_search("u1", {"strategy": "A", "best": {"p1": 2}})
    p = pw.get("u1")
    assert len(p["parameter_search_history"]) == 2
