"""Tests for cockpit_run_trace + TraceWriter (Sprint A T1, 2026-05-19).

Verifies:
- Schema migration creates the table cleanly on a fresh DB
- TraceWriter persists events in order with monotonic seq
- Payload round-trips through JSON
- Start/end event pairing produces duration_ms
- User-supplied on_event callback still fires
- Errors inside the writer do not propagate
- get_trace returns rows in seq order
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cockpit.store import CockpitStore
from cockpit.trace_writer import TraceWriter


@pytest.fixture
def store(tmp_path):
    return CockpitStore(db_path=tmp_path / "cockpit.db")


@pytest.fixture
def wish(store):
    """Insert a wish row so trace FK is satisfied."""
    task_id = "task_trace_test_0001"
    store.save_wish({
        "task_id": task_id,
        "user_id": "u1",
        "wish": "test",
        "context": {},
        "status": "running",
        "created_at": "2026-05-19T00:00:00Z",
        "updated_at": "2026-05-19T00:00:00Z",
    })
    return task_id


def test_schema_creates_trace_table(store):
    conn = store._c()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cockpit_run_trace)").fetchall()}
    assert {"trace_id", "task_id", "seq", "step_type", "skill_name",
            "tool_name", "payload", "tokens_in", "tokens_out",
            "started_at", "ended_at", "duration_ms", "error"}.issubset(cols)


def test_writer_persists_events_in_seq_order(store, wish):
    writer = TraceWriter(store, wish)
    writer.record({"type": "wish_started", "task_id": wish})
    writer.record({"type": "skill_start", "skill_name": "research"})
    writer.record({"type": "tool_call", "tool_name": "read_file",
                   "tokens_in": 5})
    writer.record({"type": "tool_result", "tool_name": "read_file",
                   "tokens_out": 12})
    writer.record({"type": "skill_end", "skill_name": "research"})
    writer.record({"type": "wish_finished"})

    rows = store.get_trace(wish)
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert [r["step_type"] for r in rows] == [
        "plan", "skill_start", "tool_call", "tool_result", "skill_end", "plan",
    ]


def test_payload_roundtrips_through_json(store, wish):
    writer = TraceWriter(store, wish)
    writer.record({"type": "audit", "verdict": "ok", "issues": ["nit_1", "nit_2"]})
    rows = store.get_trace(wish)
    assert len(rows) == 1
    assert rows[0]["payload"]["verdict"] == "ok"
    assert rows[0]["payload"]["issues"] == ["nit_1", "nit_2"]


def test_token_counts_persist(store, wish):
    writer = TraceWriter(store, wish)
    writer.record({"type": "tool_call", "tool_name": "x",
                   "tokens_in": 100, "tokens_out": 250})
    rows = store.get_trace(wish)
    assert rows[0]["tokens_in"] == 100
    assert rows[0]["tokens_out"] == 250


def test_start_end_pairing_records_duration(store, wish):
    writer = TraceWriter(store, wish)
    writer.record({"type": "skill_start", "skill_name": "writer"})
    # Synthetic latency
    import time as _t
    _t.sleep(0.01)
    writer.record({"type": "skill_end", "skill_name": "writer"})

    rows = store.get_trace(wish)
    end_row = next(r for r in rows if r["step_type"] == "skill_end")
    assert end_row["duration_ms"] is not None
    assert end_row["duration_ms"] >= 10
    assert end_row["ended_at"] is not None


def test_user_callback_still_fires(store, wish):
    calls = []
    writer = TraceWriter(store, wish, user_on_event=lambda e: calls.append(e))
    writer.record({"type": "tool_call", "tool_name": "x"})
    writer.record({"type": "skill_end", "skill_name": "x"})
    assert len(calls) == 2
    assert calls[0]["type"] == "tool_call"


def test_persist_failure_does_not_propagate(store, wish, monkeypatch):
    writer = TraceWriter(store, wish)
    def boom(_step):
        raise RuntimeError("simulated db failure")
    monkeypatch.setattr(store, "append_trace_step", boom)
    # Should not raise — error is caught + logged
    writer.record({"type": "skill_start", "skill_name": "x"})


def test_user_callback_failure_does_not_propagate(store, wish):
    def explode(_evt):
        raise RuntimeError("user callback broken")
    writer = TraceWriter(store, wish, user_on_event=explode)
    # Should not raise even though user callback throws
    writer.record({"type": "tool_call", "tool_name": "x"})
    rows = store.get_trace(wish)
    assert len(rows) == 1  # trace row still persisted


def test_unknown_event_type_recorded_with_raw_type(store, wish):
    writer = TraceWriter(store, wish)
    writer.record({"type": "custom_marker_xyz", "note": "investigative"})
    rows = store.get_trace(wish)
    assert rows[0]["step_type"] == "custom_marker_xyz"
    assert rows[0]["payload"]["note"] == "investigative"


def test_get_trace_for_unknown_task_returns_empty(store):
    assert store.get_trace("no_such_task") == []


def test_existing_wishes_table_unaffected(store, wish):
    """Sanity: adding the new table does not break existing wish queries."""
    w = store.get_wish(wish)
    assert w["task_id"] == wish
    assert w["status"] == "running"


def test_seq_monotonic_across_many_events(store, wish):
    writer = TraceWriter(store, wish)
    for i in range(50):
        writer.record({"type": "tool_call", "tool_name": f"t{i}"})
    rows = store.get_trace(wish)
    assert [r["seq"] for r in rows] == list(range(1, 51))


def test_explicit_duration_overrides_pairing(store, wish):
    """T2: emitters can supply their own duration_ms — it wins over auto-pair."""
    writer = TraceWriter(store, wish)
    writer.record({"type": "skill_completed", "skill_name": "x",
                   "duration_ms": 12345,
                   "tokens_in": 100, "tokens_out": 50})
    rows = store.get_trace(wish)
    assert rows[0]["duration_ms"] == 12345
    assert rows[0]["tokens_in"] == 100
    assert rows[0]["tokens_out"] == 50
    assert rows[0]["ended_at"] is not None
    # duration_ms should not leak into the payload blob
    assert "duration_ms" not in (rows[0]["payload"] or {})


# ── T3: GET /api/cockpit/wish/{task_id}/trace ──────────────────────────


def test_trace_endpoint_returns_steps_with_summary(store, wish, monkeypatch):
    """T3: API endpoint exposes trace + summary totals."""
    import cockpit.store as store_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cockpit import router as router_mod

    monkeypatch.setattr(store_mod, "_store", store)
    monkeypatch.setattr(router_mod, "get_store", lambda: store)

    # Seed trace
    writer = TraceWriter(store, wish)
    writer.record({"type": "wish_started"})
    writer.record({"type": "skill_started", "skill_name": "writer"})
    writer.record({"type": "tool_call", "tool_name": "read_file"})
    writer.record({"type": "tool_result", "tool_name": "read_file",
                   "duration_ms": 25})
    writer.record({"type": "skill_completed", "skill_name": "writer",
                   "tokens_in": 100, "tokens_out": 40, "duration_ms": 2500})
    writer.record({"type": "wish_completed"})

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app)

    r = client.get(f"/api/cockpit/wish/{wish}/trace")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == wish
    assert body["step_count"] == 6
    assert body["summary"]["tokens_in"] == 100
    assert body["summary"]["tokens_out"] == 40
    # Total duration is sum of all per-step durations recorded
    assert body["summary"]["duration_ms"] >= 25 + 2500
    assert body["summary"]["by_step_type"]["skill_end"] == 1
    assert body["summary"]["by_step_type"]["tool_call"] == 1
    assert body["summary"]["by_step_type"]["tool_result"] == 1
    assert len(body["steps"]) == 6


def test_trace_endpoint_filters_by_step_type(store, wish, monkeypatch):
    import cockpit.store as store_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cockpit import router as router_mod

    monkeypatch.setattr(store_mod, "_store", store)
    monkeypatch.setattr(router_mod, "get_store", lambda: store)

    writer = TraceWriter(store, wish)
    writer.record({"type": "wish_started"})
    writer.record({"type": "tool_call", "tool_name": "a"})
    writer.record({"type": "tool_call", "tool_name": "b"})
    writer.record({"type": "wish_completed"})

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app)

    r = client.get(f"/api/cockpit/wish/{wish}/trace?step_type=tool_call")
    assert r.status_code == 200
    body = r.json()
    assert body["step_count"] == 2
    assert all(s["step_type"] == "tool_call" for s in body["steps"])


def test_trace_endpoint_filters_by_skill(store, wish, monkeypatch):
    import cockpit.store as store_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cockpit import router as router_mod

    monkeypatch.setattr(store_mod, "_store", store)
    monkeypatch.setattr(router_mod, "get_store", lambda: store)

    writer = TraceWriter(store, wish)
    writer.record({"type": "skill_started", "skill_name": "alpha"})
    writer.record({"type": "skill_completed", "skill_name": "alpha"})
    writer.record({"type": "skill_started", "skill_name": "beta"})
    writer.record({"type": "skill_completed", "skill_name": "beta"})

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app)

    r = client.get(f"/api/cockpit/wish/{wish}/trace?skill=beta")
    assert r.status_code == 200
    body = r.json()
    assert body["step_count"] == 2
    assert all(s["skill_name"] == "beta" for s in body["steps"])


def test_trace_endpoint_404_for_unknown_task(store, monkeypatch):
    import cockpit.store as store_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cockpit import router as router_mod

    monkeypatch.setattr(store_mod, "_store", store)
    monkeypatch.setattr(router_mod, "get_store", lambda: store)

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app)

    r = client.get("/api/cockpit/wish/no_such_task/trace")
    assert r.status_code == 404


def test_trace_endpoint_returns_empty_for_wish_without_trace(store, wish, monkeypatch):
    """Wishes that ran before the trace feature shipped get an empty list."""
    import cockpit.store as store_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cockpit import router as router_mod

    monkeypatch.setattr(store_mod, "_store", store)
    monkeypatch.setattr(router_mod, "get_store", lambda: store)

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app)

    r = client.get(f"/api/cockpit/wish/{wish}/trace")
    assert r.status_code == 200
    body = r.json()
    assert body["step_count"] == 0
    assert body["steps"] == []
    assert body["summary"]["tokens_in"] == 0


def test_wish_runner_writes_trace(store, monkeypatch, tmp_path):
    """End-to-end: run_wish_sync wires TraceWriter and records the lifecycle.

    Uses a one-step plan that fails (skill not found) so we don't depend on a
    working LLM mock — we just need to confirm the wish_started + skill_dispatch
    + wish_completed events landed in cockpit_run_trace.
    """
    from cockpit.models import LLMConfig, Plan, WishTask
    import cockpit.store as store_mod
    import cockpit.wish_runner as runner_mod

    # Pin the singleton to our test store
    monkeypatch.setattr(store_mod, "_store", store)
    monkeypatch.setattr(runner_mod, "get_store", lambda: store)

    skills_dir = tmp_path / "skills_does_not_exist"
    monkeypatch.setenv("COCKPIT_SKILLS_DIR", str(skills_dir))

    task = WishTask(wish="trace integration test", user_id="trace_test_user")
    store.save_wish(task.to_dict())
    plan = Plan(
        task_id=task.task_id,
        skill_chain=[{"skill": "ghost_skill", "sub_goal": "no-op"}],
    )
    cfg = LLMConfig(base_url="http://mock", api_key="sk-test", model="gpt-4o-mini")

    captured = []
    runner_mod.run_wish_sync(
        task=task, plan=plan, cfg=cfg,
        on_event=lambda e: captured.append(e),
    )

    # Trace was written. skill_dispatch is normalized to step_type="skill_start"
    rows = store.get_trace(task.task_id)
    assert len(rows) >= 3, f"expected wish_started + skill_dispatch + wish_completed, got {[r['step_type'] for r in rows]}"
    types_in_order = [r["step_type"] for r in rows]
    assert types_in_order[0] == "plan"          # wish_started
    assert "skill_start" in types_in_order      # skill_dispatch normalized
    assert types_in_order[-1] == "plan"         # wish_completed

    # User on_event still fired (backwards compat)
    user_types = [e.get("type") for e in captured]
    assert "wish_started" in user_types
    assert "wish_completed" in user_types
