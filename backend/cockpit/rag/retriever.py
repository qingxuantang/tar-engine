"""Cockpit retriever — query interface to the LlamaIndex / Chroma store.

Two implementations:
  - `LlamaIndexRetriever`: real retriever, needs deps + a valid embedding key
  - `NoOpRetriever`: returns empty list. Used as a transparent fallback when
    RAG is disabled / unavailable so callers don't need to branch.

Callers should always use `get_retriever(cfg)` — it picks the right one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .config import RAGConfig, is_available, is_enabled

logger = logging.getLogger("cockpit.rag.retriever")


@dataclass
class RetrievedChunk:
    """A retrieved chunk of context with provenance + score."""

    text: str
    source: str          # e.g. "engine/docs/COCKPIT_ARCHITECTURE.md"
    score: float         # cosine similarity, higher = more relevant
    metadata: dict[str, Any]


class Retriever(Protocol):
    """Protocol for retrievers. Both Real and NoOp implement this."""

    def retrieve(self, query: str, *, top_k: Optional[int] = None) -> list[RetrievedChunk]: ...


class NoOpRetriever:
    """Returns nothing. Used when RAG is disabled or deps missing."""

    def retrieve(self, query: str, *, top_k: Optional[int] = None) -> list[RetrievedChunk]:
        return []


class LlamaIndexRetriever:
    """Real retriever backed by ChromaDB + OpenAI-compat embeddings.

    Lazily loads the index on first query. The index is read-only for the
    retriever — building happens via indexer.build_index().
    """

    def __init__(self, cfg: RAGConfig, collection_name: str = "cockpit_knowledge_l3"):
        self.cfg = cfg
        self.collection_name = collection_name
        self._retriever: Any = None  # llama_index.core.retrievers.BaseRetriever lazily set

    def _ensure_loaded(self) -> bool:
        if self._retriever is not None:
            return True

        try:
            import chromadb
            from llama_index.core import Settings, VectorStoreIndex
            from llama_index.embeddings.openai import OpenAIEmbedding
            from llama_index.vector_stores.chroma import ChromaVectorStore
        except Exception as e:
            logger.warning("RAG deps missing at retrieve time: %s", e)
            return False

        if not self.cfg.index_dir.exists():
            logger.warning("RAG index dir does not exist: %s", self.cfg.index_dir)
            return False

        try:
            embed = OpenAIEmbedding(
                api_key=self.cfg.embed_api_key,
                api_base=self.cfg.embed_base_url,
                model=self.cfg.embed_model,
            )
            Settings.embed_model = embed
            Settings.llm = None  # retriever-only, no LLM needed

            chroma_client = chromadb.PersistentClient(path=str(self.cfg.index_dir))
            try:
                chroma_collection = chroma_client.get_collection(name=self.collection_name)
            except Exception:
                logger.warning("RAG collection %s not found — empty results", self.collection_name)
                return False

            vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
            index = VectorStoreIndex.from_vector_store(
                vector_store=vector_store,
                embed_model=embed,
            )
            self._retriever = index.as_retriever(similarity_top_k=self.cfg.top_k)
            return True
        except Exception as e:
            logger.exception("failed to load RAG retriever: %s", e)
            return False

    def retrieve(self, query: str, *, top_k: Optional[int] = None) -> list[RetrievedChunk]:
        if not self._ensure_loaded():
            return []

        # Override top_k if caller supplied it
        if top_k is not None and top_k != self.cfg.top_k:
            self._retriever.similarity_top_k = top_k

        try:
            results = self._retriever.retrieve(query)
        except Exception as e:
            logger.exception("retrieve failed for query=%r: %s", query[:80], e)
            return []

        chunks: list[RetrievedChunk] = []
        for r in results:
            score = float(r.score) if r.score is not None else 0.0
            if score < self.cfg.similarity_cutoff:
                continue
            source = (r.node.metadata or {}).get("file_path") or (r.node.metadata or {}).get("source") or "<unknown>"
            # Trim very long source paths for readability
            source = source.split("/")[-1] if "/" in source else source
            chunks.append(RetrievedChunk(
                text=r.node.get_content(),
                source=source,
                score=score,
                metadata=dict(r.node.metadata or {}),
            ))
        return chunks


def get_retriever(cfg: Optional[RAGConfig] = None) -> Retriever:
    """Pick the right retriever based on enabled flag + dep availability.

    Returns NoOpRetriever when:
      - COCKPIT_RAG_ENABLED is not set
      - llama-index / chromadb not installed
      - cfg.embed_api_key is empty (can't make embedding calls)
    """
    if not is_enabled():
        return NoOpRetriever()
    if not is_available():
        return NoOpRetriever()
    if cfg is None:
        cfg = RAGConfig.from_env_and_llm()
    if not cfg.embed_api_key:
        logger.debug("RAG enabled but no embed_api_key — falling back to NoOp")
        return NoOpRetriever()
    return LlamaIndexRetriever(cfg)
