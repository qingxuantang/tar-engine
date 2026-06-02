---
name: url-summarize
description: Fetch a URL and produce a concise one-paragraph summary. Activates when the user wish contains a URL or asks to "summarize", "read", "fetch", or "tell me what this page says about" a webpage.
---

# URL Summarize Skill

Fetch a webpage and return a one-paragraph summary of its content.

## When to use

The user wish contains a URL (http:// or https://) AND asks for a summary, gist,
or explanation of what the page contains. Examples:

- "summarize https://example.com"
- "what does https://en.wikipedia.org/wiki/AI_agent say?"
- "tell me about this page: https://blog.example.com/foo"

## Execution

This skill uses a helper script to fetch the URL safely (with redirect handling
and HTML text extraction). Steps:

1. Extract the URL from the user's wish.
2. Call the fetch script via `run_bash`:
   ```
   python3 scripts/fetch.py "<URL>"
   ```
   The script prints the page's text content to stdout (max 4000 chars).
3. Read the script's output.
4. Generate a one-paragraph summary based on the text.
5. Return the summary as the final response.

## Style

- One paragraph, 3-5 sentences max.
- Focus on what the page actually says, not on the URL or technical details.
- If the page is empty, errors out, or has no readable text, say so plainly:
  "This page returned no extractable content."

## What this skill demonstrates

This SKILL.md + scripts/ pattern is how Claude Code skills work when they need
external data or computation. The SKILL.md is the entry point that the LLM reads.
Scripts are invoked **by the LLM via run_bash**, not directly by the engine. This
keeps the engine deterministic (it only reads SKILL.md and dispatches tool calls)
while letting the LLM decide which scripts to call and with what args.
