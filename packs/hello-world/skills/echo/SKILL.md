---
name: echo
description: Echo the user's input back verbatim. Useful for greetings, smoke tests, or any wish that's purely conversational and doesn't need data fetching or analysis. Activates when the user says "echo X", "hi", "hello", or similar.
---

# Echo Skill

The simplest possible skill — it echoes the user's wish back as the result.

## When to use

Use this skill when the user wish is:

- A greeting: "hi", "hello", "hey there"
- A literal echo request: "echo hello world", "say something"
- A smoke test: "is this working?"
- Anything purely conversational that doesn't need data, fetching, or analysis

## How to respond

No tools needed. No scripts to call. Just respond with a single message that includes the user's wish text wrapped in a brief acknowledgment.

Format:

```
You said: <verbatim user input>
```

That's the entire skill. Done.

## What this skill demonstrates

This SKILL.md format is the **standard Claude Code skill format**. The TAR Engine
reads this file and puts it in the LLM's system prompt. The LLM then produces a
response — for trivial skills like this one, no `run_bash` / `read_file` /
`write_file` tool calls are needed.

For skills that need data or scripts, the SKILL.md instructs the LLM to call
`run_bash` to invoke scripts in the same skill directory. See `url-summarize/SKILL.md`
in this pack for that pattern.
