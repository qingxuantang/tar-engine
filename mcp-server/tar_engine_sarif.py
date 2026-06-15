"""SARIF 2.1.0 output for tar-engine scan results.

Produces a SARIF (Static Analysis Results Interchange Format) document
consumable by GitHub's codeql-action/upload-sarif@v2, which renders
findings as code scanning alerts in the Security tab + inline PR
annotations + blocking checks.

Spec: https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from tar_engine_cli import SkillAuditOutcome

SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
SARIF_VERSION = "2.1.0"
TOOL_NAME = "tar-engine"
TOOL_URI = "https://tarai.dev"


def _severity_to_sarif_level(severity: str) -> str:
    """Map tar-engine severity → SARIF level enum.

    SARIF only accepts: error, warning, note, none.
    """
    s = (severity or "").lower()
    if s == "critical":
        return "error"
    if s == "high":
        return "error"
    if s == "warning":
        return "warning"
    if s == "info":
        return "note"
    return "none"


def _finding_to_result(finding: dict[str, Any], skill_path: Path) -> dict[str, Any]:
    """Convert a tar-engine finding dict into a SARIF result object."""

    rule_id = finding.get("rule_id") or finding.get("rule") or "tar-engine.unknown"
    message = finding.get("message") or finding.get("description") or "Audit finding"
    severity = finding.get("severity") or "info"

    result: dict[str, Any] = {
        "ruleId": rule_id,
        "message": {"text": message},
        "level": _severity_to_sarif_level(severity),
    }

    physical_location: dict[str, Any] = {
        "artifactLocation": {"uri": str(skill_path), "uriBaseId": "%SRCROOT%"},
    }

    line = finding.get("line")
    if line is None:
        hits = finding.get("hits") or []
        if hits and isinstance(hits, list):
            line = hits[0].get("line_number")

    if line:
        region: dict[str, Any] = {"startLine": int(line)}
        column = finding.get("column")
        if column:
            region["startColumn"] = int(column)
        physical_location["region"] = region

    result["locations"] = [{"physicalLocation": physical_location}]

    # Carry category / scanner provenance under properties so it survives the
    # SARIF round-trip without polluting standard fields.
    properties: dict[str, Any] = {}
    if finding.get("category"):
        properties["category"] = finding["category"]
    if finding.get("scanner"):
        properties["scanner"] = finding["scanner"]
    if finding.get("fix_template"):
        properties["fix"] = finding["fix_template"]
    if properties:
        result["properties"] = properties

    return result


def _build_rules_registry(outcomes: Iterable["SkillAuditOutcome"]) -> list[dict[str, Any]]:
    """Build the rules[] array — one entry per unique rule_id seen.

    SARIF tools (including GitHub) expect each result.ruleId to resolve to
    an entry in runs[0].tool.driver.rules[]. Empty rules[] still validates
    but loses the friendly rule-page rendering.
    """

    seen: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        for finding in outcome.findings or []:
            rule_id = finding.get("rule_id") or finding.get("rule") or "tar-engine.unknown"
            if rule_id in seen:
                continue
            entry: dict[str, Any] = {
                "id": rule_id,
                "name": rule_id,
                "shortDescription": {
                    "text": finding.get("description") or finding.get("message") or rule_id,
                },
                "defaultConfiguration": {
                    "level": _severity_to_sarif_level(finding.get("severity") or "info"),
                },
            }
            full = finding.get("fix_template") or finding.get("description")
            if full:
                entry["fullDescription"] = {"text": full}
            help_uri = finding.get("help_uri")
            if help_uri:
                entry["helpUri"] = help_uri
            if finding.get("category"):
                entry["properties"] = {"category": finding["category"]}
            seen[rule_id] = entry
    return list(seen.values())


def render_sarif(
    outcomes: list["SkillAuditOutcome"],
    *,
    tool_version: str = "0.3.0",
    invocation_args: list[str] | None = None,
    invocation_exit_code: int | None = None,
) -> str:
    """Render a SARIF 2.1.0 document covering every audited skill.

    Returns a JSON string ready to write to results.sarif.
    """

    rules = _build_rules_registry(outcomes)

    results: list[dict[str, Any]] = []
    for outcome in outcomes:
        if outcome.error:
            # Surface backend errors as a synthetic "tar-engine.backend" rule
            # so the SARIF consumer still sees one row per failed skill.
            results.append({
                "ruleId": "tar-engine.backend",
                "message": {"text": f"Audit backend error: {outcome.error}"},
                "level": "warning",
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": str(outcome.skill.path), "uriBaseId": "%SRCROOT%"},
                    },
                }],
            })
            continue
        for finding in outcome.findings or []:
            results.append(_finding_to_result(finding, outcome.skill.path))

    invocation: dict[str, Any] = {
        "executionSuccessful": True,
    }
    if invocation_args is not None:
        invocation["commandLine"] = " ".join(invocation_args)
    if invocation_exit_code is not None:
        invocation["exitCode"] = invocation_exit_code

    document: dict[str, Any] = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": tool_version,
                        "informationUri": TOOL_URI,
                        "rules": rules,
                    },
                },
                "results": results,
                "invocations": [invocation],
            },
        ],
    }

    return json.dumps(document, indent=2, ensure_ascii=False)
