---
name: generate-image
description: Create a cover image for an existing draft. Wraps `postall-agent image`. Activates when the user wish references a draft and asks for "a cover image", "add a picture", or comes right after a generate-content step in a publishing pipeline.
---

# Generate Image Skill

Generate a cover image for an existing draft using PostAll's image executor.

## Inputs

- `draft_id` (required) — the draft to attach the image to.
- `prompt` (optional) — override the auto-generated prompt with explicit imagery directions.

If `draft_id` is missing, return an error final message immediately.

## Execution

```bash
postall-agent image --draft "<DRAFT_ID>" [--prompt "<PROMPT>"]
```

## Output handling

Parse the JSON. Return:

- `draft_id` — same as input.
- `image_path` — local path to the generated image.

Final message:

```
Image generated for draft <ID>.

path: <IMAGE_PATH>
```

If the JSON has `success: false`, surface the error verbatim — common causes are billing limit, invalid draft_id, or image executor not configured.

## Composability notes

Often called between `generate-content` and `publish-content`. The user profile flag `always_generate_image` (default true) signals whether the planner should auto-insert this skill into publish pipelines.

## Prerequisite

Same as `generate-content` — `postall-agent` must be installed. Additionally, the image generation requires a working image executor (Gemini / DALL-E / etc.) configured in the postall environment. If image generation fails with auth or config errors, surface the error and suggest checking the postall configuration.
