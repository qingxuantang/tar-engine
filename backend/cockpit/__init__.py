"""TAR Engine — Cockpit module.

Conversational wish machine. Takes natural-language goals, plans skill chains,
runs them inside the engine, audits each step, and writes a profile that gets
sharper over time.

Architecture:
  - Planner: LLM that maps wish → skill chain (BYOK)
  - Dispatcher: routes plan to engine-side executor
  - Skill executor: runs skills in the engine container
  - Trace writer: records every step with token + timing instrumentation
  - Auditor integration: wires audit pipeline into the runner
  - Profile writer: updates user profile after each wish

See repo root README.md for usage.
"""

__version__ = "0.1.0"
