"""Planner Agent — calls user's OpenAI-compat LLM with TAR's private prompt + few-shots
to decompose a wish into structured intent + skill chain.

This module is closed-source IP: SYSTEM_PROMPT + few-shots + scoring logic.
LLM call goes through llm_client.chat_json() which uses the user's API key
(BYOK per PLAN_OSS_STRATEGY §2).

For testing / dev: pass `mock_planner` to bypass LLM entirely.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from . import llm_client
from .models import LLMConfig, Plan, WishTask

logger = logging.getLogger("cockpit.planner")

PROMPTS_DIR = Path(__file__).parent / "prompts"
# Allowed skills are loaded from the active pack manifest.
# OSS only ships Hello World Pack by default; curated paid packs (quant, content
# publishing, ...) register their own skills at install time via register_skills().
ALLOWED_SKILLS: set[str] = {"echo", "url-summarize"}


def register_skills(skill_names: list[str]) -> None:
    """Add skill names to the allowed list. Used by pack installers."""
    ALLOWED_SKILLS.update(skill_names)


def reset_skills_to_default() -> None:
    """Reset to OSS default (Hello World Pack only). Used by tests."""
    ALLOWED_SKILLS.clear()
    ALLOWED_SKILLS.update({"echo", "url-summarize"})


def _load_system_prompt() -> str:
    return (PROMPTS_DIR / "planner_system.txt").read_text(encoding="utf-8")


def _load_few_shots() -> list[dict]:
    with open(PROMPTS_DIR / "planner_few_shots.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _render_user_prompt(wish: str, profile: Optional[dict] = None, rag_block: str = "") -> str:
    """Build the user message from few-shots + retrieved knowledge + the actual wish.

    `rag_block` is optional pre-formatted RAG context (see cockpit/rag/
    prompt_injector.format_chunks_for_prompt). When empty, prompt shape is
    identical to pre-RAG version — exact backward compatibility.
    """
    few_shots = _load_few_shots()
    parts = [
        "Below are example inputs and the expected JSON outputs. Follow the same shape exactly.",
        "",
    ]
    for ex in few_shots:
        parts.append("--- EXAMPLE ---")
        parts.append(f"USER: {ex['user']}")
        parts.append(f"OUTPUT: {json.dumps(ex['output'], ensure_ascii=False)}")
        parts.append("")
    if rag_block:
        # Inject AFTER examples (so few-shots set the format) and BEFORE the
        # actual wish (so the model has the relevant context fresh in mind).
        parts.append(rag_block)
    parts.append("--- NEW WISH ---")
    if profile:
        parts.append(f"USER PROFILE (existing preferences): {json.dumps(profile, ensure_ascii=False)}")
    parts.append(f"USER: {wish}")
    parts.append("OUTPUT:")
    return "\n".join(parts)


def _validate_plan_dict(d: dict) -> tuple[bool, list[str]]:
    """Return (ok, errors). Don't raise; let caller decide."""
    errs = []
    if not isinstance(d.get("intent"), str):
        errs.append("missing or non-string `intent`")
    if not isinstance(d.get("sub_questions"), list):
        errs.append("missing or non-list `sub_questions`")
    chain = d.get("skill_chain")
    if not isinstance(chain, list):
        errs.append("missing or non-list `skill_chain`")
    else:
        for i, step in enumerate(chain):
            if not isinstance(step, dict):
                errs.append(f"skill_chain[{i}] not a dict")
                continue
            sk = step.get("skill")
            if sk not in ALLOWED_SKILLS:
                errs.append(f"skill_chain[{i}] invalid skill {sk!r} (not in allowed list)")
    if not isinstance(d.get("clarifications_needed", []), list):
        errs.append("`clarifications_needed` not a list")
    return (not errs), errs


# Type for an injectable mock — used in tests
MockPlanner = Callable[[WishTask, Optional[dict]], dict]


class Planner:
    """LLM-powered Planner.

    Two paths:
      1. real LLM: requires LLMConfig (BYOK). Reads system prompt + few-shots, calls LLM.
      2. mock_planner: callable returning a JSON-shape dict — used in tests.
    """

    def __init__(
        self,
        llm_config: Optional[LLMConfig] = None,
        mock_planner: Optional[MockPlanner] = None,
    ) -> None:
        self.llm_config = llm_config
        self.mock_planner = mock_planner

    def plan(self, task: WishTask, profile: Optional[dict] = None) -> Plan:
        if self.mock_planner is not None:
            d = self.mock_planner(task, profile)
            ok, errs = _validate_plan_dict(d)
            if not ok:
                logger.warning("mock planner produced invalid plan: %s", errs)
            return self._build_plan(task, d, raw="(mock)")

        if self.llm_config is None or not self.llm_config.is_configured():
            # No LLM, no mock — return minimal stub plan + clarification asking for setup
            return Plan(
                task_id=task.task_id,
                sub_questions=[{"raw_wish": task.wish}],
                skill_chain=[],
                raw_llm_response="(no llm config; configure X-LLM-* headers)",
            )

        system = _load_system_prompt()
        # Inject the current ALLOWED_SKILLS list into the system prompt so the
        # LLM picks real names instead of inventing plausible ones.
        skills_list = ", ".join(sorted(ALLOWED_SKILLS))
        system += f"\n\n## AVAILABLE SKILLS\n\n{skills_list}"

        # Knowledge L3 RAG injection (opt-in via COCKPIT_RAG_ENABLED env).
        # NoOp by default — get_retriever() falls back to no-op if disabled or
        # deps missing. The retrieved chunks are formatted and dropped into
        # the user prompt right before the actual wish.
        rag_block = ""
        try:
            from .rag import get_retriever
            from .rag.config import RAGConfig
            from .rag.prompt_injector import format_chunks_for_prompt

            rag_cfg = RAGConfig.from_env_and_llm(
                llm_base_url=self.llm_config.base_url,
                llm_api_key=self.llm_config.api_key,
            )
            retriever = get_retriever(rag_cfg)
            chunks = retriever.retrieve(task.wish)
            if chunks:
                rag_block = format_chunks_for_prompt(chunks)
                logger.info(
                    "rag injected %d chunks for wish task_id=%s (top score=%.3f)",
                    len(chunks), task.task_id, chunks[0].score,
                )
        except Exception as e:
            # RAG must NEVER block planning. Log and continue without it.
            logger.warning("rag retrieval failed; continuing without context: %s", e)

        user = _render_user_prompt(task.wish, profile=profile, rag_block=rag_block)
        try:
            resp = llm_client.chat_json(self.llm_config, system, user)
        except llm_client.LLMError as e:
            logger.error("LLM call failed: %s", e)
            return Plan(
                task_id=task.task_id,
                sub_questions=[{"raw_wish": task.wish, "_llm_error": str(e)}],
                skill_chain=[],
                raw_llm_response=f"(llm-error: {e})",
            )

        try:
            d = llm_client.parse_json_content(resp.content)
        except json.JSONDecodeError as e:
            logger.error("LLM returned non-JSON: %s", resp.content[:300])
            return Plan(
                task_id=task.task_id,
                sub_questions=[{"raw_wish": task.wish, "_parse_error": str(e)}],
                skill_chain=[],
                raw_llm_response=resp.content[:500],
            )

        ok, errs = _validate_plan_dict(d)
        if not ok:
            logger.warning("LLM produced invalid plan structure: %s", errs)
            d.setdefault("_validation_errors", errs)

        return self._build_plan(
            task,
            d,
            raw=f"tokens prompt={resp.prompt_tokens} completion={resp.completion_tokens}",
        )

    def _build_plan(self, task: WishTask, d: dict, raw: str = "") -> Plan:
        return Plan(
            task_id=task.task_id,
            sub_questions=d.get("sub_questions", []),
            skill_chain=[
                {**step, "args": step.get("args_inferred", {})}
                for step in d.get("skill_chain", [])
                if isinstance(step, dict) and step.get("skill") in ALLOWED_SKILLS
            ],
            raw_llm_response=raw,
        )
