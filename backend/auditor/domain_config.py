"""Domain configuration base class.

Each domain (quant, devops, content, etc.) provides one DomainConfig
instance that injects domain-specific knowledge into the generic
auditor agents. The agent logic is domain-agnostic; only the config
changes per domain.

Currently only 'quant' is implemented. The interface is here so
adding a new domain = writing one config file, zero agent code changes.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class RealtimeRule:
    """A pattern-matching rule for risk detection.

    Used in two modes:
      - runtime: checked on every incoming tool_call event (millisecond response)
      - static: checked against document text (SKILL.md content, configs)

    match_scope controls which mode(s) this rule applies to.
    """
    name: str
    description: str
    severity: str  # "info", "warning", "high", "critical"
    match_tool: str = ""  # regex for tool_name (empty = any; ignored in static mode)
    match_file: str = ""  # regex for file path in args (ignored in static mode)
    match_content: str = ""  # regex for content/args/document text
    message: str = ""  # human-readable alert message
    only_for_skills: List[str] = field(default_factory=list)  # empty = all skills
    skip_for_skills: List[str] = field(default_factory=list)  # empty = no skips
    match_scope: str = "both"  # "runtime" | "static" | "both"


@dataclass
class DomainConfig:
    """Domain knowledge plugin. One instance per domain."""

    name: str
    display_name: str

    # Injected into each agent's system prompt as domain context
    domain_context: str

    # Terminology mapping for the domain
    terminology: Dict[str, str] = field(default_factory=dict)

    # ── RiskGuardrail ──
    realtime_rules: List[RealtimeRule] = field(default_factory=list)
    risk_thresholds: Dict[str, float] = field(default_factory=lambda: {
        "go": 80,
        "conditional": 50,
        "no_go": 0,
    })

    # ── PostMortem ──
    failure_patterns: List[str] = field(default_factory=list)
    diagnostic_framework: str = ""

    # ── CrossRunComparator ──
    key_metrics: List[str] = field(default_factory=list)
    convergence_criteria: Dict[str, Any] = field(default_factory=dict)
