"""Unit tests for cockpit.rag — no network, no real embeddings.

Covers the contract surface: opt-in behavior, NoOp fallback, prompt
formatter, config defaults. Index/retrieve happy-path is exercised by the
M2 smoke test in tmp_index, not here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Make backend imports work
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


def test_is_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("COCKPIT_RAG_ENABLED", raising=False)
    from cockpit.rag.config import is_enabled
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_is_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("COCKPIT_RAG_ENABLED", val)
    from cockpit.rag.config import is_enabled
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_is_enabled_falsy(monkeypatch, val):
    monkeypatch.setenv("COCKPIT_RAG_ENABLED", val)
    from cockpit.rag.config import is_enabled
    assert is_enabled() is False


def test_get_retriever_noop_when_disabled(monkeypatch):
    """When COCKPIT_RAG_ENABLED is off, always NoOp regardless of other config."""
    monkeypatch.delenv("COCKPIT_RAG_ENABLED", raising=False)
    from cockpit.rag import RAGConfig, get_retriever, NoOpRetriever

    cfg = RAGConfig(embed_api_key="sk-fake")
    r = get_retriever(cfg)
    assert isinstance(r, NoOpRetriever)
    assert r.retrieve("any query") == []


def test_get_retriever_noop_when_no_key(monkeypatch):
    """Enabled but missing key → NoOp (can't make embedding calls)."""
    monkeypatch.setenv("COCKPIT_RAG_ENABLED", "1")
    from cockpit.rag import RAGConfig, get_retriever, NoOpRetriever

    cfg = RAGConfig(embed_api_key="")
    r = get_retriever(cfg)
    assert isinstance(r, NoOpRetriever)


def test_rag_config_from_env_and_llm(monkeypatch):
    """LLM key flows through to embed key by default (BYOK)."""
    monkeypatch.delenv("COCKPIT_RAG_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("COCKPIT_RAG_EMBED_BASE_URL", raising=False)
    from cockpit.rag import RAGConfig

    cfg = RAGConfig.from_env_and_llm(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="sk-dssk",
    )
    assert cfg.embed_api_key == "sk-dssk"
    assert cfg.embed_base_url == "https://api.deepseek.com/v1"


def test_rag_config_env_overrides_llm(monkeypatch):
    """Explicit COCKPIT_RAG_EMBED_* env wins over LLM defaults."""
    monkeypatch.setenv("COCKPIT_RAG_EMBED_API_KEY", "sk-explicit")
    monkeypatch.setenv("COCKPIT_RAG_EMBED_BASE_URL", "https://api.openai.com/v1")
    from cockpit.rag import RAGConfig

    cfg = RAGConfig.from_env_and_llm(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="sk-dssk",
    )
    assert cfg.embed_api_key == "sk-explicit"
    assert cfg.embed_base_url == "https://api.openai.com/v1"


def test_format_chunks_for_prompt_empty():
    from cockpit.rag.prompt_injector import format_chunks_for_prompt
    assert format_chunks_for_prompt([]) == ""


def test_format_chunks_for_prompt_includes_source_and_score():
    from cockpit.rag.prompt_injector import format_chunks_for_prompt
    from cockpit.rag.retriever import RetrievedChunk

    chunks = [
        RetrievedChunk(text="trust boundary is per-wish ephemeral",
                       source="COCKPIT_ARCHITECTURE.md", score=0.812, metadata={}),
        RetrievedChunk(text="planner uses few-shot",
                       source="planner.py", score=0.654, metadata={}),
    ]
    out = format_chunks_for_prompt(chunks)
    assert "RETRIEVED KNOWLEDGE CONTEXT" in out
    assert "COCKPIT_ARCHITECTURE.md" in out
    assert "trust boundary" in out
    assert "0.812" in out
    assert "0.654" in out
    assert "END RETRIEVED KNOWLEDGE" in out


def test_format_chunks_truncates_at_max_chars():
    from cockpit.rag.prompt_injector import format_chunks_for_prompt
    from cockpit.rag.retriever import RetrievedChunk

    # Each chunk ~800 chars; 10 chunks should overflow 2000-char limit
    big_text = "x" * 800
    chunks = [
        RetrievedChunk(text=big_text, source=f"f{i}.md", score=0.9, metadata={})
        for i in range(10)
    ]
    out = format_chunks_for_prompt(chunks, max_chars=2000)
    assert len(out) < 2400  # some overhead from header/footer/truncation msg
    assert "truncated for length" in out


def test_planner_integration_no_rag_when_disabled(monkeypatch):
    """End-to-end: Planner with RAG disabled produces no rag_block."""
    monkeypatch.delenv("COCKPIT_RAG_ENABLED", raising=False)

    from cockpit.planner import _render_user_prompt
    out = _render_user_prompt("test wish", profile=None, rag_block="")
    assert "RETRIEVED KNOWLEDGE" not in out
    assert "USER: test wish" in out


def test_planner_renders_with_rag_block():
    """When rag_block is supplied, it appears between examples and the wish."""
    from cockpit.planner import _render_user_prompt
    rag = "==== RETRIEVED KNOWLEDGE ====\nfoo bar\n==== END ===="
    out = _render_user_prompt("test wish", profile=None, rag_block=rag)
    assert "RETRIEVED KNOWLEDGE" in out
    assert out.index("RETRIEVED KNOWLEDGE") < out.index("USER: test wish")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
