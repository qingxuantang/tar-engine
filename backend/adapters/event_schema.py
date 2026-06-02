"""Canonical event schema for Engine's audit pipeline.

Every agent adapter normalizes its raw events into this format before
they enter the shared pipeline (persist → risk check → node map).

The schema is intentionally flat. Agent-specific fields go in metadata.
"""

from typing import Any, Dict, List, Optional, TypedDict


class EngineEvent(TypedDict, total=False):
    """Canonical event format used throughout Engine internals.

    Required fields: event_type, tool_name, timestamp.
    Everything else is optional (not every agent provides every field).
    """

    # ── Required ───────────────────────────────────────────────────
    event_type: str       # "tool_call" | "tool_result" | "session_end" | "model_change"
    tool_name: str        # Normalized tool name (e.g. "Bash", "Edit", "file_write")
    timestamp: str        # ISO 8601

    # ── Identity ───────────────────────────────────────────────────
    session_id: str       # Engine session ID
    tool_use_id: str      # Correlates a tool_call with its tool_result

    # ── Payload ────────────────────────────────────────────────────
    args: Dict[str, Any]  # Tool input / arguments (normalized from tool_input)
    result: str           # Primary output string (normalized from tool_response)
    result_stderr: str    # stderr if available
    result_interrupted: bool

    # ── Context ────────────────────────────────────────────────────
    cwd: str              # Working directory at time of call
    source: str           # Agent identifier: "claude_code" | "codex" | "trae" | "cursor" | "openclaw"

    # ── Metadata ───────────────────────────────────────────────────
    metadata: Dict[str, Any]  # Agent-specific extras (model, tokens, cost, etc.)


# ── Tool name normalization ────────────────────────────────────────
#
# Different agents use different names for equivalent operations.
# This map converts agent-specific tool names to canonical names
# so RiskGuardrail rules and NodeMapper signatures work universally.
#
# Key = (source, raw_tool_name), Value = canonical_name.
# If not in the map, the raw name is kept as-is.

TOOL_NAME_MAP: Dict[tuple, str] = {
    # Codex CLI (item types from codex exec --json)
    ("codex", "command_execution"):  "Bash",
    ("codex", "file_change"):        "Edit",
    ("codex", "mcp_tool_call"):      "McpTool",
    ("codex", "web_search"):         "WebSearch",
    # Trae (hypothetical, fill in when we see real events)
    ("trae", "terminal"):      "Bash",
    ("trae", "readFile"):      "Read",
    ("trae", "writeFile"):     "Write",
    ("trae", "editFile"):      "Edit",
    # Cursor
    ("cursor", "run_command"):  "Bash",
    ("cursor", "read_file"):    "Read",
    ("cursor", "write_file"):   "Write",
    ("cursor", "edit_file"):    "Edit",
}


def normalize_tool_name(source: str, raw_name: str) -> str:
    """Map an agent-specific tool name to a canonical name.

    Returns the raw name unchanged if no mapping exists.
    """
    return TOOL_NAME_MAP.get((source, raw_name), raw_name)


# ── Normalizer base ───────────────────────────────────────────────

class EventNormalizer:
    """Base class for agent-specific event normalizers.

    Subclass this and implement `normalize()` for each agent.
    The normalize method should return an EngineEvent dict.
    """

    source: str = "unknown"

    def normalize(self, raw_event: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a raw agent event into EngineEvent format.

        Must be implemented by subclasses.
        """
        raise NotImplementedError

    def _apply_tool_name_map(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Apply TOOL_NAME_MAP to the event's tool_name."""
        if "tool_name" in event:
            event["tool_name"] = normalize_tool_name(
                self.source, event["tool_name"]
            )
        return event


class CCNormalizer(EventNormalizer):
    """Normalize Claude Code (tarai-reporter) events → EngineEvent."""

    source = "claude_code"

    def normalize(self, raw_event: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(raw_event)
        normalized.setdefault("source", self.source)

        # tool_input → args
        if "tool_input" in raw_event and "args" not in raw_event:
            normalized["args"] = raw_event["tool_input"]

        # tool_response → result
        if "tool_response" in raw_event and "result" not in raw_event:
            resp = raw_event["tool_response"]
            if isinstance(resp, dict):
                normalized["result"] = resp.get("stdout", resp.get("text", ""))
                if resp.get("stderr"):
                    normalized["result_stderr"] = resp["stderr"]
                if resp.get("interrupted"):
                    normalized["result_interrupted"] = True
            elif isinstance(resp, list):
                parts = []
                for item in resp:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(item["text"])
                normalized["result"] = "\n".join(parts)

        return self._apply_tool_name_map(normalized)


class OpenClawNormalizer(EventNormalizer):
    """Normalize OpenClaw plugin/watcher events → EngineEvent.

    These arrive from either:
    - engine-guardrail plugin (Part A, real-time)
    - session file watcher (Part B, batch)
    Both already produce near-canonical format, minimal mapping needed.
    """

    source = "openclaw"

    def normalize(self, raw_event: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(raw_event)
        normalized.setdefault("source", self.source)

        # tool_input → args
        if "tool_input" in raw_event and "args" not in raw_event:
            normalized["args"] = raw_event["tool_input"]

        # tool_response → result
        if "tool_response" in raw_event and "result" not in raw_event:
            resp = raw_event["tool_response"]
            if isinstance(resp, dict):
                normalized["result"] = resp.get("stdout", resp.get("result", ""))

        return self._apply_tool_name_map(normalized)


class CodexNormalizer(EventNormalizer):
    """Normalize OpenAI Codex CLI events → EngineEvent.

    Codex CLI (`codex exec --json`) emits JSONL with nested events:
      - thread.started  → session start
      - item.completed  → tool calls (command_execution, file_change, mcp_tool_call)
      - turn.completed  → token usage
      - item.started    → tool call start (for timing)

    Item types that map to auditable tool calls:
      command_execution: {command, aggregated_output, exit_code, status}
      file_change:       {changes: [{path, kind}], status}
      mcp_tool_call:     {server, tool, arguments, result, error, status}
      web_search:        {query, action}
    """

    source = "codex"

    def normalize(self, raw_event: Dict[str, Any]) -> Dict[str, Any]:
        event_type = raw_event.get("type", "")
        ts = raw_event.get("timestamp", "")

        # thread.started → session marker
        if event_type == "thread.started":
            return {
                "event_type": "session_start",
                "tool_name": "",
                "timestamp": ts,
                "source": self.source,
                "metadata": {"thread_id": raw_event.get("thread_id", "")},
            }

        # turn.completed → token usage (not a tool call, but useful metadata)
        if event_type == "turn.completed":
            usage = raw_event.get("usage", {})
            return {
                "event_type": "turn_completed",
                "tool_name": "",
                "timestamp": ts,
                "source": self.source,
                "metadata": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "cached_input_tokens": usage.get("cached_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "reasoning_tokens": usage.get("reasoning_output_tokens", 0),
                },
            }

        # item.started / item.completed / item.updated → extract the item
        item = raw_event.get("item", {})
        item_type = item.get("type", "")
        item_id = item.get("id", "")

        if event_type == "item.started":
            return self._normalize_item_start(item_type, item_id, item, ts)

        if event_type in ("item.completed", "item.updated"):
            return self._normalize_item_complete(item_type, item_id, item, ts)

        # Passthrough for unknown types (error, turn.started, turn.failed)
        return {
            "event_type": event_type.replace(".", "_"),
            "tool_name": "",
            "timestamp": ts,
            "source": self.source,
            "metadata": {k: v for k, v in raw_event.items() if k != "type"},
        }

    def _normalize_item_start(
        self, item_type: str, item_id: str, item: dict, ts: str
    ) -> Dict[str, Any]:
        """Convert item.started → tool_call event."""
        if item_type == "command_execution":
            normalized = {
                "event_type": "tool_call",
                "tool_name": "command_execution",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": {"command": item.get("command", "")},
            }
        elif item_type == "file_change":
            changes = item.get("changes", [])
            normalized = {
                "event_type": "tool_call",
                "tool_name": "file_change",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": {
                    "changes": changes,
                    "file_path": changes[0]["path"] if changes else "",
                },
            }
        elif item_type == "mcp_tool_call":
            normalized = {
                "event_type": "tool_call",
                "tool_name": "mcp_tool_call",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": item.get("arguments", {}),
                "metadata": {
                    "mcp_server": item.get("server", ""),
                    "mcp_tool": item.get("tool", ""),
                },
            }
        elif item_type == "web_search":
            normalized = {
                "event_type": "tool_call",
                "tool_name": "web_search",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": {"query": item.get("query", "")},
            }
        else:
            # agent_message, reasoning, todo_list, etc. — non-tool items
            normalized = {
                "event_type": "agent_event",
                "tool_name": item_type,
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": {k: v for k, v in item.items() if k not in ("id", "type")},
            }

        return self._apply_tool_name_map(normalized)

    def _normalize_item_complete(
        self, item_type: str, item_id: str, item: dict, ts: str
    ) -> Dict[str, Any]:
        """Convert item.completed → tool_result event."""
        status = item.get("status", "completed")

        if item_type == "command_execution":
            normalized = {
                "event_type": "tool_result",
                "tool_name": "command_execution",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": {"command": item.get("command", "")},
                "result": (item.get("aggregated_output", "") or "")[:4096],
                "metadata": {
                    "exit_code": item.get("exit_code"),
                    "status": status,
                },
            }
        elif item_type == "file_change":
            changes = item.get("changes", [])
            paths = [c.get("path", "") for c in changes]
            kinds = [c.get("kind", "") for c in changes]
            normalized = {
                "event_type": "tool_result",
                "tool_name": "file_change",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": {
                    "changes": changes,
                    "file_path": paths[0] if paths else "",
                },
                "result": ", ".join(f"{k}: {p}" for k, p in zip(kinds, paths)),
                "metadata": {"status": status},
            }
        elif item_type == "mcp_tool_call":
            result_data = item.get("result", {})
            result_text = ""
            if isinstance(result_data, dict):
                content = result_data.get("content", [])
                if isinstance(content, list):
                    parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                    result_text = "\n".join(parts)
                elif isinstance(content, str):
                    result_text = content
            error = item.get("error")
            normalized = {
                "event_type": "tool_result",
                "tool_name": "mcp_tool_call",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": item.get("arguments", {}),
                "result": result_text[:4096],
                "metadata": {
                    "mcp_server": item.get("server", ""),
                    "mcp_tool": item.get("tool", ""),
                    "status": status,
                    "error": error.get("message") if isinstance(error, dict) else None,
                },
            }
        elif item_type == "web_search":
            normalized = {
                "event_type": "tool_result",
                "tool_name": "web_search",
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "args": {"query": item.get("query", "")},
                "result": str(item.get("action", ""))[:4096],
            }
        else:
            # agent_message, reasoning, etc.
            text = item.get("text", "")
            normalized = {
                "event_type": "agent_event",
                "tool_name": item_type,
                "tool_use_id": item_id,
                "timestamp": ts,
                "source": self.source,
                "result": text[:4096] if isinstance(text, str) else str(text)[:4096],
            }

        return self._apply_tool_name_map(normalized)


# Registry of normalizers by source name
NORMALIZERS: Dict[str, EventNormalizer] = {
    "claude_code": CCNormalizer(),
    "cc": CCNormalizer(),           # alias
    "openclaw": OpenClawNormalizer(),
    "codex": CodexNormalizer(),
}


def get_normalizer(source: str) -> EventNormalizer:
    """Get the normalizer for a given agent source.

    Falls back to CCNormalizer (the original format) if unknown.
    """
    return NORMALIZERS.get(source, NORMALIZERS["cc"])
