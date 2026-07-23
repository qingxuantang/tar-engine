# Skill Audit Dimensions — a vendor-neutral checklist

*A tool-agnostic reference for what a complete AI-agent-skill safety audit should cover.
Written to be usable by any reviewer or any tool. No product endorsement required.*

An AI agent skill (a `SKILL.md`, a Codex `skill.yaml`, a Claude Code custom command, an
`opencode.json`, plus its sibling helper scripts) is executable trust: once installed, it
runs inside your agent with your permissions. A skill can pass every surface-level check and
still behave unsafely at runtime. A serious audit therefore covers **four independent
dimensions** — each catches what the others miss.

This document defines the dimensions, not a tool. Any of them can be checked by hand, by an
open-source scanner, by a commercial service, or by CI. Pick implementations that fit your
threat model; the point is that all four dimensions get covered.

---

## 1. Static red-flags (deterministic, no model required)

Pattern-level checks over the skill file and its sibling helper scripts:

- Remote code execution: `curl … | bash`, `wget … | sh`, `iex(irm …)` and similar
  pipe-to-shell installers.
- Credential / secret exfiltration: reads or archives of `~/.aws`, `~/.ssh`, `.env`,
  keychains, token files; outbound POSTs of those.
- Obfuscation: base64 / hex / unicode-lookalike blobs, `eval` of decoded strings.
- Prompt-injection markers in prose: "ignore previous instructions", "new system prompt",
  "do not tell the user".
- Out-of-scope filesystem or network writes relative to the skill's stated purpose.

**Why:** cheap, fast, and catches the loudest attacks. **Limit:** it only sees syntax, not
intent — a skill can be clean here and still be malicious.

## 2. Semantic / intent (does it do what it says)

Read what the skill actually instructs the agent to do and compare it to its declared
purpose:

- Does a "note formatter" also package up credentials? Does a "PDF tool" open a network
  socket? Flag capability that exceeds the stated scope.
- Does the skill instruct the model to conceal its own actions from the user?
- Does it quietly escalate permissions or persist state beyond the task?

**Why:** the gap between *stated purpose* and *actual behavior* is where most non-trivial
abuse lives, and regex can't see it. **Note:** this generally needs an LLM; run it
bring-your-own-key so skill text and your API key stay under your control.

## 3. Adversarial / behavioral (runtime resistance) — the commonly-missing layer

Treat the skill as an untrusted input and test how it behaves under pressure, not just how it
reads:

- **Instruction override** — can `ignore previous` / `new system prompt` hijack it?
- **Role jailbreak** — DAN / hypothetical / fictional-roleplay bypasses.
- **Hidden payload** — base64 / leetspeak / unicode smuggling that decodes to instructions.
- **Authority spoof** — "I'm the developer / admin / platform staff".
- **Reflective injection** — output-as-instruction loops.

Static and semantic checks read the skill *as written*. This dimension asks: **what does it
actually do when an adversary pushes on it at runtime?** That behavioral gap is the one most
skill-audit guidance omits — and the one that matters once the skill is live.

## 4. Supply-chain & provenance (what you're pulling in — including the auditor)

- Parse declared dependencies and check them against a known-vulnerability database
  (e.g. [OSV](https://osv.dev/)) and a typosquat reference list.
- Flag installs from unversioned, unpinned, or unknown-publisher sources — arbitrary
  `git+https://…`, `curl | bash`, floating `latest` tags.
- Prefer versioned, hash-pinned releases from a package registry over "install this URL".

**A principle that applies to the audit tooling itself.** Any tool you install to *perform*
this audit is itself a supply-chain dependency. If a tool asks you to `curl | bash` it, or to
`uvx --from git+https://…` an unvetted, unknown-publisher repo, it fails the very check in
this dimension — no matter how good its findings are. A credible skill-audit implementation
should therefore be installable as a **pinned, hash-verifiable release from a package
registry**, offer a **zero-install path** (hosted or CI-sandboxed) for those who won't add a
third-party tool at all, and ideally **publish an audit of itself**. Verify the auditor, don't
just trust it.

---

## Using this checklist

- **Reviewers:** walk all four dimensions before trusting a skill. A pass on 1–2 is not a
  pass.
- **Docs / rule authors:** you can adopt these dimensions as vendor-neutral guidance without
  endorsing any single tool — that keeps a security-rules document conflict-free.
- **Tool authors:** map your coverage to these dimensions so users can see the gaps.

## Reference implementations

These dimensions are implemented, end to end, by the open-source
[**TAR Engine**](https://github.com/qingxuantang/tar-engine) (static + semantic + adversarial
+ supply-chain, CLI / CI action / MCP tool, pinned PyPI release, BYOK, plus a zero-install
hosted playground). It is one implementation among possible others; the dimensions above are
the standard, not the tool.
