#!/usr/bin/env python3
"""Batch-audit a list of skills defined in a YAML config.

Reads config with `sources:` containing GitHub raw URLs / local file paths,
runs each through audit_skill.py logic, writes one report per skill.

Usage:
    python3 batch_audit.py --config config/skills_to_audit.yaml --output reports/
"""
from __future__ import annotations

import argparse
import sys
import yaml
from pathlib import Path

# Reuse audit_skill helpers
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from audit_skill import (  # noqa: E402
    fetch_url,
    extract_skill_name,
    call_audit_endpoint,
    format_report_markdown,
    DEFAULT_ENGINE_URL,
)


def process_source(
    name: str,
    skill_text: str,
    source_url: str | None,
    engine_url: str,
    out_dir: Path,
    domain: str,
) -> tuple[str, int]:
    """Audit one skill, write report. Returns (skill_name, score)."""
    import re
    from datetime import datetime

    skill_name = extract_skill_name(skill_text, fallback=name)
    result = call_audit_endpoint(engine_url, skill_text, domain=domain)
    if not result.get("success", True) and "error" in result:
        print(f"  ✗ audit failed for {skill_name}: {result['error']}", file=sys.stderr)
        return (skill_name, -1)

    report_md = format_report_markdown(skill_name, source_url, result)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", skill_name)
    out_path = out_dir / f"{safe_name}-{datetime.utcnow().strftime('%Y%m%d')}.md"
    out_path.write_text(report_md, encoding="utf-8")
    score = result.get("score", 0)
    print(f"  ✓ {skill_name}: {score}/100 → {out_path}", file=sys.stderr)
    return (skill_name, score)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="YAML config file")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--engine-url", default=DEFAULT_ENGINE_URL)
    parser.add_argument("--domain", default="general")
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    sources = config.get("sources", [])
    if not sources:
        print("No sources in config.", file=sys.stderr)
        return 1

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Processing {len(sources)} source(s)...", file=sys.stderr)
    results = []
    for src in sources:
        name = src.get("name", "?")
        urls = src.get("urls", [])
        files = src.get("files", [])
        print(f"\n[{name}]", file=sys.stderr)

        for url in urls:
            try:
                text = fetch_url(url)
            except Exception as e:
                print(f"  ✗ fetch failed {url}: {e}", file=sys.stderr)
                continue
            results.append(process_source(name, text, url, args.engine_url, out_dir, args.domain))

        for fp in files:
            try:
                text = Path(fp).read_text(encoding="utf-8")
            except Exception as e:
                print(f"  ✗ read failed {fp}: {e}", file=sys.stderr)
                continue
            results.append(process_source(name, text, None, args.engine_url, out_dir, args.domain))

    # Summary
    print(f"\nDone. {len(results)} skills audited.", file=sys.stderr)
    if results:
        avg = sum(r[1] for r in results if r[1] >= 0) / max(1, sum(1 for r in results if r[1] >= 0))
        print(f"Average score: {avg:.1f}/100", file=sys.stderr)
        ranked = sorted(results, key=lambda r: r[1], reverse=True)
        print("\nLeaderboard:", file=sys.stderr)
        for nm, sc in ranked[:10]:
            print(f"  {sc:3d}  {nm}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
