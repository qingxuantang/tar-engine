# PostAll Content Pack

Multi-platform social content publishing pack for TAR Engine.

Composes [postall-agent](https://github.com/qingxuantang/postall) CLI as 4 atomic skills
that the wish machine can plan in any combination.

## What it does

Submit a wish like:

- `"draft a tweet about <topic> and publish it"` → full pipeline
- `"just generate a LinkedIn post about <topic>, don't publish"` → generate only
- `"add a cover image to draft abc123"` → image only
- `"what drafts do I have?"` → list

The planner picks 1-3 skills depending on the wish.

## Skills

| Skill | Wraps | Purpose |
|-------|-------|---------|
| `generate-content` | `postall-agent generate` | Draft text for one or more platforms |
| `generate-image` | `postall-agent image` | Cover image for an existing draft |
| `publish-content` | `postall-agent publish` | Send to platform accounts |
| `list-drafts` | `postall-agent list` | Read-only draft inventory |

## Prerequisites

1. Install `postall-agent` in the TAR Engine container:
   ```
   pip install postall-agent
   ```
2. Configure platform accounts and LLM provider in postall — see the
   [postall README](https://github.com/qingxuantang/postall).

## File layout

```
postall-content/
├── pack.yaml              # Pack manifest + skill registry
├── planner.md             # Few-shot examples for the planner
├── profile_schema.yaml    # Per-user voice / platform preferences
├── README.md              # This file
└── skills/
    ├── generate-content/SKILL.md
    ├── generate-image/SKILL.md
    ├── publish-content/SKILL.md
    └── list-drafts/SKILL.md
```

## How to use this pack

1. Make sure `postall-agent` is installed and configured.
2. Submit a wish via the cockpit endpoint:
   ```
   curl -X POST http://localhost:8765/api/cockpit/wish \
     -H "Content-Type: application/json" \
     -H "X-LLM-Api-Key: $OPENAI_API_KEY" \
     -H "X-LLM-Base-Url: https://api.openai.com/v1" \
     -H "X-LLM-Model: gpt-4o-mini" \
     -d '{"wish": "draft a tweet about AI agents and publish it", "user_id": "you"}'
   ```
3. The wish machine routes through generate-content → generate-image → publish-content.

## Status

v0.1 — atomic skills land + planner few-shots written. Not yet smoke-tested
end-to-end against a real postall-agent install (the pack ships, but verifying
the wish→publish loop requires postall configured with platform creds).
