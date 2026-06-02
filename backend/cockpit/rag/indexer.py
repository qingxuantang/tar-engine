"""Build / refresh the LlamaIndex vector index over engine docs + skills.

Knowledge L3 source documents (default):
    engine/docs/*.md            — architecture, plans, knowledge docs
    /cc-skills/*/SKILL.md       — skill bundle entry points (mounted ro)

Each source is loaded via LlamaIndex's SimpleDirectoryReader (respects
the file's natural structure: markdown headings become chunk boundaries
where possible), embedded via OpenAI-compat embedding, and persisted to
ChromaDB under `RAGConfig.index_dir`.

The indexer is **idempotent**: re-running on the same paths reuses
existing embeddings (Chroma dedupes by document hash). Set `force=True`
to wipe the collection first.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import RAGConfig, is_available

logger = logging.getLogger("cockpit.rag.indexer")

DEFAULT_COLLECTION = "cockpit_knowledge_l3"


def build_index(
    source_paths: Iterable[Path],
    cfg: RAGConfig,
    *,
    collection_name: str = DEFAULT_COLLECTION,
    force: bool = False,
) -> int:
    """Build or refresh the vector index.

    Args:
        source_paths: dirs and/or .md files to index.
        cfg: RAG config (needs valid embed_api_key).
        collection_name: chroma collection name.
        force: drop the existing collection before re-indexing.

    Returns:
        number of nodes indexed.

    Raises:
        RuntimeError if deps missing or no source files found.
    """
    if not is_available():
        raise RuntimeError("RAG deps not installed (llama-index / chromadb)")
    if not cfg.embed_api_key:
        raise RuntimeError("embed_api_key not set on RAGConfig (BYOK required)")

    # Imports kept lazy so module loads even when deps missing
    import chromadb
    from llama_index.core import (
        Document,
        Settings,
        SimpleDirectoryReader,
        StorageContext,
        VectorStoreIndex,
    )
    from llama_index.core.node_parser import MarkdownNodeParser
    from llama_index.embeddings.openai import OpenAIEmbedding
    from llama_index.vector_stores.chroma import ChromaVectorStore

    # Resolve source files (dirs → expand to .md / .txt)
    source_files: list[Path] = []
    for p in source_paths:
        p = Path(p)
        if not p.exists():
            logger.warning("source path does not exist: %s", p)
            continue
        if p.is_file():
            source_files.append(p)
        else:
            source_files.extend(p.rglob("*.md"))
            source_files.extend(p.rglob("*.txt"))

    if not source_files:
        raise RuntimeError("no source files found in given paths")

    # Configure embedding model
    embed = OpenAIEmbedding(
        api_key=cfg.embed_api_key,
        api_base=cfg.embed_base_url,
        model=cfg.embed_model,
    )
    Settings.embed_model = embed
    # We don't use the LLM at index time, only at retrieval time downstream
    Settings.llm = None

    # Set up Chroma client + collection
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=str(cfg.index_dir))

    if force:
        try:
            chroma_client.delete_collection(name=collection_name)
            logger.info("dropped collection %s (force=True)", collection_name)
        except Exception:
            pass  # didn't exist

    chroma_collection = chroma_client.get_or_create_collection(name=collection_name)
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    # Load documents — keep file_path in metadata so we can cite sources
    reader = SimpleDirectoryReader(
        input_files=[str(p) for p in source_files],
        recursive=False,
        required_exts=[".md", ".txt"],
    )
    documents = reader.load_data()
    logger.info("loaded %d source documents from %d files", len(documents), len(source_files))

    # Markdown-aware chunking: headings become chunk boundaries
    md_parser = MarkdownNodeParser()
    nodes = md_parser.get_nodes_from_documents(documents)
    logger.info("parsed into %d nodes (markdown-aware chunking)", len(nodes))

    # Build & persist
    index = VectorStoreIndex(
        nodes=nodes,
        storage_context=storage_context,
        embed_model=embed,
    )
    # Chroma persistence is automatic (PersistentClient)
    return len(nodes)


def default_source_paths() -> list[Path]:
    """The default set of paths to index for cockpit Knowledge L3.

    Override via COCKPIT_RAG_SOURCES env (colon-separated). Otherwise:
      - engine/docs/                 (project repo)
      - /cc-skills/*/SKILL.md        (mounted skill bundle entrypoints only)
    """
    import os

    env = os.environ.get("COCKPIT_RAG_SOURCES")
    if env:
        return [Path(p.strip()) for p in env.split(":") if p.strip()]

    out: list[Path] = []
    # Engine docs — try a few common locations
    for candidate in [
        Path("/app/docs"),                          # in v3 container
        Path("./docs"),  # host dev
    ]:
        if candidate.exists():
            out.append(candidate)
            break

    # Skills — only top-level SKILL.md per skill dir, not their entire trees
    skills_dir = Path(os.environ.get("COCKPIT_SKILLS_DIR", "/cc-skills"))
    if skills_dir.exists():
        for skill_md in skills_dir.glob("*/SKILL.md"):
            out.append(skill_md)

    return out
