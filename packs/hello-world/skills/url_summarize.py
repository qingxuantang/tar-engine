"""URL summarize skill — fetch a URL and summarize via BYOK LLM.

Demonstrates a real LLM-backed skill with token accounting.
"""
from __future__ import annotations

import re
import httpx
from openai import OpenAI


URL_RE = re.compile(r"https?://\S+")


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text)
    return m.group(0) if m else None


def fetch_page(url: str, timeout: float = 10.0) -> str:
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        r = client.get(url)
        r.raise_for_status()
        # Naive HTML strip — production should use trafilatura / readability
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]


def run(wish_text: str, llm_config: dict | None = None, **kwargs) -> dict:
    """Summarize the URL embedded in the wish.

    Args:
        wish_text: User's wish containing a URL.
        llm_config: dict with keys 'api_key', 'base_url', 'model'.

    Returns:
        dict with 'output' (summary text) and 'meta' (tokens).
    """
    url = extract_url(wish_text)
    if not url:
        return {
            "output": "No URL found in wish. Include a URL like https://example.com",
            "meta": {"skill": "url_summarize", "error": "no_url"},
        }

    try:
        page_text = fetch_page(url)
    except Exception as e:
        return {
            "output": f"Failed to fetch {url}: {e}",
            "meta": {"skill": "url_summarize", "error": "fetch_failed"},
        }

    cfg = llm_config or {}
    client = OpenAI(
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url", "https://api.openai.com/v1"),
    )
    resp = client.chat.completions.create(
        model=cfg.get("model", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "You summarize web pages in one paragraph."},
            {"role": "user", "content": f"Summarize this page:\n\n{page_text}"},
        ],
        max_tokens=300,
    )
    summary = resp.choices[0].message.content
    usage = resp.usage

    return {
        "output": summary,
        "meta": {
            "skill": "url_summarize",
            "tokens_in": usage.prompt_tokens if usage else 0,
            "tokens_out": usage.completion_tokens if usage else 0,
            "url": url,
        },
    }
