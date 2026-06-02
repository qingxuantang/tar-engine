"""OpenAI Codex CLI adapter.

Ingests events from `codex exec --json` JSONL output.
Codex events are nested (item.completed wraps the actual tool data),
so the CodexNormalizer flattens them into EngineEvent format.

Client-side: codex-reporter.py streams `codex exec --json` stdout
and POSTs batches to /api/codex/events.
"""

from typing import Any, Dict, List, Optional

from event_store import event_store
from .base_adapter import BaseAdapter
from .event_schema import CodexNormalizer


class CodexAdapter(BaseAdapter):
    """Codex CLI event adapter."""

    agent_name = "codex"
    session_prefix = "codex"

    def __init__(self):
        super().__init__(normalizer=CodexNormalizer())

    async def ingest_events(
        self,
        session_id: str,
        events: List[Dict[str, Any]],
        user_id: str = "",
        thread_id: str = "",
    ) -> Dict[str, Any]:
        """Ingest Codex events with thread_id tracking.

        Codex sessions map to thread_ids. If a thread.started event
        is in the batch, we capture the thread_id in session meta.
        """
        # Extract thread_id from thread.started if present
        for e in events:
            if e.get("type") == "thread.started" and e.get("thread_id"):
                thread_id = e["thread_id"]
                break

        # Auto-create session with Codex-specific meta
        session = event_store.get_session(session_id)
        if session is None:
            event_store.ensure_session(session_id, meta={
                "source": self.agent_name,
                "thread_id": thread_id,
                "auto_created": True,
                "user_id": user_id,
            })
            if user_id and user_id != "__admin__":
                conn = event_store._conn()
                conn.execute(
                    "UPDATE cc_sessions SET user_id = ? WHERE session_id = ? AND (user_id IS NULL OR user_id = '')",
                    (user_id, session_id),
                )
                conn.commit()

        # Filter out non-auditable events before pipeline
        # (agent_message, reasoning are useful context but not tool calls)
        auditable = []
        meta_events = []
        for e in events:
            etype = e.get("type", "")
            item_type = e.get("item", {}).get("type", "") if "item" in e else ""

            if etype in ("item.started", "item.completed", "item.updated"):
                if item_type in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                    auditable.append(e)
                else:
                    meta_events.append(e)
            elif etype == "turn.completed":
                meta_events.append(e)
            elif etype == "thread.started":
                meta_events.append(e)
            else:
                meta_events.append(e)

        # Ingest auditable events through the full pipeline
        result = {"stored": 0, "alerts": [], "node_updates": [], "session_ended": False, "detected_skill": None}
        if auditable:
            result = await super().ingest_events(session_id, auditable, user_id)

        # Store meta events (token usage, reasoning) as lightweight records
        if meta_events:
            normalized_meta = [self._normalizer.normalize(e) for e in meta_events]
            event_store.store_batch(session_id, normalized_meta)
            result["stored"] += len(normalized_meta)

        # Accumulate token usage from turn.completed events
        for e in meta_events:
            if e.get("type") == "turn.completed":
                usage = e.get("usage", {})
                if usage:
                    self._update_session_usage(session_id, usage)

        return result

    def _update_session_usage(self, session_id: str, usage: dict):
        """Accumulate token usage in session meta."""
        try:
            session = event_store.get_session(session_id)
            if not session:
                return
            import json
            meta = {}
            if session.get("meta"):
                try:
                    meta = json.loads(session["meta"]) if isinstance(session["meta"], str) else session["meta"]
                except (json.JSONDecodeError, TypeError):
                    meta = {}

            # Accumulate
            meta["total_input_tokens"] = meta.get("total_input_tokens", 0) + usage.get("input_tokens", 0)
            meta["total_output_tokens"] = meta.get("total_output_tokens", 0) + usage.get("output_tokens", 0)
            meta["total_cached_tokens"] = meta.get("total_cached_tokens", 0) + usage.get("cached_input_tokens", 0)
            meta["total_reasoning_tokens"] = meta.get("total_reasoning_tokens", 0) + usage.get("reasoning_output_tokens", 0)
            meta["turns"] = meta.get("turns", 0) + 1

            conn = event_store._conn()
            conn.execute(
                "UPDATE cc_sessions SET meta = ? WHERE session_id = ?",
                (json.dumps(meta), session_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[codex] usage update error: {e}")


# Module-level singleton
codex_adapter = CodexAdapter()

# Register audit orchestrator
from auditor.orchestrator import audit_orchestrator
codex_adapter.on_session_end(audit_orchestrator.on_session_end)
