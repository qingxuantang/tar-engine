"""End-to-end test for UX A path — lightweight wish without real LLM.

Validates:
  Planner output → wish_runner.run_wish_sync → skill_executor.execute_skill
    → tool calls → workspace lifecycle → token budget → store persistence
    → cleanup_workspace at end (per-§7 trust boundary)

Uses mock httpx responses so we don't hit real LLM endpoints during CI.
A separate manual real-LLM test can be done by setting the env vars
COCKPIT_TEST_REAL_LLM=1 + LLM creds.

Run from repo root:
    python -m pytest backend/cockpit/tests/test_uxa_e2e.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# Make backend imports work
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch):
    """Tmp DB + tmp skills dir + tmp workspace base."""
    db_path = tmp_path / "cockpit.db"
    skills_dir = tmp_path / "cc-skills"
    workspace_base = tmp_path / "workspaces"
    skills_dir.mkdir()
    workspace_base.mkdir()

    monkeypatch.setenv("COCKPIT_DB", str(db_path))
    monkeypatch.setenv("COCKPIT_SKILLS_DIR", str(skills_dir))
    monkeypatch.setenv("COCKPIT_WORKSPACE_DIR", str(workspace_base))

    # Reload modules so they pick up the env
    from cockpit import store as store_mod
    from cockpit import skill_executor as se_mod

    store_mod.reset_store_for_tests()
    # Force the singleton to point to our test DB (DEFAULT_DB_PATH was captured at
    # import time so env var alone won't change it). get_store(db_path=...) sets
    # the singleton.
    store_mod.get_store(db_path=db_path)

    # skill_executor reads CC_SKILLS_DIR / WORKSPACE_BASE at import time;
    # patch them directly for test
    monkeypatch.setattr(se_mod, "CC_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(se_mod, "WORKSPACE_BASE", workspace_base)

    return {
        "db_path": db_path,
        "skills_dir": skills_dir,
        "workspace_base": workspace_base,
    }


def _mock_llm_responses(*scripted: dict):
    """Create an iterator of mock httpx responses returning the scripted JSON in order."""

    class _MockResp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    iterator = iter(scripted)

    def _post(url, json=None, headers=None):
        try:
            return _MockResp(next(iterator))
        except StopIteration:
            raise AssertionError(f"unexpected extra LLM call: {json}")

    return _post


def test_skill_executor_full_loop_with_mock_llm(isolated_env):
    """Skill executor: 2 tool calls then final answer."""
    from cockpit.models import LLMConfig, WishTask
    from cockpit.skill_executor import execute_skill
    from cockpit.store import CockpitStore

    skills_dir = isolated_env["skills_dir"]
    skill_dir = skills_dir / "research_helper"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "# Research Helper\n\nGiven a sub-goal, write a 3-bullet summary file.\n"
    )

    store = CockpitStore(db_path=isolated_env["db_path"])
    task = WishTask(wish="explain what FFmpeg is", user_id="test_user")
    store.save_wish(task.to_dict())

    cfg = LLMConfig(base_url="http://mock", api_key="sk-test", model="gpt-4o-mini")

    # Scripted LLM responses:
    # 1. Tool call: write_file
    # 2. Tool call: list_files
    # 3. Final assistant message
    scripted = [
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "summary.md",
                                "content": "- FFmpeg is a video codec library\n- Used by 90% of video pipelines\n- Maintained by ~10 people",
                            }),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 200, "completion_tokens": 50},
        },
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "list_files",
                            "arguments": json.dumps({"path": "."}),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 250, "completion_tokens": 30},
        },
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Done. Wrote summary.md with 3 bullet points about FFmpeg.",
                    "tool_calls": None,
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 280, "completion_tokens": 40},
        },
    ]

    captured_events = []
    def on_event(e):
        captured_events.append(e)

    with patch("httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.side_effect = _mock_llm_responses(*scripted)

        result = execute_skill(
            task=task,
            skill_name="research_helper",
            sub_goal="explain FFmpeg",
            cfg=cfg,
        )

    assert result.success, f"expected success, got error={result.error}"
    assert result.iterations == 3, f"expected 3 iterations, got {result.iterations}"
    assert result.prompt_tokens == 730  # 200 + 250 + 280
    assert result.completion_tokens == 120  # 50 + 30 + 40
    assert "summary.md" in result.final_text or "FFmpeg" in result.final_text

    # Verify the file was actually written
    summary = isolated_env["workspace_base"] / task.task_id / "research_helper" / "summary.md"
    assert summary.exists(), "skill should have written summary.md"
    assert "FFmpeg" in summary.read_text()

    # Verify token budget recorded usage
    wish = store.get_wish(task.task_id)
    usage = json.loads(wish["token_usage"])
    assert usage["total_tokens"] == 850
    assert usage["model"] == "gpt-4o-mini"
    assert "research_helper" in usage["by_skill"]


def test_wish_runner_orchestrates_chain(isolated_env):
    """Wish runner: runs 2 skills in sequence, aggregates result, cleans workspace."""
    from cockpit.models import LLMConfig, Plan, WishTask
    from cockpit.store import CockpitStore
    from cockpit.wish_runner import run_wish_sync

    skills_dir = isolated_env["skills_dir"]
    for name in ("step_one", "step_two"):
        d = skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name}\n\nDo your sub-goal.\n")

    from cockpit.store import get_store
    store = get_store()
    task = WishTask(wish="run a 2-step plan", user_id="test_user2")
    store.save_wish(task.to_dict())

    plan = Plan(
        task_id=task.task_id,
        skill_chain=[
            {"skill": "step_one", "sub_goal": "do step one"},
            {"skill": "step_two", "sub_goal": "do step two"},
        ],
    )

    cfg = LLMConfig(base_url="http://mock", api_key="sk-test", model="gpt-4o-mini")

    # Each skill: one final assistant message (no tool calls)
    scripted = [
        {  # step_one final
            "choices": [{
                "message": {"role": "assistant", "content": "step one done", "tool_calls": None},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
        {  # step_two final
            "choices": [{
                "message": {"role": "assistant", "content": "step two done", "tool_calls": None},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 150, "completion_tokens": 30},
        },
    ]

    with patch("httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.side_effect = _mock_llm_responses(*scripted)

        result = run_wish_sync(task=task, plan=plan, cfg=cfg)

    assert len(result["skill_results"]) == 2
    assert all(r["success"] for r in result["skill_results"])
    assert "step one done" in result["final_output"]
    assert "step two done" in result["final_output"]

    # Verify wish status updated to done
    wish = store.get_wish(task.task_id)
    assert wish["status"] == "done"
    assert wish["completed_at"] is not None

    # Verify workspace was cleaned up (per §7 trust boundary)
    workspace = isolated_env["workspace_base"] / task.task_id
    assert not workspace.exists(), "workspace must be scrubbed after wish_completed"


def test_budget_exceeded_aborts_chain(isolated_env, monkeypatch):
    """If a single LLM call pushes wish over budget, remaining skills don't run."""
    from cockpit.models import LLMConfig, Plan, WishTask
    from cockpit.store import CockpitStore
    from cockpit.wish_runner import run_wish_sync

    # Tight cap so first call breaches
    monkeypatch.setenv("COCKPIT_MAX_TOKENS_PER_WISH", "100")

    skills_dir = isolated_env["skills_dir"]
    for name in ("first", "second", "third"):
        d = skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name}\n")

    from cockpit.store import get_store
    store = get_store()
    task = WishTask(wish="three steps", user_id="bduser")
    store.save_wish(task.to_dict())

    plan = Plan(
        task_id=task.task_id,
        skill_chain=[
            {"skill": "first"},
            {"skill": "second"},
            {"skill": "third"},
        ],
    )
    cfg = LLMConfig(base_url="http://mock", api_key="sk-test", model="gpt-4o-mini")

    # First call: 200 tokens — already past 100 cap
    scripted = [
        {
            "choices": [{
                "message": {"role": "assistant", "content": "first done", "tool_calls": None},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 150, "completion_tokens": 50},
        },
    ]

    with patch("httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.side_effect = _mock_llm_responses(*scripted)

        result = run_wish_sync(task=task, plan=plan, cfg=cfg)

    # Only first skill ran (and breached budget); second + third never started
    assert len(result["skill_results"]) == 1, "budget abort should stop after first skill"
    assert not result["skill_results"][0]["success"]
    assert "budget_exceeded" in (result["skill_results"][0].get("error") or "")

    wish = store.get_wish(task.task_id)
    assert wish["status"] == "failed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
