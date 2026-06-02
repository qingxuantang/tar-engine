---
name: generate-content
description: Generate draft text for one or more social platforms from a topic or URL. Wraps `postall-agent generate`. Activates when the user wish includes a content topic plus a publishing intent ("write a tweet about X", "draft a LinkedIn post on Y", "summarize this URL into a wechat article").
---

# Generate Content Skill

Produce platform-specific draft text using PostAll's generator.

## Inputs

The planner provides these args (read from `args`):

- `topic` (required) — what the content is about, in plain language.
- `platforms` (required) — comma-separated list, e.g. `twitter,linkedin,wechat`.
- `style` (optional) — `professional` / `casual` / `technical` / `playful`. Default: `professional`.
- `url` (optional) — if the wish included a URL, pass it so postall extracts content from it.

If `topic` or `platforms` is missing, return an error final message and do not call postall-agent.

## Execution

Invoke `postall-agent` via `run_bash`. Build the command from the args:

```bash
postall-agent generate \
  --topic "<TOPIC>" \
  --platforms "<PLATFORMS>" \
  [--style "<STYLE>"] \
  [--url "<URL>"]
```

The CLI prints a JSON object to stdout. Capture it.

## Output handling

Parse the JSON. Key fields you must return to the user:

- `draft_id` — needed by downstream skills (image, publish).
- `platforms` — confirmation of which platforms got drafts.
- `content_preview` — first 200 chars of each platform's draft for the user to skim.

Return a single concise final message in this shape:

```
Draft created.

draft_id: <ID>
platforms: <PLATFORMS>

Twitter:
  <first ~200 chars>

LinkedIn:
  <first ~200 chars>
```

If `postall-agent` returns `success: false`, surface the error message to the user verbatim and do NOT pretend the draft was created.

## Composability notes

Other skills in this pack consume `draft_id`. The wish-runner threads it through the `args` of the next skill automatically — you don't have to do anything special, just make sure `draft_id` is in your final message so the engine can extract it.

## Prerequisite

This skill requires `postall-agent` to be installed in the engine container:
```
pip install postall-agent
```

If `postall-agent` is not found (run_bash returns "command not found"), tell the user:
"This skill needs the postall-agent CLI installed. Run `pip install postall-agent` in the engine container and retry."
