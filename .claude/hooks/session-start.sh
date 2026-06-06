#!/bin/bash
# TAR Engine — SessionStart hook for Claude Code on the web.
#
# Installs Python dependencies and launches the local audit engine on :8765 so
# that the project's tar-engine MCP server (see ../.mcp.json) and the test suite
# work out of the box in a fresh web session. Local development is unaffected.
set -euo pipefail

# Web sessions only — skip on local machines.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"

VENV=".venv"
PYBIN="$VENV/bin/python"

# 1. Virtualenv + dependencies. Idempotent; container state is cached after the
#    first run, so re-runs are fast.
if [ ! -x "$PYBIN" ]; then
  python3 -m venv "$VENV"
fi
"$PYBIN" -m pip install --quiet --upgrade pip
"$PYBIN" -m pip install --quiet \
  "fastapi>=0.100" "uvicorn[standard]>=0.22" "websockets>=11" pyyaml \
  "python-multipart>=0.0.6" "httpx>=0.25" "mcp>=1.0" pytest

# The test suite imports top-level packages from backend/ (e.g. `from cockpit...`).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export PYTHONPATH="$CLAUDE_PROJECT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}"' >> "$CLAUDE_ENV_FILE"
fi

# 2. Launch the audit engine on :8765 if it is not already running.
#    OPENAI_API_KEY — configure it as an environment secret to enable the L3
#    semantic + L4 adversarial layers. Without it the engine still runs L1
#    (regex) audits; the secret is inherited here, never committed.
if ! curl -fsS http://localhost:8765/healthz >/dev/null 2>&1; then
  PORT=8765 LOG_LEVEL=INFO ENABLE_RAG=false \
    nohup "$PYBIN" backend/app.py > /tmp/tar-engine.log 2>&1 &
  for _ in $(seq 1 30); do
    curl -fsS http://localhost:8765/healthz >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo "tar-engine: deps ready; engine -> $(curl -fsS http://localhost:8765/healthz 2>/dev/null || echo 'not running')"
