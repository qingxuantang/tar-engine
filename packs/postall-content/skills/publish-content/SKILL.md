---
name: publish-content
description: Publish an existing draft to one or more platform accounts. Wraps `postall-agent publish`. Activates when the user wish says "publish", "send it out", "post to LinkedIn", or comes at the end of a publishing pipeline.
---

# Publish Content Skill

Push a draft to its configured platforms via PostAll's publishers.

## Inputs

- `draft_id` (required) — the draft to publish.
- `platforms` (optional) — subset of platforms to publish to. Default: all platforms the draft was generated for.

If `draft_id` is missing, return an error final message immediately.

## Execution

```bash
postall-agent publish --draft "<DRAFT_ID>" [--platforms "<PLATFORMS>"]
```

## Output handling

Parse the JSON. The response includes per-platform success/failure status. Return:

- `draft_id`
- `published_to` — list of `{platform, url, success}` entries.

Final message — list each platform on its own line:

```
Published draft <ID>.

  twitter: https://x.com/...   ✅
  linkedin: https://www.linkedin.com/posts/...  ✅
  wechat: <error message>  ❌
```

If ANY platform failed, the overall skill should still return success (others may have published). Report mixed results plainly.

If `success: false` at the top level (catastrophic failure), surface the error.

## Composability notes

This is usually the final skill in a publish pipeline. After this step, the wish is complete.

## Common failure modes

- Rate limit (Twitter): user must wait and retry tomorrow. Surface the rate-limit message verbatim.
- Auth expired (LinkedIn): user needs to refresh token. Surface and link to the auth refresh script if known.
- Platform refusal (content too long, banned terms): surface the platform's exact reason.

## Prerequisite

Same as `generate-content` — `postall-agent` must be installed and platform credentials must be configured in the postall environment.
