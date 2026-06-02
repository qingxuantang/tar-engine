"""Domain configurations for the audit pipeline.

OSS only ships the `general` domain config. Curated paid packs (quant, content
publishing, ...) ship their own DomainConfig modules.

`get_domain(name)` is the lookup helper. Pass a domain name string; returns the
matching DomainConfig or falls back to GENERAL_CONFIG.
"""
from typing import Optional

from .general import GENERAL_CONFIG


_REGISTRY = {
    "general": GENERAL_CONFIG,
}


def get_domain(name: Optional[str]):
    """Return the DomainConfig for `name`, or GENERAL_CONFIG if unknown.

    Curated paid packs can register themselves at install time by calling
    `register_domain(name, config)` from their setup hook.
    """
    if not name:
        return GENERAL_CONFIG
    return _REGISTRY.get(name, GENERAL_CONFIG)


def register_domain(name: str, config) -> None:
    """Register a DomainConfig for a domain name. Used by paid pack installers."""
    _REGISTRY[name] = config


__all__ = ["GENERAL_CONFIG", "get_domain", "register_domain"]
