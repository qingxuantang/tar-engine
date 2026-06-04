# TAR Engine

> **OSS wish machine + skill executor + audit pipeline.** Speak a goal, get results.
> BYOK LLM. Curated domain packs sold separately.

[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

---

## Install in 30 seconds — paste this into your agent

TAR Engine ships an **MCP server**. The fastest install is to hand the block
below to your agent and let it do the work. No terminal commands for you —
copy, paste into a new conversation in Claude Code / Cursor / Codex / any
agent, and the agent will pull the Docker image, register the MCP server,
verify it works, and run a smoke audit on a sample `SKILL.md`.

<details open>
<summary><b>Claude Code</b></summary>

```text
Install the TAR Engine MCP server for me — it's an AI skill security and quality auditor. Once installed I want every SKILL.md I write to be auditable with a single tool call.

Project: https://github.com/qingxuantang/tar-engine
Type: stdio MCP via Docker container

Please do this in order:
1. Verify Docker is installed and running. If not, install Docker via my system package manager and start the daemon.
2. Start the engine container:
   docker run -d --name tar-engine -p 8642:8642 ghcr.io/qingxuantang/tar-engine:latest
3. Register the MCP server with Claude Code:
   claude mcp add tar-engine docker exec -i tar-engine python -m tar_engine_mcp
4. Verify with /mcp list — "tar-engine" should appear.
5. Smoke test: call the audit_skill_text tool on a sample SKILL.md and show me the verdict + findings.

If any step fails, diagnose: Docker daemon status, port 8642 availability, container health via `docker ps`, MCP registration via `claude mcp list`. Report back what worked and what didn't.
```

</details>

<details>
<summary><b>Cursor</b></summary>

```text
Install the TAR Engine MCP server in Cursor — it audits SKILL.md files for security and quality. After install I want to be able to call audit tools from inside Cursor on any skill I edit.

Project: https://github.com/qingxuantang/tar-engine
Type: stdio MCP via Docker container

Please do this in order:
1. Verify Docker is installed and running.
2. Start the engine container:
   docker run -d --name tar-engine -p 8642:8642 ghcr.io/qingxuantang/tar-engine:latest
3. Edit Cursor's MCP config (~/.cursor/mcp.json or project-level .cursor/mcp.json) to add:
   {
     "mcpServers": {
       "tar-engine": {
         "command": "docker",
         "args": ["exec", "-i", "tar-engine", "python", "-m", "tar_engine_mcp"]
       }
     }
   }
4. Reload MCP servers in Cursor (or restart the app).
5. Smoke test: invoke audit_skill_text on a sample SKILL.md and show me the result.

If anything fails, check Docker daemon, port 8642, and Cursor's MCP logs.
```

</details>

<details>
<summary><b>Codex CLI</b></summary>

```text
Install the TAR Engine MCP server for the Codex CLI agent — it audits SKILL.md files for security and quality.

Project: https://github.com/qingxuantang/tar-engine
Type: stdio MCP via Docker container

Please do this in order:
1. Verify Docker is installed and running.
2. Start the engine container:
   docker run -d --name tar-engine -p 8642:8642 ghcr.io/qingxuantang/tar-engine:latest
3. Add the MCP server to ~/.codex/config.toml:
   [mcp.servers.tar-engine]
   command = "docker"
   args = ["exec", "-i", "tar-engine", "python", "-m", "tar_engine_mcp"]
4. Restart the Codex CLI so it picks up the new config.
5. Smoke test: call audit_skill_text on a sample SKILL.md.

Report back if any step fails so I can diagnose Docker / port / config issues.
```

</details>

<details>
<summary><b>Any other agent</b></summary>

```text
Install the TAR Engine MCP server in this environment. It's an AI skill safety auditor exposed over the Model Context Protocol.

Project: https://github.com/qingxuantang/tar-engine
Type: stdio MCP via Docker container

Steps:
1. Verify Docker is installed and the daemon is running. Install Docker if missing.
2. Start the engine container:
   docker run -d --name tar-engine -p 8642:8642 ghcr.io/qingxuantang/tar-engine:latest
3. Register this MCP server with the current agent. Server spec:
   command: docker
   args: ["exec", "-i", "tar-engine", "python", "-m", "tar_engine_mcp"]
   Adapt to whatever MCP config format this agent uses.
4. Reload / restart the agent so the server is picked up.
5. Smoke test: call the audit_skill_text tool on a sample SKILL.md and surface the verdict to me.

Diagnose any failure: Docker daemon state, port 8642 availability, container health, MCP registration.
```

</details>

> **Prefer to run the commands yourself?** See the
> [5-minute quickstart](#5-minute-quickstart) below — same outcome, manual
> control. The agent-paste path above is the recommended install for most
> users in 2026.

> **Want a hosted version without Docker?** Hosted TAR Engine MCP is
> coming. Drop your email on the waitlist at
> [tarai.dev](https://tarai.dev/) (scroll to the Playground) and we'll
> tell you when it's live.

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
│   ├── hello-world/              # Reference demo pack — 5-min install verifier
│   └── postall-content/          # Multi-platform content publishing pack (requires postall-agent)
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

Read the latest reports on **[AI 米其林指南](https://tarai.dev/)** — the public guide of skill audits, with a live Playground you can paste any SKILL.md into.

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
- Audit content engine for skill ecosystem coverage (米其林指南 model)
- Quant Pack (first paid pack — domain-curated quant trading workflow)
- Premium Content Pack enhancements on top of the OSS PostAll pack

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
