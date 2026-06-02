"""General-purpose domain configuration.

Used for CC sessions where no specific domain is specified.
Applies only universal risk rules (destructive commands, sensitive files).
No domain-specific terminology or audit context.
"""

from ..domain_config import DomainConfig

GENERAL_CONFIG = DomainConfig(
    name="general",
    display_name="General",

    domain_context=(
        "You are auditing an AI coding agent's execution. "
        "Check for security risks, destructive operations, and credential exposure."
    ),

    terminology={},

    # No extra domain-specific rules. Universal rules from
    # RiskGuardrail (sensitive_file_access, destructive_bash) still apply.
    realtime_rules=[],
)
