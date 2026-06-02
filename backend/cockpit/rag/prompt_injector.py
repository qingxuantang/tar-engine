"""Format retrieved RAG chunks for prompt injection.

The Planner and skill_executor both call into here to turn a list of
RetrievedChunk into a clearly-delimited block they can paste into the
system prompt. Keeping this in one place means we have a single source
of truth for the format and can tune it easily.
"""

from __future__ import annotations

from typing import Iterable

from .retriever import RetrievedChunk


def format_chunks_for_prompt(chunks: Iterable[RetrievedChunk], *, max_chars: int = 6000) -> str:
    """Render chunks as a markdown-fenced reference block.

    Format chosen to make it obvious to the LLM that this is reference
    context (not user input), and to make source attribution easy if the
    LLM cites back.

    Args:
        chunks: retrieved chunks (already filtered by similarity cutoff)
        max_chars: hard cap on total block length — older chunks get
            truncated if we exceed this. Default 6KB ≈ ~1500 tokens.

    Returns:
        A multi-line string ready to drop into a system prompt. Empty
        string if chunks is empty (caller branches on truthy).
    """
    chunks = list(chunks)
    if not chunks:
        return ""

    header = (
        "==== RETRIEVED KNOWLEDGE CONTEXT ====\n"
        "The following chunks were retrieved from the user's knowledge base "
        "(engine docs + skill bundles) based on similarity to the user's wish.\n"
        "Use them as REFERENCE context. Cite the source filename when relevant.\n"
        "If a chunk seems off-topic, ignore it.\n"
    )
    footer = "==== END RETRIEVED KNOWLEDGE ====\n"

    body_parts: list[str] = []
    used = len(header) + len(footer)

    for i, chunk in enumerate(chunks, 1):
        block = (
            f"\n--- chunk {i} | source: {chunk.source} | score: {chunk.score:.3f} ---\n"
            f"{chunk.text}\n"
        )
        if used + len(block) > max_chars:
            body_parts.append(f"\n[... {len(chunks) - i + 1} more chunks truncated for length ...]\n")
            break
        body_parts.append(block)
        used += len(block)

    return header + "".join(body_parts) + footer
