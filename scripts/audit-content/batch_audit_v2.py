"""Batch-audit candidates from the SQLite intake DB.

Picks up where skill_source.py leaves off. Pulls candidates from the
candidates DB (oldest-audited first per category, for balanced coverage),
runs each through the audit endpoint, writes per-skill markdown reports
into the per-language output trees, and updates last_audited_at.

Run after skill_source.py:
    python3 skill_source.py --source all
    python3 batch_audit_v2.py --limit 30 --lang en --output /tmp/michelin-v2-en/

Per-language output tree:
    <output>/
      <skill-name>.md            ← per-skill audit report
      <skill-name>.meta.json     ← candidate metadata (category, source URL)

build_site_v2.py reads both files to render per-skill + category pages.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from audit_skill import (  # noqa: E402
    parse_frontmatter,
    extract_skill_body,
    summarize_skill_heuristic,
    summarize_skill_llm,
    call_audit_endpoint,
    format_report_markdown,
    t,
    DEFAULT_ENGINE_URL,
    DEFAULT_LANG,
    SUPPORTED_LANGS,
)


def fetch_pending_candidates(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Pull `limit` candidates ordered by last_audited_at NULLS FIRST so
    untouched-or-stalest go first. Balances coverage across categories.
    """
    rows = conn.execute(
        """SELECT * FROM skill_candidates
           ORDER BY CASE WHEN last_audited_at IS NULL THEN 0 ELSE 1 END,
                    last_audited_at ASC,
                    candidate_id ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_audited(conn: sqlite3.Connection, candidate_id: str) -> None:
    conn.execute(
        """UPDATE skill_candidates
           SET audit_count = audit_count + 1,
               last_audited_at = ?
           WHERE candidate_id = ?""",
        (datetime.now(timezone.utc).isoformat(), candidate_id),
    )
    conn.commit()


def audit_one(*, cand: dict, engine_url: str, out_dir: Path,
              lang: str, use_llm_summary: bool,
              openai_api_key: str | None, openai_base_url: str | None,
              openai_model: str | None) -> dict | None:
    """Run audit on one candidate. Returns leaderboard-style dict or None."""
    skill_md_path = cand.get("skill_md_path")
    if not skill_md_path or not Path(skill_md_path).exists():
        print(f"  ✗ skill_md missing for {cand['skill_name']}", file=sys.stderr)
        return None
    skill_text = Path(skill_md_path).read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(skill_text)
    skill_name = (frontmatter.get("name") or cand["skill_name"]).strip()

    result = call_audit_endpoint(engine_url, skill_text, domain="general", lang=lang)
    if not result.get("success", True) and "error" in result:
        print(f"  ✗ audit failed for {skill_name}: {result['error']}", file=sys.stderr)
        return None

    body = extract_skill_body(skill_text)
    body_metrics = {"body_lines": body.count("\n"), "body_chars": len(body)}

    llm_summary = None
    if use_llm_summary:
        llm_summary = summarize_skill_llm(
            skill_text=skill_text, frontmatter=frontmatter,
            api_key=openai_api_key, base_url=openai_base_url, model=openai_model,
            lang=lang,
        )
    heuristic = summarize_skill_heuristic(skill_text, frontmatter, lang=lang)
    skill_summary = (
        t("auditors_read", lang, summary=llm_summary) + "\n\n" + heuristic
        if llm_summary else heuristic
    )

    report_md = format_report_markdown(
        skill_name=skill_name,
        source_url=cand.get("source_url"),
        frontmatter=frontmatter,
        skill_summary=skill_summary,
        audit_result=result,
        body_metrics=body_metrics,
        lang=lang,
    )

    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", skill_name)
    out_path = out_dir / f"{safe_name}.md"
    out_path.write_text(report_md, encoding="utf-8")

    # Sidecar metadata for build_site_v2
    meta = {
        "candidate_id": cand["candidate_id"],
        "skill_name": skill_name,
        "source_platform": cand.get("source_platform"),
        "source_url": cand.get("source_url"),
        "top_level": cand.get("top_level"),
        "subcategory": cand.get("subcategory"),
        "raw_category": cand.get("raw_category"),
        "description": cand.get("description") or frontmatter.get("description", ""),
        "grade": result.get("grade"),
        "score": result.get("score"),
        "risk_class": result.get("risk_class"),
        "severity_counts": result.get("severity_counts", {}),
        "audited_at": result.get("audit_meta", {}).get("audited_at"),
    }
    (out_dir / f"{safe_name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    score = result.get("score", 0)
    grade = result.get("grade", "?")
    sev = result.get("severity_counts", {})
    print(f"  ✓ {skill_name}: {grade} ({score}/100) "
          f"[crit={sev.get('critical', 0)} high={sev.get('high', 0)} "
          f"warn={sev.get('warning', 0)}] → {out_path.name}", file=sys.stderr)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path",
                        default=str(HERE / "candidates.db"))
    parser.add_argument("--output", required=True,
                        help="Per-skill markdown output dir for this lang")
    parser.add_argument("--lang", default=DEFAULT_LANG, choices=SUPPORTED_LANGS)
    parser.add_argument("--engine-url", default=DEFAULT_ENGINE_URL)
    parser.add_argument("--limit", type=int, default=30,
                        help="Max candidates to audit this run")
    parser.add_argument("--no-llm-summary", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    cands = fetch_pending_candidates(conn, args.limit)
    if not cands:
        print("no candidates pending audit", file=sys.stderr)
        return 0

    print(f"Auditing {len(cands)} candidates → {out_dir} (lang={args.lang})",
          file=sys.stderr)

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    openai_model = os.environ.get("OPENAI_MODEL")

    results = []
    for i, c in enumerate(cands, 1):
        print(f"[{i}/{len(cands)}] {c['skill_name']} ({c.get('top_level')}/"
              f"{c.get('subcategory')})", file=sys.stderr)
        try:
            m = audit_one(
                cand=c, engine_url=args.engine_url, out_dir=out_dir,
                lang=args.lang, use_llm_summary=not args.no_llm_summary,
                openai_api_key=openai_api_key,
                openai_base_url=openai_base_url, openai_model=openai_model,
            )
        except Exception as e:
            print(f"  ✗ exception: {e}", file=sys.stderr)
            m = None
        if m:
            results.append(m)
            mark_audited(conn, c["candidate_id"])

    print(f"\nDone. {len(results)} audited. Output: {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
