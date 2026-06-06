#!/usr/bin/env bash
#
# setup-mcp.sh — one-click installer for the TAR Engine MCP server.
#
# Registers the skill-audit MCP server with your local agent (Claude Code,
# Cursor, or Codex CLI) so you can audit SKILL.md files straight from chat.
#
# The script NEVER hardcodes a secret. Your OpenAI key is read from a flag,
# an environment variable, or an interactive (hidden) prompt, and is passed
# straight to your agent's MCP config — it is not written anywhere by this
# script and is never printed back to the terminal.
#
# Usage:
#   ./setup-mcp.sh                          # interactive: pick client, paste key
#   ./setup-mcp.sh --client claude          # Claude Code (default)
#   ./setup-mcp.sh --client cursor          # Cursor
#   ./setup-mcp.sh --client codex           # Codex CLI
#   ./setup-mcp.sh --key sk-proj-...        # supply BYOK key non-interactively
#   TAR_ENGINE_BYOK_OPENAI_KEY=sk-... ./setup-mcp.sh   # ...or via env var
#   ./setup-mcp.sh --no-byok                # static layer only, no LLM key
#   ./setup-mcp.sh --self-host http://localhost:8765   # don't use hosted tarai.dev
#
# Flags:
#   --client <claude|cursor|codex>   Target agent (default: claude)
#   --key <sk-...>                   BYOK OpenAI key (semantic + adversarial layers)
#   --model <name>                   LLM model (default: gpt-4o-mini)
#   --self-host <url>                Point at a self-hosted engine instead of tarai.dev
#   --no-byok                        Skip the key; only the free static layer runs
#   --version <tag>                  Pin a different release tag (default: v0.1.0)
#   -h, --help                       Show this help

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────
REPO="git+https://github.com/qingxuantang/tar-engine"
VERSION="v0.1.0"
CLIENT="claude"
MODEL="gpt-4o-mini"
SELF_HOST_URL=""
NO_BYOK=0
# Key may arrive via env var; a --key flag overrides it.
BYOK_KEY="${TAR_ENGINE_BYOK_OPENAI_KEY:-}"

# ── Pretty output ─────────────────────────────────────────────────────────
c_blue=$'\033[34m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'
c_red=$'\033[31m'; c_dim=$'\033[2m'; c_reset=$'\033[0m'
info()  { printf '%s==>%s %s\n' "$c_blue"  "$c_reset" "$*"; }
ok()    { printf '%s✓%s %s\n'   "$c_green" "$c_reset" "$*"; }
warn()  { printf '%s⚠%s %s\n'   "$c_yellow" "$c_reset" "$*" >&2; }
die()   { printf '%s✗%s %s\n'   "$c_red"   "$c_reset" "$*" >&2; exit 1; }

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ── Parse args ────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --client)    CLIENT="${2:-}"; shift 2 ;;
    --key)       BYOK_KEY="${2:-}"; shift 2 ;;
    --model)     MODEL="${2:-}"; shift 2 ;;
    --self-host) SELF_HOST_URL="${2:-}"; shift 2 ;;
    --version)   VERSION="${2:-}"; shift 2 ;;
    --no-byok)   NO_BYOK=1; shift ;;
    -h|--help)   usage ;;
    *)           die "unknown argument: $1  (try --help)" ;;
  esac
done

case "$CLIENT" in
  claude|cursor|codex) ;;
  *) die "unsupported --client '$CLIENT' (expected: claude, cursor, or codex)" ;;
esac

# ── Step 1: ensure uv / uvx is available ─────────────────────────────────
ensure_uv() {
  if command -v uvx >/dev/null 2>&1; then
    ok "uv present ($(uvx --version 2>/dev/null || echo uvx))"
    return
  fi
  warn "uvx not found. The MCP server runs via uvx (part of 'uv')."
  printf 'Install uv now via the official script? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES)
      info "Installing uv ..."
      curl -fsSL https://astral.sh/uv/install.sh | sh
      # uv installs to ~/.local/bin or ~/.cargo/bin — make it visible now.
      export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
      command -v uvx >/dev/null 2>&1 || die "uv installed but uvx still not on PATH. Open a new shell and re-run."
      ok "uv installed"
      ;;
    *)
      die "uv is required. Install it (https://docs.astral.sh/uv/) and re-run."
      ;;
  esac
}

# ── Step 2: resolve the BYOK key (flag > env > prompt) ───────────────────
resolve_key() {
  if [ "$NO_BYOK" -eq 1 ]; then
    BYOK_KEY=""
    warn "--no-byok set: only the free static rule layer will run (no SEM-/AR- findings)."
    return
  fi
  if [ -z "$BYOK_KEY" ]; then
    printf 'Paste your OpenAI BYOK key for semantic + adversarial layers\n'
    printf '%s(leave blank to run the free static layer only)%s\nkey: ' "$c_dim" "$c_reset"
    # -s hides the input so the key never echoes to the terminal/scrollback.
    read -rs BYOK_KEY || true
    printf '\n'
  fi
  if [ -z "$BYOK_KEY" ]; then
    warn "No key provided — installing with the static layer only."
  else
    # Show only a masked confirmation, never the full secret.
    ok "BYOK key captured (${BYOK_KEY:0:7}…, ${#BYOK_KEY} chars). Semantic + adversarial layers enabled."
  fi
}

# ── Step 3: register with the chosen client ──────────────────────────────
SPEC_ARGS=(--from "${REPO}@${VERSION}" tar-engine-mcp)

install_claude() {
  command -v claude >/dev/null 2>&1 || die "Claude Code CLI ('claude') not found on PATH."
  # Re-running should be idempotent: drop any prior registration first.
  claude mcp remove tar-engine >/dev/null 2>&1 || true

  local env_flags=()
  [ -n "$BYOK_KEY" ]      && env_flags+=(-e "TAR_ENGINE_BYOK_OPENAI_KEY=$BYOK_KEY")
  [ -n "$BYOK_KEY" ]      && env_flags+=(-e "TAR_ENGINE_BYOK_OPENAI_MODEL=$MODEL")
  [ -n "$SELF_HOST_URL" ] && env_flags+=(-e "TAR_ENGINE_URL=$SELF_HOST_URL")

  info "Registering 'tar-engine' with Claude Code ..."
  claude mcp add tar-engine "${env_flags[@]}" -- uvx "${SPEC_ARGS[@]}"
  ok "Registered. Run '/mcp list' (should show tar-engine Connected), then restart Claude Code."
}

install_cursor() {
  local cfg="$HOME/.cursor/mcp.json"
  mkdir -p "$HOME/.cursor"
  [ -f "$cfg" ] || echo '{}' > "$cfg"
  command -v python3 >/dev/null 2>&1 || die "python3 is required to safely merge $cfg"

  info "Merging 'tar-engine' into $cfg ..."
  # Pass values through the environment so the key never lands in argv / ps output.
  TE_KEY="$BYOK_KEY" TE_MODEL="$MODEL" TE_URL="$SELF_HOST_URL" \
  TE_REPO="${REPO}@${VERSION}" TE_CFG="$cfg" python3 - <<'PY'
import json, os
cfg = os.environ["TE_CFG"]
with open(cfg) as f:
    try:
        data = json.load(f)
    except Exception:
        data = {}
servers = data.setdefault("mcpServers", {})
env = {}
if os.environ.get("TE_KEY"):
    env["TAR_ENGINE_BYOK_OPENAI_KEY"] = os.environ["TE_KEY"]
    env["TAR_ENGINE_BYOK_OPENAI_MODEL"] = os.environ["TE_MODEL"]
if os.environ.get("TE_URL"):
    env["TAR_ENGINE_URL"] = os.environ["TE_URL"]
entry = {
    "command": "uvx",
    "args": ["--from", os.environ["TE_REPO"], "tar-engine-mcp"],
}
if env:
    entry["env"] = env
servers["tar-engine"] = entry
with open(cfg, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  ok "Wrote $cfg. Reload MCP servers in Cursor (or restart it), then call audit_skill_text."
}

install_codex() {
  local cfg="$HOME/.codex/config.toml"
  mkdir -p "$HOME/.codex"
  touch "$cfg"
  if grep -q '^\[mcp.servers.tar-engine\]' "$cfg" 2>/dev/null; then
    warn "An [mcp.servers.tar-engine] block already exists in $cfg — leaving it untouched."
    warn "Edit it by hand if you want to change the key/model."
    return
  fi
  info "Appending [mcp.servers.tar-engine] to $cfg ..."
  {
    printf '\n[mcp.servers.tar-engine]\n'
    printf 'command = "uvx"\n'
    printf 'args = ["--from", "%s", "tar-engine-mcp"]\n' "${REPO}@${VERSION}"
    if [ -n "$BYOK_KEY" ] || [ -n "$SELF_HOST_URL" ]; then
      printf 'env = {'
      sep=""
      if [ -n "$BYOK_KEY" ]; then
        printf '%s "TAR_ENGINE_BYOK_OPENAI_KEY" = "%s", "TAR_ENGINE_BYOK_OPENAI_MODEL" = "%s"' "$sep" "$BYOK_KEY" "$MODEL"
        sep=","
      fi
      [ -n "$SELF_HOST_URL" ] && printf '%s "TAR_ENGINE_URL" = "%s"' "$sep" "$SELF_HOST_URL"
      printf ' }\n'
    fi
  } >> "$cfg"
  ok "Updated $cfg. Restart the Codex CLI, then call audit_skill_text."
}

# ── Run ───────────────────────────────────────────────────────────────────
info "TAR Engine MCP setup — client: $CLIENT, version: $VERSION"
[ -n "$SELF_HOST_URL" ] && info "Backend: self-hosted ($SELF_HOST_URL)" \
                        || info "Backend: hosted tarai.dev (your SKILL.md is sent there to be audited)"

ensure_uv
resolve_key

case "$CLIENT" in
  claude) install_claude ;;
  cursor) install_cursor ;;
  codex)  install_codex ;;
esac

echo
ok "Done. Ask your agent:  \"Audit this SKILL.md: <paste a skill>\""
[ -n "$BYOK_KEY" ] && warn "Security: this key now lives in your agent's MCP config. Rotate it at OpenAI if it has been exposed."
