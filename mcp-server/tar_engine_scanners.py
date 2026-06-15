"""External static-analysis scanner wrappers for tar-engine.

Each scanner wraps a battle-tested external tool (`shellcheck`, `semgrep`,
`trufflehog`, `gitleaks`) as a subprocess and normalizes results into the
tar-engine finding shape. Tools are optional — if `shutil.which()` returns
None, the scanner reports as `skipped` with an install hint rather than
failing the whole scan.

Design notes
------------

- We do NOT compete with these tools on their own ground. semgrep ships
  community rules for 30+ languages; shellcheck is the de-facto bash
  static analysis tool; trufflehog/gitleaks dominate secret detection.
  By orchestrating instead of duplicating, tar-engine becomes a layered
  audit — backend (L01–L06) + external static tools + adversarial fuzz.
- Findings get a `scanner` field so the report can attribute provenance.
- Each scanner gets one shot per skill directory; aggregation is the
  caller's job.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

ScannerStatus = Literal["pass", "findings", "skipped", "error"]


@dataclass
class ExternalFinding:
    """A finding from an external scanner, normalized to tar-engine shape."""

    rule_id: str
    message: str
    severity: str  # critical | high | warning | info
    scanner: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    category: str = "external"
    fix_template: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rule_id": self.rule_id,
            "message": self.message,
            "severity": self.severity,
            "scanner": self.scanner,
            "category": self.category,
        }
        if self.file:
            d["file"] = self.file
        if self.line is not None:
            d["line"] = self.line
        if self.column is not None:
            d["column"] = self.column
        if self.fix_template:
            d["fix_template"] = self.fix_template
        return d


@dataclass
class ScanResult:
    """Per-scanner result for a single skill audit."""

    scanner: str
    status: ScannerStatus
    findings: list[ExternalFinding] = field(default_factory=list)
    files_scanned: int = 0
    skip_reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanner": self.scanner,
            "status": self.status,
            "files_scanned": self.files_scanned,
            "finding_count": len(self.findings),
            "skip_reason": self.skip_reason,
            "error": self.error,
        }


class ExternalScanner(Protocol):
    """Interface every external scanner must implement."""

    name: str
    install_hint: str
    description: str

    def is_available(self) -> bool: ...
    def scan(self, skill_dir: Path) -> ScanResult: ...


# ── Helper: pull a version string out of a tool ───────────────────────────

def _tool_version(cmd: str, version_flag: str = "--version") -> str | None:
    try:
        proc = subprocess.run(
            [cmd, version_flag],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip().split("\n")[0][:60]
    except Exception:
        pass
    return None


# ── ShellCheck ────────────────────────────────────────────────────────────

class ShellCheckScanner:
    name = "shellcheck"
    install_hint = "brew install shellcheck   # macOS\napt install shellcheck    # Debian/Ubuntu"
    description = "Industry-standard bash/sh static analysis (https://shellcheck.net)"

    def is_available(self) -> bool:
        return shutil.which("shellcheck") is not None

    def scan(self, skill_dir: Path) -> ScanResult:
        result = ScanResult(scanner=self.name, status="pass")
        shell_files = [
            p for p in skill_dir.rglob("*")
            if p.is_file() and p.suffix in {".sh", ".bash"}
        ]
        result.files_scanned = len(shell_files)
        if not shell_files:
            result.status = "pass"
            return result

        for shell_file in shell_files:
            try:
                proc = subprocess.run(
                    [
                        "shellcheck",
                        "--format=json",
                        "--severity=warning",
                        str(shell_file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                result.status = "error"
                result.error = f"shellcheck timed out on {shell_file.name}"
                return result
            except Exception as exc:
                result.status = "error"
                result.error = f"shellcheck error: {exc}"
                return result

            if not proc.stdout.strip():
                continue
            try:
                items = json.loads(proc.stdout)
            except json.JSONDecodeError:
                continue
            for item in items:
                level = (item.get("level") or "warning").lower()
                sev = {"error": "high", "warning": "warning", "info": "info", "style": "info"}.get(level, "warning")
                result.findings.append(ExternalFinding(
                    rule_id=f"shellcheck/SC{item.get('code', '0000')}",
                    message=item.get("message", "shellcheck finding"),
                    severity=sev,
                    scanner=self.name,
                    file=str(shell_file),
                    line=item.get("line"),
                    column=item.get("column"),
                    category="shell_safety",
                    fix_template=item.get("fix", {}).get("description") if isinstance(item.get("fix"), dict) else None,
                ))

        result.status = "findings" if result.findings else "pass"
        return result


# ── Semgrep ───────────────────────────────────────────────────────────────

class SemgrepScanner:
    name = "semgrep"
    install_hint = "pip install semgrep   # any platform\nbrew install semgrep  # macOS"
    description = "Multi-language code security scanner (https://semgrep.dev)"

    def is_available(self) -> bool:
        return shutil.which("semgrep") is not None

    def scan(self, skill_dir: Path) -> ScanResult:
        result = ScanResult(scanner=self.name, status="pass")
        code_files = [
            p for p in skill_dir.rglob("*")
            if p.is_file() and p.suffix in {".py", ".js", ".ts", ".rb", ".go", ".java"}
        ]
        result.files_scanned = len(code_files)
        if not code_files:
            result.status = "pass"
            return result

        try:
            proc = subprocess.run(
                [
                    "semgrep", "scan",
                    "--config=auto",
                    "--json",
                    "--quiet",
                    "--no-rewrite-rule-ids",
                    str(skill_dir),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            result.status = "error"
            result.error = "semgrep scan timed out (>120s)"
            return result
        except Exception as exc:
            result.status = "error"
            result.error = f"semgrep error: {exc}"
            return result

        if not proc.stdout.strip():
            result.status = "pass"
            return result
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result.status = "error"
            result.error = "semgrep returned malformed JSON"
            return result

        for item in data.get("results", []):
            sev_raw = (item.get("extra") or {}).get("severity", "WARNING").upper()
            sev = {"ERROR": "high", "WARNING": "warning", "INFO": "info"}.get(sev_raw, "warning")
            result.findings.append(ExternalFinding(
                rule_id=f"semgrep/{item.get('check_id', 'unknown')}",
                message=(item.get("extra") or {}).get("message", "semgrep finding"),
                severity=sev,
                scanner=self.name,
                file=item.get("path"),
                line=(item.get("start") or {}).get("line"),
                column=(item.get("start") or {}).get("col"),
                category="code_security",
            ))

        result.status = "findings" if result.findings else "pass"
        return result


# ── Trufflehog (preferred) / Gitleaks (fallback) ──────────────────────────

class TrufflehogScanner:
    name = "trufflehog"
    install_hint = "brew install trufflehog   # macOS\nSee https://github.com/trufflesecurity/trufflehog#installation"
    description = "Verified-secret detection across filesystem trees (https://github.com/trufflesecurity/trufflehog)"

    def is_available(self) -> bool:
        return shutil.which("trufflehog") is not None

    def scan(self, skill_dir: Path) -> ScanResult:
        result = ScanResult(scanner=self.name, status="pass")
        result.files_scanned = sum(1 for p in skill_dir.rglob("*") if p.is_file())

        try:
            proc = subprocess.run(
                [
                    "trufflehog", "filesystem",
                    "--json",
                    "--no-update",
                    str(skill_dir),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            result.status = "error"
            result.error = "trufflehog scan timed out (>120s)"
            return result
        except Exception as exc:
            result.status = "error"
            result.error = f"trufflehog error: {exc}"
            return result

        for line in proc.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            fs = (data.get("SourceMetadata") or {}).get("Data", {}).get("Filesystem", {}) or {}
            result.findings.append(ExternalFinding(
                rule_id=f"trufflehog/{data.get('DetectorName', 'unknown')}",
                message=f"{data.get('DetectorName', 'secret')} detected ({(data.get('Raw') or '')[:30]}...)",
                severity="critical",
                scanner=self.name,
                file=fs.get("file"),
                line=fs.get("line"),
                category="credential_exposure",
                fix_template="Rotate the exposed secret immediately, then remove from the skill and load from environment / secrets manager.",
            ))

        result.status = "findings" if result.findings else "pass"
        return result


class GitleaksScanner:
    """Fallback secret scanner when trufflehog isn't installed."""

    name = "gitleaks"
    install_hint = "brew install gitleaks   # macOS"
    description = "Secret detection (https://github.com/gitleaks/gitleaks)"

    def is_available(self) -> bool:
        return shutil.which("gitleaks") is not None

    def scan(self, skill_dir: Path) -> ScanResult:
        result = ScanResult(scanner=self.name, status="pass")
        result.files_scanned = sum(1 for p in skill_dir.rglob("*") if p.is_file())

        try:
            proc = subprocess.run(
                [
                    "gitleaks", "detect",
                    "--source", str(skill_dir),
                    "--report-format", "json",
                    "--report-path", "/dev/stdout",
                    "--no-git",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            result.status = "error"
            result.error = "gitleaks scan timed out (>120s)"
            return result
        except Exception as exc:
            result.status = "error"
            result.error = f"gitleaks error: {exc}"
            return result

        if not proc.stdout.strip():
            result.status = "pass"
            return result
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result.status = "pass"
            return result

        for item in data:
            result.findings.append(ExternalFinding(
                rule_id=f"gitleaks/{item.get('RuleID', 'unknown')}",
                message=item.get("Description", "Gitleaks finding"),
                severity="critical",
                scanner=self.name,
                file=item.get("File"),
                line=item.get("StartLine"),
                category="credential_exposure",
                fix_template="Rotate the exposed secret and replace with env var / secrets manager reference.",
            ))

        result.status = "findings" if result.findings else "pass"
        return result


# ── Registry + tool discovery ──────────────────────────────────────────────

ALL_SCANNERS: list[type[ExternalScanner]] = [
    ShellCheckScanner,
    SemgrepScanner,
    TrufflehogScanner,
    GitleaksScanner,
]


def get_active_scanners() -> list[ExternalScanner]:
    """Return one instance per supported scanner.

    Secret-scanning has overlap: trufflehog is preferred, gitleaks falls
    back when trufflehog isn't present. Returning both is fine — each
    self-reports availability.
    """
    return [cls() for cls in ALL_SCANNERS]


def check_available_tools() -> list[dict[str, Any]]:
    """Probe every supported tool. Used by `tar-engine check-tools`."""

    rows: list[dict[str, Any]] = []
    for cls in ALL_SCANNERS:
        scanner = cls()
        available = scanner.is_available()
        version = _tool_version(scanner.name) if available else None
        rows.append({
            "name": scanner.name,
            "available": available,
            "version": version,
            "description": scanner.description,
            "install_hint": scanner.install_hint,
        })
    return rows
