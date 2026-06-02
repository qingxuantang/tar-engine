"""Echo skill — echoes back the user's input verbatim.

Used by Hello World Pack as the simplest possible skill. No LLM call.
"""
from __future__ import annotations


def run(wish_text: str, **kwargs) -> dict:
    """Return the wish text wrapped as a result.

    Args:
        wish_text: The user's original wish.

    Returns:
        dict with 'output' and 'meta' keys.
    """
    return {
        "output": f"You said: {wish_text}",
        "meta": {"skill": "echo", "tokens_in": 0, "tokens_out": 0},
    }
