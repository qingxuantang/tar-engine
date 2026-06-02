"""TAR Engine — Cockpit module.

Conversational wish machine. Takes natural-language goals, plans skill chains,
runs them inside the engine, audits each step, and writes a profile that gets
sharper over time.

At import time, scans `COCKPIT_PACKS_DIR` (default: /app/packs) and registers
every pack's declared skills into the planner's ALLOWED_SKILLS list.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

__version__ = "0.1.0"

logger = logging.getLogger("cockpit.bootstrap")


def _discover_packs() -> dict[str, list[str]]:
    """Scan packs directory, return {pack_name: [skill_names]}."""
    packs_dir = Path(os.getenv("COCKPIT_PACKS_DIR", "/app/packs"))
    discovered: dict[str, list[str]] = {}
    if not packs_dir.exists():
        return discovered
    for pack_dir in packs_dir.iterdir():
        if not pack_dir.is_dir():
            continue
        manifest = pack_dir / "pack.yaml"
        if not manifest.exists():
            continue
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("failed to parse %s: %s", manifest, e)
            continue
        skills = data.get("skills") or []
        names = [s.get("name") for s in skills if isinstance(s, dict) and s.get("name")]
        if names:
            discovered[data.get("name", pack_dir.name)] = names
    return discovered


def _register_pack_skills() -> None:
    """Call this once at import to populate ALLOWED_SKILLS from packs."""
    from .planner import register_skills

    packs = _discover_packs()
    if not packs:
        logger.info("no packs found; ALLOWED_SKILLS uses Hello World defaults only")
        return
    total = 0
    for pack_name, skill_names in packs.items():
        register_skills(skill_names)
        total += len(skill_names)
        logger.info("registered pack %s with %d skills: %s", pack_name, len(skill_names), skill_names)
    logger.info("pack discovery complete: %d packs, %d total skills registered", len(packs), total)


# Run pack discovery at import time
_register_pack_skills()
