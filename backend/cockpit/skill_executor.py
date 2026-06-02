"""Engine-side skill executor — UX A pivot (2026-05-10).

Runs a SKILL.md from /cc-skills/<name>/ against the user's BYOK LLM via
OpenAI-compat tool-use loop. Replaces the old IDE-side skill execution
path (Cf. COCKPIT_ARCHITECTURE §7 + §10 decision log).

Lifecycle:
    load_skill(name) → SKILL_TEXT
    execute_skill(task, skill_name, sub_goal, cfg) →
        1. Build system prompt: skill instructions + wish context + sub-goal
        2. Tool-use loop with LLM (max 10 iterations)
        3. Tools: read_file / write_file / list_files / run_bash (sandboxed)
        4. Stream tool events to event_store (Auditor consumes)
        5. Return SkillRunResult with token usage

Per-wish ephemeral workspace: /tmp/wish_workspaces/<task_id>/<skill>/
Workspace is scrubbed at wish_completed via cleanup_workspace() to satisfy
trust boundary §7 (per-wish ephemeral, not persisted).

Token usage accumulated and returned. The token_budget enforcer (separate
module) gates LLM calls before they are issued.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from event_store import event_store

from .models import LLMConfig, WishTask
from .token_budget import BudgetConfig, BudgetExceeded, charge_after_call

logger = logging.getLogger("cockpit.executor")

CC_SKILLS_DIR = Path(os.getenv("COCKPIT_SKILLS_DIR", "/cc-skills"))
PACKS_DIR = Path(os.getenv("COCKPIT_PACKS_DIR", "/app/packs"))
WORKSPACE_BASE = Path(os.getenv("COCKPIT_WORKSPACE_DIR", "/tmp/wish_workspaces"))
MAX_TOOL_ITERATIONS = 10
DEFAULT_BASH_TIMEOUT_S = 30
TOOL_RESULT_TRUNCATE_AT = 6000  # don't blow up context with huge tool outputs


class SkillExecutorError(Exception):
    """Raised when skill execution cannot proceed."""


class SkillNotFound(SkillExecutorError):
    """SKILL.md not found at expected path."""


@dataclass
class SkillRunResult:
    skill_name: str
    success: bool
    final_text: str = ""
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "success": self.success,
            "final_text": self.final_text,
            "iterations": self.iterations,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "events_count": len(self.events),
            "error": self.error,
            "session_id": self.session_id,
        }


# ---------------------------------------------------------------------------
# Skill bundle loading
# ---------------------------------------------------------------------------


def find_skill_path(skill_name: str) -> Optional[Path]:
    """Locate a skill's directory across known skill sources.

    Search order:
      1. CC_SKILLS_DIR / <name> / SKILL.md  (Claude Code host-mounted skills)
      2. PACKS_DIR / <any pack> / skills / <name> / SKILL.md  (bundled packs)

    Returns the skill directory (containing SKILL.md and any scripts/), or None.
    """
    # 1. Host-mounted Claude Code skill
    primary = CC_SKILLS_DIR / skill_name
    if (primary / "SKILL.md").exists():
        return primary

    # 2. Bundled packs — scan all packs/<pack>/skills/<name>/SKILL.md
    if PACKS_DIR.exists():
        for pack_dir in PACKS_DIR.iterdir():
            if not pack_dir.is_dir():
                continue
            candidate = pack_dir / "skills" / skill_name
            if (candidate / "SKILL.md").exists():
                return candidate

    return None


def load_skill(skill_name: str) -> str:
    """Load SKILL.md content from any known skill source.

    Raises SkillNotFound if the skill is missing.
    """
    skill_dir = find_skill_path(skill_name)
    if skill_dir is None:
        raise SkillNotFound(
            f"skill '{skill_name}' not found in {CC_SKILLS_DIR} "
            f"or any pack under {PACKS_DIR}"
        )
    return (skill_dir / "SKILL.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-wish workspace
# ---------------------------------------------------------------------------


def _workspace_dir(task_id: str, skill_name: str) -> Path:
    """Per-wish per-skill ephemeral workspace.

    Sanitizes skill_name for filesystem use (Chinese names need this).
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in skill_name)
    p = WORKSPACE_BASE / task_id / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def cleanup_workspace(task_id: str) -> int:
    """Per-wish ephemeral cleanup. Called at wish_completed.

    Removes /tmp/wish_workspaces/<task_id>/. Returns bytes freed (best effort).
    Critical for honoring the per-wish ephemeral trust boundary (§7).
    """
    p = WORKSPACE_BASE / task_id
    if not p.exists():
        return 0
    total = 0
    for fp in p.rglob("*"):
        if fp.is_file():
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    shutil.rmtree(p, ignore_errors=True)
    logger.info("cleanup_workspace task=%s freed_bytes=%d", task_id, total)
    return total


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI-compat function calling schema)
# ---------------------------------------------------------------------------


def _tools_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a file. Path may be relative to the per-wish workspace, "
                    "or absolute under /cc-skills/ for skill bundle resources."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "Write content to a file in the per-wish workspace. "
                    "Creates parent dirs as needed. Cannot write outside workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List entries in a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": (
                    "Run a bash command. Working dir is the per-wish workspace. "
                    "30 second timeout by default."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"},
                        "timeout_s": {"type": "integer", "default": 30},
                    },
                    "required": ["cmd"],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Path scoping (defensive: stop skills from reading /etc/shadow, etc.)
# ---------------------------------------------------------------------------


def _resolve_path(workspace: Path, requested: str, *, for_write: bool = False) -> Path:
    """Resolve a tool's path argument against the workspace, with scoping.

    Read paths allowed: workspace/* or /cc-skills/*
    Write paths allowed: workspace/* only
    """
    p = Path(requested)
    if p.is_absolute():
        s = str(p.resolve())
        if for_write:
            if not s.startswith(str(workspace.resolve())):
                raise SkillExecutorError(f"write outside workspace not allowed: {requested}")
        else:
            ws_str = str(workspace.resolve())
            cc_str = str(CC_SKILLS_DIR.resolve())
            if not (s.startswith(ws_str) or s.startswith(cc_str)):
                raise SkillExecutorError(f"path outside allowed scope: {requested}")
        return Path(s)
    # Relative path → workspace
    return (workspace / p).resolve()


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


def _exec_tool(name: str, args: dict, workspace: Path) -> str:
    """Execute one tool call, return string result for the LLM."""
    try:
        if name == "read_file":
            path = _resolve_path(workspace, args.get("path", ""))
            text = path.read_text(encoding="utf-8")
            if len(text) > TOOL_RESULT_TRUNCATE_AT:
                text = text[:TOOL_RESULT_TRUNCATE_AT] + f"\n... [truncated, {len(text) - TOOL_RESULT_TRUNCATE_AT} bytes]"
            return text

        if name == "write_file":
            path = _resolve_path(workspace, args.get("path", ""), for_write=True)
            content = args.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"wrote {len(content)} bytes to {path}"

        if name == "list_files":
            path = _resolve_path(workspace, args.get("path", "."))
            if not path.exists():
                return f"path does not exist: {path}"
            if not path.is_dir():
                return f"not a directory: {path}"
            entries = []
            for c in sorted(path.iterdir()):
                kind = "dir" if c.is_dir() else "file"
                entries.append(f"{kind}\t{c.name}")
            return "\n".join(entries) if entries else "(empty)"

        if name == "run_bash":
            timeout = int(args.get("timeout_s", DEFAULT_BASH_TIMEOUT_S))
            cmd = args.get("cmd", "")
            if not cmd:
                return "error: empty cmd"
            r = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                cwd=str(workspace),
                timeout=timeout,
            )
            stdout = r.stdout[:TOOL_RESULT_TRUNCATE_AT // 2]
            stderr = r.stderr[:TOOL_RESULT_TRUNCATE_AT // 4]
            return f"exit={r.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"

        return f"unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return f"tool '{name}' timed out after {timeout}s"
    except SkillExecutorError as e:
        return f"tool '{name}' error: {e}"
    except Exception as e:  # noqa: BLE001 — surface any tool failure to LLM
        logger.exception("tool '%s' unexpected failure", name)
        return f"tool '{name}' unexpected error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# LLM tool-use call (OpenAI-compatible)
# ---------------------------------------------------------------------------


def _llm_chat_with_tools(
    cfg: LLMConfig,
    messages: list[dict],
    tools: list[dict],
    *,
    timeout_s: float = 120.0,
) -> dict:
    """Single LLM call with tool definitions. Returns parsed JSON response.

    No retry here — caller decides retry semantics. Token budget enforcer should
    pre-check before this call (Task C).
    """
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post(url, json=payload, headers=headers)
    if r.status_code != 200:
        raise SkillExecutorError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def execute_skill(
    *,
    task: WishTask,
    skill_name: str,
    sub_goal: str,
    cfg: LLMConfig,
    extra_context: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
    budget_config: Optional[BudgetConfig] = None,
) -> SkillRunResult:
    """Execute a single skill against user's BYOK LLM.

    Args:
        task: the parent WishTask (used for task_id / user_id / wish text).
        skill_name: name as it appears in /cc-skills/<name>/.
        sub_goal: Planner-derived sub-question this skill should solve.
        cfg: user's LLM config (BYOK; never persisted).
        extra_context: optional preceding output (e.g., previous skill's summary).
        on_event: optional callback invoked for each emitted event (for streaming
            to Telegram / WebSocket).
        budget_config: per-wish + per-day caps. Defaults to env-driven config.

    Returns:
        SkillRunResult.

    Note: budget enforcement is post-call — if a single LLM call pushes the wish
    past its cap, that call's tokens ARE charged (we already paid), but no further
    LLM calls are made and the run terminates with success=False, error="budget_exceeded:<kind>".
    """
    skill_text = load_skill(skill_name)
    workspace = _workspace_dir(task.task_id, skill_name)

    # Stage the skill bundle (scripts / references / etc) into the workspace
    # so the LLM can invoke them with relative paths like `python3 scripts/foo.py`.
    # SKILL.md itself stays in skill_text (system prompt); the rest of the bundle
    # is copied so tool calls work without absolute paths.
    skill_dir = find_skill_path(skill_name)
    if skill_dir is not None:
        for item in skill_dir.iterdir():
            if item.name == "SKILL.md":
                continue  # SKILL.md is in the system prompt, no need to stage
            dest = workspace / item.name
            try:
                if item.is_dir():
                    if not dest.exists():
                        shutil.copytree(item, dest)
                else:
                    if not dest.exists():
                        shutil.copy2(item, dest)
            except Exception as e:
                logger.warning(
                    "failed to stage skill bundle item %s: %s", item, e
                )

    session_id = f"cockpit-{task.task_id}-{skill_name.replace(' ', '_')[:32]}"
    event_store.ensure_session(
        session_id,
        meta={
            "wish_task_id": task.task_id,
            "user_id": task.user_id,
            "skill": skill_name,
            "source": "cockpit_skill_executor",
        },
    )

    events_buffered: list[dict[str, Any]] = []

    def emit(e: dict) -> None:
        events_buffered.append(e)
        if on_event:
            try:
                on_event(e)
            except Exception:
                logger.exception("on_event callback failed; continuing")

    # Knowledge L3 RAG retrieval (opt-in via COCKPIT_RAG_ENABLED env).
    # We blend the skill name + sub_goal as the query — both signal what
    # domain context would help this particular run.
    rag_block = ""
    try:
        from .rag import get_retriever
        from .rag.config import RAGConfig
        from .rag.prompt_injector import format_chunks_for_prompt

        rag_cfg = RAGConfig.from_env_and_llm(
            llm_base_url=cfg.base_url,
            llm_api_key=cfg.api_key,
        )
        retriever = get_retriever(rag_cfg)
        rag_query = f"{skill_name} — {sub_goal}"
        chunks = retriever.retrieve(rag_query)
        if chunks:
            rag_block = format_chunks_for_prompt(chunks)
            logger.info(
                "rag injected %d chunks for skill=%s sub_goal=%r (top=%.3f)",
                len(chunks), skill_name, sub_goal[:60], chunks[0].score,
            )
    except Exception as e:
        # RAG failures must never block skill execution.
        logger.warning("rag retrieval failed in skill_executor; continuing: %s", e)

    # Merge RAG block into extra_context (which is a free-form prepend slot)
    merged_context = "\n\n".join(filter(None, [rag_block, extra_context or ""]))

    system_prompt = _build_system_prompt(
        wish=task.wish,
        sub_goal=sub_goal,
        skill_name=skill_name,
        skill_text=skill_text,
        workspace=workspace,
        extra_context=merged_context,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Sub-goal: {sub_goal}"},
    ]

    tools = _tools_schema()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    skill_started_at = time.monotonic()

    emit({"type": "skill_started", "skill": skill_name, "sub_goal": sub_goal})

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            resp = _llm_chat_with_tools(cfg, messages, tools)
        except SkillExecutorError as e:
            err = f"LLM call failed at iteration {iteration}: {e}"
            emit({
                "type": "skill_error", "skill": skill_name, "error": err,
                "tokens_in": total_prompt_tokens,
                "tokens_out": total_completion_tokens,
                "duration_ms": int((time.monotonic() - skill_started_at) * 1000),
            })
            _flush(session_id, events_buffered)
            return SkillRunResult(
                skill_name=skill_name,
                success=False,
                error=err,
                iterations=iteration,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                events=events_buffered,
                session_id=session_id,
            )

        usage = resp.get("usage") or {}
        call_prompt_tokens = int(usage.get("prompt_tokens", 0))
        call_completion_tokens = int(usage.get("completion_tokens", 0))
        total_prompt_tokens += call_prompt_tokens
        total_completion_tokens += call_completion_tokens

        # Charge against budget (records to store + raises if cap blown)
        try:
            charge_after_call(
                task_id=task.task_id,
                user_id=task.user_id,
                model=cfg.model,
                prompt_tokens=call_prompt_tokens,
                completion_tokens=call_completion_tokens,
                skill=skill_name,
                config=budget_config,
            )
        except BudgetExceeded as e:
            err = f"budget_exceeded:{e.kind} current={e.current} limit={e.limit}"
            emit({
                "type": "budget_exceeded", "skill": skill_name, "kind": e.kind,
                "current": e.current, "limit": e.limit,
                "tokens_in": total_prompt_tokens,
                "tokens_out": total_completion_tokens,
                "duration_ms": int((time.monotonic() - skill_started_at) * 1000),
            })
            _flush(session_id, events_buffered)
            return SkillRunResult(
                skill_name=skill_name,
                success=False,
                error=err,
                iterations=iteration + 1,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                events=events_buffered,
                session_id=session_id,
            )
        except KeyError:
            # task_id missing in store (test scenarios where caller didn't persist
            # the wish). Log + continue — budget enforcement is best-effort here.
            logger.warning("budget charge skipped: task_id %s not in store", task.task_id)

        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            err = f"LLM response shape error at iter {iteration}: {e}; body keys={list(resp.keys())}"
            emit({
                "type": "skill_error", "skill": skill_name, "error": err,
                "tokens_in": total_prompt_tokens,
                "tokens_out": total_completion_tokens,
                "duration_ms": int((time.monotonic() - skill_started_at) * 1000),
            })
            _flush(session_id, events_buffered)
            return SkillRunResult(
                skill_name=skill_name,
                success=False,
                error=err,
                iterations=iteration,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                events=events_buffered,
                session_id=session_id,
            )
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            final_text = msg.get("content") or ""
            emit({
                "type": "skill_completed",
                "skill": skill_name,
                "final_text": final_text,
                "iterations": iteration + 1,
                "tokens_in": total_prompt_tokens,
                "tokens_out": total_completion_tokens,
                "duration_ms": int((time.monotonic() - skill_started_at) * 1000),
            })
            _flush(session_id, events_buffered)
            return SkillRunResult(
                skill_name=skill_name,
                success=True,
                final_text=final_text,
                iterations=iteration + 1,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                events=events_buffered,
                session_id=session_id,
            )

        for tc in tool_calls:
            fn_name = tc.get("function", {}).get("name", "")
            try:
                fn_args = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            emit({
                "type": "tool_call",
                "skill": skill_name,
                "tool": fn_name,
                "args": _redact_for_log(fn_args),
                "iteration": iteration,
            })

            tool_started_at = time.monotonic()
            result = _exec_tool(fn_name, fn_args, workspace)
            tool_duration_ms = int((time.monotonic() - tool_started_at) * 1000)

            emit({
                "type": "tool_result",
                "skill": skill_name,
                "tool": fn_name,
                "result_preview": result[:500],
                "result_size_bytes": len(result.encode("utf-8")) if isinstance(result, str) else 0,
                "iteration": iteration,
                "duration_ms": tool_duration_ms,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })

    err = f"hit max iterations ({MAX_TOOL_ITERATIONS}) without final response"
    emit({
        "type": "skill_truncated", "skill": skill_name, "error": err,
        "tokens_in": total_prompt_tokens,
        "tokens_out": total_completion_tokens,
        "duration_ms": int((time.monotonic() - skill_started_at) * 1000),
    })
    _flush(session_id, events_buffered)
    return SkillRunResult(
        skill_name=skill_name,
        success=False,
        error=err,
        iterations=MAX_TOOL_ITERATIONS,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        events=events_buffered,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_system_prompt(
    *,
    wish: str,
    sub_goal: str,
    skill_name: str,
    skill_text: str,
    workspace: Path,
    extra_context: str,
) -> str:
    return f"""You are an engine-side skill executor. You will execute a single skill in a sandboxed workspace, on behalf of the user, against their original wish.

USER'S WISH:
{wish}

YOUR SUB-GOAL FOR THIS SKILL:
{sub_goal}

SANDBOXED WORKSPACE: {workspace}
You can:
- read_file: read from workspace or /cc-skills/
- write_file: write to workspace only (NOT to skill bundle)
- list_files: any allowed path
- run_bash: in workspace, 30s default timeout

Rules:
- Stay focused on the sub-goal. Do NOT do unrelated work.
- Do NOT exfiltrate data to the network. The workspace is per-wish ephemeral.
- When you have completed the sub-goal, respond with a final text summary and STOP calling tools.

==== SKILL: {skill_name} ====
{skill_text}
==== END OF SKILL ====

{extra_context}
"""


def _redact_for_log(args: dict) -> dict:
    """Avoid logging huge content fields (write_file content, etc.)."""
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + f"... [{len(v) - 200} more chars]"
        else:
            out[k] = v
    return out


def _flush(session_id: str, events: list[dict[str, Any]]) -> None:
    """Best-effort batch-write events to event_store.

    Failures are logged but never raised — auditing should not block execution.
    """
    if not events:
        return
    try:
        event_store.store_batch(session_id, events)
    except Exception:
        logger.exception("failed to flush events to event_store; events dropped")
