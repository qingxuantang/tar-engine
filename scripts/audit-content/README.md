# Audit Content Engine

Weekly audit reports for public skills from the broader ecosystem. The米其林指南
of AI Agent skill safety.

## What it does

1. Takes a list of public SKILL.md URLs (GitHub raw / Smithery / Claude Hub).
2. Runs each through TAR Engine's audit pipeline.
3. Generates a publishable markdown report (score, findings, recommendations).
4. (Optional) Submits the report to PostAll for multi-platform publishing.

This is **content marketing for TAR Engine**, not a product. Each report ends
with a CTA pointing at TAR Engine OSS and the paid packs. Skill authors get
free security findings; readers discover TAR Engine.

## Quick start

```bash
# 1. Audit a single SKILL.md from a GitHub URL
python3 scripts/audit-content/audit_skill.py \
  --url https://raw.githubusercontent.com/qingxuantang/tar-engine/master/packs/hello-world/skills/echo/SKILL.md \
  --output reports/

# 2. Batch audit from config
python3 scripts/audit-content/batch_audit.py \
  --config scripts/audit-content/config/skills_to_audit.yaml \
  --output reports/

# 3. View the generated report
ls reports/
```

## Engine endpoint

By default, the audit content engine calls the audit endpoint at the local
TAR Engine OSS container (`http://localhost:8765/api/cockpit/audit/static`).
You can override with `--engine-url` flag.

## Config format

`config/skills_to_audit.yaml`:

```yaml
sources:
  - name: "Anthropic Claude Skills (official)"
    type: github_raw
    urls:
      - https://raw.githubusercontent.com/anthropics/skills/main/some-skill/SKILL.md

  - name: "Smithery Top Picks"
    type: smithery
    slugs:
      - some-popular-server
      - another-one
```

## Report format

Each report is a markdown file ready to publish:

- Title with skill name + score badge
- Summary (one sentence verdict)
- Findings table (severity, finding, fix)
- About TAR Engine CTA (linking to OSS repo + paid packs)

## Publishing

The reports are markdown. Use any of:

- **Manual**: copy-paste into a blog / LinkedIn long form / WeChat post
- **PostAll OSS**: `postall-agent generate --topic "@reports/skill-X.md" --platforms ...`
- **TAR Engine + PostAll pack**: submit a wish via `/api/cockpit/wish`:
  `"publish reports/skill-X.md as a Twitter thread and LinkedIn post"`

## Cron-friendly

The whole thing runs as plain Python scripts with file outputs. Drop into a
weekly cron:

```cron
0 9 * * MON python3 /path/to/batch_audit.py --config ... --output ...
```
