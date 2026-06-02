# TAR Engine

> **OSS wish machine + skill executor + audit pipeline.** Speak a goal, get results.
> BYOK LLM. Curated domain packs sold separately.

[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

---

## What it is

TAR Engine is an OSS **wish machine** for AI agents.

You install it on your laptop or server, point it at your LLM key (BYOK), and
talk to it in plain English: *"Find the best parameters for strategy A."* /
*"Summarize this URL and post a draft to LinkedIn."*

The engine:

1. **Plans** — decomposes the wish into a skill chain
2. **Executes** — runs the skills (engine-side, no IDE plugin needed)
3. **Traces** — records every step (tokens, timing, args, outputs)
4. **Audits** — scans each skill action against a configurable rule set
5. **Reflects** — opt-in retrospective LLM pass that distills lessons into your profile

This is the OSS core. **Curated domain packs** — turnkey skill chains for specific
verticals like quant trading or content publishing — are sold separately and ship
with their own planner templates, domain knowledge, and skill bundles.

---

## 5-minute quickstart

```bash
# 1. Clone
git clone https://github.com/qingxuantang/tar-engine.git
cd tar-engine

# 2. Configure
cp .env.example .env
# Edit .env and set OPENAI_API_KEY

# 3. Run
docker compose up -d

# 4. Verify
curl http://localhost:8765/healthz

# 5. Submit your first wish (uses Hello World Pack)
curl -X POST http://localhost:8765/api/cockpit/wish \
  -H "Content-Type: application/json" \
  -H "X-LLM-Api-Key: $OPENAI_API_KEY" \
  -d '{"wish": "echo hello world", "pack": "hello-world"}'

# Response:
# { "task_id": "tsk_...", "status": "queued" }

# 6. Fetch the result
curl http://localhost:8765/api/cockpit/wish/tsk_...
```

---

## Why TAR Engine

Three concrete design choices set TAR Engine apart from generic skill marketplaces:

- **Engine-side execution.** Skills run inside the engine container, not in your
  IDE. Your IDE doesn't need a plugin. CI / cron / Telegram can all trigger wishes.

- **Built-in audit pipeline.** L1 static rules + L2 capability bitmap + L3 dynamic
  LLM review. Every skill action gets scored. Your CFO / compliance officer can
  trust the trace.

- **Retrospective + profile.** Wishes don't just run; they *teach* the engine. The
  retrospective extracts lessons from each run, stores them on your profile, and
  injects them into the next plan. The engine gets sharper the more you use it.

---

## What's in this repo

```
tar-engine/
├── backend/
│   ├── app.py                    # FastAPI entry
│   ├── cockpit/                  # Wish machine: planner / dispatcher / skill executor / trace / retrospective
│   ├── auditor/                  # Audit pipeline: L1 / L2 / L3 + risk scorer + orchestrator
│   ├── adapters/                 # IDE/runtime adapters (Claude Code, Codex CLI, generic webhook)
│   └── knowledge/                # Knowledge L3 RAG (LlamaIndex + ChromaDB)
├── packs/
│   └── hello-world/              # Reference demo pack — 5-min install verifier
├── frontend/                     # Web UI (static, optional)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── docs/                         # Architecture, deployment, contribution notes
```

---

## Curated domain packs (paid, not in this repo)

The OSS engine is enough to author your own packs. If you want a turnkey domain
solution, we ship curated packs as paid add-ons:

- **Quant Trading Pack** — strategy research, factor mining, backtest audit, live signal monitoring (ships first)
- **Content Publishing Pack** — multi-platform content generation with built-in QA gate and cross-platform consistency audit (ships second)

Packs come with curated skill chains, domain-tuned planner few-shot templates,
domain knowledge corpora, profile schemas, and the audit dashboard pre-wired.

Visit [tarai.dev](https://tarai.dev) for pack details and pricing.

---

## Audit reports (米其林指南)

We publish weekly audit reports of popular open-source skills from major skill
platforms — Smithery, Claude Hub, MCPHub. Each report includes a security score,
specific findings, and remediation suggestions. The audit pipeline used to generate
these is the same one shipped in this OSS engine. Try it on your own skills:

```bash
curl -X POST http://localhost:8765/api/cockpit/audit/static \
  -H "Content-Type: application/json" \
  -d '{"skill_text": "<your SKILL.md content>"}'
```

Read the latest reports on [tarai.dev/audits](https://tarai.dev/audits).

---

## Status

This is the **0.1.0 minimum public release**. Core wish machine + audit pipeline
+ trace + retrospective + hello-world pack all work. The Web UI is functional but
spartan. Contributor docs and CI are not yet polished.

What works today:

- ✅ Wish → planner → skill chain → execution
- ✅ Execution trace with token + timing instrumentation
- ✅ Audit pipeline (L1 static rules + L2 capability bitmap)
- ✅ Hello World Pack runs end-to-end via curl
- ✅ Docker Compose deployment

What's coming (next 4-12 weeks):

- AI retrospective with lesson distillation + profile injection
- Web UI polish + run timeline panel
- Telegram bot entry
- L3 dynamic LLM audit
- Quant Pack (first paid pack)
- Content Publishing Pack (second paid pack)

---

## Contributing

This is early days. The fastest way to help is:

1. Try the 5-minute quickstart and report friction
2. Author your own pack and link it in Discussions
3. Run the audit pipeline on your favorite skill and share findings

PR contributions to `backend/cockpit/`, `backend/auditor/`, and `packs/hello-world/`
are welcome. The paid packs (quant trading, content publishing) are first-party
only — we won't accept PRs that add UGC packs to the `packs/` directory.

---

## License

Apache 2.0 — see [LICENSE](LICENSE) for the full terms.

---

## Acknowledgments

TAR Engine started as an audit tool for AI quant trading workflows. It grew into
a general-purpose wish machine through real conversations with quant engineers,
content creators, and security teams who all asked variations of the same
question: *"How do I make my AI agents do real work without losing my job to a
hallucinated trade?"*

Built by [Mark Zhou](https://tarai.dev) with [Claude Code](https://claude.com/code).
