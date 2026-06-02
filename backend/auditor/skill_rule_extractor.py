"""Extract risk rules from SKILL.md content using LLM.

When Engine detects a new skill for the first time, this module reads
the skill's SKILL.md and extracts:
  1. Explicit constraints (numbers, limits, forbidden actions)
  2. Safety requirements
  3. Validation rules

The extracted rules become RealtimeRule entries scoped to that skill,
enabling context-aware risk checking.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx


EXTRACT_RULES_PROMPT = '''你是 AI Agent 安全审计专家。以下是一个 AI skill 的完整定义文件（SKILL.md）。

你的任务：从中提取所有可用于实时风险检测的规则和约束。

提取目标：
1. **数值约束**：任何出现的具体数值限制（如"杠杆 1.0-1.8x"、"单币仓位上限 15%"）
2. **禁止行为**：明确说"不要/禁止/不允许"的操作
3. **验证规则**：需要检查/确认的条件（如"cap_ratios 总和必须等于 1"）
4. **安全要求**：涉及密钥、权限、破坏性操作的约束
5. **领域判断**：这个 skill 属于什么领域（quant/devops/content/general）

对于每条规则，生成一个可以用正则表达式匹配的检测模式。

输出格式（严格 JSON）：
{
  "domain": "quant",
  "rules": [
    {
      "rule_name": "英文下划线命名",
      "description": "规则描述",
      "severity": "warning|high|critical",
      "match_tool": "工具名正则（如 Edit|Write）或空字符串",
      "match_file": "文件路径正则或空字符串",
      "match_content": "内容正则或空字符串",
      "message": "中文告警消息",
      "source_text": "原文中的相关文字（简短引用）"
    }
  ]
}

规则编写指南：
- severity: 会导致资金损失或安全问题 = critical/high，配置偏差 = warning，需确认 = info
- match_tool: 只填与规则相关的工具（Edit|Write, Bash, Read 等），空 = 匹配所有工具
- match_content: 用 Python re 正则语法，要能实际匹配到违规内容
- 宁可少提几条高质量规则，不要生成大量低质量/无法匹配的规则
- 不要提取太泛泛的规则（如"要小心"、"注意安全"）
- 每条规则必须有可执行的 match_content 正则

SKILL 名称: <<<SKILL_NAME>>>
SKILL 内容：
---
<<<CONTENT>>>
---

请直接输出 JSON，不要有其他文字。'''


class SkillRuleExtractor:
    """Extract risk rules from SKILL.md using LLM."""

    def __init__(self):
        self._llm_provider = None
        self._api_key = None
        self._model = None
        self._base_url = None
        self._auto_detect()

    def _auto_detect(self):
        key = os.getenv("OPENAI_API_KEY", "")
        if key and len(key) > 10:
            self._llm_provider = "openai"
            self._api_key = key
            self._model = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")
            self._base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            return
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if key and len(key) > 10:
            self._llm_provider = "anthropic"
            self._api_key = key
            self._model = "claude-haiku-4-5-20251001"
            return
        self._llm_provider = None

    def extract_rules(self, skill_name: str, content: str) -> Dict[str, Any]:
        """Extract rules from SKILL.md content.

        Returns {"domain": str, "rules": List[Dict]}
        """
        if not self._llm_provider:
            print("[skill_rule_extractor] No LLM provider available")
            return {"domain": "general", "rules": []}

        prompt = EXTRACT_RULES_PROMPT.replace("<<<SKILL_NAME>>>", skill_name)
        # Truncate very long skills to stay within context
        truncated = content[:12000] if len(content) > 12000 else content
        prompt = prompt.replace("<<<CONTENT>>>", truncated)

        try:
            response = self._call_llm(prompt)
            return self._parse_response(response)
        except Exception as e:
            print(f"[skill_rule_extractor] Extraction failed for '{skill_name}': {e}")
            return {"domain": "general", "rules": []}

    def _parse_response(self, response: str) -> Dict[str, Any]:
        """Parse LLM response into structured rules."""
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        data = json.loads(text)
        domain = data.get("domain", "general")
        rules = data.get("rules", [])

        # Validate each rule
        valid_rules = []
        for r in rules:
            if not r.get("rule_name") or not r.get("match_content"):
                continue
            # Validate regex compiles
            try:
                if r.get("match_content"):
                    re.compile(r["match_content"])
                if r.get("match_tool"):
                    re.compile(r["match_tool"])
                if r.get("match_file"):
                    re.compile(r["match_file"])
            except re.error:
                continue
            # Normalize severity
            if r.get("severity") not in ("info", "warning", "high", "critical"):
                r["severity"] = "warning"
            valid_rules.append(r)

        return {"domain": domain, "rules": valid_rules}

    def _call_llm(self, prompt: str) -> str:
        if self._llm_provider == "anthropic":
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        elif self._llm_provider == "openai":
            resp = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        raise ValueError(f"No LLM provider: {self._llm_provider}")
