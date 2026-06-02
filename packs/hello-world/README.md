# Hello World Pack

The simplest possible TAR Engine pack. Two trivial skills, no domain knowledge.

## What it does

Submit a wish like:

- `"echo hi there"` → runs `echo` skill, returns "You said: hi there"
- `"summarize https://example.com"` → runs `url_summarize` skill, returns a one-paragraph summary

## Why it exists

This pack is the **fastest way to verify** your TAR Engine OSS install is working.
The wish → planner → skill chain → trace → audit loop runs end-to-end with no
custom skill authoring required.

Use it as a reference when authoring your own pack. The two skills here show:

- `echo.py` — the minimal skill shape (no LLM, no I/O)
- `url_summarize.py` — a real skill (httpx + BYOK OpenAI client + token accounting)

## File layout

```
hello-world/
├── pack.yaml              # Pack manifest
├── planner.md             # Few-shot planner template
├── profile_schema.yaml    # User profile schema
├── skills/
│   ├── echo.py
│   └── url_summarize.py
└── README.md              # This file
```

## Next step

Once Hello World Pack runs cleanly, look at the curated **first-party packs**
(quant trading, content publishing, ...) that wrap TAR Engine into turnkey
domain workflows. Those are the paid layer; this Hello World is the free demo.
