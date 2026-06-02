"""Event persistence for TAR Engine V2 auditor pipeline.

Stores all Claude Code execution events (tool calls, results, assistant
messages) in SQLite. Provides retrieval by session, by skill, and
decision-chain extraction.

Uses the same ENGINE_HOME/state.db as the existing workflow engine,
adding new tables: cc_events, cc_sessions, audit_reports.
"""

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ENGINE_HOME = Path(os.getenv("ENGINE_HOME", Path.home() / ".engine"))
DB_PATH = ENGINE_HOME / "state.db"

# Tool names that are "mechanical" (not decision points)
MECHANICAL_TOOLS = frozenset({
    "Read", "Glob", "Grep", "Bash",  # info gathering
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",  # task management
})

# Tool names that are "decision" tools (create or modify state)
DECISION_TOOLS = frozenset({
    "Edit", "Write", "NotebookEdit",  # file mutations
})


def _hash_token(token: str) -> str:
    """SHA-256 hash a token for storage. Raw token is never persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class EventStore:
    """SQLite-backed event persistence. Thread-safe via per-thread connections."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def _ensure_tables(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cc_sessions (
                session_id   TEXT PRIMARY KEY,
                skill_name   TEXT,
                user_id      TEXT,
                domain       TEXT DEFAULT 'quant',
                started_at   TEXT NOT NULL,
                ended_at     TEXT,
                status       TEXT DEFAULT 'running',
                meta         TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS cc_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                tool_name    TEXT DEFAULT '',
                payload      TEXT NOT NULL,
                ts           TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES cc_sessions(session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_cc_events_session
                ON cc_events(session_id, id);
            CREATE INDEX IF NOT EXISTS idx_cc_events_type
                ON cc_events(event_type);

            CREATE TABLE IF NOT EXISTS audit_reports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                report_type  TEXT NOT NULL,
                content      TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES cc_sessions(session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_audit_session
                ON audit_reports(session_id);
        """)
        conn.commit()
        self._migrate_schema()

    def _migrate_schema(self):
        """Apply incremental schema migrations (idempotent)."""
        conn = self._conn()
        # Migration 1: add tool_use_id column to cc_events (2026-04-17)
        try:
            conn.execute("SELECT tool_use_id FROM cc_events LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE cc_events ADD COLUMN tool_use_id TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_events_tool_use_id ON cc_events(tool_use_id)")
            conn.commit()

        # Migration 2: users + invite_codes tables (2026-04-17)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      TEXT PRIMARY KEY,
                email        TEXT UNIQUE,
                token        TEXT UNIQUE NOT NULL,
                display_name TEXT DEFAULT '',
                created_at   TEXT NOT NULL,
                last_seen    TEXT,
                status       TEXT DEFAULT 'active',
                meta         TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_users_token ON users(token);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

            CREATE TABLE IF NOT EXISTS invite_codes (
                code         TEXT PRIMARY KEY,
                created_by   TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                used_by      TEXT,
                used_at      TEXT,
                max_uses     INTEGER DEFAULT 1,
                use_count    INTEGER DEFAULT 0,
                expires_at   TEXT,
                meta         TEXT DEFAULT '{}'
            );
        """)
        conn.commit()

        # Migration 3: add token_expires_at column to users (2026-04-18)
        try:
            conn.execute("SELECT token_expires_at FROM users LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE users ADD COLUMN token_expires_at TEXT")
            conn.commit()

        # Migration 4: hash existing plaintext tokens (2026-04-18)
        # Detect plaintext tokens (tok_ prefix = unhashed, 64-char hex = already hashed)
        rows = conn.execute("SELECT user_id, token FROM users").fetchall()
        for r in rows:
            tok = r["token"] if isinstance(r, sqlite3.Row) else r[1]
            uid = r["user_id"] if isinstance(r, sqlite3.Row) else r[0]
            # Plaintext tokens start with tok_ or eng_; hashed ones are 64-char hex
            if tok and len(tok) != 64:
                conn.execute(
                    "UPDATE users SET token = ? WHERE user_id = ?",
                    (_hash_token(tok), uid),
                )
        conn.commit()

        # Migration 5: cc_alerts table for persisted risk alerts (2026-04-19)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cc_alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                rule_name    TEXT NOT NULL,
                severity     TEXT NOT NULL,
                message      TEXT NOT NULL,
                tool_name    TEXT DEFAULT '',
                ts           TEXT NOT NULL,
                details      TEXT DEFAULT '{}',
                FOREIGN KEY (session_id) REFERENCES cc_sessions(session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cc_alerts_session
                ON cc_alerts(session_id);
            CREATE INDEX IF NOT EXISTS idx_cc_alerts_severity
                ON cc_alerts(severity);

            CREATE TABLE IF NOT EXISTS audit_settings (
                key          TEXT PRIMARY KEY,
                value        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cc_skill_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                skill_name      TEXT NOT NULL,
                confidence      REAL DEFAULT 0,
                started_at      TEXT NOT NULL,
                ended_at        TEXT,
                first_event_id  INTEGER,
                last_event_id   INTEGER,
                event_count     INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'active',
                FOREIGN KEY (session_id) REFERENCES cc_sessions(session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_skill_runs_session
                ON cc_skill_runs(session_id);
            CREATE INDEX IF NOT EXISTS idx_skill_runs_skill
                ON cc_skill_runs(skill_name);
        """)
        conn.commit()

        # Migration 6: skill_risk_genes table (2026-04-21)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS skill_risk_genes (
                skill_name   TEXT PRIMARY KEY,
                gene         TEXT NOT NULL,
                version      INTEGER DEFAULT 1,
                updated_at   TEXT NOT NULL
            );
        """)
        conn.commit()

        # Migration 7: add device_id column to users (2026-04-23)
        try:
            conn.execute("SELECT device_id FROM users LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE users ADD COLUMN device_id TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_device_id ON users(device_id)")
            conn.commit()

        # Migration 8: add skill_run_id column to audit_reports (2026-04-30)
        try:
            conn.execute("SELECT skill_run_id FROM audit_reports LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE audit_reports ADD COLUMN skill_run_id INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_skill_run ON audit_reports(skill_run_id)")
            conn.commit()

        # Migration 9: add skill_run_id column to cc_alerts (2026-05-01)
        try:
            conn.execute("SELECT skill_run_id FROM cc_alerts LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE cc_alerts ADD COLUMN skill_run_id INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_alerts_skill_run ON cc_alerts(skill_run_id)")
            conn.commit()

        # Migration 10: skill extracted rules + quant skill whitelist (2026-05-01)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cc_skill_extracted_rules (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name      TEXT NOT NULL,
                rule_name       TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                severity        TEXT NOT NULL DEFAULT 'warning',
                match_tool      TEXT DEFAULT '',
                match_file      TEXT DEFAULT '',
                match_content   TEXT DEFAULT '',
                message         TEXT DEFAULT '',
                source_text     TEXT DEFAULT '',
                extracted_at    TEXT NOT NULL,
                UNIQUE(skill_name, rule_name)
            );
            CREATE INDEX IF NOT EXISTS idx_skill_rules_skill
                ON cc_skill_extracted_rules(skill_name);
        """)
        conn.commit()

        # Seed quant skill whitelist (can be updated via API)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO audit_settings (key, value) VALUES (?, ?)",
                ("domain_skills", json.dumps({
                    "quant": ["回测分析", "自动迭代", "AI CTA"],
                })),
            )
            conn.commit()
        except Exception:
            pass

        # Migration 11: add parent_run_id + detection_method to cc_skill_runs (2026-05-02)
        try:
            conn.execute("ALTER TABLE cc_skill_runs ADD COLUMN parent_run_id INTEGER")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE cc_skill_runs ADD COLUMN detection_method TEXT DEFAULT 'sliding_window'")
        except Exception:
            pass
        conn.commit()

        # Migration 12: add role column to users (2026-05-04)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        except Exception:
            pass
        conn.commit()

        # Migration 13: skill_capabilities table (2026-05-04)
        # Per-skill 6-dimension capability bitmap, replaces single-axis intent.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS skill_capabilities (
                skill_name          TEXT PRIMARY KEY,
                caps_json           TEXT NOT NULL,
                source              TEXT NOT NULL DEFAULT 'manual',
                updated_at          TEXT NOT NULL,
                history_hints_json  TEXT DEFAULT '{}'
            );
        """)
        conn.commit()

        # Migration 14: split declared (frontmatter, immutable) vs effective
        # (admin can narrow OR extend, but extensions get audited).
        # The author's allowed-tools declaration is the AUTHORITATIVE record;
        # admin choices live in the existing caps_json (renamed conceptually
        # to "effective"). frontmatter_drift alerts compare against declared.
        try:
            conn.execute(
                "ALTER TABLE skill_capabilities ADD COLUMN declared_caps_json TEXT DEFAULT '{}'"
            )
        except Exception:
            pass
        conn.commit()

    # ── Session management ──────────────────────────────────────────

    def create_session(
        self,
        session_id: str,
        skill_name: str = "",
        user_id: str = "",
        domain: str = "quant",
        meta: Optional[Dict] = None,
    ) -> str:
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO cc_sessions (session_id, skill_name, user_id, domain, started_at, meta) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                skill_name,
                user_id,
                domain,
                datetime.utcnow().isoformat(),
                json.dumps(meta or {}),
            ),
        )
        conn.commit()
        return session_id

    def ensure_session(
        self,
        session_id: str,
        meta: Optional[Dict] = None,
    ) -> bool:
        """Create session if it doesn't exist. Returns True if newly created.

        Used for auto-session creation when CC's session_id arrives
        with the first batch of events (no explicit create_session call).
        """
        existing = self.get_session(session_id)
        if existing is not None:
            return False
        self.create_session(
            session_id=session_id,
            skill_name=meta.get("skill_name", "") if meta else "",
            user_id=meta.get("user_id", "") if meta else "",
            domain=meta.get("domain", "general") if meta else "general",
            meta=meta,
        )
        return True

    def end_session(self, session_id: str, status: str = "completed"):
        conn = self._conn()
        conn.execute(
            "UPDATE cc_sessions SET ended_at = ?, status = ? WHERE session_id = ?",
            (datetime.utcnow().isoformat(), status, session_id),
        )
        conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict]:
        row = self._conn().execute(
            "SELECT * FROM cc_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── Event storage ───────────────────────────────────────────────

    def store(self, session_id: str, event: Dict[str, Any]) -> int:
        """Store a single event. Returns the event id."""
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO cc_events (session_id, event_type, tool_name, tool_use_id, payload, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                event.get("event_type", "unknown"),
                event.get("tool_name", ""),
                event.get("tool_use_id", ""),
                json.dumps(event, ensure_ascii=False),
                event.get("timestamp", datetime.utcnow().isoformat()),
            ),
        )
        conn.commit()
        return cur.lastrowid

    def store_batch(self, session_id: str, events: List[Dict[str, Any]]) -> int:
        """Store multiple events in one transaction. Returns count stored.

        Side-effect: injects _event_id into each event dict in-place,
        so downstream pipeline steps can reference the DB-assigned ID.
        """
        conn = self._conn()
        rows = [
            (
                session_id,
                e.get("event_type", "unknown"),
                e.get("tool_name", ""),
                e.get("tool_use_id", ""),
                json.dumps(e, ensure_ascii=False),
                e.get("timestamp", datetime.utcnow().isoformat()),
            )
            for e in events
        ]
        conn.executemany(
            "INSERT INTO cc_events (session_id, event_type, tool_name, tool_use_id, payload, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        # Inject DB-assigned IDs back into event dicts for downstream use
        if rows:
            last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            first_id = last_id - len(rows) + 1
            for i, e in enumerate(events):
                e["_event_id"] = first_id + i
        return len(rows)

    # ── Event retrieval ─────────────────────────────────────────────

    def get_session_events(self, session_id: str) -> List[Dict]:
        """Get all events for a session, ordered by id."""
        rows = self._conn().execute(
            "SELECT payload FROM cc_events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_session_events_range(self, session_id: str, from_id: int, to_id: int) -> List[Dict]:
        """Get events for a session within an event ID range."""
        rows = self._conn().execute(
            "SELECT payload FROM cc_events WHERE session_id = ? AND id >= ? AND id <= ? ORDER BY id",
            (session_id, from_id, to_id),
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_history(self, skill_name: str, limit: int = 20) -> List[Dict]:
        """Get recent sessions for a skill (for cross-run comparison)."""
        rows = self._conn().execute(
            "SELECT * FROM cc_sessions WHERE skill_name = ? ORDER BY started_at DESC LIMIT ?",
            (skill_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_tool_calls(self, session_id: str, limit: int = 15) -> List[Dict]:
        """Get the most recent tool_call events for a session.

        Returns list of dicts with id + parsed payload, newest last.
        Used by sliding window skill detection.
        """
        rows = self._conn().execute(
            "SELECT id, payload FROM cc_events "
            "WHERE session_id = ? AND event_type = 'tool_call' "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        result = []
        for r in reversed(rows):  # reverse to get oldest-first order
            try:
                payload = json.loads(r["payload"])
                payload["_event_id"] = r["id"]
                result.append(payload)
            except (json.JSONDecodeError, TypeError):
                pass
        return result

    # ── Skill runs ─────────────────────────────────────────────────

    def get_active_skill_run(self, session_id: str) -> Optional[Dict]:
        """Get the currently active skill_run for a session."""
        row = self._conn().execute(
            "SELECT * FROM cc_skill_runs "
            "WHERE session_id = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_active_explicit_run(self, session_id: str) -> Optional[Dict]:
        """Get the currently active explicit (Skill tool) run for a session."""
        row = self._conn().execute(
            "SELECT * FROM cc_skill_runs "
            "WHERE session_id = ? AND status = 'active' AND detection_method = 'explicit' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def end_explicit_skill_run(self, session_id: str):
        """End the active explicit skill run for a session."""
        conn = self._conn()
        conn.execute(
            "UPDATE cc_skill_runs SET status = 'ended', ended_at = ? "
            "WHERE session_id = ? AND status = 'active' AND detection_method = 'explicit'",
            (datetime.utcnow().isoformat(), session_id),
        )
        conn.commit()

    def create_skill_run(
        self, session_id: str, skill_name: str,
        confidence: float, first_event_id: int,
        parent_run_id: int = None,
        detection_method: str = "sliding_window",
    ) -> int:
        """Create a new skill_run. Returns the run id.

        For explicit detections (Skill tool), the previous active run is NOT
        ended — it becomes the parent. For sliding_window detections, the
        previous active run is ended (original behavior).
        """
        conn = self._conn()
        now = datetime.utcnow().isoformat()

        if detection_method == "sliding_window":
            # Original behavior: end previous active run
            conn.execute(
                "UPDATE cc_skill_runs SET status = 'ended', ended_at = ? "
                "WHERE session_id = ? AND status = 'active'",
                (now, session_id),
            )

        cur = conn.execute(
            "INSERT INTO cc_skill_runs "
            "(session_id, skill_name, confidence, started_at, first_event_id, "
            "last_event_id, event_count, parent_run_id, detection_method) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (session_id, skill_name, confidence, now, first_event_id,
             first_event_id, parent_run_id, detection_method),
        )
        conn.commit()
        return cur.lastrowid

    def update_skill_run(self, run_id: int, last_event_id: int, event_count: int):
        """Update a skill_run's last_event_id and event_count."""
        conn = self._conn()
        conn.execute(
            "UPDATE cc_skill_runs SET last_event_id = ?, event_count = ? WHERE id = ?",
            (last_event_id, event_count, run_id),
        )
        conn.commit()

    def end_skill_run(self, run_id: int):
        """Mark a skill_run as ended."""
        conn = self._conn()
        conn.execute(
            "UPDATE cc_skill_runs SET status = 'ended', ended_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), run_id),
        )
        conn.commit()

    def get_decision_chain(self, session_id: str) -> List[Dict]:
        """Extract decision-relevant events (tool calls that mutate state)."""
        rows = self._conn().execute(
            "SELECT payload FROM cc_events WHERE session_id = ? AND event_type = 'tool_call' ORDER BY id",
            (session_id,),
        ).fetchall()
        decisions = []
        for r in rows:
            event = json.loads(r["payload"])
            tool = event.get("tool_name", "")
            if tool in DECISION_TOOLS or tool not in MECHANICAL_TOOLS:
                decisions.append(event)
        return decisions

    def get_decision_chain_range(self, session_id: str, from_id: int, to_id: int) -> List[Dict]:
        """Extract decision-relevant events within an event ID range."""
        rows = self._conn().execute(
            "SELECT payload FROM cc_events WHERE session_id = ? AND id >= ? AND id <= ? AND event_type = 'tool_call' ORDER BY id",
            (session_id, from_id, to_id),
        ).fetchall()
        decisions = []
        for r in rows:
            event = json.loads(r["payload"])
            tool = event.get("tool_name", "")
            if tool in DECISION_TOOLS or tool not in MECHANICAL_TOOLS:
                decisions.append(event)
        return decisions

    def get_event_pair(self, tool_use_id: str) -> Dict[str, Optional[Dict]]:
        """Get the pre/post event pair for a given tool_use_id."""
        rows = self._conn().execute(
            "SELECT payload FROM cc_events WHERE tool_use_id = ? ORDER BY id",
            (tool_use_id,),
        ).fetchall()
        pair = {"tool_call": None, "tool_result": None}
        for r in rows:
            event = json.loads(r["payload"])
            etype = event.get("event_type", "")
            if etype in pair:
                pair[etype] = event
        return pair

    def get_event_count(self, session_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) as cnt FROM cc_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_event_count_range(self, session_id: str, from_id: int, to_id: int) -> int:
        """Count events within an event ID range."""
        row = self._conn().execute(
            "SELECT COUNT(*) as cnt FROM cc_events WHERE session_id = ? AND id >= ? AND id <= ?",
            (session_id, from_id, to_id),
        ).fetchone()
        return row["cnt"] if row else 0

    # ── Audit reports ───────────────────────────────────────────────

    def save_report(
        self, session_id: str, report_type: str, content: Dict[str, Any],
        skill_run_id: int = None,
    ) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO audit_reports (session_id, report_type, content, created_at, skill_run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                report_type,
                json.dumps(content, ensure_ascii=False),
                datetime.utcnow().isoformat(),
                skill_run_id,
            ),
        )
        conn.commit()
        return cur.lastrowid

    def get_reports(self, session_id: str) -> List[Dict]:
        rows = self._conn().execute(
            "SELECT * FROM audit_reports WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_reports_for_run(self, skill_run_id: int) -> List[Dict]:
        """Get audit reports for a specific skill run."""
        rows = self._conn().execute(
            "SELECT * FROM audit_reports WHERE skill_run_id = ? ORDER BY created_at",
            (skill_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def has_skill_been_audited(self, skill_name: str) -> bool:
        """Check if any run of this skill has an audit report."""
        row = self._conn().execute(
            "SELECT COUNT(*) as cnt FROM audit_reports ar "
            "JOIN cc_skill_runs sr ON ar.skill_run_id = sr.id "
            "WHERE sr.skill_name = ?",
            (skill_name,),
        ).fetchone()
        return row["cnt"] > 0 if row else False

    # ── Extracted rules ────────────────────────────────────────────

    def store_extracted_rules(self, skill_name: str, rules: List[Dict]) -> int:
        """Store rules extracted from a skill's SKILL.md. Returns count stored."""
        conn = self._conn()
        stored = 0
        for r in rules:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO cc_skill_extracted_rules "
                    "(skill_name, rule_name, description, severity, match_tool, "
                    "match_file, match_content, message, source_text, extracted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_name,
                        r.get("rule_name", r.get("name", "")),
                        r.get("description", ""),
                        r.get("severity", "warning"),
                        r.get("match_tool", ""),
                        r.get("match_file", ""),
                        r.get("match_content", ""),
                        r.get("message", ""),
                        r.get("source_text", ""),
                        datetime.utcnow().isoformat(),
                    ),
                )
                stored += 1
            except Exception as e:
                print(f"[event_store] store rule error: {e}")
        conn.commit()
        return stored

    def get_extracted_rules(self, skill_name: str = "") -> List[Dict]:
        """Get extracted rules, optionally filtered by skill."""
        conn = self._conn()
        if skill_name:
            rows = conn.execute(
                "SELECT * FROM cc_skill_extracted_rules WHERE skill_name = ?",
                (skill_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cc_skill_extracted_rules"
            ).fetchall()
        return [dict(r) for r in rows]

    def has_skill_rules_extracted(self, skill_name: str) -> bool:
        """Check if rules have already been extracted for this skill."""
        row = self._conn().execute(
            "SELECT COUNT(*) as cnt FROM cc_skill_extracted_rules WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()
        return row["cnt"] > 0 if row else False

    def get_domain_skills(self, domain: str = "quant") -> List[str]:
        """Get skill names tagged for a specific domain."""
        try:
            row = self._conn().execute(
                "SELECT value FROM audit_settings WHERE key = 'domain_skills'"
            ).fetchone()
            if row:
                mapping = json.loads(row["value"])
                return mapping.get(domain, [])
        except Exception:
            pass
        return []

    def add_skill_to_domain(self, skill_name: str, domain: str = "quant"):
        """Tag a skill as belonging to a domain."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT value FROM audit_settings WHERE key = 'domain_skills'"
            ).fetchone()
            mapping = json.loads(row["value"]) if row else {}
            skills = mapping.get(domain, [])
            if skill_name not in skills:
                skills.append(skill_name)
                mapping[domain] = skills
                conn.execute(
                    "INSERT OR REPLACE INTO audit_settings (key, value) VALUES (?, ?)",
                    ("domain_skills", json.dumps(mapping, ensure_ascii=False)),
                )
                conn.commit()
        except Exception as e:
            print(f"[event_store] add_skill_to_domain error: {e}")

    # ── Skill capabilities ──────────────────────────────────────────
    #
    # Two-track model (post-redesign 2026-05-04):
    #   declared  — comes from SKILL.md frontmatter `allowed-tools`. Authoritative
    #               record of what the AUTHOR designed the skill to do. Admin
    #               cannot edit; only re-running the frontmatter scan updates it.
    #   effective — what the admin currently authorizes. Starts as a copy of
    #               declared. Admin can narrow (less than declared) freely;
    #               admin can extend (more than declared) but the diff is
    #               surfaced in audit and frontmatter_drift alerts still fire.
    #
    # Guardrail uses BOTH at runtime:
    #   - frontmatter_drift alert: when an event needs a cap that declared=false
    #     (regardless of effective). Cannot be silenced by admin.
    #   - capability_violation alert: when effective=false. Means admin
    #     explicitly disallowed it.

    # Default when nothing is known. Permissive on read; conservative on writes
    # so an unclassified skill writing to strategy code still surfaces.
    DEFAULT_CAPS = {
        "read_files": True,
        "write_to_data": False,
        "write_to_knowledge": False,
        "modify_strategy_code": False,
        "external_api": False,
        "git_destructive": False,
    }

    def get_skill_capabilities(self, skill_name: str) -> Dict[str, Any]:
        """Return declared + effective + history for a skill.

        If no row exists: declared = {} (no frontmatter known), effective =
        DEFAULT_CAPS, source = 'default'.
        """
        row = self._conn().execute(
            "SELECT * FROM skill_capabilities WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()
        if not row:
            return {
                "skill_name": skill_name,
                "declared": {},
                "effective": dict(self.DEFAULT_CAPS),
                "source": "default",
                "updated_at": None,
                "history_hints": {},
            }
        try:
            effective = json.loads(row["caps_json"])
        except Exception:
            effective = dict(self.DEFAULT_CAPS)
        try:
            declared = json.loads(row["declared_caps_json"] or "{}")
        except Exception:
            declared = {}
        try:
            hints = json.loads(row["history_hints_json"] or "{}")
        except Exception:
            hints = {}
        # Backfill any newly-introduced cap keys with safe defaults
        for k, v in self.DEFAULT_CAPS.items():
            effective.setdefault(k, v)
        return {
            "skill_name": skill_name,
            "declared": declared,
            "effective": effective,
            "source": row["source"],
            "updated_at": row["updated_at"],
            "history_hints": hints,
        }

    def set_declared_capabilities(self, skill_name: str,
                                  declared: Dict[str, bool]) -> Dict[str, Any]:
        """Frontmatter-derived. Idempotent: re-running with same SKILL.md
        produces the same row. Initializes effective = declared on first
        write so a freshly-discovered skill operates within author intent
        until admin says otherwise.
        """
        conn = self._conn()
        now = datetime.utcnow().isoformat()
        existing = conn.execute(
            "SELECT caps_json, source, history_hints_json "
            "FROM skill_capabilities WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()

        if existing:
            # Keep existing effective + admin overrides; only refresh declared.
            conn.execute(
                "UPDATE skill_capabilities "
                "SET declared_caps_json = ?, updated_at = ? "
                "WHERE skill_name = ?",
                (json.dumps(declared, ensure_ascii=False), now, skill_name),
            )
        else:
            # First time we see this skill — effective starts as a copy of
            # declared (the author's intent is the initial authorization).
            conn.execute(
                "INSERT INTO skill_capabilities "
                "(skill_name, caps_json, declared_caps_json, source, "
                "updated_at, history_hints_json) "
                "VALUES (?, ?, ?, 'frontmatter', ?, '{}')",
                (
                    skill_name,
                    json.dumps(declared, ensure_ascii=False),
                    json.dumps(declared, ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()
        return self.get_skill_capabilities(skill_name)

    def set_effective_capabilities(self, skill_name: str,
                                   effective: Dict[str, bool]) -> Dict[str, Any]:
        """Admin override. Updates only the effective bitmap; declared stays
        as-is. Always sets source='manual' so the UI shows the admin touched
        this row. Does NOT prevent admin from extending beyond declared —
        but the diff is visible in get_skill_capabilities output, and the
        frontmatter_drift alerts continue firing.
        """
        conn = self._conn()
        now = datetime.utcnow().isoformat()
        existing = conn.execute(
            "SELECT declared_caps_json, history_hints_json "
            "FROM skill_capabilities WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()
        declared_json = existing["declared_caps_json"] if existing else "{}"
        hints_json = existing["history_hints_json"] if existing else "{}"
        conn.execute(
            "INSERT OR REPLACE INTO skill_capabilities "
            "(skill_name, caps_json, declared_caps_json, source, "
            "updated_at, history_hints_json) "
            "VALUES (?, ?, ?, 'manual', ?, ?)",
            (
                skill_name,
                json.dumps(effective, ensure_ascii=False),
                declared_json,
                now,
                hints_json,
            ),
        )
        conn.commit()
        return self.get_skill_capabilities(skill_name)

    def update_skill_history_hints(self, skill_name: str,
                                    hints: Dict[str, Any]) -> None:
        """Path B output: refresh observed-behavior counts. Does NOT touch
        either capability bitmap — observation never grants permission.
        Creates a row with default caps if the skill has no row yet.
        """
        conn = self._conn()
        now = datetime.utcnow().isoformat()
        existing = conn.execute(
            "SELECT 1 FROM skill_capabilities WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE skill_capabilities "
                "SET history_hints_json = ?, updated_at = ? "
                "WHERE skill_name = ?",
                (json.dumps(hints, ensure_ascii=False), now, skill_name),
            )
        else:
            conn.execute(
                "INSERT INTO skill_capabilities "
                "(skill_name, caps_json, declared_caps_json, source, "
                "updated_at, history_hints_json) "
                "VALUES (?, ?, '{}', 'default', ?, ?)",
                (
                    skill_name,
                    json.dumps(self.DEFAULT_CAPS, ensure_ascii=False),
                    now,
                    json.dumps(hints, ensure_ascii=False),
                ),
            )
        conn.commit()

    def list_skill_capabilities(self) -> List[Dict[str, Any]]:
        """List all skills with capabilities. Used by the panel."""
        rows = self._conn().execute(
            "SELECT skill_name FROM skill_capabilities"
        ).fetchall()
        return [self.get_skill_capabilities(r["skill_name"]) for r in rows]

    # ── User management ──────────────────────────────────────────────

    def create_invite_code(self, code: str, created_by: str = "admin",
                           max_uses: int = 1, expires_at: str = None):
        conn = self._conn()
        conn.execute(
            "INSERT INTO invite_codes (code, created_by, created_at, max_uses, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, created_by, datetime.utcnow().isoformat(), max_uses, expires_at),
        )
        conn.commit()

    def validate_invite_code(self, code: str) -> Optional[Dict]:
        """Check if an invite code is valid. Returns the code record or None."""
        row = self._conn().execute(
            "SELECT * FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()
        if not row:
            return None
        rec = dict(row)
        # Check usage limit
        if rec["use_count"] >= rec["max_uses"]:
            return None
        # Check expiry
        if rec["expires_at"]:
            if datetime.utcnow().isoformat() > rec["expires_at"]:
                return None
        return rec

    def use_invite_code(self, code: str, user_id: str):
        conn = self._conn()
        conn.execute(
            "UPDATE invite_codes SET use_count = use_count + 1, used_by = ?, used_at = ? "
            "WHERE code = ?",
            (user_id, datetime.utcnow().isoformat(), code),
        )
        conn.commit()

    def create_user(self, user_id: str, token: str, email: str = "",
                    display_name: str = "",
                    token_expires_at: str = None,
                    device_id: str = "") -> Dict:
        conn = self._conn()
        conn.execute(
            "INSERT INTO users (user_id, email, token, display_name, created_at, token_expires_at, device_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, email or None, _hash_token(token), display_name,
             datetime.utcnow().isoformat(), token_expires_at, device_id),
        )
        conn.commit()
        # Return raw token only once at registration; DB stores only the hash
        return {"user_id": user_id, "token": token}

    def get_user_by_token(self, token: str) -> Optional[Dict]:
        row = self._conn().execute(
            "SELECT * FROM users WHERE token = ? AND status = 'active'", (_hash_token(token),)
        ).fetchone()
        if not row:
            return None
        # Check token expiry
        expires = row["token_expires_at"] if "token_expires_at" in row.keys() else None
        if expires and datetime.utcnow().isoformat() > expires:
            return None
        # Update last_seen
        self._conn().execute(
            "UPDATE users SET last_seen = ? WHERE user_id = ?",
            (datetime.utcnow().isoformat(), row["user_id"]),
        )
        self._conn().commit()
        return dict(row)

    def get_user(self, user_id: str) -> Optional[Dict]:
        row = self._conn().execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict]:
        rows = self._conn().execute(
            "SELECT user_id, email, display_name, created_at, last_seen, status "
            "FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_invite_codes(self) -> List[Dict]:
        rows = self._conn().execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def revoke_user(self, user_id: str) -> bool:
        """Deactivate a user, invalidating their token."""
        conn = self._conn()
        conn.execute(
            "UPDATE users SET status = 'disabled' WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return True

    def rotate_user_token(self, user_id: str, new_token: str,
                          expires_at: str = None) -> bool:
        """Replace a user's token with a new one. Stores hash, not plaintext."""
        conn = self._conn()
        conn.execute(
            "UPDATE users SET token = ?, token_expires_at = ? WHERE user_id = ?",
            (_hash_token(new_token), expires_at, user_id),
        )
        conn.commit()
        return True

    # ── Alert persistence ──────────────────────────────────────────

    def store_alerts(self, session_id: str, alerts: list, skill_run_id: int = None) -> int:
        """Persist risk alerts for a session. Returns count stored."""
        conn = self._conn()
        rows = [
            (
                session_id,
                a.rule_name,
                a.severity,
                a.message,
                a.tool_name,
                a.timestamp,
                json.dumps(a.details if isinstance(a.details, dict) else {}, ensure_ascii=False),
                skill_run_id,
            )
            for a in alerts
        ]
        conn.executemany(
            "INSERT INTO cc_alerts (session_id, rule_name, severity, message, tool_name, ts, details, skill_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_session_alerts(self, session_id: str) -> list:
        """Get all alerts for a session."""
        rows = self._conn().execute(
            "SELECT * FROM cc_alerts WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_alerts_for_run(self, skill_run_id: int) -> list:
        """Get alerts for a specific skill run."""
        rows = self._conn().execute(
            "SELECT * FROM cc_alerts WHERE skill_run_id = ? ORDER BY id",
            (skill_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_alert_summary(self) -> list:
        """Get alert counts grouped by session_id for dashboard."""
        rows = self._conn().execute(
            "SELECT session_id, COUNT(*) as alert_count, "
            "SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) as critical_count, "
            "SUM(CASE WHEN severity = 'high' THEN 1 ELSE 0 END) as high_count "
            "FROM cc_alerts GROUP BY session_id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Risk Genes ─────────────────────────────────────────────────

    def get_risk_gene(self, skill_name: str) -> Optional[Dict]:
        """Get the Risk Gene for a skill. Returns parsed gene dict or None."""
        row = self._conn().execute(
            "SELECT gene FROM skill_risk_genes WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["gene"])
        except (json.JSONDecodeError, TypeError):
            return None

    def upsert_risk_gene(self, skill_name: str, gene: Dict):
        """Insert or update a Risk Gene for a skill."""
        conn = self._conn()
        conn.execute(
            "INSERT INTO skill_risk_genes (skill_name, gene, version, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(skill_name) DO UPDATE SET "
            "gene = excluded.gene, version = excluded.version, updated_at = excluded.updated_at",
            (
                skill_name,
                json.dumps(gene, ensure_ascii=False),
                gene.get("version", 1),
                gene.get("updated_at", datetime.utcnow().isoformat()),
            ),
        )
        conn.commit()

    def list_risk_genes(self) -> List[Dict]:
        """List all Risk Genes. Returns list of {skill_name, gene, version, updated_at}."""
        rows = self._conn().execute(
            "SELECT skill_name, gene, version, updated_at FROM skill_risk_genes ORDER BY updated_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            entry = dict(r)
            try:
                entry["gene"] = json.loads(entry["gene"])
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(entry)
        return result

    # ── Stats ───────────────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        conn = self._conn()
        sessions = conn.execute("SELECT COUNT(*) as c FROM cc_sessions").fetchone()["c"]
        events = conn.execute("SELECT COUNT(*) as c FROM cc_events").fetchone()["c"]
        reports = conn.execute("SELECT COUNT(*) as c FROM audit_reports").fetchone()["c"]
        alerts = conn.execute("SELECT COUNT(*) as c FROM cc_alerts").fetchone()["c"]
        return {"sessions": sessions, "events": events, "reports": reports, "alerts": alerts}


# Module-level singleton
event_store = EventStore()
