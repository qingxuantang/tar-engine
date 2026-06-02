---
name: list-drafts
description: List recent content drafts. Wraps `postall-agent list`. Activates when the user wish is read-only — "what drafts do I have", "show me unpublished posts", "any pending content".
---

# List Drafts Skill

Read-only inspection of the draft store.

## Inputs

- `status` (optional) — filter by `draft` / `published` / `partial`. Default: all.

## Execution

```bash
postall-agent list [--status <STATUS>]
```

## Output handling

Parse the JSON. The response contains an array of drafts, each with `id`, `topic`, `platforms`, `status`, `created_at`.

Format a human-readable summary. Group by status if no filter was applied:

```
Found <N> drafts.

📝 Unpublished (3):
  - abc123  | "Apple WWDC AI demo"  | twitter, linkedin  | created 2h ago
  - def456  | "LangChain alternatives"  | twitter  | created 1d ago

✅ Published (12):
  - ghi789  | "MacBook M5 review"  | twitter, linkedin, wechat  | published 3d ago
  ...
```

If the list is empty, say so plainly: "No drafts found."

If the JSON has `success: false`, surface the error verbatim.

## Composability notes

Read-only. Doesn't produce a `draft_id` for downstream chaining (the user can issue a follow-up wish like "publish abc123" to chain manually).

## Prerequisite

Same as other postall skills — `postall-agent` must be installed.
