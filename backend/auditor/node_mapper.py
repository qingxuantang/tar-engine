"""Runtime node mapper — maps tool_call events to business DAG nodes.

Given a skill's node signatures (from SignatureExtractor), this module:
1. Matches each incoming event to the most likely DAG node
2. Maintains a DAG state machine (pending → active → done)
3. Provides the current DAG state for frontend visualization

The mapper uses a scoring system: each event is scored against all nodes,
and the highest-scoring match wins. If no node scores above threshold,
the event is attributed to the currently active node (sub-operation).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class NodeStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    ERROR = "error"


@dataclass
class DAGNode:
    """Runtime state of a single DAG node."""
    id: str
    name: str
    description: str
    depends_on: List[str]
    status: NodeStatus = NodeStatus.PENDING
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    event_count: int = 0
    events: List[str] = field(default_factory=list)  # event summaries


@dataclass
class MatchResult:
    """Result of matching an event to a node."""
    node_id: str
    node_name: str
    score: float
    matched_by: str  # which signature type matched


# Minimum score to consider a match valid
MATCH_THRESHOLD = 1.0


class CompiledSignature:
    """Pre-compiled regex patterns for a single node."""

    def __init__(self, node: Dict):
        self.node_id = node["id"]
        self.node_name = node.get("name", "")
        sigs = node.get("signatures", {})

        self.file_res = self._compile_list(sigs.get("file_patterns", []))
        self.cmd_res = self._compile_list(sigs.get("command_patterns", []))
        self.content_res = self._compile_list(sigs.get("content_patterns", []))
        self.keywords = [k.lower() for k in sigs.get("keywords", []) if k]

    def _compile_list(self, patterns: List[str]) -> List[re.Pattern]:
        compiled = []
        for p in patterns:
            if not p:
                continue
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error:
                pass  # Skip invalid patterns
        return compiled


class NodeMapper:
    """Maps runtime events to DAG nodes and tracks state."""

    def __init__(self, nodes: List[Dict]):
        """Initialize with node signatures from SignatureExtractor.

        Args:
            nodes: List of node dicts with 'id', 'name', 'signatures', 'depends_on'
        """
        self._signatures = [CompiledSignature(n) for n in nodes]
        self._dag: Dict[str, DAGNode] = {}
        self._current_node_id: Optional[str] = None
        self._node_order = []

        for n in nodes:
            nid = n["id"]
            self._dag[nid] = DAGNode(
                id=nid,
                name=n.get("name", ""),
                description=n.get("description", ""),
                depends_on=n.get("depends_on", []),
            )
            self._node_order.append(nid)

    def map_event(self, event: Dict[str, Any]) -> Optional[MatchResult]:
        """Map a single event to the best matching DAG node.

        Returns MatchResult if matched, None if no match.
        Also updates DAG state.
        """
        event_type = event.get("event_type", "")
        tool_name = event.get("tool_name", "")
        timestamp = event.get("timestamp", datetime.utcnow().isoformat())

        # Only match tool_call events (tool_result follows the same node)
        if event_type == "tool_result":
            # Attribute to current active node
            if self._current_node_id and self._current_node_id in self._dag:
                node = self._dag[self._current_node_id]
                node.event_count += 1
                return MatchResult(
                    node_id=self._current_node_id,
                    node_name=node.name,
                    score=0,
                    matched_by="follow_active",
                )
            return None

        if event_type != "tool_call":
            return None

        # Score against all nodes
        file_path = self._extract_file(event)
        command = self._extract_command(event)
        content = self._extract_content(event)
        all_text = f"{file_path} {command} {content} {tool_name}".lower()

        best_match: Optional[MatchResult] = None
        best_score = 0.0

        for sig in self._signatures:
            score, matched_by = self._score(sig, file_path, command, content, all_text)
            if score > best_score:
                best_score = score
                best_match = MatchResult(
                    node_id=sig.node_id,
                    node_name=sig.node_name,
                    score=score,
                    matched_by=matched_by,
                )

        # Apply match or fall back to current node
        if best_match and best_match.score >= MATCH_THRESHOLD:
            self._activate_node(best_match.node_id, timestamp)
            node = self._dag[best_match.node_id]
            node.event_count += 1
            summary = f"[{tool_name}] {file_path or command or ''}".strip()[:80]
            node.events.append(summary)
            return best_match

        # No strong match: attribute to current active node
        if self._current_node_id and self._current_node_id in self._dag:
            node = self._dag[self._current_node_id]
            node.event_count += 1
            return MatchResult(
                node_id=self._current_node_id,
                node_name=node.name,
                score=best_score,
                matched_by="fallback_active",
            )

        # No active node: try to activate the first pending node
        first_pending = self._first_pending()
        if first_pending:
            self._activate_node(first_pending, timestamp)
            self._dag[first_pending].event_count += 1
            return MatchResult(
                node_id=first_pending,
                node_name=self._dag[first_pending].name,
                score=0,
                matched_by="first_pending",
            )

        return None

    def _score(
        self,
        sig: CompiledSignature,
        file_path: str,
        command: str,
        content: str,
        all_text: str,
    ) -> Tuple[float, str]:
        """Score an event against a node's signatures. Returns (score, matched_by)."""
        score = 0.0
        matched_by = ""

        # File pattern match (strongest signal)
        for pattern in sig.file_res:
            if file_path and pattern.search(file_path):
                score += 3.0
                matched_by = f"file:{pattern.pattern}"
                break

        # Command pattern match
        for pattern in sig.cmd_res:
            if command and pattern.search(command):
                score += 2.5
                if not matched_by:
                    matched_by = f"cmd:{pattern.pattern}"
                break

        # Content pattern match
        for pattern in sig.content_res:
            if content and pattern.search(content):
                score += 2.0
                if not matched_by:
                    matched_by = f"content:{pattern.pattern}"
                break

        # Keyword match (weaker signal, accumulates)
        kw_hits = 0
        for kw in sig.keywords:
            if kw in all_text:
                kw_hits += 1
        if kw_hits:
            score += min(kw_hits * 0.5, 2.0)
            if not matched_by:
                matched_by = f"keywords:{kw_hits}"

        return score, matched_by

    def _activate_node(self, node_id: str, timestamp: str):
        """Transition a node to active state, completing the previous active node."""
        # Complete previous active node if different
        if self._current_node_id and self._current_node_id != node_id:
            prev = self._dag.get(self._current_node_id)
            if prev and prev.status == NodeStatus.ACTIVE:
                prev.status = NodeStatus.DONE
                prev.ended_at = timestamp

        # Activate new node
        node = self._dag.get(node_id)
        if node:
            if node.status == NodeStatus.PENDING:
                node.status = NodeStatus.ACTIVE
                node.started_at = timestamp
            elif node.status == NodeStatus.DONE:
                # Re-entered node (iterative workflow)
                node.status = NodeStatus.ACTIVE
            self._current_node_id = node_id

            # Also mark skipped dependencies as done
            self._mark_skipped_deps(node_id, timestamp)

    def _mark_skipped_deps(self, node_id: str, timestamp: str):
        """If a node becomes active but its deps are still pending, mark them done."""
        node = self._dag.get(node_id)
        if not node:
            return
        for dep_id in node.depends_on:
            dep = self._dag.get(dep_id)
            if dep and dep.status == NodeStatus.PENDING:
                dep.status = NodeStatus.DONE
                dep.started_at = dep.started_at or timestamp
                dep.ended_at = timestamp

    def _first_pending(self) -> Optional[str]:
        """Get the first pending node in order."""
        for nid in self._node_order:
            if self._dag[nid].status == NodeStatus.PENDING:
                return nid
        return None

    def complete(self, timestamp: Optional[str] = None):
        """Mark the session as complete. Finalize all active nodes."""
        ts = timestamp or datetime.utcnow().isoformat()
        for node in self._dag.values():
            if node.status == NodeStatus.ACTIVE:
                node.status = NodeStatus.DONE
                node.ended_at = ts

    # ── Extraction helpers ──────────────────────────────────────────

    def _extract_file(self, event: Dict) -> str:
        args = event.get("args", {})
        if isinstance(args, dict):
            for key in ("file_path", "path", "file", "filename"):
                if key in args and isinstance(args[key], str):
                    return args[key]
        return ""

    def _extract_command(self, event: Dict) -> str:
        args = event.get("args", {})
        if isinstance(args, dict):
            return args.get("command", "")
        return ""

    def _extract_content(self, event: Dict) -> str:
        args = event.get("args", {})
        if isinstance(args, dict):
            parts = []
            for k in ("content", "new_string", "old_string", "pattern"):
                v = args.get(k, "")
                if isinstance(v, str):
                    parts.append(v)
            return " ".join(parts)
        return ""

    # ── State queries ───────────────────────────────────────────────

    def get_dag_state(self) -> List[Dict]:
        """Get current DAG state for frontend rendering."""
        return [
            {
                "id": n.id,
                "name": n.name,
                "description": n.description,
                "status": n.status.value,
                "depends_on": n.depends_on,
                "started_at": n.started_at,
                "ended_at": n.ended_at,
                "event_count": n.event_count,
                "events": n.events[:50],  # cap at 50 summaries
            }
            for n in (self._dag[nid] for nid in self._node_order)
        ]

    def get_active_node(self) -> Optional[Dict]:
        """Get the currently active node."""
        if self._current_node_id:
            node = self._dag.get(self._current_node_id)
            if node:
                return {
                    "id": node.id,
                    "name": node.name,
                    "status": node.status.value,
                    "event_count": node.event_count,
                }
        return None

    def get_progress(self) -> Dict:
        """Get overall progress."""
        total = len(self._dag)
        done = sum(1 for n in self._dag.values() if n.status == NodeStatus.DONE)
        active = sum(1 for n in self._dag.values() if n.status == NodeStatus.ACTIVE)
        return {
            "total": total,
            "done": done,
            "active": active,
            "pending": total - done - active,
            "percent": round(done / total * 100) if total else 0,
        }
