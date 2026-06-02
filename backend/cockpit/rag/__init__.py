"""Cockpit RAG (Knowledge L3 retrieval) — added 2026-05-13.

LlamaIndex-backed retrieval over engine docs + skill bundles. Powers the
"inject relevant domain context into Planner prompt" capability discussed
in the COCKPIT_ARCHITECTURE.md §11 Knowledge L3 row.

Design constraints:

1. **Opt-in via env**: `COCKPIT_RAG_ENABLED=1` flips it on. Default off so
   existing V3 flow is untouched until users opt in.
2. **Graceful degradation**: if llama-index / chromadb are not installed,
   `get_retriever()` returns a stub that always yields `[]`. Cockpit
   never crashes because RAG is missing.
3. **Per-request embedding key**: the embedding API key is the same OpenAI
   key the user passes per-wish via X-LLM-Api-Key (BYOK). We do NOT
   persist it on Engine.
4. **Index persistence**: ChromaDB writes to `COCKPIT_RAG_INDEX_DIR` (default
   `/data/rag_index`), survives container restarts.

Public surface:
    is_available()           → bool, are deps + config OK
    get_retriever(cfg)       → Retriever (or stub)
    build_index(paths, cfg)  → (re)build index from a list of dirs/files

See:
    indexer.py       — document loading + chunking + embedding + write
    retriever.py     — query interface
    prompt_injector.py — format retrieved chunks for prompt injection
    config.py        — env-driven config
"""

from .config import RAGConfig, is_available
from .retriever import get_retriever, NoOpRetriever

__all__ = [
    "RAGConfig",
    "is_available",
    "get_retriever",
    "NoOpRetriever",
]
