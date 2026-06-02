"""Base adapter — shared event ingestion pipeline for all agents.

Subclasses only need to:
  1. Set `agent_name` and `session_prefix`
  2. Provide a normalizer (or use the default for their source)
  3. Optionally override `create_session()` for agent-specific logic

The base class handles the entire pipeline:
  normalize → persist → risk check → node map → skill detect → audit
"""

import asyncio
import secrets
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from event_store import event_store
from auditor.domains import get_domain
from auditor.risk_guardrail import RiskGuardrail, RiskAlert
from auditor.node_mapper import NodeMapper
from auditor.signature_extractor import SignatureExtractor
from auditor.skill_registry import skill_registry
from auditor.skill_rule_extractor import SkillRuleExtractor
from auditor.domain_config import RealtimeRule

from .event_schema import EventNormalizer, CCNormalizer, get_normalizer


class BaseAdapter:
    """Generic adapter for ingesting agent events into Engine.

    Owns the full pipeline: session management, event normalization,
    risk checking, node mapping, skill detection, and audit triggers.
    """

    agent_name: str = "unknown"
    session_prefix: str = "agent"

    def __init__(self, normalizer: Optional[EventNormalizer] = None):
        self._normalizer = normalizer or get_normalizer(self.agent_name)
        self._guardrails: Dict[str, RiskGuardrail] = {}
        self._node_mappers: Dict[str, NodeMapper] = {}
        self._signature_extractor = SignatureExtractor()
        self._rule_extractor = SkillRuleExtractor()
        self._alert_callbacks: List[Callable] = []
        self._session_end_callbacks: List[Callable] = []
        self._rules_loaded_for: set = set()  # skills whose extracted rules are loaded

    # ── Guardrails ─────────────────────────────────────────────────

    def get_guardrail(self, domain: str = "general", skill_intent: str = "unknown",
                      skill_name: str = "") -> RiskGuardrail:
        # Capability bitmap is per-skill (not per-intent) so the cache key
        # must include skill_name. Without that, two skills sharing the same
        # intent would reuse the same guardrail and the second would see the
        # first's caps.
        cache_key = f"{domain}:{skill_intent}:{skill_name}"
        if cache_key not in self._guardrails:
            config = get_domain(domain)
            effective = None
            declared = None
            if skill_name:
                try:
                    record = event_store.get_skill_capabilities(skill_name)
                    effective = record.get("effective")
                    declared = record.get("declared") or None
                except Exception:
                    pass
            self._guardrails[cache_key] = RiskGuardrail(
                config,
                skill_intent=skill_intent,
                skill_capabilities=effective,
                declared_capabilities=declared,
            )
        return self._guardrails[cache_key]

    def invalidate_guardrails_for_skill(self, skill_name: str):
        """Drop cached guardrails referencing this skill so the next
        check rebuilds with fresh capabilities. Called when caps change."""
        suffix = f":{skill_name}"
        stale = [k for k in self._guardrails if k.endswith(suffix)]
        for k in stale:
            del self._guardrails[k]

    # ── Callbacks ──────────────────────────────────────────────────

    def on_alert(self, callback: Callable):
        self._alert_callbacks.append(callback)

    def on_session_end(self, callback: Callable):
        self._session_end_callbacks.append(callback)

    # ── Session lifecycle ──────────────────────────────────────────

    def create_session(
        self,
        skill_name: str = "",
        user_id: str = "",
        domain: str = "general",
        meta: Optional[Dict] = None,
        skill_nodes: Optional[List[Dict]] = None,
        parsed_skill: Optional[Dict] = None,
    ) -> Dict[str, str]:
        session_id = f"{self.session_prefix}_{secrets.token_urlsafe(16)}"
        session_secret = secrets.token_urlsafe(32)

        event_store.create_session(
            session_id=session_id,
            skill_name=skill_name,
            user_id=user_id,
            domain=domain,
            meta={**(meta or {}), "_secret_hash": hash(session_secret)},
        )

        nodes = skill_nodes
        if not nodes and parsed_skill:
            try:
                nodes = self._signature_extractor.extract(parsed_skill)
            except Exception as e:
                print(f"[{self.agent_name}] signature extraction failed: {e}")

        if nodes:
            self._node_mappers[session_id] = NodeMapper(nodes)

        return {
            "session_id": session_id,
            "session_secret": session_secret,
            "domain": domain,
            "has_dag": nodes is not None and len(nodes) > 0,
        }

    async def end_session(self, session_id: str, status: str = "completed"):
        event_store.end_session(session_id, status=status)
        for cb in self._session_end_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(session_id)
                else:
                    cb(session_id)
            except Exception as e:
                print(f"[{self.agent_name}] session_end callback error: {e}")

    # ── Event ingestion (the shared pipeline) ──────────────────────

    async def ingest_events(
        self,
        session_id: str,
        events: List[Dict[str, Any]],
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Ingest a batch of events through the full pipeline.

        Steps:
          1. Auto-create session if needed
          2. Normalize events (agent format → EngineEvent)
          3. Persist to EventStore
          4. Real-time risk check
          5. Node mapping (if DAG loaded)
          6. Skill run detection (sliding window)
          7. Session-end detection
        """
        # 1. Auto-create session
        session = event_store.get_session(session_id)
        if session is None:
            first = events[0] if events else {}
            meta_in = first.get("metadata", {})
            event_store.ensure_session(session_id, meta={
                "cwd": first.get("cwd", ""),
                "source": self.agent_name,
                "auto_created": True,
                "user_id": user_id,
                **{k: v for k, v in meta_in.items()
                   if k in ("permission_mode", "reporter_version", "platform")},
            })
            if user_id and user_id != "__admin__":
                conn = event_store._conn()
                conn.execute(
                    "UPDATE cc_sessions SET user_id = ? WHERE session_id = ? AND (user_id IS NULL OR user_id = '')",
                    (user_id, session_id),
                )
                conn.commit()
            session = event_store.get_session(session_id)

        # 2. Normalize
        normalized = [self._normalizer.normalize(e) for e in events]

        # 3. Persist
        stored = event_store.store_batch(session_id, normalized)

        # 4. Risk check (with skill context + intent)
        domain = session.get("domain", "general")
        active_run = event_store.get_active_skill_run(session_id)
        current_skill = active_run["skill_name"] if active_run else session.get("skill_name", "")
        # Resolve skill intent for rule suppression
        try:
            from auditor.skill_registry import get_skill_intent
            skill_intent = get_skill_intent(current_skill)
        except Exception:
            skill_intent = "unknown"
        guardrail = self.get_guardrail(domain, skill_intent=skill_intent,
                                        skill_name=current_skill)

        # Load extracted rules for this skill if not already loaded
        if current_skill and current_skill not in self._rules_loaded_for:
            self._load_skill_rules(current_skill, guardrail)

        alerts = guardrail.check_batch(normalized, skill_name=current_skill)
        if alerts:
            run_id = active_run["id"] if active_run else None
            event_store.store_alerts(session_id, alerts, skill_run_id=run_id)

        # 5. Node mapping
        node_updates = []
        mapper = self._node_mappers.get(session_id)
        if mapper:
            for event in normalized:
                match = mapper.map_event(event)
                if match:
                    node_updates.append({
                        "node_id": match.node_id,
                        "node_name": match.node_name,
                        "score": match.score,
                        "matched_by": match.matched_by,
                    })

        # 6. Alert callbacks
        if alerts and self._alert_callbacks:
            for cb in self._alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(session_id, alerts)
                    else:
                        cb(session_id, alerts)
                except Exception as e:
                    print(f"[{self.agent_name}] alert callback error: {e}")

        # 7. Session-end detection
        has_end = any(
            e.get("event_type") in ("session_end", "skill_end")
            for e in normalized
        )
        if has_end:
            if mapper:
                mapper.complete()
            await self.end_session(session_id)

        # 7.5 Auto-end idle explicit runs. Without this, an explicit run
        # owns the session segment forever — Bug A's known side effect — so
        # any work the user does after the original skill completes gets
        # mis-attributed (e.g. engine code edits showing up as "回测分析"
        # writes to modify_strategy_code). Ending on inactivity restores
        # honest event attribution.
        self._maybe_end_idle_explicit_run(session_id, normalized)

        # 8. Explicit Skill tool detection (highest priority)
        explicit_skill = self._detect_explicit_skill(session_id, normalized)

        # 9. Sliding window skill detection (fallback)
        detected_skill = explicit_skill
        if not explicit_skill:
            detected_skill = self._detect_skill_run(session_id, normalized)

        return {
            "stored": stored,
            "alerts": [a.to_dict() for a in alerts],
            "node_updates": node_updates,
            "session_ended": has_end,
            "detected_skill": detected_skill,
        }

    # Idle window for ending an explicit run that hasn't seen activity
    # tied to the same skill. Tuned for interactive skills with brief
    # user-reply pauses (Telegram round-trips, reading reports, typing
    # a follow-up). Long-paused work should be re-invoked via /skill.
    _EXPLICIT_RUN_IDLE_SECONDS = 10 * 60

    def _maybe_end_idle_explicit_run(self, session_id: str,
                                      normalized: List[Dict]) -> None:
        """End the active explicit run if the gap between its last event and
        the first event in this batch exceeds the idle threshold AND the new
        batch isn't (re-)invoking the same skill via the Skill tool."""
        if not normalized:
            return
        active = event_store.get_active_explicit_run(session_id)
        if not active:
            return

        # If this batch contains a Skill tool call for the SAME skill, the
        # explicit detector will refresh the run's event_count and the user is
        # clearly still in scope — leave it alone.
        for ev in normalized:
            if ev.get("event_type") != "tool_call":
                continue
            if ev.get("tool_name") != "Skill":
                continue
            args = ev.get("tool_input", ev.get("args", {}))
            target = ""
            if isinstance(args, dict):
                target = args.get("skill", "")
            elif isinstance(args, str):
                target = args.strip()
            if target == active["skill_name"]:
                return  # same-skill re-invocation; let _detect_explicit_skill handle it

        # Compare timestamps. last_event_id row gives the most recent event the
        # run has accepted; the new batch's first tool_call ts gives the gap.
        try:
            last_event_id = active.get("last_event_id") or active.get("first_event_id")
            if not last_event_id:
                return
            conn = event_store._conn()
            row = conn.execute(
                "SELECT ts FROM cc_events WHERE id = ?", (last_event_id,),
            ).fetchone()
            if not row or not row["ts"]:
                return
            from datetime import datetime as _dt

            def _parse(ts):
                ts = (ts or "").rstrip("Z")
                # Trim trailing offset (handles "+00:00" too)
                if "+" in ts[10:]:
                    ts = ts.split("+")[0]
                return _dt.fromisoformat(ts)

            last_ts = _parse(row["ts"])
            first_new = next(
                (ev for ev in normalized if ev.get("event_type") == "tool_call"),
                normalized[0],
            )
            new_ts_str = first_new.get("timestamp") or first_new.get("ts") or ""
            if not new_ts_str:
                return
            new_ts = _parse(new_ts_str)
            gap = (new_ts - last_ts).total_seconds()
            if gap >= self._EXPLICIT_RUN_IDLE_SECONDS:
                event_store.end_skill_run(active["id"])
                print(
                    f"[{self.agent_name}] explicit run #{active['id']} "
                    f"({active['skill_name']}) ended after {gap:.0f}s idle"
                )
        except Exception as e:
            print(f"[{self.agent_name}] idle-end check failed: {e}")

    def _detect_explicit_skill(
        self, session_id: str, normalized: List[Dict]
    ) -> Optional[str]:
        """Detect explicit Skill tool invocations in the event batch.

        When tool_name == "Skill", extract args.skill as the skill name
        and create a skill_run with detection_method='explicit'.
        Returns the skill name if a new explicit run was created.
        """
        skill_invocations = []
        for ev in normalized:
            if ev.get("event_type") != "tool_call":
                continue
            if ev.get("tool_name") != "Skill":
                continue
            args = ev.get("tool_input", ev.get("args", {}))
            if isinstance(args, dict):
                skill_name = args.get("skill", "")
            elif isinstance(args, str):
                skill_name = args.strip()
            else:
                continue
            if skill_name:
                event_id = ev.get("_event_id", 0)
                skill_invocations.append((skill_name, event_id))

        if not skill_invocations:
            # No Skill tool calls in this batch — update event count on
            # active explicit run if one exists
            active_explicit = event_store.get_active_explicit_run(session_id)
            if active_explicit:
                latest_id = normalized[-1].get("_event_id", 0) if normalized else 0
                if latest_id:
                    event_store.update_skill_run(
                        active_explicit["id"],
                        last_event_id=latest_id,
                        event_count=active_explicit["event_count"] + len(normalized),
                    )
            return None

        last_created = None
        for skill_name, event_id in skill_invocations:
            # Check if this skill is already the active explicit run
            active_explicit = event_store.get_active_explicit_run(session_id)
            if active_explicit and active_explicit["skill_name"] == skill_name:
                # Same skill invoked again, just update
                event_store.update_skill_run(
                    active_explicit["id"],
                    last_event_id=event_id,
                    event_count=active_explicit["event_count"] + 1,
                )
                continue

            # End previous explicit run if any
            event_store.end_explicit_skill_run(session_id)

            # Get sliding-window active run as potential parent
            active_sw = event_store.get_active_skill_run(session_id)
            parent_id = active_sw["id"] if active_sw else None

            run_id = event_store.create_skill_run(
                session_id=session_id,
                skill_name=skill_name,
                confidence=1.0,  # explicit invocation = full confidence
                first_event_id=event_id,
                parent_run_id=parent_id,
                detection_method="explicit",
            )
            print(f"[{self.agent_name}] explicit skill: '{skill_name}' for {session_id} (run_id={run_id})")

            # Auto-audit first run of a new skill
            if not event_store.has_skill_been_audited(skill_name):
                asyncio.create_task(self._auto_audit_first_run(session_id, skill_name))
            # Auto-extract rules from SKILL.md
            if not event_store.has_skill_rules_extracted(skill_name):
                asyncio.create_task(self._auto_extract_skill_rules(skill_name))

            last_created = skill_name

        return last_created

    def _detect_skill_run(
        self, session_id: str, normalized: List[Dict]
    ) -> Optional[str]:
        """Sliding window skill detection with stickiness heuristic."""
        recent_tools = event_store.get_recent_tool_calls(session_id, limit=15)
        if len(recent_tools) < skill_registry.MIN_EVENTS_FOR_MATCH:
            return None

        active_run = event_store.get_active_skill_run(session_id)

        # Explicit user intent dominates: when the user invoked Skill X via the
        # Skill tool, that scope owns the session segment until they invoke
        # another skill or the session ends. Sliding-window keyword matching
        # is heuristic and must not transition away from an explicit run —
        # otherwise file-heavy skills get misclassified as e.g. doc-index and
        # the explicit run is killed by create_skill_run(sliding_window).
        # The explicit run's event_count was already updated by
        # _detect_explicit_skill in the same processing step.
        if active_run and active_run.get("detection_method") == "explicit":
            return None

        latest_event_id = recent_tools[-1].get("_event_id", 0)
        result = skill_registry.match_events(recent_tools)

        if not result:
            if active_run:
                event_store.update_skill_run(
                    active_run["id"],
                    last_event_id=latest_event_id,
                    event_count=active_run["event_count"] + len(normalized),
                )
                conn = event_store._conn()
                conn.commit()
            return None

        skill_name, score = result

        if active_run and active_run["skill_name"] == skill_name:
            event_store.update_skill_run(
                active_run["id"],
                last_event_id=latest_event_id,
                event_count=active_run["event_count"] + len(normalized),
            )
        elif active_run:
            current_score = skill_registry.score_skill(
                active_run["skill_name"], recent_tools
            )
            if score > max(current_score * 1.5, skill_registry.MATCH_THRESHOLD):
                event_store.create_skill_run(
                    session_id=session_id,
                    skill_name=skill_name,
                    confidence=score,
                    first_event_id=latest_event_id,
                )
                print(f"[{self.agent_name}] skill transition: '{active_run['skill_name']}' → '{skill_name}' (score={score:.2f} vs {current_score:.2f})")
                # Auto-audit first run of a new skill
                if not event_store.has_skill_been_audited(skill_name):
                    asyncio.create_task(self._auto_audit_first_run(session_id, skill_name))
                # Auto-extract rules from SKILL.md
                if not event_store.has_skill_rules_extracted(skill_name):
                    asyncio.create_task(self._auto_extract_skill_rules(skill_name))
            else:
                event_store.update_skill_run(
                    active_run["id"],
                    last_event_id=latest_event_id,
                    event_count=active_run["event_count"] + len(normalized),
                )
                skill_name = active_run["skill_name"]
        else:
            event_store.create_skill_run(
                session_id=session_id,
                skill_name=skill_name,
                confidence=score,
                first_event_id=latest_event_id,
            )
            print(f"[{self.agent_name}] skill run: '{skill_name}' for {session_id} (score={score:.2f})")
            # Auto-audit first run of a new skill
            if not event_store.has_skill_been_audited(skill_name):
                asyncio.create_task(self._auto_audit_first_run(session_id, skill_name))
            # Auto-extract rules from SKILL.md
            if not event_store.has_skill_rules_extracted(skill_name):
                asyncio.create_task(self._auto_extract_skill_rules(skill_name))

        conn = event_store._conn()
        conn.execute(
            "UPDATE cc_sessions SET skill_name = ? WHERE session_id = ?",
            (skill_name, session_id),
        )
        conn.commit()

        if not active_run or active_run["skill_name"] != skill_name:
            return skill_name
        return None

    async def _auto_audit_first_run(self, session_id: str, skill_name: str):
        """Auto-trigger audit for the first run of a newly detected skill."""
        try:
            # Small delay to let more events accumulate
            await asyncio.sleep(30)
            # Get the latest run for this skill
            conn = event_store._conn()
            row = conn.execute(
                "SELECT * FROM cc_skill_runs WHERE session_id = ? AND skill_name = ? ORDER BY id DESC LIMIT 1",
                (session_id, skill_name),
            ).fetchone()
            if not row:
                return
            run = dict(row)
            from auditor.orchestrator import audit_orchestrator
            await audit_orchestrator.audit_skill_run(
                session_id=session_id,
                skill_run_id=run["id"],
                skill_name=skill_name,
                from_id=run["first_event_id"],
                to_id=run["last_event_id"] or run["first_event_id"],
            )
            print(f"[{self.agent_name}] Auto-audited first run of '{skill_name}'")
        except Exception as e:
            print(f"[{self.agent_name}] Auto-audit failed for '{skill_name}': {e}")

    # ── Skill rule extraction ────────────────────────────────────────

    def _load_skill_rules(self, skill_name: str, guardrail: RiskGuardrail):
        """Load extracted rules for a skill into the guardrail."""
        self._rules_loaded_for.add(skill_name)
        rules_data = event_store.get_extracted_rules(skill_name)
        if not rules_data:
            return
        dynamic_rules = []
        for r in rules_data:
            dynamic_rules.append(RealtimeRule(
                name=r["rule_name"],
                description=r.get("description", ""),
                severity=r.get("severity", "warning"),
                match_tool=r.get("match_tool", ""),
                match_file=r.get("match_file", ""),
                match_content=r.get("match_content", ""),
                message=r.get("message", ""),
                only_for_skills=[skill_name],
            ))
        if dynamic_rules:
            guardrail.add_dynamic_rules(dynamic_rules)
            print(f"[{self.agent_name}] Loaded {len(dynamic_rules)} extracted rules for '{skill_name}'")

    async def _auto_extract_skill_rules(self, skill_name: str):
        """Auto-extract rules from SKILL.md for a newly detected skill."""
        try:
            # Check if already extracted
            if event_store.has_skill_rules_extracted(skill_name):
                return

            # Get SKILL.md content
            content = skill_registry.get_skill_content(skill_name)
            if not content:
                print(f"[{self.agent_name}] No SKILL.md found for '{skill_name}', skipping rule extraction")
                return

            # Extract rules via LLM
            result = self._rule_extractor.extract_rules(skill_name, content)
            rules = result.get("rules", [])
            domain = result.get("domain", "general")

            if rules:
                stored = event_store.store_extracted_rules(skill_name, rules)
                print(f"[{self.agent_name}] Extracted {stored} rules from '{skill_name}' (domain={domain})")

            # Auto-tag skill domain
            if domain and domain != "general":
                event_store.add_skill_to_domain(skill_name, domain)
                print(f"[{self.agent_name}] Tagged '{skill_name}' as domain='{domain}'")

        except Exception as e:
            print(f"[{self.agent_name}] Rule extraction failed for '{skill_name}': {e}")

    # ── Query helpers ──────────────────────────────────────────────

    def get_sessions(self, limit: int = 50, user_id: str = None) -> List[Dict]:
        conn = event_store._conn()
        base_query = (
            "SELECT s.*, COALESCE(a.alert_count, 0) as alert_count "
            "FROM cc_sessions s "
            "LEFT JOIN (SELECT session_id, COUNT(*) as alert_count FROM cc_alerts GROUP BY session_id) a "
            "ON s.session_id = a.session_id"
        )
        if user_id:
            rows = conn.execute(
                f"{base_query} WHERE s.user_id = ? ORDER BY s.started_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"{base_query} ORDER BY s.started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session_detail(self, session_id: str) -> Optional[Dict]:
        session = event_store.get_session(session_id)
        if session is None:
            return None
        session["event_count"] = event_store.get_event_count(session_id)
        session["reports"] = event_store.get_reports(session_id)
        return session

    def get_session_events(self, session_id: str) -> List[Dict]:
        return event_store.get_session_events(session_id)

    def get_session_events_range(self, session_id: str, from_id: int, to_id: int) -> List[Dict]:
        return event_store.get_session_events_range(session_id, from_id, to_id)

    def get_dag_state(self, session_id: str) -> Optional[Dict]:
        mapper = self._node_mappers.get(session_id)
        if not mapper:
            return None
        return {
            "nodes": mapper.get_dag_state(),
            "active_node": mapper.get_active_node(),
            "progress": mapper.get_progress(),
        }

    def load_skill_for_session(self, session_id: str, parsed_skill: Dict) -> bool:
        try:
            nodes = self._signature_extractor.extract(parsed_skill)
            if nodes:
                self._node_mappers[session_id] = NodeMapper(nodes)
                return True
        except Exception as e:
            print(f"[{self.agent_name}] load_skill failed: {e}")
        return False

    def get_session_alerts(self, session_id: str) -> list:
        return event_store.get_session_alerts(session_id)

    def get_stats(self) -> Dict:
        return event_store.stats()
