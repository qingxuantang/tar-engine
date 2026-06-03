"""SQLite persistent store for cockpit wishes, plans, run results, and profiles.

DB lives at $COCKPIT_DB or /data/cockpit.db (next to existing state.db in the
engine_engine-data volume — same persistence guarantees).

Why separate DB: avoids coupling cockpit to the audit pipeline's state.db. Schema
migrations are cleaner. Either DB can be wiped without affecting the other.

Threading: opens a fresh sqlite connection per thread (sqlite3 default rule).
WAL mode for concurrent reader/writer; should be plenty for cockpit MVP loads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(os.environ.get("COCKPIT_DB", "/data/cockpit.db"))


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS cockpit_wishes (
        task_id        TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        wish           TEXT NOT NULL,
        context        TEXT,                -- JSON
        status         TEXT NOT NULL,        -- pending|planning|dispatched|running|done|failed
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        token_usage    TEXT DEFAULT '{}',    -- JSON: aggregated per-wish token cost (UX A, 2026-05-10)
        completed_at   TEXT                  -- mirror of run_results.completed_at, for daily-sum queries
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wish_user ON cockpit_wishes (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_wish_status ON cockpit_wishes (status)",
    "CREATE INDEX IF NOT EXISTS idx_wish_user_completed ON cockpit_wishes (user_id, completed_at)",
    """
    CREATE TABLE IF NOT EXISTS cockpit_plans (
        task_id           TEXT PRIMARY KEY,
        intent            TEXT,
        sub_questions     TEXT,            -- JSON array
        skill_chain       TEXT,            -- JSON array
        clarifications    TEXT,            -- JSON array
        raw_llm_response  TEXT,
        created_at        TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES cockpit_wishes (task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cockpit_run_results (
        task_id          TEXT PRIMARY KEY,
        skill_results    TEXT,             -- JSON array
        final_output     TEXT,
        audit_verdicts   TEXT,             -- JSON array
        profile_updates  TEXT,             -- JSON array
        completed_at     TEXT,
        FOREIGN KEY (task_id) REFERENCES cockpit_wishes (task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cockpit_profiles (
        user_id     TEXT PRIMARY KEY,
        version     TEXT NOT NULL,
        data        TEXT NOT NULL,        -- JSON blob
        updated_at  TEXT NOT NULL
    )
    """,
    # ── Execution Trace (Sprint A T1, 2026-05-19) ──────────────────────
    # One row per discrete step in a wish run. Captures the full execution
    # timeline (plan / skill_start / tool_call / tool_result / skill_end /
    # audit / profile_update / error) with timing + token cost.
    # Designed to be queryable per-step without parsing JSON blobs.
    # See docs/10-next/PLAN_EXECUTION_TRACE_AND_RETROSPECTIVE.md.
    """
    CREATE TABLE IF NOT EXISTS cockpit_run_trace (
        trace_id     TEXT PRIMARY KEY,
        task_id      TEXT NOT NULL,
        seq          INTEGER NOT NULL,
        step_type    TEXT NOT NULL,
        skill_name   TEXT,
        tool_name    TEXT,
        payload      TEXT,                -- JSON
        tokens_in    INTEGER DEFAULT 0,
        tokens_out   INTEGER DEFAULT 0,
        started_at   TEXT NOT NULL,
        ended_at     TEXT,
        duration_ms  INTEGER,
        error        TEXT,
        FOREIGN KEY (task_id) REFERENCES cockpit_wishes (task_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trace_task ON cockpit_run_trace (task_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_trace_step ON cockpit_run_trace (task_id, step_type)",
    # ── Audit baseline history (C1, 2026-06-02) ────────────────────────
    # One row per static audit of a SKILL.md. Enables same-skill historical
    # comparison: after N audits the report can cite mean/stddev of past
    # scores and recurring rule_ids. skill_hash = sha256(skill_name +
    # frontmatter.description[:100]) — stable across body edits, changes
    # when the skill's identity meaningfully shifts.
    # See § "Historical baseline" in the audit report formatter.
    """
    CREATE TABLE IF NOT EXISTS cockpit_audit_history (
        audit_id          TEXT PRIMARY KEY,
        skill_hash        TEXT NOT NULL,
        skill_name        TEXT NOT NULL,
        score             INTEGER NOT NULL,
        grade             TEXT NOT NULL,
        sev_counts        TEXT NOT NULL,    -- JSON: {critical, high, warning, info}
        finding_rule_ids  TEXT NOT NULL,    -- JSON array of rule_ids that fired
        domain            TEXT NOT NULL,
        engine_version    TEXT NOT NULL,
        rule_set_version  TEXT NOT NULL,
        audited_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_skill ON cockpit_audit_history (skill_hash, audited_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_name ON cockpit_audit_history (skill_name)",
]


_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    for ddl in SCHEMA:
        conn.execute(ddl)
    _migrate_add_columns(conn)
    conn.commit()
    return conn


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for older DBs.

    SQLite's CREATE TABLE IF NOT EXISTS won't add new columns to existing tables.
    We add them defensively here so older DBs don't crash on read.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(cockpit_wishes)").fetchall()}
    if "token_usage" not in cols:
        conn.execute("ALTER TABLE cockpit_wishes ADD COLUMN token_usage TEXT DEFAULT '{}'")
    if "completed_at" not in cols:
        conn.execute("ALTER TABLE cockpit_wishes ADD COLUMN completed_at TEXT")


class CockpitStore:
    """Per-instance store. Construct once and pass around (or use module-level singleton)."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self.db_path)
        return self._conn

    # ── wishes ───────────────────────────────────────────────────────────

    def save_wish(self, task_dict: dict[str, Any]) -> None:
        now = _now()
        self._c().execute(
            """INSERT OR REPLACE INTO cockpit_wishes
               (task_id, user_id, wish, context, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                task_dict["task_id"],
                task_dict["user_id"],
                task_dict["wish"],
                json.dumps(task_dict.get("context") or {}, ensure_ascii=False),
                task_dict.get("status", "pending"),
                task_dict.get("created_at", now),
                now,
            ),
        )
        self._c().commit()

    def get_wish(self, task_id: str) -> Optional[dict[str, Any]]:
        row = self._c().execute(
            "SELECT * FROM cockpit_wishes WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["context"] = json.loads(d["context"]) if d["context"] else {}
        return d

    def update_wish_status(self, task_id: str, status: str) -> None:
        """Update wish status. Sets completed_at when status enters terminal state."""
        now = _now()
        terminal = status in ("done", "failed")
        if terminal:
            self._c().execute(
                "UPDATE cockpit_wishes SET status = ?, updated_at = ?, completed_at = ? WHERE task_id = ?",
                (status, now, now, task_id),
            )
        else:
            self._c().execute(
                "UPDATE cockpit_wishes SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, now, task_id),
            )
        self._c().commit()

    def update_wish_token_usage(self, task_id: str, usage: dict[str, Any]) -> None:
        """Replace the token_usage JSON for a wish. Caller decides aggregation logic."""
        self._c().execute(
            "UPDATE cockpit_wishes SET token_usage = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(usage, ensure_ascii=False), _now(), task_id),
        )
        self._c().commit()

    def add_wish_token_usage(
        self,
        task_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        model: str = "",
        skill: str = "",
        estimated_cost_usd: float = 0.0,
    ) -> dict[str, Any]:
        """Atomically add to a wish's running token tally. Returns the new aggregate."""
        row = self._c().execute(
            "SELECT token_usage FROM cockpit_wishes WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown task_id: {task_id}")
        current = json.loads(row["token_usage"] or "{}")
        current["prompt_tokens"] = current.get("prompt_tokens", 0) + prompt_tokens
        current["completion_tokens"] = current.get("completion_tokens", 0) + completion_tokens
        current["total_tokens"] = current["prompt_tokens"] + current["completion_tokens"]
        current["estimated_cost_usd"] = round(
            current.get("estimated_cost_usd", 0.0) + estimated_cost_usd, 6
        )
        if model and not current.get("model"):
            current["model"] = model
        if skill:
            by_skill = current.setdefault("by_skill", {})
            s = by_skill.setdefault(skill, {"prompt": 0, "completion": 0})
            s["prompt"] += prompt_tokens
            s["completion"] += completion_tokens
        self.update_wish_token_usage(task_id, current)
        return current

    def sum_user_tokens_since(self, user_id: str, since_iso: str) -> dict[str, Any]:
        """Sum a user's token usage across all wishes completed since `since_iso`.

        Used by the daily budget enforcer (Task C). Pulls from completed_at so
        in-flight wishes don't count yet.
        """
        rows = self._c().execute(
            """SELECT token_usage FROM cockpit_wishes
               WHERE user_id = ? AND completed_at IS NOT NULL AND completed_at >= ?""",
            (user_id, since_iso),
        ).fetchall()
        prompt = 0
        completion = 0
        cost = 0.0
        for r in rows:
            usage = json.loads(r["token_usage"] or "{}")
            prompt += int(usage.get("prompt_tokens", 0))
            completion += int(usage.get("completion_tokens", 0))
            cost += float(usage.get("estimated_cost_usd", 0.0))
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "estimated_cost_usd": round(cost, 6),
            "wish_count": len(rows),
        }

    def list_wishes(self, user_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        if user_id:
            rows = self._c().execute(
                "SELECT * FROM cockpit_wishes WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = self._c().execute(
                "SELECT * FROM cockpit_wishes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["context"] = json.loads(d["context"]) if d["context"] else {}
            out.append(d)
        return out

    # ── plans ───────────────────────────────────────────────────────────

    def save_plan(self, plan_dict: dict[str, Any], raw_llm_response: str = "") -> None:
        self._c().execute(
            """INSERT OR REPLACE INTO cockpit_plans
               (task_id, intent, sub_questions, skill_chain, clarifications,
                raw_llm_response, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                plan_dict["task_id"],
                plan_dict.get("intent"),
                json.dumps(plan_dict.get("sub_questions", []), ensure_ascii=False),
                json.dumps(plan_dict.get("skill_chain", []), ensure_ascii=False),
                json.dumps(plan_dict.get("clarifications_needed", []), ensure_ascii=False),
                raw_llm_response,
                _now(),
            ),
        )
        self._c().commit()

    def get_plan(self, task_id: str) -> Optional[dict[str, Any]]:
        row = self._c().execute(
            "SELECT * FROM cockpit_plans WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("sub_questions", "skill_chain", "clarifications"):
            d[k] = json.loads(d[k]) if d[k] else []
        return d

    # ── run results ─────────────────────────────────────────────────────

    def save_run_result(self, result_dict: dict[str, Any]) -> None:
        self._c().execute(
            """INSERT OR REPLACE INTO cockpit_run_results
               (task_id, skill_results, final_output, audit_verdicts, profile_updates, completed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                result_dict["task_id"],
                json.dumps(result_dict.get("skill_results", []), ensure_ascii=False),
                result_dict.get("final_output"),
                json.dumps(result_dict.get("audit_verdicts", []), ensure_ascii=False),
                json.dumps(result_dict.get("profile_updates", []), ensure_ascii=False),
                result_dict.get("completed_at"),
            ),
        )
        self._c().commit()

    def get_run_result(self, task_id: str) -> Optional[dict[str, Any]]:
        row = self._c().execute(
            "SELECT * FROM cockpit_run_results WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("skill_results", "audit_verdicts", "profile_updates"):
            d[k] = json.loads(d[k]) if d[k] else []
        return d

    # ── profiles ────────────────────────────────────────────────────────

    def get_profile(self, user_id: str) -> dict[str, Any]:
        row = self._c().execute(
            "SELECT * FROM cockpit_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            default = {
                "user_id": user_id,
                "version": "v0",
                "factor_preferences": {},
                "risk_thresholds": {},
                "parameter_search_history": [],
                "resolved_conflicts": [],
            }
            self.save_profile(user_id, default)
            return default
        return json.loads(row["data"])

    def save_profile(self, user_id: str, data: dict[str, Any]) -> None:
        data["user_id"] = user_id  # enforce
        version = data.get("version", "v0")
        self._c().execute(
            """INSERT OR REPLACE INTO cockpit_profiles (user_id, version, data, updated_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, version, json.dumps(data, ensure_ascii=False), _now()),
        )
        self._c().commit()

    def update_profile(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        prof = self.get_profile(user_id)
        for k, v in updates.items():
            prof[k] = v
        self.save_profile(user_id, prof)
        return prof

    # ── execution trace (Sprint A T1) ─────────────────────────────────

    def append_trace_step(self, step: dict[str, Any]) -> None:
        """Append one execution-trace step.

        Required keys: trace_id, task_id, seq, step_type, started_at.
        Optional: skill_name, tool_name, payload (dict — JSON-encoded),
                  tokens_in, tokens_out, ended_at, duration_ms, error.
        """
        payload = step.get("payload")
        payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        self._c().execute(
            """INSERT INTO cockpit_run_trace
               (trace_id, task_id, seq, step_type, skill_name, tool_name,
                payload, tokens_in, tokens_out, started_at, ended_at,
                duration_ms, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                step["trace_id"],
                step["task_id"],
                int(step["seq"]),
                step["step_type"],
                step.get("skill_name"),
                step.get("tool_name"),
                payload_json,
                int(step.get("tokens_in", 0) or 0),
                int(step.get("tokens_out", 0) or 0),
                step["started_at"],
                step.get("ended_at"),
                int(step["duration_ms"]) if step.get("duration_ms") is not None else None,
                step.get("error"),
            ),
        )
        self._c().commit()

    def get_trace(self, task_id: str) -> list[dict[str, Any]]:
        """Return all trace steps for a wish, ordered by seq."""
        rows = self._c().execute(
            "SELECT * FROM cockpit_run_trace WHERE task_id = ? ORDER BY seq ASC",
            (task_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("payload"):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (ValueError, TypeError):
                    pass
            out.append(d)
        return out

    # ── audit baseline history (C1, 2026-06-02) ───────────────────────

    def record_audit(self, *, audit_id: str, skill_hash: str, skill_name: str,
                     score: int, grade: str, sev_counts: dict[str, int],
                     finding_rule_ids: list[str], domain: str,
                     engine_version: str, rule_set_version: str,
                     audited_at: Optional[str] = None) -> None:
        """Record one static audit. Idempotent on audit_id."""
        self._c().execute(
            """INSERT OR REPLACE INTO cockpit_audit_history
               (audit_id, skill_hash, skill_name, score, grade, sev_counts,
                finding_rule_ids, domain, engine_version, rule_set_version,
                audited_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                audit_id,
                skill_hash,
                skill_name,
                int(score),
                grade,
                json.dumps(sev_counts, ensure_ascii=False),
                json.dumps(finding_rule_ids, ensure_ascii=False),
                domain,
                engine_version,
                rule_set_version,
                audited_at or _now(),
            ),
        )
        self._c().commit()

    def get_audit_history(self, skill_hash: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to `limit` past audits for a skill, newest first.

        Used by the baseline distiller to compute mean/stddev/top-recurring-rules.
        """
        rows = self._c().execute(
            """SELECT audit_id, skill_hash, skill_name, score, grade, sev_counts,
                      finding_rule_ids, domain, engine_version, rule_set_version,
                      audited_at
               FROM cockpit_audit_history
               WHERE skill_hash = ?
               ORDER BY audited_at DESC
               LIMIT ?""",
            (skill_hash, int(limit)),
        ).fetchall()
        return [self._decode_audit_row(dict(r)) for r in rows]

    def get_audit_history_by_name(self, skill_name: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return audits for a skill keyed by skill_name (not skill_hash).

        Used by the MCP `get_audit_baseline(skill_name)` tool — callers won't
        have the hash, only the human-readable name. We return ALL audits where
        skill_name matched, across whatever skill_hashes existed under that
        name (covers renames + description rewrites cleanly).
        """
        rows = self._c().execute(
            """SELECT audit_id, skill_hash, skill_name, score, grade, sev_counts,
                      finding_rule_ids, domain, engine_version, rule_set_version,
                      audited_at
               FROM cockpit_audit_history
               WHERE skill_name = ?
               ORDER BY audited_at DESC
               LIMIT ?""",
            (skill_name, int(limit)),
        ).fetchall()
        return [self._decode_audit_row(dict(r)) for r in rows]

    @staticmethod
    def _decode_audit_row(d: dict[str, Any]) -> dict[str, Any]:
        for jf in ("sev_counts", "finding_rule_ids"):
            if d.get(jf):
                try:
                    d[jf] = json.loads(d[jf])
                except (ValueError, TypeError):
                    pass
        return d


# ──────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────

_store: Optional[CockpitStore] = None


def get_store(db_path: Optional[Path] = None) -> CockpitStore:
    global _store
    if _store is None or db_path is not None:
        _store = CockpitStore(db_path=db_path or DEFAULT_DB_PATH)
    return _store


def reset_store_for_tests() -> None:
    """Test-only: forget the singleton so tests can pin a tmp path."""
    global _store
    if _store and _store._conn:
        _store._conn.close()
    _store = None
