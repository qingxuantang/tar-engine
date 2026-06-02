"""Claude Code adapter — thin subclass of BaseAdapter.

Keeps backward compatibility: the module-level `cc_adapter` singleton
is the same object that app.py and the rest of the codebase import.
All CC-specific logic (session prefix, auto-detect skill from ID) lives here.
"""

from typing import Any, Dict, List, Optional

from event_store import event_store
from .base_adapter import BaseAdapter
from .event_schema import CCNormalizer


class CCAdapter(BaseAdapter):
    """Claude Code event adapter."""

    agent_name = "cc"
    session_prefix = "cc"

    def __init__(self):
        super().__init__(normalizer=CCNormalizer())

    def create_session(
        self,
        skill_name: str = "",
        user_id: str = "",
        domain: str = "general",
        meta: Optional[Dict] = None,
        skill_nodes: Optional[List[Dict]] = None,
        parsed_skill: Optional[Dict] = None,
    ) -> Dict[str, str]:
        """Create session with CC-specific defaults (domain=general)."""
        return super().create_session(
            skill_name=skill_name,
            user_id=user_id,
            domain=domain,
            meta=meta,
            skill_nodes=skill_nodes,
            parsed_skill=parsed_skill,
        )

    async def ingest_events(
        self,
        session_id: str,
        events: List[Dict[str, Any]],
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Ingest with CC-specific auto-session skill detection."""
        # CC-specific: auto-detect skill_name from session_id pattern
        session = event_store.get_session(session_id)
        if session is None and events:
            first = events[0]
            meta_in = first.get("metadata", {})
            skill_name = ""
            if session_id.startswith("cta-live-"):
                skill_name = "AI CTA"
            event_store.ensure_session(session_id, meta={
                "cwd": first.get("cwd", ""),
                "permission_mode": meta_in.get("permission_mode", ""),
                "transcript_path": meta_in.get("transcript_path", ""),
                "reporter_version": meta_in.get("reporter_version", ""),
                "platform": meta_in.get("platform", ""),
                "source": self.agent_name,
                "domain": "general",
                "auto_created": True,
                "user_id": user_id,
                "skill_name": skill_name,
            })
            if user_id and user_id != "__admin__":
                conn = event_store._conn()
                conn.execute(
                    "UPDATE cc_sessions SET user_id = ? WHERE session_id = ? AND (user_id IS NULL OR user_id = '')",
                    (user_id, session_id),
                )
                conn.commit()

        # Delegate to base pipeline (normalize → persist → risk → map → detect)
        return await super().ingest_events(session_id, events, user_id)


# Module-level singleton (backward compatible)
cc_adapter = CCAdapter()

# Register audit orchestrator
from auditor.orchestrator import audit_orchestrator
cc_adapter.on_session_end(audit_orchestrator.on_session_end)
