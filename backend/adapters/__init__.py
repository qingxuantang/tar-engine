"""Agent adapters — multi-platform event ingestion for Engine.

Each adapter normalizes a specific agent's event format into the
canonical EngineEvent schema, then feeds it through the shared
pipeline (persist → risk check → node map → skill detect → audit).

Usage:
    from adapters.cc_adapter import cc_adapter      # Claude Code
    from adapters.codex_adapter import codex_adapter  # OpenAI Codex (future)
"""
