"""Skill capability classification.

Replaces the single-axis `skill_intent` model (read_only / modify / execute)
with a 6-dimension capability bitmap that the admin (or auto-inference) can
toggle independently. Lets us distinguish e.g. "can write reports to data/"
from "can edit strategy code" — the read_only intent conflated both.

Capability keys:
    read_files            — Read / Glob / Grep tool calls
    write_to_data         — Edit/Write into data/ output/ results/ uploads/
    write_to_knowledge    — Edit/Write into knowledge bases (.claude/skills/knowledge/, etc.)
    modify_strategy_code  — Edit/Write source files (config.py, factors/, *.py outside data/)
    external_api          — Bash with curl/wget/http(s) or non-telegram MCP tools
    git_destructive       — rm -rf, git push, git reset --hard, DROP TABLE/DATABASE

Three inference paths populate the skill_capabilities table:
    * frontmatter — parse `allowed-tools` from SKILL.md (path A)
    * learned     — aggregate observed Edit/Write paths over historical runs (path B)
    * manual      — admin override via panel UI (path C, highest priority)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

CAP_KEYS = (
    "read_files",
    "write_to_data",
    "write_to_knowledge",
    "modify_strategy_code",
    "external_api",
    "git_destructive",
)

# Telegram reply/react/edit_message are reporting-only — they leave the box
# but only to send messages back to the user, never to mutate external state.
# Treat them as benign so a read-only skill that reports via Telegram doesn't
# trip the external_api check.
_BENIGN_MCP_PATTERNS = re.compile(
    r"telegram.*(reply|react|edit_message|download_attachment)", re.IGNORECASE
)

_DESTRUCTIVE_BASH = re.compile(
    r"\brm\s+-rf\b|\bgit\s+push\b|\bgit\s+reset\s+--hard\b|"
    r"\bdrop\s+(table|database)\b|\bgit\s+branch\s+-D\b",
    re.IGNORECASE,
)
_EXTERNAL_API_BASH = re.compile(
    r"\bcurl\s|\bwget\s|\bhttpx?\b|\bhttps?://",
    re.IGNORECASE,
)

# Path-prefix hints. Order matters: knowledge/strategy_code checked before data
# so a *.py inside data/ is still classified as data write (the file is an
# output artifact, not strategy source).
_KNOWLEDGE_PATH_RE = re.compile(
    r"(?:^|/)\.?claude/skills/knowledge/|(?:^|/)skills/knowledge/|"
    r"(?:^|/)knowledge/(?:策略模式|反思记录|因子知识|框架技巧|数据经验|论坛内容)/",
    re.IGNORECASE,
)
_DATA_PATH_RE = re.compile(
    r"(?:^|/)data/|(?:^|/)output[s_-]?[^/]*/|(?:^|/)results?/|"
    r"(?:^|/)uploads?/|(?:^|/)tmp/|/分析报告\.md$|/回测结果",
    re.IGNORECASE,
)
# Strategy-source patterns: matched only AFTER knowledge/data exclusions.
_STRATEGY_CODE_PATH_RE = re.compile(
    r"(?:^|/)config\.py$|(?:^|/)settings\.py$|"
    r"(?:^|/)factors?/|(?:^|/)sections?/|(?:^|/)strategy[_-]?pool/|"
    r"(?:^|/)strategy/|(?:^|/)signals?/|(?:^|/)core/|"
    r"\.py$|\.yaml$|\.yml$",
    re.IGNORECASE,
)

# Tools that perform reads
_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "NotebookRead"})
# Tools that perform writes/edits
_WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


def required_capability(event: Dict[str, Any]) -> Optional[str]:
    """Return the capability key required for this event, or None if benign.

    Benign events (Skill, TaskCreate, telegram replies, plain Bash without
    network or destructive patterns) return None — no capability check needed.
    """
    if event.get("event_type") != "tool_call":
        return None

    tool = event.get("tool_name", "")
    if not tool:
        return None
    args = event.get("tool_input", event.get("args", {}))
    if not isinstance(args, dict):
        args = {}

    if tool in _READ_TOOLS:
        return "read_files"

    if tool in _WRITE_TOOLS:
        path = (args.get("file_path") or args.get("notebook_path") or "")
        return _classify_write_path(path)

    if tool == "Bash":
        cmd = (args.get("command") or "")
        if _DESTRUCTIVE_BASH.search(cmd):
            return "git_destructive"
        if _EXTERNAL_API_BASH.search(cmd):
            return "external_api"
        return None  # plain local shell work doesn't need a cap

    if tool.startswith("mcp__"):
        if _BENIGN_MCP_PATTERNS.search(tool):
            return None
        return "external_api"

    # Skill, TaskCreate/Update/List, Agent, etc. — orchestration, no cap needed
    return None


def _classify_write_path(path: str) -> str:
    """Map a write target path to the capability that authorizes it.

    Knowledge and data are checked first so artifact paths under those roots
    win even if the file is a *.py (e.g. someone writes a script into data/).
    """
    if not path:
        return "modify_strategy_code"  # safest default when path is unknown
    p = path.lower()
    if _KNOWLEDGE_PATH_RE.search(p):
        return "write_to_knowledge"
    if _DATA_PATH_RE.search(p):
        return "write_to_data"
    if _STRATEGY_CODE_PATH_RE.search(p):
        return "modify_strategy_code"
    # Misc files (.md, .txt, .json outside above roots) treated as data writes
    return "write_to_data"


# ── Path A: infer capabilities from SKILL.md frontmatter ───────────────

# Frontmatter format CC's standard SkillMd uses:
#   allowed-tools:
#     - Read
#     - Edit
#     - Write
# (also accepts inline list `allowed-tools: [Read, Edit, Write]`)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_ALLOWED_TOOLS_BLOCK_RE = re.compile(
    r"^allowed-tools\s*:\s*(.+?)(?=^\S|\Z)", re.MULTILINE | re.DOTALL,
)


def parse_allowed_tools(skill_md_content: str) -> List[str]:
    """Pull the allowed-tools list out of a SKILL.md frontmatter block.

    Returns [] if no frontmatter or no allowed-tools key.
    Accepts either YAML inline list or YAML block list.
    """
    fm_match = _FRONTMATTER_RE.search(skill_md_content)
    if not fm_match:
        return []
    fm = fm_match.group(1)
    block_match = _ALLOWED_TOOLS_BLOCK_RE.search(fm)
    if not block_match:
        return []
    block = block_match.group(1).strip()

    # Inline list: `allowed-tools: [Read, Edit, Write]`
    if block.startswith("["):
        inner = block.strip("[]\n ")
        return [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]

    # Block list: each `  - ToolName` on its own line
    tools = []
    for line in block.split("\n"):
        line = line.strip()
        if line.startswith("-"):
            tool = line[1:].strip().strip("'\"")
            if tool:
                tools.append(tool)
    return tools


def capabilities_from_allowed_tools(allowed_tools: List[str]) -> Dict[str, bool]:
    """Conservative inference: enable a capability iff the corresponding
    tool family appears in allowed-tools. The author opting into Edit/Write
    means the skill IS permitted to write — defaults match that intent.

    External-API and git-destructive are NOT auto-enabled from allowed-tools
    alone: Bash is too generic. Admin must opt in via path C.
    """
    has = {t.strip().lower() for t in allowed_tools}
    if not has:
        # No frontmatter → use default (permissive on read/data/knowledge)
        from event_store import event_store
        return dict(event_store.DEFAULT_CAPS)

    # `*` wildcard — author allowed everything; mirror the permissive defaults
    if "*" in has:
        from event_store import event_store
        return dict(event_store.DEFAULT_CAPS)

    write_tool_present = any(t in has for t in ("edit", "write", "notebookedit"))
    read_tool_present = any(t in has for t in ("read", "glob", "grep", "notebookread"))

    return {
        "read_files": read_tool_present,
        # If the author allowed Edit/Write, they expect the skill to write
        # SOMETHING — enable both data and knowledge by default. Strategy
        # code stays off (path B / path C must opt in).
        "write_to_data": write_tool_present,
        "write_to_knowledge": write_tool_present,
        "modify_strategy_code": False,
        "external_api": False,
        "git_destructive": False,
    }


# ── Path B: learn capabilities from observed event history ─────────────

# Minimum observations before we trust the learned capability map.
_MIN_RUNS_TO_LEARN = 1
_MIN_EVENTS_TO_LEARN = 3


def learn_capabilities_from_history(skill_name: str) -> Optional[Dict[str, Any]]:
    """Aggregate cc_skill_runs + cc_events for this skill and return
    {history_hints} reflecting actually-observed behavior.

    Path B is OBSERVATION ONLY — it never grants capabilities. Returns
    counts and sample paths so the UI can show "观察到 N 次 X" alongside
    each capability row, and so the realtime guardrail can compare the
    declared bitmap against actual behavior to fire frontmatter_drift
    alerts.

    Returns None if there's not enough history to learn from.
    """
    from event_store import event_store
    import json as _json

    conn = event_store._conn()
    runs = conn.execute(
        "SELECT id, session_id, first_event_id, last_event_id, event_count "
        "FROM cc_skill_runs "
        "WHERE skill_name = ? AND event_count >= ? "
        "ORDER BY started_at DESC LIMIT 50",
        (skill_name, _MIN_EVENTS_TO_LEARN),
    ).fetchall()
    if len(runs) < _MIN_RUNS_TO_LEARN:
        return None

    counts = {k: 0 for k in CAP_KEYS}
    sample_paths: Dict[str, List[str]] = {k: [] for k in CAP_KEYS}

    for run in runs:
        first_id = run["first_event_id"]
        last_id = run["last_event_id"] or first_id
        if first_id is None:
            continue
        # CRITICAL: must filter by session_id. cc_events.id is a global
        # autoincrement, so multiple sessions interleave. Without the
        # session filter, a cron-job event landing inside [first_id,last_id]
        # (e.g. CTA snapshots from a different session) gets misattributed
        # to this skill_run.
        events = conn.execute(
            "SELECT tool_name, payload FROM cc_events "
            "WHERE session_id = ? AND id BETWEEN ? AND ? "
            "AND event_type = 'tool_call'",
            (run["session_id"], first_id, last_id),
        ).fetchall()
        for ev in events:
            try:
                payload = _json.loads(ev["payload"]) if ev["payload"] else {}
            except Exception:
                payload = {}
            args = payload.get("tool_input") or payload.get("args") or {}
            cap = required_capability({
                "event_type": "tool_call",
                "tool_name": ev["tool_name"],
                "tool_input": args,
            })
            if not cap:
                continue
            counts[cap] += 1
            if cap in ("write_to_data", "write_to_knowledge", "modify_strategy_code"):
                fp = (args.get("file_path") or "")[:200]
                if fp and fp not in sample_paths[cap] and len(sample_paths[cap]) < 5:
                    sample_paths[cap].append(fp)

    if sum(counts.values()) == 0:
        return None

    # Build history_hints: int counts plus a few sample paths for write categories.
    # No capability bitmap here — capabilities only come from frontmatter; this
    # is purely observational data shown in the UI and used by the guardrail
    # for drift detection.
    hints: Dict[str, Any] = dict(counts)
    for k, paths in sample_paths.items():
        if paths:
            hints[f"{k}__samples"] = paths

    return {
        "history_hints": hints,
        "n_runs": len(runs),
    }

