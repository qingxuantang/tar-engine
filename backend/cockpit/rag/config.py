"""RAG config — env-driven, no persisted secrets.

Cf. COCKPIT_ARCHITECTURE §7 (BYOK) — the embedding API key is the same
per-request key the user passes in, never persisted on the engine.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cockpit.rag.config")


@dataclass
class RAGConfig:
    """Per-request RAG config.

    The embedding API key is BYOK: callers pass it in alongside the wish
    (same as LLMConfig). Default model is OpenAI text-embedding-3-small
    (cheap, good quality, OpenAI-compat).
    """

    embed_base_url: str = "https://api.openai.com/v1"
    embed_api_key: str = ""
    embed_model: str = "text-embedding-3-small"
    index_dir: Path = Path("/data/rag_index")
    top_k: int = 5
    # Minimum cosine similarity for a chunk to be included. Lower = more recall.
    similarity_cutoff: float = 0.3

    @classmethod
    def from_env_and_llm(cls, llm_base_url: str = "", llm_api_key: str = "") -> "RAGConfig":
        """Build config from env defaults + a user LLM config (BYOK key).

        Args:
            llm_base_url: user's LLM endpoint (used as default for embeddings —
                most OpenAI-compat providers serve embeddings on the same host).
            llm_api_key: user's API key (used per-request, never persisted).
        """
        return cls(
            embed_base_url=os.environ.get("COCKPIT_RAG_EMBED_BASE_URL") or llm_base_url or "https://api.openai.com/v1",
            embed_api_key=os.environ.get("COCKPIT_RAG_EMBED_API_KEY") or llm_api_key,
            embed_model=os.environ.get("COCKPIT_RAG_EMBED_MODEL", "text-embedding-3-small"),
            index_dir=Path(os.environ.get("COCKPIT_RAG_INDEX_DIR", "/data/rag_index")),
            top_k=int(os.environ.get("COCKPIT_RAG_TOP_K", "5")),
            similarity_cutoff=float(os.environ.get("COCKPIT_RAG_SIMILARITY_CUTOFF", "0.3")),
        )

    def is_ready(self) -> bool:
        """Can we make embedding calls? Needs key + module deps."""
        return bool(self.embed_api_key) and is_available()


def is_enabled() -> bool:
    """RAG opt-in env flag. Defaults to OFF — V3 baseline flow unchanged."""
    return os.environ.get("COCKPIT_RAG_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def is_available() -> bool:
    """Are the optional deps importable? If not, callers should NoOp."""
    try:
        import llama_index.core  # noqa: F401
        import chromadb  # noqa: F401
        from llama_index.embeddings.openai import OpenAIEmbedding  # noqa: F401
        from llama_index.vector_stores.chroma import ChromaVectorStore  # noqa: F401
        return True
    except Exception as e:  # pragma: no cover — depends on env
        logger.debug("RAG not available: %s", e)
        return False
