"""Day 1 skeleton tests — verify cockpit module loads + endpoints respond."""

from __future__ import annotations

import asyncio

import pytest


def test_models_llmconfig_from_headers():
    from cockpit.models import LLMConfig

    cfg = LLMConfig.from_headers(
        {
            "X-LLM-Base-Url": "https://api.openai.com/v1",
            "X-LLM-Api-Key": "sk-fake",
            "X-LLM-Model": "gpt-4o",
            "Other": "ignore",
        }
    )
    assert cfg is not None
    assert cfg.is_configured()
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.model == "gpt-4o"
    # repr must not leak the api_key
    assert "sk-fake" not in repr(cfg)
    assert "***" in repr(cfg)


def test_models_llmconfig_missing_headers_returns_none():
    from cockpit.models import LLMConfig

    assert LLMConfig.from_headers({}) is None
    assert LLMConfig.from_headers({"X-LLM-Base-Url": "x"}) is None


def test_models_wishtask_id_unique():
    from cockpit.models import WishTask

    a = WishTask(wish="x", user_id="u1")
    b = WishTask(wish="x", user_id="u1")
    assert a.task_id != b.task_id
    assert a.task_id.startswith("tsk_")


def test_planner_no_config_returns_clarification():
    """No LLM + no mock → planner returns empty chain with hint to configure."""
    from cockpit.models import WishTask
    from cockpit.planner import Planner

    task = WishTask(wish="帮我找因子最优参数", user_id="u1")
    plan = Planner().plan(task)
    assert plan.skill_chain == []
    assert "no llm config" in (plan.raw_llm_response or "")


def test_planner_with_mock_param_search():
    from cockpit.models import WishTask
    from cockpit.planner import Planner

    def mock(task, profile):
        return {
            "intent": "parameter_search",
            "sub_questions": [{"id": "q1", "question": "ok"}],
            "skill_chain": [
                {"skill": "数据更新", "purpose": "x", "args_inferred": {}},
                {"skill": "自动迭代", "purpose": "y", "args_inferred": {}},
            ],
            "clarifications_needed": [],
        }

    task = WishTask(wish="帮我找因子最优参数", user_id="u1")
    plan = Planner(mock_planner=mock).plan(task)
    skills = [s["skill"] for s in plan.skill_chain]
    assert "数据更新" in skills
    assert "自动迭代" in skills


def test_planner_filters_invalid_skills():
    """If LLM hallucinates a skill not in allowed list, it should be filtered out."""
    from cockpit.models import WishTask
    from cockpit.planner import Planner

    def mock(task, profile):
        return {
            "intent": "x",
            "sub_questions": [],
            "skill_chain": [
                {"skill": "数据更新", "args_inferred": {}},
                {"skill": "fake_skill_does_not_exist", "args_inferred": {}},  # should be dropped
            ],
            "clarifications_needed": [],
        }

    task = WishTask(wish="x", user_id="u1")
    plan = Planner(mock_planner=mock).plan(task)
    skills = [s["skill"] for s in plan.skill_chain]
    assert skills == ["数据更新"]


def test_dispatcher_round_trip():
    from cockpit.dispatcher import Dispatcher
    from cockpit.models import Plan, WishTask

    async def run():
        d = Dispatcher()
        task = WishTask(wish="x", user_id="u1")
        plan = Plan(task_id=task.task_id, skill_chain=[{"skill": "数据更新"}])
        await d.dispatch(task, plan)
        item = await d.next_for_user("u1", timeout=1.0)
        assert item is not None
        assert item["task"]["task_id"] == task.task_id
        assert item["plan"]["skill_chain"][0]["skill"] == "数据更新"

    asyncio.run(run())


def test_profile_writer_get_returns_default_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_DB", str(tmp_path / "test.db"))
    from cockpit.profile_writer import ProfileWriter, reset_profile_writer_for_tests
    from cockpit.store import CockpitStore, reset_store_for_tests

    reset_store_for_tests()
    reset_profile_writer_for_tests()
    pw = ProfileWriter(store=CockpitStore(tmp_path / "test.db"))
    p = pw.get("u1")
    assert p["user_id"] == "u1"
    assert "factor_preferences" in p
    assert "risk_thresholds" in p


def test_profile_writer_update_persists_within_instance(tmp_path):
    from cockpit.profile_writer import ProfileWriter
    from cockpit.store import CockpitStore

    pw = ProfileWriter(store=CockpitStore(tmp_path / "test.db"))
    pw.update("u1", {"factor_preferences": {"frequently_used": ["MA34"]}})
    assert pw.get("u1")["factor_preferences"] == {"frequently_used": ["MA34"]}


def test_router_health_endpoint():
    """Smoke-test FastAPI route via TestClient (no server needed)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cockpit.router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    r = client.get("/api/cockpit/health")
    assert r.status_code == 200
    body = r.json()
    assert body["subsystem"] == "cockpit"


def test_router_submit_wish_without_llm_returns_empty_chain(tmp_path, monkeypatch):
    """Without a real LLM call, planner returns task_id but empty chain."""
    monkeypatch.setenv("COCKPIT_DB", str(tmp_path / "router.db"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cockpit.router import router
    from cockpit.store import reset_store_for_tests
    from cockpit.profile_writer import reset_profile_writer_for_tests

    reset_store_for_tests()
    reset_profile_writer_for_tests()

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    r = client.post(
        "/api/cockpit/wish",
        json={"wish": "帮我找因子最优参数", "user_id": "u1"},
        # No LLM headers → planner returns empty chain w/ "no llm config" hint
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"].startswith("tsk_")
    assert isinstance(body["plan"]["skill_chain"], list)


def test_router_submit_wish_rejects_empty_wish(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_DB", str(tmp_path / "router.db"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cockpit.router import router
    from cockpit.store import reset_store_for_tests

    reset_store_for_tests()

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    r = client.post("/api/cockpit/wish", json={"wish": "", "user_id": "u1"})
    assert r.status_code == 400
