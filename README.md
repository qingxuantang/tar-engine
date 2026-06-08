# TAR Engine

> **OSS wish machine + skill executor + audit pipeline.** Speak a goal, get results.
> BYOK LLM. Curated domain packs sold separately.

[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

---

## Install

TAR Engine ships an **MCP server** as a Python package, runnable with
[`uvx`](https://docs.astral.sh/uv/) — no Docker. By default it talks to
the hosted backend at **[tarai.dev](https://tarai.dev/)** (free,
rate-limited).

### Read this first — what you're trusting

- **Where SKILL.md goes.** With the default config the MCP server POSTs
  the SKILL.md content you ask it to audit to `https://tarai.dev`. We
  don't write skill text to disk or log it, but it does leave your
  machine. If you're auditing proprietary or sensitive skills,
  [self-host](#self-host) and set `TAR_ENGINE_URL=http://localhost:8765`.
- **No silent key forwarding.** The server does NOT forward your
  `OPENAI_API_KEY`. Semantic + adversarial audit layers require an
  explicit opt-in via `TAR_ENGINE_BYOK_OPENAI_KEY` in the MCP server
  config — see [BYOK](#byok-semantic--adversarial-layers) below.
- **What environments work.** Claude Code CLI, Cursor, Codex CLI —
  anywhere your agent can launch a subprocess. **Claude Desktop,
  Claude.ai web, and the mobile apps cannot install local MCP servers**;
  hosted endpoint for those is coming — [waitlist on tarai.dev](https://tarai.dev/).

### Step 0 — install `uv` (one-time, ~5 seconds)

The package is run via `uvx`, which comes with `uv`. Install once:

```bash
# macOS / Linux
curl -fsSL https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex

# Alternative — via pipx if you don't trust curl|sh
pipx install uv

# Alternative — via pip
pip install --user uv
```

Verify with `uvx --version`.

### Step 1 — register with your agent

**One-click:** grab [`setup-mcp.sh`](setup-mcp.sh) and run it — it checks
for `uv`, prompts for an optional BYOK key (hidden input, never written to
disk by the script), and registers the server with your agent:

```bash
curl -fsSL https://raw.githubusercontent.com/qingxuantang/tar-engine/master/setup-mcp.sh -o setup-mcp.sh
chmod +x setup-mcp.sh
./setup-mcp.sh                       # Claude Code (default); add --client cursor|codex
```

Or configure it manually below. These pin to the **`v0.1.2`** release tag,
so every launch installs the same intentionally-cut version. Swap `@v0.1.2`
for `@master` if you'd rather track the latest unreleased work, or for a
different tag once we cut one.

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add tar-engine -- uvx --from "git+https://github.com/qingxuantang/tar-engine@v0.1.2" tar-engine-mcp
```

Verify: `/mcp list` should show `tar-engine` Connected. Restart Claude
Code so this session picks up the new tool surface, then ask:

> Audit this SKILL.md: [paste a skill]

</details>

<details>
<summary><b>Cursor</b></summary>

Edit `~/.cursor/mcp.json` (or project-level `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "tar-engine": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/qingxuantang/tar-engine@v0.1.2", "tar-engine-mcp"]
    }
  }
}
```

Reload MCP servers in Cursor (or restart the app), then call
`audit_skill_text` from inside Cursor.

</details>

<details>
<summary><b>Codex CLI</b></summary>

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.tar-engine]
command = "uvx"
args = ["--from", "git+https://github.com/qingxuantang/tar-engine@v0.1.2", "tar-engine-mcp"]
```

Restart the Codex CLI, then call `audit_skill_text`.

</details>

<details>
<summary><b>Any other MCP-compatible agent</b></summary>

Most agents accept an MCP server spec with `command` + `args` (JSON or
TOML). Add:

- **command:** `uvx`
- **args:** `["--from", "git+https://github.com/qingxuantang/tar-engine@v0.1.2", "tar-engine-mcp"]`
- **env (optional):**
  - `TAR_ENGINE_URL=http://localhost:8765` to self-host
  - `TAR_ENGINE_BYOK_OPENAI_KEY=sk-...` to enable semantic + adversarial layers

Reload the agent and call `audit_skill_text` to verify.

</details>

### BYOK (semantic + adversarial layers)

By default only the **static rule layer** runs against your skill —
free, deterministic, no LLM cost. To enable the semantic LLM review and
the adversarial prompt-fuzz pass, supply your own LLM key explicitly:

```json
"tar-engine": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/qingxuantang/tar-engine@v0.1.2", "tar-engine-mcp"],
  "env": {
    "TAR_ENGINE_BYOK_OPENAI_KEY": "sk-..."
  }
}
```

We deliberately do **not** read `OPENAI_API_KEY` from your general
environment. Most Claude Code / Cursor / OpenAI SDK users have that key
set for unrelated purposes — silent relay would be wrong. Set
`TAR_ENGINE_BYOK_OPENAI_KEY` only when you want this MCP server to use
your key.

### Self-host

If the privacy / latency tradeoff of the hosted backend doesn't work
for you, run the engine locally:

```bash
git clone https://github.com/qingxuantang/tar-engine
cd tar-engine
cp .env.example .env  # add OPENAI_API_KEY if you want semantic + adversarial
docker compose up -d
```

Then point the MCP server at it:

```json
"env": {
  "TAR_ENGINE_URL": "http://localhost:8765"
}
```

Same tool surface, no data leaves your machine, your own OpenAI key, no
rate limit beyond what your hardware supports.

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
