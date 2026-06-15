# TAR Engine MCP Server

Expose TAR Engine's skill security audit as MCP tools and prompts. Any
MCP-compatible client — Claude Code, Claude Desktop, Cursor — can call the
audit pipeline directly from a conversation.

## What you get

Two surfaces ship in the same `tar-engine-mcp` package:

- **`tar-engine`** — a one-shot CLI that walks a directory, audits every
  skill it finds, and exits with a CI-friendly status code. Drop it into
  GitHub Actions or a pre-commit hook to fail builds with risky skills.
- **`tar-engine-mcp`** — the MCP server below, for interactive use from
  Claude Code / Cursor / Claude Desktop.

Both talk to the same backend (default `https://tarai.dev`, override with
`TAR_ENGINE_URL`).

### CLI quick start

```bash
# scan a directory of skills, fail the build if any score is below 70
tar-engine scan ./skills --min-score 70

# list discovered skills without running the audit
tar-engine list ./skills

# JSON output for downstream processing
tar-engine scan ./skills --json
```

Discovery covers five formats out of the box:

| File pattern                | Format                              |
|-----------------------------|-------------------------------------|
| `**/SKILL.md`               | OpenClaw, Claude Code, generic md   |
| `**/.claude/commands/*.md`  | Claude Code custom commands         |
| `**/skill.yaml` / `.yml`    | Codex                               |
| `**/manifest.json`          | Codex / Claude Code (key-detected)  |
| `**/opencode.json`          | OpenCode                            |

Each skill's audit payload includes its primary file plus sibling
`.sh / .py / .js / .ts / .yaml / .json` helper files in the same
directory tree (capped at 200 KB total). This catches the "SKILL.md
looks clean but `install.sh` does the dirty work" pattern.

### CI integration

**GitHub Actions** — fail the build if any skill scores below 70:

```yaml
- name: Audit skills
  run: |
    pipx install tar-engine-mcp
    tar-engine scan ./skills --min-score 70
```

**Pre-commit hook** (`.git/hooks/pre-commit`):

```bash
#!/usr/bin/env bash
tar-engine scan ./skills --min-score 80 || exit 1
```

The exit code is `0` on success, `1` when any skill is below the
threshold, and `2` for usage errors / missing path.

---

**MCP tools** (the LLM invokes these on its own when the user describes audit
intent):

- `audit_skill_text(skill_text, lang?, domain?)` — audit a SKILL.md you have
  in hand. Returns structured findings with rule_id, severity, line-level
  evidence, category, and remediation hints.
- `audit_skill_url(url, lang?, domain?)` — fetch a SKILL.md from a URL and
  audit it.
- `list_audit_rules(category?, lang?)` — list the rule registry the
  pipeline applies (PI-, SS-, FA-, DE-, CE-, MP-, SEM-, AR- prefixes).
- `get_audit_baseline(skill_name, limit?)` — historical audit records and
  trend for a named skill.

**Prompts** (slash commands users can trigger explicitly):

- `audit-skill` — audit a SKILL.md (paste text or URL)
- `audit-best-practices` — the three defensive constraints every SKILL.md
  should include before publishing
- `audit-trend` — show how a skill's audit score has changed over time

## Configure your client

### Claude Code

Add to `~/.config/claude-code/mcp.json`:

```json
{
  "mcpServers": {
    "tar-engine": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "tar-engine-oss",
        "python", "/app/mcp-server/tar_engine_mcp.py"
      ]
    }
  }
}
```

Restart Claude Code. Available commands:

- Type `/` to see `audit-skill`, `audit-best-practices`, `audit-trend`
- Or just say "audit my SKILL.md" and Claude will call the right tool

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "tar-engine": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "tar-engine-oss",
        "python", "/app/mcp-server/tar_engine_mcp.py"
      ]
    }
  }
}
```

### Cursor

Settings → MCP → Add Server → use the same `docker exec` command.

### Run directly (without docker)

If you run the engine locally without docker:

```bash
TAR_ENGINE_URL=http://localhost:8765 \
  python -m pip install mcp httpx
TAR_ENGINE_URL=http://localhost:8765 \
  python mcp-server/tar_engine_mcp.py
```

Then in your client config, point `command` at this script directly.

## Environment

The MCP server reads:

- `TAR_ENGINE_URL` (default `http://localhost:8765`) — base URL of the
  running engine
- `TAR_ENGINE_TIMEOUT` (default `180`) — per-request timeout in seconds.
  Full-stack audit (L1 + L3 + L4) routinely takes 30-60s; default leaves
  headroom.
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` — forwarded to the
  engine for the semantic (L3) and adversarial-fuzz (L4) layers. Without
  these the audit still runs L1 (regex) but skips L3/L4.

## Example user flows

**Audit a skill you're writing.** Open Claude Code in a project that has
a `SKILL.md`. Ask "audit this skill for security issues." Claude reads the
file, calls `audit_skill_text`, and walks you through findings with line
numbers and fix suggestions.

**Check a community skill before installing.** "Audit
github.com/someone/cool-skill SKILL.md." Claude calls `audit_skill_url`
with the raw URL, returns findings, you decide whether to install.

**See your audit trend.** After running audits on the same SKILL.md
multiple times, "show me the trend for my-skill." Claude calls
`get_audit_baseline` and reports mean/stddev/trend plus rule_ids that keep
recurring.

**Learn the defensive pattern.** Type `/audit-best-practices`. Get the
three-line SKILL.md prefix that closes the W23 edition's worst attack
class (encoded-instruction injection that succeeded against 5 of 5
Anthropic-official skills).

## Historical baseline persistence

Every audit triggered through the MCP server lands in the same
`cockpit_audit_history` table the web endpoint writes to. The baseline you
see via `get_audit_baseline` accumulates across all paths — MCP, CLI, web,
direct HTTP. Same skill, same identity, one trend.

Set `no_history: true` in the audit body when you want a one-off audit
that doesn't pollute the baseline (e.g. auditing a third-party skill you
don't own).
