#!/usr/bin/env python3
"""Audit a single SKILL.md and produce a publishable markdown report.

Fetches the SKILL.md content (from URL or local file), submits it to the
TAR Engine audit endpoint, and writes a formatted markdown report.

Usage:
    python3 audit_skill.py --url <SKILL.md URL> --output reports/
    python3 audit_skill.py --file path/to/SKILL.md --output reports/
    python3 audit_skill.py --url ... --engine-url http://localhost:8765 --output ...

Report format (v0.2): 8 sections — header / what this skill does / score
breakdown by category / findings / what we didn't audit / methodology /
limitations / about TAR Engine.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


DEFAULT_ENGINE_URL = "http://localhost:8765"
REPORT_FORMAT_VERSION = "0.2"
DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "zh")


# ── Report localization (chrome strings only; finding content is localized
#    by the engine via the i18n table) ───────────────────────────────────
REPORT_STRINGS = {
    "en": {
        "audited_by": "Audited by",
        "report_format": "Report format",
        "source": "Source",
        "verdict": "Verdict",
        "verdict_critical": "**{risk}** — {n} critical finding{s} block this skill from production use until remediated.",
        "verdict_high": "**{risk}** — {n} high-severity issue{s} need author attention before deploying to a shared environment.",
        "verdict_warning": "**{risk}** — {n} warning{s} worth reviewing, but the skill is likely safe for personal use.",
        "verdict_clean": "**{risk}** — no signature matches across the applied rule set. This is a *clean static pass*; see § What we didn't audit for what that leaves uncovered.",
        "sec_what_does": "What this skill does",
        "auditors_read": "_Auditor's read (LLM-generated):_ {summary}",
        "author_description": "**Author description:** {desc}",
        "observed": "**Observed:** {name} is {first}; {rest}.",
        "no_description": "_No description available for {name}._",
        "frontmatter_facts": "**Frontmatter facts:**",
        "declared_tools": "- **Declared `allowed-tools`:** `{tools}`",
        "body_size": "- **Body size:** {lines} lines / {chars} chars",
        "sec_score_breakdown": "Score breakdown by category",
        "breakdown_intro": "Each category gets its own sub-score. A category with no rule hits gets 100; a category with a single critical finding drops to 80.",
        "th_category": "Category",
        "th_rules_evaluated": "Rules evaluated",
        "th_findings": "Findings",
        "th_max_severity": "Max severity",
        "th_sub_score": "Sub-score",
        "sec_historical": "Historical baseline (same-skill comparison)",
        "first_audit_msg": "This is the **first recorded audit** for this skill identity (hashed from name + description). The baseline section will show mean / stddev / trend after 2+ audits accumulate.",
        "prior_audits": "- **Prior audits on record:** {n} (first {first}, most recent prior {last})",
        "score_stats": "- **Score statistics:** mean {mean} ± {sd} (range {min}–{max}){band}",
        "this_vs_last": "- **This audit vs last:** {delta} ({icon} {trend})",
        "out_of_band": "- **Out-of-band notice:** this score is outside the skill's historical normal band — worth a closer read.",
        "recurring_header": "- **Top recurring findings across history:**",
        "recurring_row": "  - `{rid}` — hit in {ct} of {n} prior audits ({pct}%)",
        "baseline_note": "_Baseline assumes the skill's name + description haven't changed. A rename or rewrite starts a fresh baseline._",
        "sec_findings": "Findings",
        "findings_intro": "**{n}** rule{s} matched. Each finding below cites the matched line and a remediation hint.",
        "zero_findings": "**Zero rule hits** across the applied rule set.",
        "every_cat_evaluated": "Every category below was evaluated and produced no finding. This is a *clean static pass*. Categories evaluated:",
        "cat_line_clean": "- {cat} — {n} rule{s} ✓",
        "finding_card_h3": "{i}. {icon} `{rid}` — {name} ({sev_upper})",
        "f_category": "- **Category:** {cat}",
        "f_why_matched": "- **Why this matched:** {msg}",
        "f_rule_intent": "- **Rule intent:** {desc}",
        "f_matches_in_doc": "- **Matches in document:** {n}",
        "evidence_header": "**Evidence ({shown} of {total} match{es}):**",
        "line_label": "_Line {n}:_",
        "suggested_fix": "**Suggested fix:** {fix}",
        "no_fix_template": "_(no remediation template registered for this rule)_",
        "sec_didnt_audit": "What we didn't audit",
        "didnt_intro": "Static audit is fast but not exhaustive. This run did **not** check:",
        "didnt_runtime": "- **Runtime behavior.** We didn't execute the skill in a sandbox. Dynamic prompt construction, runtime branching, and model-dependent tool use go undetected.",
        "didnt_chains": "- **Cross-skill chains.** When this skill is chained with others (e.g. via TAR Engine's planner), emergent behavior from skill-to-skill state flow isn't analyzed.",
        "didnt_external": "- **External dependencies.** If the skill instructs the LLM to download and execute a script from a URL, we flag the pipe-to-shell pattern but don't inspect the remote payload itself.",
        "didnt_semantic": "- **Semantic intent.** Our rules are pattern-based. A skill written to be polite but reach the same outcome as a critical-flagged one would pass; this is the static-vs-dynamic tradeoff.",
        "didnt_jailbreak": "- **LLM-side jailbreaks.** Resilience against adversarial prompts delivered AT this skill (e.g. through user input it passes to the model) is not in scope here.",
        "sec_methodology": "Methodology",
        "method_header": "**How the score was computed:**",
        "method_step1": "1. Document text is scanned against a static rule set of {n} signature patterns. Each rule carries a permanent `rule_id` (e.g. `PI-001`), a category, a severity, and a remediation template.",
        "method_step2": "2. Each rule hit deducts from a 100-point base: critical -20, high -10, warning -5, info -1.",
        "method_step3": "3. The letter grade is gated by max severity AND total score: any critical → F; any high → at most D; any warning → at most C; otherwise A/B by score band.",
        "method_step4": "4. Per-category sub-scores apply the same deduction formula to that category's findings only — so you can see WHICH risk surface drove the loss.",
        "provenance_header": "**Engine + rule set provenance:**",
        "engine_version": "- Engine version: `{v}`",
        "rule_set_version": "- Rule set version: `{v}`",
        "commit": "- Commit: `{v}`",
        "domain_config": "- Domain config: `{v}`",
        "audited_at": "- Audited at: `{v}`",
        "rules_applied": "- Rules applied: {n} static rules (full registry below)",
        "registry_summary": "Full rule registry applied to this audit",
        "th_rule_id": "Rule ID",
        "th_name": "Name",
        "th_severity": "Severity",
        "sec_limitations": "Known limitations of this report",
        "lim_false_positives": "- **False positives are possible.** A SKILL.md *documenting* a dangerous pattern (e.g. an audit skill explaining `curl | sh`) will match the rule even though the skill's intent is to detect, not execute. Read the matched lines before reacting.",
        "lim_false_negatives": "- **False negatives are guaranteed in narrow ways.** Patterns obfuscated by string concatenation, environment variable indirection, or non-English equivalents will slip past regex.",
        "lim_baseline": "- **Baseline sample size.** Same-skill trend analysis (§ Historical baseline) gets meaningful with n≥3 prior audits. With fewer priors the stddev band is widened to avoid false out-of-band signals.",
        "sec_about": "About TAR Engine",
        "about_blurb": "TAR Engine is an OSS \"wish machine\" with built-in audit. Speak a goal; the engine plans, runs and audits skills inside its own container. BYOK. — [github.com/qingxuantang/tar-engine](https://github.com/qingxuantang/tar-engine)",
        "risk_class_critical": "Critical risk",
        "risk_class_high": "High risk",
        "risk_class_medium": "Medium risk",
        "risk_class_low": "Low risk",
    },
    "zh": {
        "audited_by": "审计来自",
        "report_format": "报告格式",
        "source": "来源",
        "verdict": "判定",
        "verdict_critical": "**{risk}** — {n} 个严重问题，必须修复后才能上生产。",
        "verdict_high": "**{risk}** — {n} 个高危问题，部署到共享环境前作者需要处理。",
        "verdict_warning": "**{risk}** — {n} 个 warning 建议审视，个人使用基本安全。",
        "verdict_clean": "**{risk}** — 当前规则集没有任何匹配。这是一次*干净的静态通过*；具体覆盖范围请看 § 我们没审计什么。",
        "sec_what_does": "这个 skill 做什么",
        "auditors_read": "_审计员视角（LLM 生成）：_ {summary}",
        "author_description": "**作者描述：** {desc}",
        "observed": "**观察：** {name} 是{first}；{rest}。",
        "no_description": "_{name} 没有可用描述。_",
        "frontmatter_facts": "**Frontmatter 信息：**",
        "declared_tools": "- **声明的 `allowed-tools`：** `{tools}`",
        "body_size": "- **正文规模：** {lines} 行 / {chars} 字符",
        "sec_score_breakdown": "按类别分项打分",
        "breakdown_intro": "每个类别独立计分。没有任何 rule 命中的类别为 100；命中 1 个 critical 的类别降到 80。",
        "th_category": "类别",
        "th_rules_evaluated": "评估规则数",
        "th_findings": "命中数",
        "th_max_severity": "最高严重度",
        "th_sub_score": "分项得分",
        "sec_historical": "历史 baseline（同 skill 对比）",
        "first_audit_msg": "这是该 skill 身份（name + description 的 hash）下的**首次审计**。累积 2+ 次后这里会显示 mean / stddev / trend 等趋势信息。",
        "prior_audits": "- **历史审计次数：** {n} 次（最早 {first}，最近一次 {last}）",
        "score_stats": "- **分数统计：** 均值 {mean} ± {sd}（范围 {min}–{max}）{band}",
        "this_vs_last": "- **本次 vs 上次：** {delta}（{icon} {trend}）",
        "out_of_band": "- **超出正常区间提示：** 本次分数已经在该 skill 历史正常带宽之外——建议仔细复核。",
        "recurring_header": "- **历史重复命中规则：**",
        "recurring_row": "  - `{rid}` — {n} 次审计中命中 {ct} 次（{pct}%）",
        "baseline_note": "_Baseline 假设 skill 的 name + description 没变。改名 / 改 description 会重新建 baseline。_",
        "sec_findings": "审计发现",
        "findings_intro": "**{n}** 条规则命中。每条 finding 含命中行号 + 上下文证据 + 修复建议。",
        "zero_findings": "**0 条规则命中**。",
        "every_cat_evaluated": "下列每个类别都被评估过，没有任何 finding。这是一次*干净的静态通过*。已评估类别：",
        "cat_line_clean": "- {cat} — {n} 条规则 ✓",
        "finding_card_h3": "{i}. {icon} `{rid}` — {name}（{sev_upper}）",
        "f_category": "- **类别：** {cat}",
        "f_why_matched": "- **匹配原因：** {msg}",
        "f_rule_intent": "- **规则意图：** {desc}",
        "f_matches_in_doc": "- **文档中匹配次数：** {n}",
        "evidence_header": "**证据（展示 {shown} / 共 {total} 处匹配）：**",
        "line_label": "_第 {n} 行：_",
        "suggested_fix": "**修复建议：** {fix}",
        "no_fix_template": "_（该规则未注册修复模板）_",
        "sec_didnt_audit": "我们没审计什么",
        "didnt_intro": "静态审计快但不全面。这次扫描**没有**覆盖：",
        "didnt_runtime": "- **运行时行为。** 我们没在沙箱中实际执行该 skill。动态 prompt 构造 / 运行时分支 / 模型依赖的工具调用都没被捕获。",
        "didnt_chains": "- **跨 skill 链路。** 当该 skill 跟其他 skill 串联使用（例如通过 TAR Engine planner）时，skill 间状态流转产生的涌现行为不在分析范围。",
        "didnt_external": "- **外部依赖内容。** 如果 skill 指示 LLM 下载并执行远程脚本，我们会标记 pipe-to-shell 模式，但不会检查远程 payload 本身。",
        "didnt_semantic": "- **语义意图。** 我们的规则是基于模式的。一个写得礼貌但能达成跟 critical 同样后果的 skill 会过审——这是静态审计 vs 动态审计的固有取舍。",
        "didnt_jailbreak": "- **LLM 侧越狱。** 针对该 skill 本身的对抗性 prompt 攻击（例如通过它传给模型的用户输入）不在本审计范围。",
        "sec_methodology": "方法学",
        "method_header": "**分数是怎么算出来的：**",
        "method_step1": "1. 文档被扫描通过 {n} 条静态规则的签名模式。每条规则有永久 `rule_id`（例如 `PI-001`）、类别、严重度、修复模板。",
        "method_step2": "2. 每次规则命中从 100 分基数中扣分：critical -20，high -10，warning -5，info -1。",
        "method_step3": "3. 字母等级由最高严重度 + 总分双重 gate：有 critical → F；有 high → 最高 D；有 warning → 最高 C；否则按分数 A/B 分档。",
        "method_step4": "4. 每个类别的子分用同样的扣分公式，但只统计该类别下的 finding——所以你能看到**哪个风险面**导致了主要扣分。",
        "provenance_header": "**Engine 与规则集 provenance：**",
        "engine_version": "- Engine 版本：`{v}`",
        "rule_set_version": "- 规则集版本：`{v}`",
        "commit": "- Commit：`{v}`",
        "domain_config": "- Domain 配置：`{v}`",
        "audited_at": "- 审计时间：`{v}`",
        "rules_applied": "- 应用了 {n} 条静态规则（完整 registry 见下）",
        "registry_summary": "本次审计应用的完整规则 registry",
        "th_rule_id": "Rule ID",
        "th_name": "名称",
        "th_severity": "严重度",
        "sec_limitations": "本报告已知局限",
        "lim_false_positives": "- **可能有误报。** 如果一个 SKILL.md 是在*文档化*一个危险模式（例如审计 skill 解释 `curl | sh` 的原理），它仍然会匹配规则即使该 skill 意图是检测而非执行。看到 finding 先读匹配行再反应。",
        "lim_false_negatives": "- **必然有漏报（在某些范围）。** 用字符串拼接、环境变量间接引用、或非英语等价表述混淆的模式会绕过 regex。",
        "lim_baseline": "- **Baseline 样本量。** 同 skill 趋势分析（§ 历史 baseline）在 n≥3 次审计后才有意义。少于 3 次时 stddev 区间会主动加宽以避免误判超出范围。",
        "sec_about": "关于 TAR Engine",
        "about_blurb": "TAR Engine 是一个 OSS 「许愿机」，内置审计能力。说出目标，引擎在自己的容器里 plan、运行并审计 skill。BYOK。— [github.com/qingxuantang/tar-engine](https://github.com/qingxuantang/tar-engine)",
        "risk_class_critical": "严重风险",
        "risk_class_high": "高风险",
        "risk_class_medium": "中等风险",
        "risk_class_low": "低风险",
    },
}


def t(key: str, lang: str, **fmt) -> str:
    """Lookup + format helper for report strings."""
    bundle = REPORT_STRINGS.get(lang) or REPORT_STRINGS[DEFAULT_LANG]
    s = bundle.get(key) or REPORT_STRINGS[DEFAULT_LANG].get(key, key)
    return s.format(**fmt) if fmt else s


def fetch_url(url: str, timeout: float = 15.0) -> str:
    """Fetch the contents of a URL. Raises on HTTP error."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "TAR Engine Audit Content Engine/0.2.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_frontmatter(skill_text: str) -> dict:
    """Pull the YAML-ish frontmatter block from a SKILL.md and parse it.

    Returns a dict of fields. Tolerates missing/malformed frontmatter (returns {}).
    Only flat string values are supported (the common SKILL.md case).
    """
    m = re.search(r"^---\s*\n(.*?)\n---", skill_text, re.MULTILINE | re.DOTALL)
    if not m:
        return {}
    fm = m.group(1)
    parsed: dict = {}
    for line in fm.splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if kv:
            key, val = kv.group(1), kv.group(2)
            # Strip surrounding quotes if present
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            parsed[key] = val
    return parsed


def extract_skill_body(skill_text: str) -> str:
    """Return the SKILL.md content after the frontmatter block."""
    m = re.search(r"^---\s*\n.*?\n---\s*\n(.*)$", skill_text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else skill_text


_HEURISTIC_OBS = {
    "en": {
        "sections_fmt": "{n} top-level sections ({names}{more})",
        "more": ", …",
        "lines_fmt": "~{n} lines of instructions",
        "scripts": "delegates to packaged scripts",
        "curl": "makes outbound network calls",
        "dense": "dense body",
        "concise": "concise body",
    },
    "zh": {
        "sections_fmt": "{n} 个一级章节（{names}{more}）",
        "more": "，…",
        "lines_fmt": "约 {n} 行指令",
        "scripts": "委托给打包脚本",
        "curl": "有对外网络调用",
        "dense": "正文较密集",
        "concise": "正文简洁",
    },
}


def summarize_skill_heuristic(skill_text: str, frontmatter: dict, lang: str = DEFAULT_LANG) -> str:
    """Build a brief 'what this skill does' paragraph WITHOUT an LLM.

    Uses frontmatter description + structural heuristics from the body.
    This is the fallback when no BYOK LLM is available.
    """
    description = (frontmatter.get("description") or "").strip()
    name = (frontmatter.get("name") or "this skill").strip()
    body = extract_skill_body(skill_text)

    body_lines = body.count("\n")
    body_chars = len(body)
    has_scripts = "run_bash" in body.lower() or "scripts/" in body.lower() or "./script" in body.lower()
    has_curl = bool(re.search(r"\bcurl\b|\bwget\b|requests\.|httpx\.", body))
    section_headers = re.findall(r"^##\s+(.+?)$", body, re.MULTILINE)

    h = _HEURISTIC_OBS.get(lang, _HEURISTIC_OBS[DEFAULT_LANG])

    summary = []
    if description:
        summary.append(t("author_description", lang, desc=description))

    obs_parts = []
    if section_headers:
        more = h["more"] if len(section_headers) > 5 else ""
        obs_parts.append(h["sections_fmt"].format(
            n=len(section_headers),
            names=", ".join(s.strip() for s in section_headers[:5]),
            more=more,
        ))
    obs_parts.append(h["lines_fmt"].format(n=body_lines))
    if has_scripts:
        obs_parts.append(h["scripts"])
    if has_curl:
        obs_parts.append(h["curl"])
    if body_chars > 0:
        density_key = "dense" if body_chars / max(body_lines, 1) > 80 else "concise"
        obs_parts.append(h[density_key])

    if obs_parts:
        summary.append(t("observed", lang, name=name,
                         first=obs_parts[0], rest="，".join(obs_parts[1:]) if lang == "zh" else ", ".join(obs_parts[1:])))
    return "\n\n".join(summary) if summary else t("no_description", lang, name=name)


def summarize_skill_llm(skill_text: str, frontmatter: dict,
                       api_key: Optional[str], base_url: Optional[str],
                       model: Optional[str], lang: str = DEFAULT_LANG) -> Optional[str]:
    """Optional LLM-powered skill summary (BYOK).

    Returns None when no API key is configured — caller should fall back to
    the heuristic summary. Failures here never block the report.
    """
    if not api_key:
        return None
    try:
        # Lazy import: openai client is in the engine env, but the audit script
        # may run elsewhere — fall back cleanly if missing.
        from openai import OpenAI  # type: ignore
    except ImportError:
        return None

    name = frontmatter.get("name") or "this skill"
    description = frontmatter.get("description") or ""

    if lang == "zh":
        system = (
            "你是 AI skill 审计员。给定一份 SKILL.md，用 2-3 句中文做行为摘要："
            "重点描述 LLM 被指示做什么、会调用哪些工具、会产出什么。"
            "不要复述作者的营销语气，要技术、直接、克制。"
        )
        user = (
            f"Skill 名: {name}\n"
            f"作者描述: {description}\n\n"
            f"SKILL.md 全文：\n```\n{skill_text[:8000]}\n```\n\n"
            "写一段 2-3 句的行为摘要（中文）。"
        )
    else:
        system = (
            "You are an expert AI skill auditor. Given a SKILL.md file, produce a "
            "concise 2-3 sentence summary of what the skill actually does, focused on "
            "BEHAVIOR (what the LLM is instructed to do, what tools it touches, what "
            "outputs it produces). Skip the author marketing tone. Be technical and direct."
        )
        user = (
            f"Skill name: {name}\n"
            f"Author description: {description}\n\n"
            f"SKILL.md full text:\n```\n{skill_text[:8000]}\n```\n\n"
            "Write a 2-3 sentence behavioral summary."
        )
    try:
        client = OpenAI(api_key=api_key, base_url=base_url or None)
        resp = client.chat.completions.create(
            model=model or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        print(f"⚠ LLM summary failed: {e}", file=sys.stderr)
        return None


def call_audit_endpoint(engine_url: str, skill_text: str, domain: str = "general",
                        lang: str = DEFAULT_LANG) -> dict:
    """Call TAR Engine's static audit endpoint."""
    url = engine_url.rstrip("/") + "/api/cockpit/audit/static"
    body = json.dumps({"skill_text": skill_text, "domain": domain, "lang": lang}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
        except Exception:
            err = {"status_code": e.code, "reason": e.reason}
        return {"success": False, "error": err}


def grade_to_badge(grade: str, score: float) -> str:
    """Map letter grade + score to an emoji badge."""
    badges = {
        "A": "🟢 A",
        "B": "🟢 B",
        "C": "🟡 C",
        "D": "🟠 D",
        "F": "🔴 F",
    }
    return badges.get(grade, f"⚫ {grade}")


def severity_icon(sev: str) -> str:
    return {
        "critical": "🔴",
        "high": "🟠",
        "warning": "🟡",
        "info": "🔵",
        "none": "⚪",
    }.get(sev, "⚪")


def _risk_class_localized(risk_class: str, lang: str) -> str:
    """Map server risk_class string to localized form."""
    mapping = {
        "Critical": t("risk_class_critical", lang),
        "High": t("risk_class_high", lang),
        "Medium": t("risk_class_medium", lang),
        "Low": t("risk_class_low", lang),
    }
    return mapping.get(risk_class, risk_class)


def format_report_markdown(
    skill_name: str,
    source_url: Optional[str],
    frontmatter: dict,
    skill_summary: str,
    audit_result: dict,
    body_metrics: dict,
    lang: str = DEFAULT_LANG,
) -> str:
    """Render the audit result as a publishable markdown post (v0.2, 8 sections)."""
    score = audit_result.get("score", 0)
    grade = audit_result.get("grade", "?")
    risk_class_loc = _risk_class_localized(
        audit_result.get("risk_class", "Unknown"), lang
    )
    sev_counts = audit_result.get("severity_counts", {})
    breakdown = audit_result.get("score_breakdown_by_category", {})
    findings = audit_result.get("findings", [])
    rules_applied = audit_result.get("rules_applied", [])
    audit_meta = audit_result.get("audit_meta", {})
    badge = grade_to_badge(grade, score)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d")

    md = []

    # ── Section 1: Header + verdict ────────────────────────────────
    if lang == "zh":
        title_prefix = "审计报告"
    else:
        title_prefix = "Audit Report"
    md.append(f"# {title_prefix}: `{skill_name}` — {badge} ({score}/100)")
    md.append("")
    md.append(
        f"*{t('audited_by', lang)} [TAR Engine](https://github.com/qingxuantang/tar-engine)"
        f" · {timestamp} · {t('report_format', lang)} v{REPORT_FORMAT_VERSION}*"
    )
    md.append("")
    if source_url:
        md.append(f"**{t('source', lang)}:** [`{source_url}`]({source_url})")
        md.append("")
    crit = sev_counts.get("critical", 0)
    high = sev_counts.get("high", 0)
    warn = sev_counts.get("warning", 0)
    if crit:
        s_suffix = "s" if (crit > 1 and lang == "en") else ""
        verdict = t("verdict_critical", lang, risk=risk_class_loc, n=crit, s=s_suffix)
    elif high:
        s_suffix = "s" if (high > 1 and lang == "en") else ""
        verdict = t("verdict_high", lang, risk=risk_class_loc, n=high, s=s_suffix)
    elif warn:
        s_suffix = "s" if (warn > 1 and lang == "en") else ""
        verdict = t("verdict_warning", lang, risk=risk_class_loc, n=warn, s=s_suffix)
    else:
        verdict = t("verdict_clean", lang, risk=risk_class_loc)
    md.append(f"**{t('verdict', lang)}:** {verdict}")
    md.append("")

    # ── Section 2: What this skill does ────────────────────────────
    md.append(f"## {t('sec_what_does', lang)}")
    md.append("")
    md.append(skill_summary)
    md.append("")
    fm_extras = []
    allowed = frontmatter.get("allowed-tools") or frontmatter.get("allowed_tools")
    if allowed:
        fm_extras.append(t("declared_tools", lang, tools=allowed))
    if body_metrics.get("body_lines"):
        fm_extras.append(t("body_size", lang,
                           lines=body_metrics["body_lines"],
                           chars=body_metrics["body_chars"]))
    if fm_extras:
        md.append(t("frontmatter_facts", lang))
        md.append("")
        md.extend(fm_extras)
        md.append("")

    # ── Section 3: Score breakdown by category ─────────────────────
    md.append(f"## {t('sec_score_breakdown', lang)}")
    md.append("")
    md.append(t("breakdown_intro", lang))
    md.append("")
    md.append(f"| {t('th_category', lang)} | {t('th_rules_evaluated', lang)} "
              f"| {t('th_findings', lang)} | {t('th_max_severity', lang)} "
              f"| {t('th_sub_score', lang)} |")
    md.append("|---|---:|---:|:---:|---:|")
    for cat_key, cat in breakdown.items():
        max_sev = cat.get("max_severity", "none")
        sev_cell = f"{severity_icon(max_sev)} {max_sev}"
        md.append(
            f"| {cat['category_display']} | {cat['rules_evaluated']} | "
            f"{cat['findings_count']} | {sev_cell} | {cat['score']}/100 |"
        )
    md.append("")

    # ── Section 3.5: Historical baseline (same-skill trend, C1) ───
    baseline = audit_result.get("historical_baseline")
    md.append(f"## {t('sec_historical', lang)}")
    md.append("")
    if not baseline or baseline.get("trend") == "first_audit" or baseline.get("n_prior_audits", 0) == 0:
        md.append(t("first_audit_msg", lang))
        md.append("")
    else:
        n = baseline.get("n_prior_audits", 0)
        stats = baseline.get("score_stats") or {}
        trend = baseline.get("trend", "stable")
        delta = baseline.get("delta_vs_last")
        in_band = baseline.get("in_normal_band")
        recurring = baseline.get("top_recurring_rules") or []
        first_at = baseline.get("first_audit_at")
        last_at = baseline.get("last_prior_audit_at")
        trend_icon = {"improved": "📈", "stable": "➡️", "regressed": "📉", "first_audit": "🆕"}.get(trend, "ℹ️")
        # Localized trend word
        trend_word = {
            "improved": "improved" if lang == "en" else "上升",
            "stable":   "stable"   if lang == "en" else "稳定",
            "regressed":"regressed" if lang == "en" else "下降",
            "first_audit": "first audit" if lang == "en" else "首次审计",
        }.get(trend, trend)
        band_str = ""
        if stats.get("stddev") is not None:
            band_lo = round(stats["mean"] - max(stats.get("stddev", 0), 3), 1)
            band_hi = round(stats["mean"] + max(stats.get("stddev", 0), 3), 1)
            if lang == "zh":
                band_str = f"（正常区间 {band_lo} – {band_hi}）"
            else:
                band_str = f" (normal band: {band_lo} – {band_hi})"
        md.append(t("prior_audits", lang, n=n, first=first_at, last=last_at))
        if stats:
            md.append(t("score_stats", lang,
                        mean=stats.get('mean'), sd=stats.get('stddev'),
                        min=stats.get('min'), max=stats.get('max'),
                        band=band_str))
        if delta is not None:
            delta_str = f"{'+' if delta > 0 else ''}{delta}"
            md.append(t("this_vs_last", lang, delta=delta_str, icon=trend_icon, trend=trend_word))
        if in_band is False:
            md.append(t("out_of_band", lang))
        if recurring:
            md.append(t("recurring_header", lang))
            for r in recurring[:5]:
                md.append(t("recurring_row", lang,
                            rid=r['rule_id'], ct=r['hit_count'],
                            n=n, pct=r['hit_rate_pct']))
        md.append("")
        md.append(t("baseline_note", lang))
        md.append("")

    # ── Section 4: Findings (rich cards) ──────────────────────────
    md.append(f"## {t('sec_findings', lang)}")
    md.append("")
    if findings:
        s_suffix = "s" if (len(findings) > 1 and lang == "en") else ""
        md.append(t("findings_intro", lang, n=len(findings), s=s_suffix))
        md.append("")
        for i, f in enumerate(findings, 1):
            sev = f.get("severity", "info")
            sev_upper = sev.upper() if lang == "en" else {
                "critical": "严重", "high": "高", "warning": "警告", "info": "提示"
            }.get(sev, sev.upper())
            rule_id = f.get("rule_id", "?")
            rule_name = f.get("rule_name", "?")
            category_display = f.get("category_display", f.get("category", "?"))
            message = f.get("message", "")
            description = f.get("description") or ""
            fix = f.get("fix_template") or t("no_fix_template", lang)
            hits = f.get("hits", [])
            md.append("### " + t("finding_card_h3", lang,
                                  i=i, icon=severity_icon(sev),
                                  rid=rule_id, name=rule_name,
                                  sev_upper=sev_upper))
            md.append("")
            md.append(t("f_category", lang, cat=category_display))
            md.append(t("f_why_matched", lang, msg=message))
            if description:
                md.append(t("f_rule_intent", lang, desc=description))
            md.append(t("f_matches_in_doc", lang, n=f.get('match_count', 0)))
            md.append("")
            if hits:
                total = f.get('match_count', len(hits))
                shown = min(len(hits), 3)
                es = "es" if (total > 1 and lang == "en") else ""
                md.append(t("evidence_header", lang, shown=shown, total=total, es=es))
                md.append("")
                for h in hits:
                    md.append(t("line_label", lang, n=h['line_number']))
                    md.append("```")
                    md.append(h.get("excerpt") or h.get("line_text", ""))
                    md.append("```")
                    md.append("")
            md.append(t("suggested_fix", lang, fix=fix))
            md.append("")
    else:
        md.append(t("zero_findings", lang))
        md.append("")
        md.append(t("every_cat_evaluated", lang))
        md.append("")
        for cat_key, cat in breakdown.items():
            s_suffix = "s" if (cat['rules_evaluated'] > 1 and lang == "en") else ""
            md.append(t("cat_line_clean", lang,
                        cat=cat['category_display'],
                        n=cat['rules_evaluated'], s=s_suffix))
        md.append("")

    # ── Section 5: What we didn't audit ───────────────────────────
    md.append(f"## {t('sec_didnt_audit', lang)}")
    md.append("")
    md.append(t("didnt_intro", lang))
    md.append("")
    md.append(t("didnt_runtime", lang))
    md.append(t("didnt_chains", lang))
    md.append(t("didnt_external", lang))
    md.append(t("didnt_semantic", lang))
    md.append(t("didnt_jailbreak", lang))
    md.append("")

    # ── Section 6: Methodology ────────────────────────────────────
    md.append(f"## {t('sec_methodology', lang)}")
    md.append("")
    md.append(t("method_header", lang))
    md.append("")
    md.append(t("method_step1", lang, n=audit_meta.get('rule_count', '?')))
    md.append(t("method_step2", lang))
    md.append(t("method_step3", lang))
    md.append(t("method_step4", lang))
    md.append("")
    md.append(t("provenance_header", lang))
    md.append("")
    md.append(t("engine_version", lang, v=audit_meta.get('engine_version', '?')))
    md.append(t("rule_set_version", lang, v=audit_meta.get('rule_set_version', '?')))
    md.append(t("commit", lang, v=audit_meta.get('commit_sha', '?')))
    md.append(t("domain_config", lang, v=audit_meta.get('domain', '?')))
    md.append(t("audited_at", lang, v=audit_meta.get('audited_at', '?')))
    md.append(t("rules_applied", lang, n=len(rules_applied)))
    md.append("")
    md.append("<details>")
    md.append(f"<summary>{t('registry_summary', lang)}</summary>")
    md.append("")
    md.append(f"| {t('th_rule_id', lang)} | {t('th_name', lang)} "
              f"| {t('th_category', lang)} | {t('th_severity', lang)} |")
    md.append("|---|---|---|:---:|")
    for r in rules_applied:
        md.append(f"| `{r['rule_id']}` | {r['rule_name']} | "
                  f"{r['category']} | {r['severity']} |")
    md.append("")
    md.append("</details>")
    md.append("")

    # ── Section 7: Limitations ────────────────────────────────────
    md.append(f"## {t('sec_limitations', lang)}")
    md.append("")
    md.append(t("lim_false_positives", lang))
    md.append(t("lim_false_negatives", lang))
    md.append(t("lim_baseline", lang))
    md.append("")

    # ── Section 8: About TAR Engine (compact CTA) ─────────────────
    md.append("---")
    md.append("")
    md.append(f"## {t('sec_about', lang)}")
    md.append("")
    md.append(t("about_blurb", lang))
    md.append("")

    return "\n".join(md)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="URL to the SKILL.md")
    src.add_argument("--file", help="Local path to SKILL.md")
    parser.add_argument("--output", required=True, help="Output directory for the report")
    parser.add_argument("--engine-url", default=DEFAULT_ENGINE_URL,
                        help=f"TAR Engine base URL (default: {DEFAULT_ENGINE_URL})")
    parser.add_argument("--domain", default="general", help="Audit domain (default: general)")
    parser.add_argument("--lang", default=DEFAULT_LANG, choices=SUPPORTED_LANGS,
                        help=f"Report language (default: {DEFAULT_LANG})")
    parser.add_argument("--name-override", help="Override the skill name extracted from frontmatter")
    parser.add_argument("--no-llm-summary", action="store_true",
                        help="Force heuristic skill summary even if OPENAI_API_KEY is set")
    args = parser.parse_args()
    lang = args.lang

    # Fetch skill text
    if args.url:
        print(f"Fetching {args.url} ...", file=sys.stderr)
        try:
            skill_text = fetch_url(args.url)
        except Exception as e:
            print(f"Fetch failed: {e}", file=sys.stderr)
            return 1
        source_url = args.url
    else:
        skill_text = Path(args.file).read_text(encoding="utf-8")
        source_url = None

    frontmatter = parse_frontmatter(skill_text)
    skill_name = (args.name_override or frontmatter.get("name") or "unnamed-skill").strip()
    print(f"Auditing skill: {skill_name} (lang={lang})", file=sys.stderr)

    body = extract_skill_body(skill_text)
    body_metrics = {
        "body_lines": body.count("\n"),
        "body_chars": len(body),
    }

    # Skill summary: try LLM (BYOK), fall back to heuristic
    llm_summary = None
    if not args.no_llm_summary:
        llm_summary = summarize_skill_llm(
            skill_text=skill_text,
            frontmatter=frontmatter,
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
            model=os.environ.get("OPENAI_MODEL"),
            lang=lang,
        )
    heuristic = summarize_skill_heuristic(skill_text, frontmatter, lang=lang)
    if llm_summary:
        skill_summary = t("auditors_read", lang, summary=llm_summary) + "\n\n" + heuristic
    else:
        skill_summary = heuristic

    # Call audit endpoint
    result = call_audit_endpoint(args.engine_url, skill_text, domain=args.domain, lang=lang)
    if not result.get("success", True) and "error" in result:
        print(f"Audit failed: {json.dumps(result['error'], indent=2)}", file=sys.stderr)
        return 1

    # Render report
    report_md = format_report_markdown(
        skill_name=skill_name,
        source_url=source_url,
        frontmatter=frontmatter,
        skill_summary=skill_summary,
        audit_result=result,
        body_metrics=body_metrics,
        lang=lang,
    )

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", skill_name)
    out_path = out_dir / f"{safe_name}-{datetime.utcnow().strftime('%Y%m%d')}.md"
    out_path.write_text(report_md, encoding="utf-8")

    print(f"✅ Report written: {out_path}", file=sys.stderr)
    print(out_path)  # stdout: the path, for piping
    return 0


if __name__ == "__main__":
    sys.exit(main())
