#!/usr/bin/env python3
"""Audit a single SKILL.md and produce a publishable markdown report.

Fetches the SKILL.md content (from URL or local file), submits it to the
TAR Engine audit endpoint, and writes a formatted markdown report.

Usage:
    python3 audit_skill.py --url <SKILL.md URL> --output reports/
    python3 audit_skill.py --file path/to/SKILL.md --output reports/
    python3 audit_skill.py --url ... --engine-url http://localhost:8765 --output ...

Report format (v0.2): 8 sections — header / what this skill does / score
breakdown by category / findings / what we didn't audit / methodology /
limitations / about TAR Engine.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


DEFAULT_ENGINE_URL = "http://localhost:8765"
REPORT_FORMAT_VERSION = "0.2"


def fetch_url(url: str, timeout: float = 15.0) -> str:
    """Fetch the contents of a URL. Raises on HTTP error."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "TAR Engine Audit Content Engine/0.2.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_frontmatter(skill_text: str) -> dict:
    """Pull the YAML-ish frontmatter block from a SKILL.md and parse it.

    Returns a dict of fields. Tolerates missing/malformed frontmatter (returns {}).
    Only flat string values are supported (the common SKILL.md case).
    """
    m = re.search(r"^---\s*\n(.*?)\n---", skill_text, re.MULTILINE | re.DOTALL)
    if not m:
        return {}
    fm = m.group(1)
    parsed: dict = {}
    for line in fm.splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if kv:
            key, val = kv.group(1), kv.group(2)
            # Strip surrounding quotes if present
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            parsed[key] = val
    return parsed


def extract_skill_body(skill_text: str) -> str:
    """Return the SKILL.md content after the frontmatter block."""
    m = re.search(r"^---\s*\n.*?\n---\s*\n(.*)$", skill_text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else skill_text


def summarize_skill_heuristic(skill_text: str, frontmatter: dict) -> str:
    """Build a brief 'what this skill does' paragraph WITHOUT an LLM.

    Uses frontmatter description + structural heuristics from the body.
    This is the fallback when no BYOK LLM is available.
    """
    description = (frontmatter.get("description") or "").strip()
    name = (frontmatter.get("name") or "this skill").strip()
    body = extract_skill_body(skill_text)

    body_lines = body.count("\n")
    body_chars = len(body)
    has_scripts = "run_bash" in body.lower() or "scripts/" in body.lower() or "./script" in body.lower()
    has_curl = bool(re.search(r"\bcurl\b|\bwget\b|requests\.|httpx\.", body))
    section_headers = re.findall(r"^##\s+(.+?)$", body, re.MULTILINE)

    summary = []
    if description:
        summary.append(f"**Author description:** {description}")

    obs_parts = []
    if section_headers:
        obs_parts.append(f"{len(section_headers)} top-level sections "
                         f"({', '.join(s.strip() for s in section_headers[:5])}"
                         f"{', …' if len(section_headers) > 5 else ''})")
    obs_parts.append(f"~{body_lines} lines of instructions")
    if has_scripts:
        obs_parts.append("delegates to packaged scripts")
    if has_curl:
        obs_parts.append("makes outbound network calls")
    if body_chars > 0:
        density = "dense" if body_chars / max(body_lines, 1) > 80 else "concise"
        obs_parts.append(f"{density} body")

    if obs_parts:
        summary.append(f"**Observed:** {name} is {obs_parts[0]}; "
                       f"{', '.join(obs_parts[1:])}.")
    return "\n\n".join(summary) if summary else f"_No description available for {name}._"


def summarize_skill_llm(skill_text: str, frontmatter: dict,
                       api_key: Optional[str], base_url: Optional[str],
                       model: Optional[str]) -> Optional[str]:
    """Optional LLM-powered skill summary (BYOK).

    Returns None when no API key is configured — caller should fall back to
    the heuristic summary. Failures here never block the report.
    """
    if not api_key:
        return None
    try:
        # Lazy import: openai client is in the engine env, but the audit script
        # may run elsewhere — fall back cleanly if missing.
        from openai import OpenAI  # type: ignore
    except ImportError:
        return None

    name = frontmatter.get("name") or "this skill"
    description = frontmatter.get("description") or ""

    system = (
        "You are an expert AI skill auditor. Given a SKILL.md file, produce a "
        "concise 2-3 sentence summary of what the skill actually does, focused on "
        "BEHAVIOR (what the LLM is instructed to do, what tools it touches, what "
        "outputs it produces). Skip the author marketing tone. Be technical and direct."
    )
    user = (
        f"Skill name: {name}\n"
        f"Author description: {description}\n\n"
        f"SKILL.md full text:\n```\n{skill_text[:8000]}\n```\n\n"
        "Write a 2-3 sentence behavioral summary."
    )
    try:
        client = OpenAI(api_key=api_key, base_url=base_url or None)
        resp = client.chat.completions.create(
            model=model or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        print(f"⚠ LLM summary failed: {e}", file=sys.stderr)
        return None


def call_audit_endpoint(engine_url: str, skill_text: str, domain: str = "general") -> dict:
    """Call TAR Engine's static audit endpoint."""
    url = engine_url.rstrip("/") + "/api/cockpit/audit/static"
    body = json.dumps({"skill_text": skill_text, "domain": domain}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
        except Exception:
            err = {"status_code": e.code, "reason": e.reason}
        return {"success": False, "error": err}


def grade_to_badge(grade: str, score: float) -> str:
    """Map letter grade + score to an emoji badge."""
    badges = {
        "A": "🟢 A",
        "B": "🟢 B",
        "C": "🟡 C",
        "D": "🟠 D",
        "F": "🔴 F",
    }
    return badges.get(grade, f"⚫ {grade}")


def severity_icon(sev: str) -> str:
    return {
        "critical": "🔴",
        "high": "🟠",
        "warning": "🟡",
        "info": "🔵",
        "none": "⚪",
    }.get(sev, "⚪")


def format_report_markdown(
    skill_name: str,
    source_url: Optional[str],
    frontmatter: dict,
    skill_summary: str,
    audit_result: dict,
    body_metrics: dict,
) -> str:
    """Render the audit result as a publishable markdown post (v0.2, 8 sections)."""
    score = audit_result.get("score", 0)
    grade = audit_result.get("grade", "?")
    risk_class = audit_result.get("risk_class", "Unknown")
    sev_counts = audit_result.get("severity_counts", {})
    breakdown = audit_result.get("score_breakdown_by_category", {})
    findings = audit_result.get("findings", [])
    rules_applied = audit_result.get("rules_applied", [])
    audit_meta = audit_result.get("audit_meta", {})
    badge = grade_to_badge(grade, score)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d")

    md = []

    # ── Section 1: Header + verdict ────────────────────────────────
    md.append(f"# Audit Report: `{skill_name}` — {badge} ({score}/100)")
    md.append("")
    md.append(f"*Audited by [TAR Engine](https://github.com/qingxuantang/tar-engine) "
              f"on {timestamp} · Report format v{REPORT_FORMAT_VERSION}*")
    md.append("")
    if source_url:
        md.append(f"**Source:** [`{source_url}`]({source_url})")
        md.append("")
    crit = sev_counts.get("critical", 0)
    high = sev_counts.get("high", 0)
    warn = sev_counts.get("warning", 0)
    if crit:
        verdict = (f"**{risk_class} risk** — {crit} critical finding"
                   f"{'s' if crit > 1 else ''} block this skill from production use "
                   f"until remediated.")
    elif high:
        verdict = (f"**{risk_class} risk** — {high} high-severity issue"
                   f"{'s' if high > 1 else ''} need author attention before "
                   f"deploying to a shared environment.")
    elif warn:
        verdict = (f"**{risk_class} risk** — {warn} warning"
                   f"{'s' if warn > 1 else ''} worth reviewing, but the skill is "
                   f"likely safe for personal use.")
    else:
        verdict = (f"**{risk_class} risk** — no signature matches across the "
                   f"applied rule set. This is a *clean static pass*; see "
                   f"§ What we didn't audit for what that leaves uncovered.")
    md.append(f"**Verdict:** {verdict}")
    md.append("")

    # ── Section 2: What this skill does ────────────────────────────
    md.append("## What this skill does")
    md.append("")
    md.append(skill_summary)
    md.append("")
    fm_extras = []
    if frontmatter.get("description"):
        pass  # already in summary
    allowed = frontmatter.get("allowed-tools") or frontmatter.get("allowed_tools")
    if allowed:
        fm_extras.append(f"- **Declared `allowed-tools`:** `{allowed}`")
    if body_metrics.get("body_lines"):
        fm_extras.append(f"- **Body size:** {body_metrics['body_lines']} lines / "
                         f"{body_metrics['body_chars']} chars")
    if fm_extras:
        md.append("**Frontmatter facts:**")
        md.append("")
        md.extend(fm_extras)
        md.append("")

    # ── Section 3: Score breakdown by category ─────────────────────
    md.append("## Score breakdown by category")
    md.append("")
    md.append("Each category gets its own sub-score. A category with no rule hits "
              "gets 100; a category with a single critical finding drops to 80.")
    md.append("")
    md.append("| Category | Rules evaluated | Findings | Max severity | Sub-score |")
    md.append("|---|---:|---:|:---:|---:|")
    for cat_key, cat in breakdown.items():
        max_sev = cat.get("max_severity", "none")
        sev_cell = f"{severity_icon(max_sev)} {max_sev}"
        md.append(
            f"| {cat['category_display']} | {cat['rules_evaluated']} | "
            f"{cat['findings_count']} | {sev_cell} | {cat['score']}/100 |"
        )
    md.append("")

    # ── Section 4: Findings (rich cards) ──────────────────────────
    md.append("## Findings")
    md.append("")
    if findings:
        md.append(f"**{len(findings)}** rule"
                  f"{'s' if len(findings) != 1 else ''} matched. Each finding "
                  f"below cites the matched line and a remediation hint.")
        md.append("")
        for i, f in enumerate(findings, 1):
            sev = f.get("severity", "info")
            sev_label = sev.upper()
            rule_id = f.get("rule_id", "?")
            rule_name = f.get("rule_name", "?")
            category_display = f.get("category_display", f.get("category", "?"))
            message = f.get("message", "")
            description = f.get("description") or ""
            fix = f.get("fix_template") or "_(no remediation template registered for this rule)_"
            hits = f.get("hits", [])
            md.append(f"### {i}. {severity_icon(sev)} `{rule_id}` — {rule_name} "
                      f"({sev_label})")
            md.append("")
            md.append(f"- **Category:** {category_display}")
            md.append(f"- **Why this matched:** {message}")
            if description:
                md.append(f"- **Rule intent:** {description}")
            md.append(f"- **Matches in document:** {f.get('match_count', 0)}")
            md.append("")
            if hits:
                md.append(f"**Evidence ({min(len(hits), 3)} of "
                          f"{f.get('match_count', len(hits))} match"
                          f"{'es' if f.get('match_count', len(hits)) != 1 else ''}):**")
                md.append("")
                for h in hits:
                    md.append(f"_Line {h['line_number']}:_")
                    md.append("```")
                    md.append(h.get("excerpt") or h.get("line_text", ""))
                    md.append("```")
                    md.append("")
            md.append(f"**Suggested fix:** {fix}")
            md.append("")
    else:
        md.append("**Zero rule hits** across the applied rule set.")
        md.append("")
        md.append("Every category below was evaluated and produced no finding. "
                  "This is a *clean static pass*. Categories evaluated:")
        md.append("")
        for cat_key, cat in breakdown.items():
            md.append(f"- {cat['category_display']} — "
                      f"{cat['rules_evaluated']} rule"
                      f"{'s' if cat['rules_evaluated'] != 1 else ''} ✓")
        md.append("")

    # ── Section 5: What we didn't audit ───────────────────────────
    md.append("## What we didn't audit")
    md.append("")
    md.append("Static audit is fast but not exhaustive. This run did **not** check:")
    md.append("")
    md.append("- **Runtime behavior.** We didn't execute the skill in a sandbox. "
              "Dynamic prompt construction, runtime branching, and "
              "model-dependent tool use go undetected.")
    md.append("- **Cross-skill chains.** When this skill is chained with others "
              "(e.g. via TAR Engine's planner), emergent behavior from "
              "skill-to-skill state flow isn't analyzed.")
    md.append("- **External dependencies.** If the skill instructs the LLM to "
              "download and execute a script from a URL, we flag the pipe-to-shell "
              "pattern but don't inspect the remote payload itself.")
    md.append("- **Semantic intent.** Our rules are pattern-based. A skill written "
              "to be polite but reach the same outcome as a critical-flagged one "
              "would pass; this is the static-vs-dynamic tradeoff.")
    md.append("- **LLM-side jailbreaks.** Resilience against adversarial prompts "
              "delivered AT this skill (e.g. through user input it passes to the "
              "model) is not in scope here.")
    md.append("")

    # ── Section 6: Methodology ────────────────────────────────────
    md.append("## Methodology")
    md.append("")
    md.append("**How the score was computed:**")
    md.append("")
    md.append("1. Document text is scanned against a static rule set of "
              f"{audit_meta.get('rule_count', '?')} signature patterns. Each rule "
              "carries a permanent `rule_id` (e.g. `PI-001`), a category, a "
              "severity, and a remediation template.")
    md.append("2. Each rule hit deducts from a 100-point base: critical -20, "
              "high -10, warning -5, info -1.")
    md.append("3. The letter grade is gated by max severity AND total score: any "
              "critical → F; any high → at most D; any warning → at most C; "
              "otherwise A/B by score band.")
    md.append("4. Per-category sub-scores apply the same deduction formula to that "
              "category's findings only — so you can see WHICH risk surface "
              "drove the loss.")
    md.append("")
    md.append("**Engine + rule set provenance:**")
    md.append("")
    md.append(f"- Engine version: `{audit_meta.get('engine_version', '?')}`")
    md.append(f"- Rule set version: `{audit_meta.get('rule_set_version', '?')}`")
    md.append(f"- Commit: `{audit_meta.get('commit_sha', '?')}`")
    md.append(f"- Domain config: `{audit_meta.get('domain', '?')}`")
    md.append(f"- Audited at: `{audit_meta.get('audited_at', '?')}`")
    md.append(f"- Rules applied: {len(rules_applied)} static rules "
              "(full registry below)")
    md.append("")
    md.append("<details>")
    md.append("<summary>Full rule registry applied to this audit</summary>")
    md.append("")
    md.append("| Rule ID | Name | Category | Severity |")
    md.append("|---|---|---|:---:|")
    for r in rules_applied:
        md.append(f"| `{r['rule_id']}` | {r['rule_name']} | "
                  f"{r['category']} | {r['severity']} |")
    md.append("")
    md.append("</details>")
    md.append("")

    # ── Section 7: Limitations ────────────────────────────────────
    md.append("## Known limitations of this report")
    md.append("")
    md.append("- **False positives are possible.** A SKILL.md *documenting* a "
              "dangerous pattern (e.g. an audit skill explaining `curl | sh`) "
              "will match the rule even though the skill's intent is to detect, "
              "not execute. Read the matched lines before reacting.")
    md.append("- **False negatives are guaranteed in narrow ways.** Patterns "
              "obfuscated by string concatenation, environment variable "
              "indirection, or non-English equivalents will slip past regex.")
    md.append("- **No historical baseline yet.** This is run #1 for this skill. "
              "Once TAR Engine accumulates ≥3 audits for the same skill, "
              "future reports will include a same-skill trend "
              "(score delta vs the rolling mean, standard deviation band).")
    md.append("")

    # ── Section 8: About TAR Engine (compact CTA) ─────────────────
    md.append("---")
    md.append("")
    md.append("## About TAR Engine")
    md.append("")
    md.append("TAR Engine is an OSS \"wish machine\" with built-in audit. Speak "
              "a goal; the engine plans, runs and audits skills inside its own "
              "container. BYOK. — "
              "[github.com/qingxuantang/tar-engine](https://github.com/qingxuantang/tar-engine)")
    md.append("")

    return "\n".join(md)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="URL to the SKILL.md")
    src.add_argument("--file", help="Local path to SKILL.md")
    parser.add_argument("--output", required=True, help="Output directory for the report")
    parser.add_argument("--engine-url", default=DEFAULT_ENGINE_URL,
                        help=f"TAR Engine base URL (default: {DEFAULT_ENGINE_URL})")
    parser.add_argument("--domain", default="general", help="Audit domain (default: general)")
    parser.add_argument("--name-override", help="Override the skill name extracted from frontmatter")
    parser.add_argument("--no-llm-summary", action="store_true",
                        help="Force heuristic skill summary even if OPENAI_API_KEY is set")
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

    frontmatter = parse_frontmatter(skill_text)
    skill_name = (args.name_override or frontmatter.get("name") or "unnamed-skill").strip()
    print(f"Auditing skill: {skill_name}", file=sys.stderr)

    body = extract_skill_body(skill_text)
    body_metrics = {
        "body_lines": body.count("\n"),
        "body_chars": len(body),
    }

    # Skill summary: try LLM (BYOK), fall back to heuristic
    llm_summary = None
    if not args.no_llm_summary:
        llm_summary = summarize_skill_llm(
            skill_text=skill_text,
            frontmatter=frontmatter,
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
            model=os.environ.get("OPENAI_MODEL"),
        )
    heuristic = summarize_skill_heuristic(skill_text, frontmatter)
    if llm_summary:
        skill_summary = (
            f"_Auditor's read (LLM-generated):_ {llm_summary}\n\n"
            f"{heuristic}"
        )
    else:
        skill_summary = heuristic

    # Call audit endpoint
    result = call_audit_endpoint(args.engine_url, skill_text, domain=args.domain)
    if not result.get("success", True) and "error" in result:
        print(f"Audit failed: {json.dumps(result['error'], indent=2)}", file=sys.stderr)
        return 1

    # Render report
    report_md = format_report_markdown(
        skill_name=skill_name,
        source_url=source_url,
        frontmatter=frontmatter,
        skill_summary=skill_summary,
        audit_result=result,
        body_metrics=body_metrics,
    )

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
