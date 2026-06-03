"""Skill source intake — pulls SKILL.md candidates from various platforms.

Sources implemented (v0):
  - lenny-skills: clones github.com/RefoundAI/lenny-skills, walks SKILL.md files
  - anthropic-skills: fetches via GitHub API the anthropics/skills repo
  - github-direct: takes a list of GitHub repo paths and pulls their SKILL.md

Each source is a class with a `harvest()` method that returns
SkillCandidate objects. Candidates go into the candidates SQLite where
batch_audit.py picks them up.

Categorization:
  - Each source maps its raw platform category → our internal subcategory
    via config/taxonomy.yaml `platform_category_mapping`
  - If the source has no native category, we apply the name_inference_rules
    regex list as fallback
  - Subcategory → top-level is derived by walking the taxonomy tree

Mark wants daily 30-50 audits across categories. The candidates table has
last_audited_at so batch_audit can pick "oldest-audited per category" to
keep coverage balanced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml


HERE = Path(__file__).parent
TAXONOMY_PATH = HERE / "config" / "taxonomy.yaml"
DEFAULT_DB_PATH = Path(os.environ.get("MICHELIN_CANDIDATES_DB",
                                       str(HERE / "candidates.db")))


# ── Taxonomy helpers ────────────────────────────────────────────────────


def _load_taxonomy() -> dict:
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


def _build_sub_to_top(taxonomy: dict) -> dict[str, str]:
    """Map subcategory id → top-level id."""
    out: dict[str, str] = {}
    for top in taxonomy.get("taxonomy", []):
        for sub in top.get("subcategories", []):
            out[sub["id"]] = top["id"]
    return out


def _build_alias_to_sub(taxonomy: dict) -> dict[str, str]:
    """Map any alias string (lowercased) → subcategory id.

    Lets `name_inference_rules` find subcategories by user-friendly names
    too, not only by regex.
    """
    out: dict[str, str] = {}
    for top in taxonomy.get("taxonomy", []):
        for sub in top.get("subcategories", []):
            sub_id = sub["id"]
            out[sub_id.lower()] = sub_id
            for alias in (sub.get("aliases") or []):
                out[alias.lower()] = sub_id
    return out


def categorize(*, platform: str, raw_category: Optional[str],
               skill_name: str, description: str = "",
               taxonomy: dict) -> tuple[Optional[str], Optional[str]]:
    """Resolve a (top-level, subcategory) pair for a candidate.

    Order:
      1. Platform-specific raw_category mapping
      2. Name + description regex inference rules (concatenated)
      3. None — caller may default
    """
    mapping = (taxonomy.get("platform_category_mapping") or {}).get(platform, {})
    sub_to_top = _build_sub_to_top(taxonomy)

    if raw_category and raw_category in mapping:
        sub = mapping[raw_category]
        return (sub_to_top.get(sub), sub)
    if raw_category and mapping.get("_default"):
        sub = mapping["_default"]
        return (sub_to_top.get(sub), sub)

    # Name + description inference fallback
    haystack = f"{skill_name} {description}"
    for rule in (taxonomy.get("name_inference_rules") or []):
        if re.search(rule["pattern"], haystack):
            sub = rule["subcategory"]
            return (sub_to_top.get(sub), sub)

    return (None, None)


# ── Candidate dataclass + sqlite store ───────────────────────────────────


@dataclass
class SkillCandidate:
    candidate_id: str
    source_platform: str       # "lenny-skills" / "anthropic-skills" / "github-direct"
    source_url: str
    skill_name: str
    description: str = ""
    raw_category: Optional[str] = None
    top_level: Optional[str] = None
    subcategory: Optional[str] = None
    skill_md_path: Optional[str] = None   # local path to cached SKILL.md
    audit_count: int = 0
    last_audited_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


CANDIDATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_candidates (
    candidate_id      TEXT PRIMARY KEY,
    source_platform   TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    skill_name        TEXT NOT NULL,
    description       TEXT,
    raw_category      TEXT,
    top_level         TEXT,
    subcategory       TEXT,
    skill_md_path     TEXT,
    audit_count       INTEGER DEFAULT 0,
    last_audited_at   TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cand_source ON skill_candidates (source_platform);
CREATE INDEX IF NOT EXISTS idx_cand_top ON skill_candidates (top_level);
CREATE INDEX IF NOT EXISTS idx_cand_audited ON skill_candidates (last_audited_at);
"""


def _db(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(CANDIDATES_SCHEMA)
    return conn


def upsert_candidate(c: SkillCandidate, conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO skill_candidates
           (candidate_id, source_platform, source_url, skill_name, description,
            raw_category, top_level, subcategory, skill_md_path, audit_count,
            last_audited_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                   COALESCE((SELECT audit_count FROM skill_candidates WHERE candidate_id = ?), 0),
                   (SELECT last_audited_at FROM skill_candidates WHERE candidate_id = ?),
                   ?)""",
        (
            c.candidate_id, c.source_platform, c.source_url, c.skill_name,
            c.description, c.raw_category, c.top_level, c.subcategory,
            c.skill_md_path, c.candidate_id, c.candidate_id, c.created_at,
        ),
    )
    conn.commit()


def make_candidate_id(*, platform: str, identifier: str) -> str:
    return hashlib.sha256(f"{platform}::{identifier}".encode("utf-8")).hexdigest()[:16]


def parse_frontmatter(text: str) -> dict[str, str]:
    """Best-effort YAML frontmatter parser (returns flat strings only)."""
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.MULTILINE | re.DOTALL)
    if not m:
        return {}
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if kv:
            val = kv.group(2)
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            fm[kv.group(1)] = val
    return fm


# ── Source 1: Lenny Skills (GitHub clone) ────────────────────────────────


class LennySkillsSource:
    """Walks github.com/RefoundAI/lenny-skills repo for SKILL.md files."""

    REPO_URL = "https://github.com/RefoundAI/lenny-skills.git"
    PLATFORM_ID = "lenny-skills"

    def __init__(self, cache_dir: Path, taxonomy: dict):
        self.cache_dir = Path(cache_dir) / "lenny-skills"
        self.taxonomy = taxonomy

    def harvest(self) -> Iterable[SkillCandidate]:
        if not self.cache_dir.exists():
            self.cache_dir.parent.mkdir(parents=True, exist_ok=True)
            print(f"  cloning {self.REPO_URL} → {self.cache_dir}", file=sys.stderr)
            subprocess.run(
                ["git", "clone", "--depth", "1", self.REPO_URL, str(self.cache_dir)],
                check=True, capture_output=True,
            )
        else:
            print(f"  refreshing {self.cache_dir}", file=sys.stderr)
            subprocess.run(["git", "-C", str(self.cache_dir), "pull", "--ff-only"],
                            capture_output=True)

        # Lenny's repo organizes skills as <category>/<skill-name>/SKILL.md
        # The directory name above the SKILL.md is the category in his
        # taxonomy (Product Management, Leadership, etc.).
        for skill_md in self.cache_dir.rglob("SKILL.md"):
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception as e:
                print(f"  read failed {skill_md}: {e}", file=sys.stderr)
                continue
            fm = parse_frontmatter(text)
            skill_name = fm.get("name", skill_md.parent.name)
            description = fm.get("description", "")

            # Lenny's category is the immediate parent directory of the
            # skill folder (so SKILL.md is at category/skill-name/SKILL.md).
            # The category folder name uses the human-readable label.
            try:
                category_dir = skill_md.parent.parent
                raw_category = self._normalize_lenny_category(category_dir.name)
            except Exception:
                raw_category = None

            top, sub = categorize(
                platform=self.PLATFORM_ID,
                raw_category=raw_category,
                skill_name=skill_name,
                description=description,
                taxonomy=self.taxonomy,
            )

            rel_path = str(skill_md.relative_to(self.cache_dir))
            source_url = f"https://github.com/RefoundAI/lenny-skills/blob/main/{rel_path}"

            yield SkillCandidate(
                candidate_id=make_candidate_id(
                    platform=self.PLATFORM_ID, identifier=rel_path),
                source_platform=self.PLATFORM_ID,
                source_url=source_url,
                skill_name=skill_name,
                description=description,
                raw_category=raw_category,
                top_level=top,
                subcategory=sub,
                skill_md_path=str(skill_md),
            )

    @staticmethod
    def _normalize_lenny_category(folder_name: str) -> str:
        # Repo folders may use kebab-case or with-spaces; map back to the
        # human label used in the taxonomy mapping.
        canonical = {
            "product-management": "Product Management",
            "hiring-and-teams": "Hiring & Teams",
            "hiring-teams": "Hiring & Teams",
            "leadership": "Leadership",
            "ai-and-technology": "AI & Technology",
            "ai-technology": "AI & Technology",
            "communication": "Communication",
            "growth": "Growth",
            "marketing": "Marketing",
            "career": "Career",
            "sales-and-gtm": "Sales & GTM",
            "sales-gtm": "Sales & GTM",
            "engineering": "Engineering",
            "design": "Design",
        }
        return canonical.get(folder_name.lower(), folder_name.replace("-", " ").title())


# ── Source 2: Anthropic official skills (GitHub API) ─────────────────────


class AnthropicSkillsSource:
    """Fetches SKILL.md from anthropics/skills via the GitHub raw URLs."""

    REPO_OWNER = "anthropics"
    REPO_NAME = "skills"
    PLATFORM_ID = "anthropic-skills"

    def __init__(self, cache_dir: Path, taxonomy: dict):
        self.cache_dir = Path(cache_dir) / "anthropic-skills"
        self.taxonomy = taxonomy

    def harvest(self) -> Iterable[SkillCandidate]:
        # Enumerate via GitHub tree API
        api_url = (f"https://api.github.com/repos/{self.REPO_OWNER}/"
                   f"{self.REPO_NAME}/git/trees/main?recursive=1")
        req = urllib.request.Request(api_url, headers={
            "User-Agent": "tar-engine-skill-source/0.1",
            "Accept": "application/vnd.github.v3+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                tree_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  GitHub API failed: {e}", file=sys.stderr)
            return
        skill_paths = [t["path"] for t in tree_data.get("tree", [])
                       if t["path"].endswith("SKILL.md")]

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for path in skill_paths:
            raw_url = (f"https://raw.githubusercontent.com/{self.REPO_OWNER}/"
                       f"{self.REPO_NAME}/main/{path}")
            cache_path = self.cache_dir / path.replace("/", "__")
            if not cache_path.exists():
                try:
                    with urllib.request.urlopen(raw_url, timeout=10) as resp:
                        cache_path.write_bytes(resp.read())
                except Exception as e:
                    print(f"  fetch failed {raw_url}: {e}", file=sys.stderr)
                    continue
            text = cache_path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            skill_name = fm.get("name") or path.split("/")[-2]
            description = fm.get("description", "")

            # Anthropic doesn't categorize officially — let name inference do it.
            top, sub = categorize(
                platform=self.PLATFORM_ID,
                raw_category=None,
                skill_name=skill_name,
                taxonomy=self.taxonomy,
            )

            yield SkillCandidate(
                candidate_id=make_candidate_id(
                    platform=self.PLATFORM_ID, identifier=path),
                source_platform=self.PLATFORM_ID,
                source_url=raw_url,
                skill_name=skill_name,
                description=description,
                raw_category=None,
                top_level=top,
                subcategory=sub,
                skill_md_path=str(cache_path),
            )


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default=str(HERE / ".source-cache"),
                        help="Where to store source clones / fetched SKILL.md")
    parser.add_argument("--source",
                        choices=["lenny", "anthropic", "all"],
                        default="all",
                        help="Which source to harvest (default: all)")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--show-categories", action="store_true",
                        help="After ingest, print a per-category count summary")
    args = parser.parse_args()

    taxonomy = _load_taxonomy()
    cache_dir = Path(args.cache_dir)
    db_path = Path(args.db_path)
    conn = _db(db_path)

    sources = []
    if args.source in ("lenny", "all"):
        sources.append(LennySkillsSource(cache_dir, taxonomy))
    if args.source in ("anthropic", "all"):
        sources.append(AnthropicSkillsSource(cache_dir, taxonomy))

    total = 0
    per_top: dict[str, int] = {}
    per_sub: dict[str, int] = {}
    unmapped = 0
    for src in sources:
        print(f"Harvesting from {src.PLATFORM_ID}...", file=sys.stderr)
        for c in src.harvest():
            upsert_candidate(c, conn)
            total += 1
            if c.top_level:
                per_top[c.top_level] = per_top.get(c.top_level, 0) + 1
            else:
                unmapped += 1
            if c.subcategory:
                per_sub[c.subcategory] = per_sub.get(c.subcategory, 0) + 1
        print(f"  done.", file=sys.stderr)

    print(f"\nTotal candidates: {total} (unmapped categories: {unmapped})",
          file=sys.stderr)
    if args.show_categories:
        print("\nPer top-level distribution:", file=sys.stderr)
        for top, ct in sorted(per_top.items(), key=lambda x: -x[1]):
            print(f"  {top}: {ct}", file=sys.stderr)
        print("\nPer subcategory distribution:", file=sys.stderr)
        for sub, ct in sorted(per_sub.items(), key=lambda x: -x[1]):
            print(f"  {sub}: {ct}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
