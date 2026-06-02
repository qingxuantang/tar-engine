"""End-to-end test — full cockpit flow with mocked LLM.

Submits a wish → planner produces chain → auditor runs on each skill →
result persisted → readable via GET endpoints.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    """Tmp DB + tmp skills dir + reset module singletons."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in ["数据更新", "因子研究", "自动迭代", "选币策略配置"]:
        d = skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n# {name}\n\nClean skill for testing.\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("COCKPIT_DB", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("COCKPIT_SKILLS_DIR", str(skills_dir))

    # Reload modules so new env takes effect
    from cockpit import auditor_integration, store, profile_writer
    importlib.reload(store)
    importlib.reload(profile_writer)
    importlib.reload(auditor_integration)

    return tmp_path


def _mock_planner(wish, profile):
    """Returns a deterministic plan covering the parameter-search use case."""
    return {
        "intent": "parameter_search",
        "sub_questions": [
            {"id": "q1", "question": "Which factors does strategy A use?"},
        ],
        "skill_chain": [
            {"skill": "数据更新", "purpose": "ensure data fresh", "args_inferred": {}},
            {"skill": "因子研究", "purpose": "compute factor values", "args_inferred": {}},
            {"skill": "自动迭代", "purpose": "grid search params", "args_inferred": {}},
            {"skill": "选币策略配置", "purpose": "pick optimal", "args_inferred": {}},
        ],
        "clarifications_needed": [],
    }


def test_full_flow_with_mock_llm(e2e_env):
    """submit → planner-with-mock → audit → persist → read back."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Build app with patched planner so we don't need a real LLM
    from cockpit import router as router_module
    importlib.reload(router_module)
    from cockpit.models import LLMConfig, WishTask
    from cockpit.planner import Planner

    # Monkey-patch Planner.plan to use the mock — deeper than the mock_planner kwarg
    # because router constructs Planner internally.
    real_init = Planner.__init__

    def patched_init(self, llm_config=None, mock_planner=None):
        real_init(self, llm_config=llm_config, mock_planner=_mock_planner)

    Planner.__init__ = patched_init  # type: ignore

    try:
        app = FastAPI()
        app.include_router(router_module.router)
        client = TestClient(app)

        # Submit wish
        r = client.post(
            "/api/cockpit/wish",
            json={"wish": "find optimal params for strategy A", "user_id": "u1"},
            headers={
                "X-LLM-Base-Url": "https://fake/v1",
                "X-LLM-Api-Key": "sk-fake",
                "X-LLM-Model": "test",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()

        task_id = body["task_id"]
        assert task_id.startswith("tsk_")

        # Plan should have 4 skills
        skill_chain = body["plan"]["skill_chain"]
        assert len(skill_chain) == 4
        assert {s["skill"] for s in skill_chain} == {"数据更新", "因子研究", "自动迭代", "选币策略配置"}

        # Audit verdicts: each skill audited (clean SKILL.md, no alerts → grade A)
        verdicts = body["audit_verdicts"]
        assert len(verdicts) == 4
        for v in verdicts:
            assert v["status"] == "audited"
            assert v["grade"] == "A"
            assert v["score"] == 100
            assert v["alerts"] == []

        # Chain summary
        sum_ = body["chain_summary"]
        assert sum_["audited_skills"] == 4
        assert sum_["missing_skills"] == 0
        assert sum_["chain_score"] == 100.0
        assert sum_["chain_risk_class"] == "Low"

        # Read back via GET endpoint
        r2 = client.get(f"/api/cockpit/wish/{task_id}")
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["wish"]["status"] == "dispatched"
        assert body2["plan"]["skill_chain"][0]["skill"] == "数据更新"
        assert len(body2["result"]["audit_verdicts"]) == 4

        # List wishes
        r3 = client.get("/api/cockpit/wishes?user_id=u1")
        assert r3.status_code == 200
        wishes = r3.json()
        assert len(wishes) == 1
        assert wishes[0]["task_id"] == task_id

        # Profile read
        r4 = client.get("/api/cockpit/profile/u1")
        assert r4.status_code == 200
        prof = r4.json()
        assert prof["user_id"] == "u1"
        assert "factor_preferences" in prof
    finally:
        Planner.__init__ = real_init  # type: ignore


def test_wishes_persist_across_app_restarts(e2e_env):
    """After 'restart' (new TestClient instance), wishes from DB still visible."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cockpit import router as router_module
    importlib.reload(router_module)
    from cockpit.planner import Planner

    real_init = Planner.__init__
    Planner.__init__ = lambda self, **kw: real_init(self, mock_planner=_mock_planner)  # type: ignore

    try:
        # Round 1
        app1 = FastAPI()
        app1.include_router(router_module.router)
        c1 = TestClient(app1)
        r = c1.post("/api/cockpit/wish", json={"wish": "test", "user_id": "u1"})
        task_id = r.json()["task_id"]

        # "Restart" — reset module singletons, reload router
        from cockpit import store, profile_writer
        importlib.reload(store)
        importlib.reload(profile_writer)
        importlib.reload(router_module)

        # Round 2 — fresh app, same DB
        app2 = FastAPI()
        app2.include_router(router_module.router)
        c2 = TestClient(app2)
        r2 = c2.get(f"/api/cockpit/wish/{task_id}")
        assert r2.status_code == 200
        assert r2.json()["wish"]["task_id"] == task_id
    finally:
        Planner.__init__ = real_init  # type: ignore
