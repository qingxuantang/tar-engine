# Cockpit Module

Wish machine core. Contains:

- `router.py` — FastAPI routes (`/api/cockpit/wish`, `/api/cockpit/wish/{id}/trace`)
- `wish_runner.py` — orchestrates plan → execute → trace → audit
- `planner.py` — LLM planner that maps wish → skill chain
- `dispatcher.py` — routes plan to engine-side or external IDE executor
- `skill_executor.py` — runs skills inside the engine container
- `trace_writer.py` — records execution trace (token, timing, args, outputs)
- `auditor_integration.py` — wires audit pipeline into runner
- `profile_writer.py` — updates user profile after each wish
- `store.py` — SQLite persistence (wishes / runs / trace / retrospectives)
- `models.py` — Pydantic models
- `llm_client.py` — OpenAI-compatible client (BYOK)
- `token_budget.py` — budget enforcement
- `telegram_bot.py` — optional Telegram entry
- `rag/` — Knowledge L3 RAG (LlamaIndex + ChromaDB)
- `prompts/` — planner system prompt + few-shot template

## Running locally

The cockpit module is mounted at `/api/cockpit/*` by `backend/app.py`. Start the
engine via docker compose (see repo root README) and submit a wish:

```bash
curl -X POST http://localhost:8765/api/cockpit/wish \
  -H "Content-Type: application/json" \
  -H "X-LLM-Api-Key: $OPENAI_API_KEY" \
  -d '{"wish": "echo hello", "pack": "hello-world"}'
```

Returns a `task_id`. Poll the trace endpoint:

```bash
curl http://localhost:8765/api/cockpit/wish/$TASK_ID/trace
```

## Architecture notes

- LLM keys are passed via `X-LLM-Api-Key` request header. The engine never persists
  them. Each request brings its own key.
- Skill execution is engine-side. The engine does NOT depend on an IDE plugin.
- Audit is wired into the runner as a post-skill hook (L1 + L2 always-on, L3 opt-in).
- The retrospective module reads the trace after run completion and writes lessons
  back to the user profile (opt-in).
