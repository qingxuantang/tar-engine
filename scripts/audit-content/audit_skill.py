#!/usr/bin/env python3
"""Audit a single SKILL.md and produce a publishable markdown report.

Fetches the SKILL.md content (from URL or local file), submits it to the
TAR Engine audit endpoint, and writes a formatted markdown report.

Usage:
    python3 audit_skill.py --url <SKILL.md URL> --output reports/
    python3 audit_skill.py --file path/to/SKILL.md --output reports/
    python3 audit_skill.py --url ... --engine-url http://localhost:8765 --output ...
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


DEFAULT_ENGINE_URL = "http://localhost:8765"


def fetch_url(url: str, timeout: float = 15.0) -> str:
    """Fetch the contents of a URL. Raises on HTTP error."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "TAR Engine Audit Content Engine/0.1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_skill_name(skill_text: str, fallback: str = "unnamed") -> str:
    """Pull the skill name from the SKILL.md frontmatter `name:` field."""
    m = re.search(r"^---\s*\n(.*?)\n---", skill_text, re.MULTILINE | re.DOTALL)
    if not m:
        return fallback
    fm = m.group(1)
    nm = re.search(r"^name:\s*(.+?)\s*$", fm, re.MULTILINE)
    return nm.group(1).strip() if nm else fallback


def call_audit_endpoint(engine_url: str, skill_text: str, domain: str = "general") -> dict:
    """Call TAR Engine's static audit endpoint."""
    url = engine_url.rstrip("/") + "/api/cockpit/audit/static"
    body = json.dumps({"skill_text": skill_text, "domain": domain}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Try to parse error body
        try:
            err = json.loads(e.read().decode("utf-8"))
        except Exception:
            err = {"status_code": e.code, "reason": e.reason}
        return {"success": False, "error": err}


def grade_to_badge(score: float) -> str:
    """Map 0-100 score to a letter grade emoji badge."""
    if score >= 90:
        return "🟢 A"
    if score >= 75:
        return "🟢 B"
    if score >= 60:
        return "🟡 C"
    if score >= 40:
        return "🟠 D"
    return "🔴 F"


def format_report_markdown(
    skill_name: str,
    source_url: Optional[str],
    audit_result: dict,
) -> str:
    """Render the audit result as a publishable markdown post."""
    score = audit_result.get("score", 0)
    badge = grade_to_badge(score)
    findings = audit_result.get("findings", [])
    audit_meta = audit_result.get("audit_meta", {})

    timestamp = datetime.utcnow().strftime("%Y-%m-%d")

    md = []
    md.append(f"# Audit Report: `{skill_name}` — {badge} ({score}/100)")
    md.append("")
    md.append(f"*Audited by [TAR Engine](https://github.com/qingxuantang/tar-engine) on {timestamp}*")
    md.append("")

    if source_url:
        md.append(f"**Source:** [{source_url}]({source_url})")
        md.append("")

    # Verdict line
    if score >= 90:
        verdict = "Clean. No significant safety concerns detected."
    elif score >= 75:
        verdict = "Mostly clean. A few minor recommendations noted below."
    elif score >= 60:
        verdict = "Acceptable. Some safety concerns worth reviewing before production use."
    elif score >= 40:
        verdict = "Concerning. Multiple findings that warrant author attention."
    else:
        verdict = "High risk. We recommend not using this skill in production until findings are addressed."

    md.append(f"**Verdict:** {verdict}")
    md.append("")

    # Findings table
    if findings:
        md.append("## Findings")
        md.append("")
        md.append("| Severity | Rule | Description |")
        md.append("|---|---|---|")
        for f in findings:
            sev = f.get("severity", "info")
            rule = f.get("rule_id", "?")
            desc = f.get("message", "?")
            sev_icon = {
                "critical": "🔴 Critical",
                "high": "🟠 High",
                "warning": "🟡 Warning",
                "info": "🔵 Info",
            }.get(sev, sev)
            md.append(f"| {sev_icon} | `{rule}` | {desc[:200]} |")
        md.append("")
    else:
        md.append("## Findings")
        md.append("")
        md.append("No findings — this skill passed every rule in the audit pipeline cleanly.")
        md.append("")

    # Methodology note
    md.append("## How this audit was run")
    md.append("")
    md.append("This report is generated automatically by TAR Engine's static")
    md.append("audit pipeline. The SKILL.md content above is run through:")
    md.append("")
    md.append("- **L1 Static Rules** — regex-based detection of prompt injection,")
    md.append("  dangerous commands, sensitive file access, data exfiltration risk, ")
    md.append("  and overly permissive capability claims (17 universal rules).")
    md.append("- **L2 Capability Bitmap** — comparing declared `allowed-tools`")
    md.append("  against the actual operations the skill instructs the LLM to perform.")
    md.append("")

    # CTA — the whole point of these reports
    md.append("---")
    md.append("")
    md.append("## About TAR Engine")
    md.append("")
    md.append("TAR Engine is an OSS wish machine that runs skills inside its own")
    md.append("container with built-in audit. Speak a goal, the engine plans + executes")
    md.append("+ audits + writes a profile that gets sharper over time. BYOK.")
    md.append("")
    md.append("- **Audit your own skills:** `git clone https://github.com/qingxuantang/tar-engine && docker compose up`")
    md.append("- **Or skip the DIY:** use a [curated domain pack](https://github.com/qingxuantang/tar-engine#curated-domain-packs-paid-not-in-this-repo) for turnkey quant trading or content publishing.")
    md.append("- **Submit your skill** for next week's audit roundup: [reply on Twitter](https://twitter.com/intent/tweet?text=audit%20my%20skill%3A) or open an issue.")
    md.append("")
    md.append(f"_Engine version used: {audit_meta.get('engine_version', 'unknown')}._")
    md.append("")

    return "\n".join(md)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="URL to the SKILL.md")
    src.add_argument("--file", help="Local path to SKILL.md")
    parser.add_argument("--output", required=True, help="Output directory for the report")
    parser.add_argument("--engine-url", default=DEFAULT_ENGINE_URL, help=f"TAR Engine base URL (default: {DEFAULT_ENGINE_URL})")
    parser.add_argument("--domain", default="general", help="Audit domain (default: general)")
    parser.add_argument("--name-override", help="Override the skill name extracted from frontmatter")
    args = parser.parse_args()

    # Fetch skill text
    if args.url:
        print(f"Fetching {args.url} ...", file=sys.stderr)
        try:
            skill_text = fetch_url(args.url)
        except Exception as e:
            print(f"Fetch failed: {e}", file=sys.stderr)
            return 1
        source_url = args.url
    else:
        skill_text = Path(args.file).read_text(encoding="utf-8")
        source_url = None

    skill_name = args.name_override or extract_skill_name(skill_text, fallback="unnamed-skill")
    print(f"Auditing skill: {skill_name}", file=sys.stderr)

    # Call audit endpoint
    result = call_audit_endpoint(args.engine_url, skill_text, domain=args.domain)
    if not result.get("success", True) and "error" in result:
        print(f"Audit failed: {json.dumps(result['error'], indent=2)}", file=sys.stderr)
        return 1

    # Render report
    report_md = format_report_markdown(skill_name, source_url, result)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", skill_name)
    out_path = out_dir / f"{safe_name}-{datetime.utcnow().strftime('%Y%m%d')}.md"
    out_path.write_text(report_md, encoding="utf-8")

    print(f"✅ Report written: {out_path}", file=sys.stderr)
    print(out_path)  # stdout: the path, for piping
    return 0


if __name__ == "__main__":
    sys.exit(main())
