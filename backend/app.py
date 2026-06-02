"""TAR Engine OSS — FastAPI application.

Cockpit-only entry point. Wires up:
- POST /api/cockpit/wish               — submit a wish, returns task_id
- GET  /api/cockpit/wish/{id}          — fetch wish status
- GET  /api/cockpit/wishes             — list recent wishes
- GET  /api/cockpit/profile/{user_id}  — fetch user profile
- GET  /api/cockpit/health             — cockpit health check
- GET  /healthz                        — overall health

Skill execution is engine-side (no IDE plugin needed).
LLM calls are BYOK — keys come from request headers, never stored.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Ensure backend/ is on sys.path so internal modules can use top-level imports
# (e.g. `from event_store import ...`, `from auditor import ...`).
HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# cockpit.router must be importable now that sys.path has backend/
from cockpit.router import router as cockpit_router

app = FastAPI(
    title="TAR Engine OSS",
    description="OSS wish machine + skill executor + audit pipeline. BYOK.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cockpit_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": "0.1.0"}


# Serve static frontend if present (repo root / frontend/)
FRONTEND_DIR = HERE.parent / "frontend"
if FRONTEND_DIR.exists() and any(FRONTEND_DIR.iterdir()):
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port)
