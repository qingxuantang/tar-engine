"""Extract execution signatures from parsed skill steps.

Given the output of skill_parser (list of steps with names, descriptions,
commands, scripts), this module generates "signatures" for each node:
regex patterns that identify when a runtime tool_call belongs to that node.

Two modes:
  1. LLM-based: Send steps to LLM, get rich signatures (one-time, cached)
  2. Rule-based fallback: Extract file paths and commands from step fields

The LLM also performs node normalization (approach C+A):
  - Uses SKILL.md structure as base (A)
  - Merges overly granular steps, splits overly coarse ones (C)
  - Targets 8-30 meaningful business nodes per skill
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


CACHE_DIR = Path(os.getenv("ENGINE_HOME", Path.home() / ".engine")) / "signature_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EXTRACT_PROMPT = '''你是工作流分析专家。以下是一个 AI skill 的步骤列表（从 SKILL.md 解析）。

你的任务：
1. 将这些步骤归纳为 8-30 个有意义的业务节点（不是 Read/Edit 这种原子操作）
2. 对于每个节点，生成"执行签名"——当 AI 在运行时执行工具调用时，什么模式表明它正在执行这个节点

归纳规则：
- 如果多个步骤是同一个业务动作的细分（如"读取因子A"、"读取因子B"），合并为一个节点（"因子扫描"）
- 如果一个步骤包含多个独立业务动作，拆分
- 节点名称要简洁、有业务含义（"因子扫描"、"回测执行"、"仓位计算"），不要技术名称
- 保持原有的依赖顺序

输出格式（严格 JSON）：
{
  "nodes": [
    {
      "id": "node_1",
      "name": "节点名称（业务语言）",
      "description": "这个节点做什么",
      "original_step_ids": ["step_1", "step_2"],
      "signatures": {
        "file_patterns": ["factors/.*\\\\.py", "config/factor_.*"],
        "command_patterns": ["python3.*backtest", "scan.*factor"],
        "content_patterns": ["IC.*值", "因子.*权重"],
        "keywords": ["因子", "扫描", "IC"]
      },
      "depends_on": []
    }
  ]
}

签名规则：
- file_patterns: 正则表达式，匹配 tool_call 中的 file_path 参数
- command_patterns: 正则表达式，匹配 Bash 命令
- content_patterns: 正则表达式，匹配 Edit/Write 的内容
- keywords: 关键词列表，匹配事件中任意文本字段
- 所有 pattern 用 Python re 语法
- 宁可宽松匹配（误判后可修正），不要太严格导致漏判

步骤列表：
---
<<<STEPS>>>
---

SKILL 名称: <<<SKILL_NAME>>>
SKILL 描述: <<<SKILL_DESC>>>

请直接输出 JSON，不要有其他文字。'''


class SignatureExtractor:
    """Extract node signatures from skill steps using LLM or rules."""

    def __init__(self, skill_parser=None):
        """Reuse skill_parser's LLM configuration if available."""
        self._parser = skill_parser
        self._llm_provider = None
        self._api_key = None
        self._model = None

        if skill_parser:
            self._llm_provider = getattr(skill_parser, "llm_provider", None)
            self._api_key = getattr(skill_parser, "api_key", None)
            self._model = getattr(skill_parser, "model", None)

        if not self._llm_provider:
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
            self._model = "claude-sonnet-4-20250514"
            return
        self._llm_provider = "rule-based"

    def extract(
        self,
        parsed_skill: Dict[str, Any],
        use_cache: bool = True,
    ) -> List[Dict]:
        """Extract node signatures from a parsed skill.

        Args:
            parsed_skill: Output of skill_parser.parse() with 'steps', 'name', 'description'
            use_cache: Use cached signatures if available

        Returns:
            List of node dicts with 'id', 'name', 'signatures', 'depends_on'
        """
        steps = parsed_skill.get("steps", [])
        if not steps:
            return []

        # Cache key based on skill content
        cache_key = self._cache_key(parsed_skill)
        if use_cache:
            cached = self._load_cache(cache_key)
            if cached:
                return cached

        # Try LLM extraction
        if self._llm_provider and self._llm_provider != "rule-based":
            try:
                nodes = self._extract_llm(parsed_skill)
                if nodes:
                    self._save_cache(cache_key, nodes)
                    return nodes
            except Exception as e:
                print(f"[signature_extractor] LLM extraction failed: {e}, falling back to rules")

        # Rule-based fallback
        nodes = self._extract_rules(parsed_skill)
        self._save_cache(cache_key, nodes)
        return nodes

    def _extract_llm(self, parsed_skill: Dict) -> List[Dict]:
        """Use LLM to extract and normalize nodes with signatures."""
        steps_text = json.dumps(parsed_skill.get("steps", []), ensure_ascii=False, indent=2)
        skill_name = parsed_skill.get("name", "unknown")
        skill_desc = parsed_skill.get("description", "")

        prompt = EXTRACT_PROMPT.replace("<<<STEPS>>>", steps_text)
        prompt = prompt.replace("<<<SKILL_NAME>>>", skill_name)
        prompt = prompt.replace("<<<SKILL_DESC>>>", skill_desc)

        response = self._call_llm(prompt)

        # Parse JSON from response (handle markdown code blocks)
        json_text = response.strip()
        if json_text.startswith("```"):
            json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
            json_text = re.sub(r"\s*```$", "", json_text)

        data = json.loads(json_text)
        nodes = data.get("nodes", data if isinstance(data, list) else [])

        # Validate structure
        for node in nodes:
            if "signatures" not in node:
                node["signatures"] = {}
            for key in ("file_patterns", "command_patterns", "content_patterns", "keywords"):
                if key not in node["signatures"]:
                    node["signatures"][key] = []
            if "depends_on" not in node:
                node["depends_on"] = []

        return nodes

    def _extract_rules(self, parsed_skill: Dict) -> List[Dict]:
        """Rule-based signature extraction (no LLM needed)."""
        steps = parsed_skill.get("steps", [])
        nodes = []

        for step in steps:
            sig = {
                "file_patterns": [],
                "command_patterns": [],
                "content_patterns": [],
                "keywords": [],
            }

            # Extract file patterns from script field
            script = step.get("script", "")
            if script:
                escaped = re.escape(script).replace(r"\*", ".*")
                sig["file_patterns"].append(escaped)

            # Extract command patterns
            command = step.get("command", "")
            if command:
                # Take key parts of the command as pattern
                parts = command.split()
                if len(parts) >= 2:
                    sig["command_patterns"].append(
                        re.escape(parts[-1]) if parts[-1].endswith(".py") else re.escape(command[:50])
                    )

            # Extract keywords from name and description
            name = step.get("name", "")
            desc = step.get("description", "")
            for text in (name, desc):
                # Chinese words (2+ chars between punctuation)
                chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
                sig["keywords"].extend(chinese[:3])
                # English words (capitalized or technical)
                english = re.findall(r"[A-Z][a-z]+|[a-z]{4,}", text)
                sig["keywords"].extend(english[:3])

            sig["keywords"] = list(set(sig["keywords"]))

            nodes.append({
                "id": step.get("id", f"node_{len(nodes)+1}"),
                "name": name or f"Step {len(nodes)+1}",
                "description": desc,
                "original_step_ids": [step.get("id", "")],
                "signatures": sig,
                "depends_on": step.get("depends_on", []),
            })

        return nodes

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
            base_url = getattr(self, "_base_url", "https://api.openai.com/v1").rstrip("/")
            resp = httpx.post(
                f"{base_url}/chat/completions",
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
        raise ValueError(f"Cannot call LLM: provider={self._llm_provider}")

    def _cache_key(self, parsed_skill: Dict) -> str:
        content = json.dumps(parsed_skill.get("steps", []), sort_keys=True)
        return "sig_" + hashlib.md5(content.encode()).hexdigest()

    def _load_cache(self, key: str) -> Optional[List[Dict]]:
        path = CACHE_DIR / f"{key}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return None

    def _save_cache(self, key: str, nodes: List[Dict]):
        path = CACHE_DIR / f"{key}.json"
        path.write_text(json.dumps(nodes, ensure_ascii=False, indent=2))
