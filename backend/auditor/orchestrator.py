"""Audit Orchestrator — coordinates audit agents on session end.

Registers as a session_end callback on CCAdapter. When a session
completes, it runs the audit pipeline:

1. DecisionChainAuditor → structured decision report
2. RiskScorer → risk scorecard with go/no-go

Future agents (CrossRunComparator, PostMortemAgent) will be added
here as they're implemented.
"""

import asyncio
import traceback
from typing import Dict, Optional

from event_store import event_store


class AuditOrchestrator:
    """Coordinates audit agents. Registered as session_end callback."""

    def __init__(self):
        self._running = set()  # Track in-flight audits

    async def on_session_end(self, session_id: str):
        """Main entry point — called by CCAdapter when session ends."""
        if session_id in self._running:
            print(f"[Orchestrator] Audit already running for {session_id}, skipping")
            return

        self._running.add(session_id)
        try:
            await self._run_audit_pipeline(session_id)
        finally:
            self._running.discard(session_id)

    async def _run_audit_pipeline(self, session_id: str):
        """Run the full audit pipeline for a session."""
        session = event_store.get_session(session_id)
        if not session:
            print(f"[Orchestrator] Session {session_id} not found")
            return

        print(f"[Orchestrator] Starting audit for session {session_id} "
              f"(skill={session.get('skill_name', '?')})")

        # Step 1: Decision Chain Audit
        decision_report = await self._run_decision_audit(session_id)

        # Step 2: Risk Scoring
        scorecard = await self._run_risk_scorer(session_id, decision_report)

        # Step 3: Risk Gene distillation
        gene = await self._run_risk_gene_distillation(
            session_id, decision_report, scorecard
        )

        # Step 4: (Future) Cross-run comparison if enough history
        # skill_name = session.get("skill_name", "")
        # if skill_name:
        #     history = event_store.get_history(skill_name, limit=3)
        #     if len(history) >= 3:
        #         await self._run_cross_run_comparison(session_id, skill_name)

        # Step 5: (Future) Post-mortem if risk is high
        # if scorecard and scorecard.get("score", 100) < 50:
        #     await self._run_post_mortem(session_id)

        score = scorecard.get("score", "?") if scorecard else "?"
        rec = scorecard.get("recommendation", "?") if scorecard else "?"
        gene_status = f", gene={'updated' if gene else 'skipped'}"
        print(f"[Orchestrator] Audit complete for {session_id}: "
              f"score={score}, recommendation={rec}{gene_status}")

    async def audit_skill_run(self, session_id: str, skill_run_id: int,
                              skill_name: str, from_id: int, to_id: int):
        """Run audit pipeline for a specific skill run."""
        run_key = f"run-{skill_run_id}"
        if run_key in self._running:
            print(f"[Orchestrator] Audit already running for run #{skill_run_id}, skipping")
            return

        self._running.add(run_key)
        try:
            print(f"[Orchestrator] Starting audit for run #{skill_run_id} ({skill_name})")

            # Step 1: Decision Chain on event range
            from auditor.decision_chain_auditor import audit_skill_run as audit_run_decisions
            loop = asyncio.get_event_loop()
            decision_report = await loop.run_in_executor(
                None, audit_run_decisions, session_id, skill_run_id, skill_name, from_id, to_id
            )

            # Step 2: Risk Scoring on event range
            from auditor.risk_scorer import score_skill_run
            scorecard = await loop.run_in_executor(
                None, score_skill_run, session_id, skill_run_id, from_id, to_id, decision_report
            )

            score = scorecard.get("score", "?") if scorecard else "?"
            print(f"[Orchestrator] Run #{skill_run_id} audit complete: score={score}")
        finally:
            self._running.discard(run_key)

    async def _run_decision_audit(
        self, session_id: str
    ) -> Optional[Dict]:
        """Run DecisionChainAuditor in a thread pool (it does blocking I/O)."""
        try:
            from auditor.decision_chain_auditor import audit_session
            # Run blocking LLM call in thread pool
            loop = asyncio.get_event_loop()
            report = await loop.run_in_executor(None, audit_session, session_id)
            return report
        except Exception as e:
            print(f"[Orchestrator] DecisionChainAuditor failed: {e}")
            traceback.print_exc()
            return None

    async def _run_risk_scorer(
        self, session_id: str, decision_report: Optional[Dict]
    ) -> Optional[Dict]:
        """Run RiskScorer."""
        try:
            from auditor.risk_scorer import score_session
            loop = asyncio.get_event_loop()
            scorecard = await loop.run_in_executor(
                None, score_session, session_id, decision_report
            )
            return scorecard
        except Exception as e:
            print(f"[Orchestrator] RiskScorer failed: {e}")
            traceback.print_exc()
            return None

    async def _run_risk_gene_distillation(
        self,
        session_id: str,
        decision_report: Optional[Dict],
        scorecard: Optional[Dict],
    ) -> Optional[Dict]:
        """Distill audit findings into a compact Risk Gene for the skill."""
        try:
            from auditor.risk_gene_distiller import distill_risk_gene
            loop = asyncio.get_event_loop()
            gene = await loop.run_in_executor(
                None, distill_risk_gene, session_id,
                decision_report, scorecard
            )
            return gene
        except Exception as e:
            print(f"[Orchestrator] RiskGeneDistiller failed: {e}")
            traceback.print_exc()
            return None


# Singleton
audit_orchestrator = AuditOrchestrator()
