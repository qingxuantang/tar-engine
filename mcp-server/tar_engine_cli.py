"""TAR Engine CLI — scan a directory of skills and report audit findings.

Companion to the `tar-engine-mcp` MCP server. Wraps the same hosted/self-hosted
backend audit endpoint, but exposed as a one-shot CLI so users can drop it
into CI:

    tar-engine scan ./skills --min-score 70

Supported skill formats:
    - SKILL.md                       (OpenClaw, Claude Code, plain markdown)
    - .claude/commands/*.md          (Claude Code custom commands)
    - skill.yaml / skill.yml         (Codex)
    - manifest.json                  (Codex / Claude Code, when a claude/codex/commands key is present)
    - opencode.json                  (OpenCode)

Each discovered skill is POSTed to the backend's audit endpoint
(default https://tarai.dev). The backend returns the same structured
findings the MCP server consumes. The CLI aggregates per-skill results,
prints a terminal report, and optionally fails the build via --min-score.

Backend selection follows the same env vars as the MCP server:
    TAR_ENGINE_URL                   default https://tarai.dev
    TAR_ENGINE_TIMEOUT               default 180 seconds
    TAR_ENGINE_BYOK_OPENAI_KEY       optional, forwarded for L02/L03 layers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

import httpx

ENGINE_URL = os.environ.get("TAR_ENGINE_URL", "https://tarai.dev").rstrip("/")
DEFAULT_TIMEOUT = float(os.environ.get("TAR_ENGINE_TIMEOUT", "180"))

_IS_HOSTED = "tarai.dev" in ENGINE_URL
AUDIT_PATH = "/api/audit-demo" if _IS_HOSTED else "/api/cockpit/audit/static"

LLM_HEADERS = {
    k: v for k, v in (
        ("X-LLM-Api-Key", os.environ.get("TAR_ENGINE_BYOK_OPENAI_KEY", "")),
        ("X-LLM-Base-Url", os.environ.get("TAR_ENGINE_BYOK_OPENAI_BASE_URL", "")),
        ("X-LLM-Model", os.environ.get("TAR_ENGINE_BYOK_OPENAI_MODEL", "")),
    ) if v
}

SkillFormat = Literal["openclaw", "claude", "codex", "opencode", "unknown"]

# File extensions read into the audit payload alongside the primary skill file.
# This catches the common "SKILL.md looks clean but install.sh does the dirty
# work" pattern. Total payload capped at SIDE_FILE_BUDGET_BYTES below.
SIDE_FILE_EXTENSIONS = {".sh", ".py", ".js", ".ts", ".yaml", ".yml", ".json"}
SIDE_FILE_BUDGET_BYTES = 200 * 1024  # 200 KB cap on concatenated side files

# Directory names always skipped during discovery.
IGNORE_DIRS = {".git", "node_modules", "dist", "build", ".venv", ".pytest_cache", "__pycache__"}


@dataclass
class DiscoveredSkill:
    """A single skill candidate found during directory walk."""

    path: Path
    """Absolute path of the primary skill file (e.g. .../my-skill/SKILL.md)."""

    name: str
    """Display name, derived from frontmatter / first heading / directory name."""

    format: SkillFormat
    """Best-effort format classification."""

    primary_content: str
    """Content of the primary skill file."""

    side_files: list[Path] = field(default_factory=list)
    """Sibling helper files (.sh/.py/.js/etc.) included in the audit payload."""


@dataclass
class SkillAuditOutcome:
    """Per-skill audit result, normalized across endpoint variants."""

    skill: DiscoveredSkill
    score: int | None
    grade: str | None
    risk_class: str | None
    severity_counts: dict[str, int]
    findings: list[dict[str, Any]]
    breakdown: dict[str, Any]
    raw: dict[str, Any]
    error: str | None = None
    scanner_results: list[dict[str, Any]] = field(default_factory=list)


def _max_severity(findings: list[dict[str, Any]]) -> str:
    """Highest severity present across findings. Used by --strict gate."""
    rank = {"critical": 4, "high": 3, "warning": 2, "info": 1}
    best = 0
    for f in findings:
        sev = (f.get("severity") or "").lower()
        best = max(best, rank.get(sev, 0))
    return {4: "critical", 3: "high", 2: "warning", 1: "info"}.get(best, "none")


# ── Discovery ────────────────────────────────────────────────────────────


def detect_format(file_path: Path, content: str, sibling_names: set[str]) -> SkillFormat:
    """Classify the skill format. Ported from dabit3/skill-audit detector.ts
    and extended for our discovery surface."""

    name = file_path.name

    if name == "SKILL.md":
        return "openclaw"

    parent = file_path.parent.name
    grandparent = file_path.parent.parent.name if file_path.parent.parent else ""
    if parent == "commands" and grandparent == ".claude":
        return "claude"

    if name in {"skill.yaml", "skill.yml"}:
        return "codex"

    if name == "opencode.json":
        return "opencode"

    if name == "manifest.json":
        # Could be Codex, Claude Code, or unrelated. Look inside.
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return "unknown"
        if isinstance(data, dict):
            if any(k in data for k in ("claude", "commands")):
                return "claude"
            if any(k in data for k in ("codex", "skill_id", "agent_id")):
                return "codex"
        return "unknown"

    # Content-based heuristics for stray .md files in a skills/ tree.
    if "$ARGUMENTS" in content or "/command" in content:
        return "claude"
    if "## Instructions" in content and "## Scripts" in content:
        return "openclaw"

    return "unknown"


def derive_skill_name(file_path: Path, content: str) -> str:
    """Pull a display name from frontmatter, first heading, or directory."""

    # YAML frontmatter `name:` (any of openclaw/codex)
    m = re.search(r"^name\s*:\s*['\"]?([^'\"\n]+)['\"]?\s*$", content, re.MULTILINE)
    if m:
        return m.group(1).strip()

    # First-level markdown heading: `# Foo` or `# Foo - subtitle`
    m = re.search(r"^#\s+(.+?)(?:\s+[-—|]\s+|$)", content, re.MULTILINE)
    if m:
        return m.group(1).strip()

    # JSON/YAML "title" field
    m = re.search(r"['\"]?title['\"]?\s*:\s*['\"]([^'\"\n]+)['\"]", content)
    if m:
        return m.group(1).strip()

    # Fall back to the directory name.
    return file_path.parent.name


def _iter_candidate_files(base: Path) -> Iterable[Path]:
    """Walk base, yield files whose name matches a known primary-skill pattern."""

    primary_names = {
        "SKILL.md",
        "skill.yaml",
        "skill.yml",
        "manifest.json",
        "opencode.json",
    }

    for path in base.rglob("*"):
        if not path.is_file():
            continue

        # Skip anything under an ignored directory.
        if any(part in IGNORE_DIRS for part in path.parts):
            continue

        if path.name in primary_names:
            yield path
            continue

        # Claude Code custom commands: .claude/commands/<name>.md
        if (
            path.suffix == ".md"
            and path.parent.name == "commands"
            and path.parent.parent.name == ".claude"
        ):
            yield path


def discover_skills(base: Path) -> list[DiscoveredSkill]:
    """Walk base, return one DiscoveredSkill per primary file found.
    Deduplicates: at most one skill per directory."""

    seen_dirs: set[Path] = set()
    skills: list[DiscoveredSkill] = []

    # Sort to make output deterministic.
    candidates = sorted(_iter_candidate_files(base), key=lambda p: str(p))

    for primary in candidates:
        directory = primary.parent
        if directory in seen_dirs:
            continue
        try:
            content = primary.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            sibling_names = {p.name for p in directory.iterdir() if p.is_file()}
        except OSError:
            sibling_names = set()

        fmt = detect_format(primary, content, sibling_names)
        name = derive_skill_name(primary, content)

        side_files = _collect_side_files(directory, primary)

        skills.append(
            DiscoveredSkill(
                path=primary,
                name=name,
                format=fmt,
                primary_content=content,
                side_files=side_files,
            )
        )
        seen_dirs.add(directory)

    return skills


def _collect_side_files(directory: Path, primary: Path) -> list[Path]:
    """Find sibling helper files worth including in the audit payload.

    Walks the primary file's directory tree (not the whole project), capped at
    SIDE_FILE_BUDGET_BYTES total to keep payload sane. Used by item D in
    PLAN_MULTI_FORMAT_DISCOVERY_AND_CI_ERGONOMICS.md — catches the
    `SKILL.md clean, install.sh malicious` pattern.
    """

    collected: list[Path] = []
    budget = SIDE_FILE_BUDGET_BYTES

    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path == primary:
            continue
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in SIDE_FILE_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > budget:
            continue
        collected.append(path)
        budget -= size

    return collected


def assemble_audit_payload(skill: DiscoveredSkill) -> str:
    """Build the single text blob sent to the backend audit endpoint.

    Format:
        # === primary: <relative path> ===
        <primary content>

        # === side-file: <relative path> ===
        <side file content>
        ...

    Backend rules grep this whole blob, so injection patterns inside
    install.sh get flagged the same as patterns in SKILL.md. Provenance
    headers stay machine-readable in case the report wants per-file
    attribution later.
    """

    parts = [
        f"# === primary: {skill.path.name} ===",
        skill.primary_content,
    ]
    for side in skill.side_files:
        try:
            text = side.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = side.relative_to(skill.path.parent)
        parts.append("")
        parts.append(f"# === side-file: {rel} ===")
        parts.append(text)

    return "\n".join(parts)


# ── Backend call ─────────────────────────────────────────────────────────


async def _audit_one(
    skill: DiscoveredSkill,
    lang: str,
    domain: str,
    run_external_scanners: bool = True,
) -> SkillAuditOutcome:
    payload = assemble_audit_payload(skill)
    body = {"skill_text": payload, "lang": lang, "domain": domain}

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as cx:
            r = await cx.post(
                f"{ENGINE_URL}{AUDIT_PATH}",
                json=body,
                headers={"Content-Type": "application/json", **LLM_HEADERS},
            )
            r.raise_for_status()
            result = r.json()
        outcome = SkillAuditOutcome(
            skill=skill,
            score=result.get("score"),
            grade=result.get("grade"),
            risk_class=result.get("risk_class"),
            severity_counts=result.get("severity_counts", {}),
            findings=list(result.get("findings", []) or []),
            breakdown=result.get("score_breakdown_by_category", {}),
            raw=result,
        )
        # Mark the backend findings with the engine source so per-scanner
        # attribution works once external scanners merge in.
        for f in outcome.findings:
            f.setdefault("scanner", "tar-engine")
        outcome.scanner_results.append({
            "scanner": "tar-engine",
            "status": "findings" if outcome.findings else "pass",
            "files_scanned": 1,
            "finding_count": len(outcome.findings),
            "skip_reason": None,
            "error": None,
        })
    except httpx.HTTPError as exc:
        outcome = SkillAuditOutcome(
            skill=skill,
            score=None,
            grade=None,
            risk_class=None,
            severity_counts={},
            findings=[],
            breakdown={},
            raw={},
            error=str(exc),
        )
        outcome.scanner_results.append({
            "scanner": "tar-engine",
            "status": "error",
            "files_scanned": 0,
            "finding_count": 0,
            "skip_reason": None,
            "error": str(exc),
        })

    if run_external_scanners:
        _merge_external_scans(outcome)

    return outcome


def _merge_external_scans(outcome: SkillAuditOutcome) -> None:
    """Run any locally-available external static analyzers on the skill dir.

    Subprocess calls happen synchronously inside the calling thread — callers
    already use bounded concurrency, so one outbound process per skill is fine.
    """
    try:
        from tar_engine_scanners import get_active_scanners
    except ImportError:
        return

    skill_dir = outcome.skill.path.parent
    sev_counts = dict(outcome.severity_counts)

    for scanner in get_active_scanners():
        if not scanner.is_available():
            outcome.scanner_results.append({
                "scanner": scanner.name,
                "status": "skipped",
                "files_scanned": 0,
                "finding_count": 0,
                "skip_reason": "not installed",
                "install_hint": scanner.install_hint,
                "error": None,
            })
            continue
        try:
            result = scanner.scan(skill_dir)
        except Exception as exc:  # defensive — never let a scanner break the run
            outcome.scanner_results.append({
                "scanner": scanner.name,
                "status": "error",
                "files_scanned": 0,
                "finding_count": 0,
                "skip_reason": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        for finding in result.findings:
            d = finding.to_dict()
            outcome.findings.append(d)
            sev = d.get("severity", "info")
            sev_counts[sev] = sev_counts.get(sev, 0) + 1

        outcome.scanner_results.append({
            **result.to_dict(),
            "install_hint": getattr(scanner, "install_hint", None),
        })

    outcome.severity_counts = sev_counts


async def _audit_all(
    skills: list[DiscoveredSkill],
    lang: str,
    domain: str,
    concurrency: int = 4,
    run_external_scanners: bool = True,
) -> list[SkillAuditOutcome]:
    """Audit all discovered skills with bounded concurrency."""

    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(skill: DiscoveredSkill) -> SkillAuditOutcome:
        async with semaphore:
            return await _audit_one(skill, lang, domain, run_external_scanners)

    return await asyncio.gather(*(_bounded(s) for s in skills))


# ── Output ───────────────────────────────────────────────────────────────


def _format_count(value: int, label: str, color: str = "") -> str:
    if value == 0:
        return ""
    return f"  {label}={value}"


def render_terminal(outcomes: list[SkillAuditOutcome], verbose: bool) -> None:
    """Plain ANSI-free terminal output (CI-friendly)."""

    for outcome in outcomes:
        s = outcome.skill
        rel = _relpath(s.path)
        side_count = len(s.side_files)
        side_note = f"  side-files={side_count}" if side_count else ""

        print()
        print(f"{s.name}  [{s.format}]")
        print(f"  {rel}{side_note}")

        if outcome.error:
            print(f"  ERROR: {outcome.error}")
            continue

        grade = outcome.grade or "?"
        score = outcome.score if outcome.score is not None else "?"
        risk = outcome.risk_class or "?"
        print(f"  Grade: {grade}  Score: {score}/100  Risk: {risk}")

        sev = outcome.severity_counts or {}
        sev_line = "".join(
            _format_count(sev.get(s, 0), s)
            for s in ("critical", "high", "warning", "info")
        ).strip()
        if sev_line:
            print(f"  Severity: {sev_line}")

        if outcome.breakdown:
            for cat, info in outcome.breakdown.items():
                if not isinstance(info, dict):
                    continue
                print(
                    f"    {cat}: {info.get('score', '?')}/100  "
                    f"findings={info.get('findings_count', 0)}  "
                    f"max={info.get('max_severity', 'none')}"
                )

        if outcome.scanner_results:
            print("  Scanners:")
            for sr in outcome.scanner_results:
                name = sr.get("scanner", "?")
                status = sr.get("status", "?")
                count = sr.get("finding_count", 0)
                files = sr.get("files_scanned", 0)
                if status == "skipped":
                    print(f"    [skip] {name}: not installed")
                elif status == "error":
                    print(f"    [err]  {name}: {sr.get('error', 'error')}")
                elif status == "pass":
                    files_note = f" ({files} file{'s' if files != 1 else ''})" if files else ""
                    print(f"    [ok]   {name}: 0 findings{files_note}")
                else:  # findings
                    files_note = f" ({files} file{'s' if files != 1 else ''})" if files else ""
                    print(f"    [!]    {name}: {count} finding{'s' if count != 1 else ''}{files_note}")

        if verbose and outcome.findings:
            print("  Findings:")
            for i, f in enumerate(outcome.findings, 1):
                sev = (f.get("severity") or "").upper()
                rule = f.get("rule_id") or f.get("rule") or "?"
                msg = f.get("message") or f.get("description") or ""
                line = f.get("line")
                where = f":{line}" if line else ""
                scanner_tag = f" [{f.get('scanner')}]" if f.get("scanner") else ""
                print(f"    {i}. [{sev}]{scanner_tag} {rule}  {msg}{where}")


def render_summary(
    outcomes: list[SkillAuditOutcome],
    min_score: int | None,
    failed_min_score: list[SkillAuditOutcome],
    strict: bool = False,
    failed_strict: list[SkillAuditOutcome] | None = None,
) -> None:
    valid = [o for o in outcomes if o.score is not None]
    errored = [o for o in outcomes if o.error]

    print()
    print("Audit Summary")
    print(f"  Skills audited: {len(outcomes)}")
    if errored:
        print(f"  Errored: {len(errored)}")
    if valid:
        avg = sum(o.score for o in valid) / len(valid)
        print(f"  Average score: {avg:.1f}/100")
    by_fmt: dict[str, int] = {}
    for o in outcomes:
        by_fmt[o.skill.format] = by_fmt.get(o.skill.format, 0) + 1
    if by_fmt:
        formats = ", ".join(f"{k}={v}" for k, v in sorted(by_fmt.items()))
        print(f"  By format: {formats}")

    if min_score is not None:
        if failed_min_score:
            print(f"  FAIL --min-score: {len(failed_min_score)} skill(s) scored below {min_score}")
        else:
            print(f"  PASS --min-score: all skills meet {min_score}")

    if strict:
        n = len(failed_strict or [])
        if n:
            print(f"  FAIL --strict: {n} skill(s) have warning+ severity findings")
        else:
            print(f"  PASS --strict: no warning+ severity findings")

    # Surface skipped scanners as install suggestions
    seen_skipped: set[str] = set()
    install_hints: list[tuple[str, str]] = []
    for o in outcomes:
        for sr in o.scanner_results or []:
            if sr.get("status") == "skipped" and sr.get("scanner") not in seen_skipped:
                seen_skipped.add(sr["scanner"])
                if sr.get("install_hint"):
                    install_hints.append((sr["scanner"], sr["install_hint"]))
    if install_hints:
        print()
        print("Optional scanners not installed (audit still ran without them):")
        for name, hint in install_hints:
            first_line = hint.split("\n")[0]
            print(f"  - {name}: {first_line}")
        print("  Run `tar-engine check-tools` for full install instructions.")


def _build_json_payload(
    outcomes: list[SkillAuditOutcome],
    min_score: int | None,
    strict: bool,
) -> dict[str, Any]:
    return {
        "results": [
            {
                "name": o.skill.name,
                "path": str(o.skill.path),
                "format": o.skill.format,
                "side_files": [str(p) for p in o.skill.side_files],
                "score": o.score,
                "grade": o.grade,
                "risk_class": o.risk_class,
                "severity_counts": o.severity_counts,
                "score_breakdown_by_category": o.breakdown,
                "scanner_results": o.scanner_results,
                "findings": o.findings,
                "error": o.error,
            }
            for o in outcomes
        ],
        "summary": {
            "total": len(outcomes),
            "errored": sum(1 for o in outcomes if o.error),
            "average_score": (
                round(
                    sum(o.score for o in outcomes if o.score is not None)
                    / max(1, sum(1 for o in outcomes if o.score is not None)),
                    1,
                )
                if any(o.score is not None for o in outcomes)
                else None
            ),
            "min_score_threshold": min_score,
            "strict": strict,
            "by_format": _by_format(outcomes),
        },
    }


def render_json(
    outcomes: list[SkillAuditOutcome],
    min_score: int | None,
    strict: bool = False,
    output: Path | None = None,
) -> None:
    text = json.dumps(_build_json_payload(outcomes, min_score, strict), indent=2, ensure_ascii=False)
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        print(text)


def render_sarif_to(
    outcomes: list[SkillAuditOutcome],
    *,
    argv: list[str],
    exit_code: int,
    output: Path | None,
) -> None:
    from tar_engine_sarif import render_sarif

    text = render_sarif(
        outcomes,
        tool_version="0.3.0",
        invocation_args=argv,
        invocation_exit_code=exit_code,
    )
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        print(text)


def _by_format(outcomes: list[SkillAuditOutcome]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.skill.format] = counts.get(o.skill.format, 0) + 1
    return counts


def _relpath(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# ── CLI ──────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tar-engine",
        description=(
            "Audit AI skill safety from the command line. Walks a directory, "
            "discovers SKILL.md / .claude/commands/*.md / skill.yaml / "
            "manifest.json / opencode.json, runs each through the tar-engine "
            "audit pipeline (default: https://tarai.dev), and exits with a "
            "CI-friendly status code. Companion to the tar-engine-mcp MCP server."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help="Audit AI skill safety: discover + audit every skill under a path. CI-friendly.",
    )
    scan.add_argument("path", nargs="?", default=".", help="Directory to scan (default: .)")
    scan.add_argument(
        "--min-score",
        type=int,
        default=None,
        metavar="N",
        help="Exit 1 if any skill's total score is below N.",
    )
    scan.add_argument(
        "--lang",
        choices=("en", "zh"),
        default="en",
        help="Findings language (default: en).",
    )
    scan.add_argument(
        "--domain",
        default="general",
        help="Audit domain (default: general).",
    )
    scan.add_argument(
        "--concurrency",
        type=int,
        default=4,
        metavar="N",
        help="Max parallel backend requests (default: 4).",
    )
    scan.add_argument(
        "--format",
        "-f",
        choices=("pretty", "json", "sarif"),
        default="pretty",
        help=(
            "Output format. `pretty` is the terminal report (default), `json` emits "
            "the structured payload, `sarif` emits SARIF 2.1.0 for upload to "
            "GitHub Security via codeql-action/upload-sarif@v2."
        ),
    )
    scan.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write report to FILE instead of stdout (json / sarif formats).",
    )
    scan.add_argument(
        "--json",
        action="store_true",
        help="Alias for --format json (kept for backward compatibility).",
    )
    scan.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail if any skill has critical / high / warning severity findings. "
            "Complements --min-score (either failure path exits 1)."
        ),
    )
    scan.add_argument(
        "--no-external-scanners",
        action="store_true",
        help=(
            "Skip optional shellcheck / semgrep / trufflehog / gitleaks integration. "
            "By default, any of these found on PATH augment the audit."
        ),
    )
    scan.add_argument("-v", "--verbose", action="store_true", help="Show per-finding detail.")
    scan.add_argument(
        "--no-summary",
        action="store_true",
        help="Suppress the trailing summary block (terminal mode only).",
    )

    list_cmd = sub.add_parser(
        "list",
        help="List discovered skills without auditing.",
    )
    list_cmd.add_argument("path", nargs="?", default=".", help="Directory to scan (default: .)")
    list_cmd.add_argument("--json", action="store_true")

    check_cmd = sub.add_parser(
        "check-tools",
        help="Show which optional external scanners are installed (shellcheck / semgrep / trufflehog / gitleaks).",
    )
    check_cmd.add_argument("--json", action="store_true", help="Emit structured JSON.")

    return parser.parse_args(argv)


def _run_list(args: argparse.Namespace) -> int:
    base = Path(args.path).resolve()
    if not base.exists():
        print(f"Path not found: {base}", file=sys.stderr)
        return 2

    skills = discover_skills(base)

    if args.json:
        payload = [
            {
                "name": s.name,
                "format": s.format,
                "path": str(s.path),
                "side_files": [str(p) for p in s.side_files],
            }
            for s in skills
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not skills:
        print("No skills found.")
        return 0

    print(f"Found {len(skills)} skill(s):")
    for s in skills:
        side = f"  +{len(s.side_files)} side-files" if s.side_files else ""
        print(f"  {s.name}  [{s.format}]")
        print(f"    {_relpath(s.path)}{side}")
    return 0


def _run_scan(args: argparse.Namespace, argv: list[str]) -> int:
    base = Path(args.path).resolve()
    if not base.exists():
        print(f"Path not found: {base}", file=sys.stderr)
        return 2

    # --json is a backwards-compat alias for --format json.
    fmt = "json" if args.json else args.format

    skills = discover_skills(base)
    if not skills:
        if fmt == "json":
            payload = {"results": [], "summary": {"total": 0}}
            text = json.dumps(payload, indent=2)
            if args.output:
                args.output.write_text(text, encoding="utf-8")
            else:
                print(text)
        elif fmt == "sarif":
            render_sarif_to(
                [], argv=argv, exit_code=0, output=args.output,
            )
        else:
            print("No skills found.")
        return 0

    run_external = not args.no_external_scanners
    outcomes = asyncio.run(
        _audit_all(skills, args.lang, args.domain, args.concurrency, run_external_scanners=run_external)
    )

    failed_min: list[SkillAuditOutcome] = []
    if args.min_score is not None:
        failed_min = [
            o for o in outcomes
            if o.score is not None and o.score < args.min_score
        ]

    failed_strict: list[SkillAuditOutcome] = []
    if args.strict:
        for o in outcomes:
            sc = o.severity_counts or {}
            if sc.get("critical", 0) + sc.get("high", 0) + sc.get("warning", 0) > 0:
                failed_strict.append(o)

    # Compute exit code before rendering SARIF so it can carry the right
    # invocation.exitCode value.
    exit_code = 1 if (failed_min or failed_strict) else 0

    if fmt == "json":
        render_json(outcomes, args.min_score, args.strict, args.output)
    elif fmt == "sarif":
        render_sarif_to(outcomes, argv=argv, exit_code=exit_code, output=args.output)
    else:
        render_terminal(outcomes, args.verbose)
        if not args.no_summary:
            render_summary(outcomes, args.min_score, failed_min, args.strict, failed_strict)

    return exit_code


def _run_check_tools(args: argparse.Namespace) -> int:
    try:
        from tar_engine_scanners import check_available_tools
    except ImportError as exc:
        print(f"check-tools requires tar_engine_scanners module: {exc}", file=sys.stderr)
        return 2

    rows = check_available_tools()

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print()
    print("External scanners (optional — graceful skip when absent):")
    print()
    for row in rows:
        mark = "[ok]  " if row["available"] else "[miss]"
        version = f" ({row['version']})" if row.get("version") else ""
        print(f"  {mark} {row['name']}{version}")
        print(f"         {row['description']}")
        if not row["available"]:
            hint_lines = (row.get("install_hint") or "").split("\n")
            for line in hint_lines:
                if line.strip():
                    print(f"         install: {line.strip()}")
        print()

    available = sum(1 for r in rows if r["available"])
    print(f"{available}/{len(rows)} scanners available.")
    if available < len(rows):
        print("Install missing tools above for deeper coverage. `tar-engine scan` works either way.")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv) if argv is None else list(argv)
    args = _parse_args(argv)
    if args.command == "scan":
        return _run_scan(args, raw_argv)
    if args.command == "list":
        return _run_list(args)
    if args.command == "check-tools":
        return _run_check_tools(args)
    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
