#!/usr/bin/env python3
"""Fetch a URL and print extracted text content to stdout.

Used by the url-summarize skill. Invoked by the LLM via run_bash:

    python3 scripts/fetch.py <URL>

Output: extracted text, max 4000 chars. Errors go to stderr; stdout always has
something for the LLM to read (even if empty).
"""
import re
import sys
import urllib.request
import urllib.error


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: fetch.py <URL>", file=sys.stderr)
        return 2

    url = sys.argv[1]
    if not url.startswith(("http://", "https://")):
        print(f"refusing non-http URL: {url}", file=sys.stderr)
        return 2

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (TAR Engine url-summarize/0.1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} fetching {url}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    # Naive HTML strip — adequate for hello-world demo. Production skill would
    # use trafilatura / readability.
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    print(text[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
