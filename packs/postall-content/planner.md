# PostAll Content Pack Planner

Planner few-shot examples for content publishing wishes.

## Available skills

1. `generate-content` — Produces a draft for one or more platforms from a topic / URL.
   Required args: `topic`, `platforms` (comma-list). Optional: `style`, `url`.
   Returns: `{draft_id, platforms, content_preview}`.

2. `generate-image` — Generates a cover image for an existing draft.
   Required args: `draft_id`. Optional: `prompt` (override).
   Returns: `{draft_id, image_path}`.

3. `publish-content` — Publishes a draft to platform accounts.
   Required args: `draft_id`. Optional: `platforms` (default: all in draft).
   Returns: `{draft_id, published_to: [{platform, url}]}`.

4. `list-drafts` — Lists drafts. Read-only.
   Optional args: `status` (`draft` / `published` / `partial`).
   Returns: `{drafts: [...]}`.

## Few-shot examples

### Example 1 — Full pipeline from topic

User wish: `"draft a tweet and LinkedIn post about Apple WWDC AI demo and publish both"`

Output:
```json
{
  "intent": "publish_content_pipeline",
  "skill_chain": [
    {"skill": "generate-content", "args": {"topic": "Apple WWDC AI demo", "platforms": "twitter,linkedin"}},
    {"skill": "generate-image", "args": {"draft_id": "$prev.draft_id"}},
    {"skill": "publish-content", "args": {"draft_id": "$prev.draft_id", "platforms": "twitter,linkedin"}}
  ],
  "clarifications_needed": []
}
```

### Example 2 — Draft only, no publish

User wish: `"write a Twitter draft about the new MacBook M5 launch but don't publish yet"`

Output:
```json
{
  "intent": "generate_only",
  "skill_chain": [
    {"skill": "generate-content", "args": {"topic": "new MacBook M5 launch", "platforms": "twitter"}}
  ],
  "clarifications_needed": []
}
```

### Example 3 — Add image to existing draft

User wish: `"generate a cover image for draft abc123"`

Output:
```json
{
  "intent": "image_for_existing",
  "skill_chain": [
    {"skill": "generate-image", "args": {"draft_id": "abc123"}}
  ],
  "clarifications_needed": []
}
```

### Example 4 — Publish existing draft

User wish: `"publish draft abc123 to LinkedIn only"`

Output:
```json
{
  "intent": "publish_existing",
  "skill_chain": [
    {"skill": "publish-content", "args": {"draft_id": "abc123", "platforms": "linkedin"}}
  ],
  "clarifications_needed": []
}
```

### Example 5 — Inspect what's queued

User wish: `"what drafts do I have that haven't been published yet?"`

Output:
```json
{
  "intent": "list_unpublished",
  "skill_chain": [
    {"skill": "list-drafts", "args": {"status": "draft"}}
  ],
  "clarifications_needed": []
}
```

### Example 6 — Generate from a URL

User wish: `"read https://example.com/blog-post and post about it on Twitter and WeChat"`

Output:
```json
{
  "intent": "publish_content_pipeline",
  "skill_chain": [
    {"skill": "generate-content", "args": {"topic": "summary of https://example.com/blog-post", "platforms": "twitter,wechat", "url": "https://example.com/blog-post"}},
    {"skill": "publish-content", "args": {"draft_id": "$prev.draft_id", "platforms": "twitter,wechat"}}
  ],
  "clarifications_needed": []
}
```

## Notes

- `$prev.draft_id` indicates the planner expects the engine to thread output
  from the previous step into the next step's args. The engine wires this at
  runtime — the planner does not resolve it.
- If the user wish doesn't mention platforms, ask in `clarifications_needed`.
- If the user wish is purely conversational ("hi", "what can you do"), don't
  match any skill — let the runner return without skills.
