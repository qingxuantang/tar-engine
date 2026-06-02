"""Skill Registry — auto-detect which skill a CC session is running.

Maintains a registry of known skills with lightweight fingerprints
(keywords + file patterns). When a session has no skill_name, matches
incoming events against known skills to auto-tag.

Sources:
1. Local filesystem scan (OpenClaw skills, ~/.claude/skills/, ~/.claude/commands/)
2. Reporter-uploaded skill definitions (remote users)
3. Engine's existing skill_cache (previously parsed skills)

This is deliberately simpler than full SignatureExtractor matching.
We only need to identify WHICH skill, not map to individual DAG nodes.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# Directories to scan for skills (evaluated at scan time)
# Container deployments mount skill dirs at custom paths — honor env overrides
# so frontmatter parsing (path A) doesn't silently miss them.
def _default_skill_dirs() -> List["Path"]:
    dirs: List[Path] = [
        Path.home() / ".openclaw" / "workspace" / "skills",
        Path.home() / ".claude" / "skills",
        Path.home() / ".claude" / "commands",
    ]
    extra = os.getenv("OPENCLAW_SKILLS_PATH")
    if extra:
        dirs.append(Path(extra))
    extra_cc = os.getenv("CLAUDE_SKILLS_PATH", "/cc-skills")
    if extra_cc:
        dirs.append(Path(extra_cc))
    return dirs


DEFAULT_SKILL_DIRS = _default_skill_dirs()


def _extract_keywords_from_md(content: str) -> Set[str]:
    """Extract meaningful keywords from a SKILL.md or command .md file.

    Pulls from: name, description, code blocks (commands, file paths),
    Chinese terms, and technical identifiers.
    """
    keywords = set()

    # Frontmatter name/description
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            fm = content[3:end]
            for line in fm.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip().lower()
                    val = val.strip().strip("\"'")
                    if key in ("name", "description") and val:
                        # Add whole phrases and individual words
                        keywords.add(val.lower())
                        for w in re.findall(r"[\w\u4e00-\u9fff]{2,}", val):
                            keywords.add(w.lower())

    # Script/command references in code blocks
    for block in re.findall(r"```(?:\w+)?\s*\n(.*?)```", content, re.DOTALL):
        # Python/bash commands
        for cmd in re.findall(r"python3?\s+([\w/._-]+\.py)", block):
            keywords.add(cmd.lower())
        # File paths
        for fp in re.findall(r"(?:/[\w._-]+)+", block):
            basename = fp.rsplit("/", 1)[-1]
            if basename:
                keywords.add(basename.lower())

    # Chinese terms (2+ chars)
    for term in re.findall(r"[\u4e00-\u9fff]{2,}", content):
        keywords.add(term)

    # Technical identifiers from headers
    for header in re.findall(r"^#{1,3}\s+(.+)$", content, re.MULTILINE):
        for w in re.findall(r"[\w\u4e00-\u9fff]{2,}", header):
            keywords.add(w.lower())

    # Remove overly generic keywords
    STOPWORDS = {
        "the", "and", "for", "with", "this", "that", "from", "your",
        "use", "used", "using", "will", "can", "should", "must",
        "file", "path", "name", "type", "value", "true", "false",
        "step", "run", "make", "get", "set", "add", "new", "all",
        "not", "but", "any", "only", "when", "how", "what", "each",
        "you", "note", "如果", "需要", "确认", "可以", "不要",
        "的", "了", "在", "是", "和", "或", "为", "与", "等",
        "然后", "如果", "否则", "以下", "使用", "检查", "读取",
        "步骤", "配置", "文件", "目录", "说明", "操作", "执行",
        "输出", "输入", "结果", "信息", "内容", "规则", "流程",
        "工具", "命令", "参数", "选项", "设置", "模式", "格式",
    }
    keywords -= STOPWORDS

    return keywords


class SkillFingerprint:
    """Lightweight fingerprint for matching events to a skill."""

    def __init__(self, name: str, keywords: Set[str], source: str = "",
                 source_path: str = "", raw_content: str = ""):
        self.name = name
        self.keywords = keywords
        self.source = source  # "filesystem", "reporter", "cache"
        self.source_path = source_path  # path to SKILL.md (filesystem skills only)
        self.raw_content = raw_content  # raw SKILL.md text (reporter/cache sources)

    def score_events(self, event_text: str, idf_weights: Optional[Dict[str, float]] = None) -> float:
        """Score how well a batch of event text matches this skill.

        Uses IDF-weighted scoring when weights are provided:
        rare keywords (unique to this skill) count much more than
        common ones shared by many skills.

        Returns a score 0.0-1.0.
        """
        if not self.keywords:
            return 0.0
        text_lower = event_text.lower()

        if idf_weights:
            # IDF-weighted: rare keywords count more
            total_weight = 0.0
            hit_weight = 0.0
            for kw in self.keywords:
                w = idf_weights.get(kw, 1.0)
                total_weight += w
                if kw in text_lower:
                    hit_weight += w
            return hit_weight / total_weight if total_weight > 0 else 0.0
        else:
            # Fallback: simple fraction
            hits = sum(1 for kw in self.keywords if kw in text_lower)
            return hits / len(self.keywords)


class SkillRegistry:
    """Registry of known skills for auto-matching sessions."""

    # Minimum match score to auto-tag a session.
    # IDF-weighted scoring means distinctive keywords boost scores,
    # but 0.12 is sufficient with IDF since generic-keyword noise is suppressed.
    MATCH_THRESHOLD = 0.12
    # Minimum number of tool_call events before attempting match
    MIN_EVENTS_FOR_MATCH = 3

    # Persist reporter-uploaded skills so they survive restarts
    _REPORTER_CACHE = Path(os.getenv("ENGINE_HOME", Path.home() / ".engine")) / "reporter_skills.json"

    def __init__(self):
        self._skills: Dict[str, SkillFingerprint] = {}
        self._scanned = False
        self._extra_dirs: List[Path] = []
        self._idf_weights: Dict[str, float] = {}  # keyword → IDF weight
        self._idf_dirty = True  # recalculate when skills change

    def add_scan_dir(self, path: str):
        """Add an extra directory to scan for skills."""
        p = Path(path)
        if p.exists() and p.is_dir():
            self._extra_dirs.append(p)

    def scan_local(self):
        """Scan local filesystem for skill definitions."""
        dirs = DEFAULT_SKILL_DIRS + self._extra_dirs

        for skill_dir in dirs:
            try:
                if not skill_dir.exists():
                    continue
                entries = list(skill_dir.iterdir())
            except (PermissionError, OSError):
                # Container mounts can be readable at the dir level but not on
                # individual files; skip the dir rather than crash startup.
                continue
            # Each subdirectory is a skill
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                    name = entry.name
                    skill_md = entry / "SKILL.md"
                    if not skill_md.exists():
                        md_files = list(entry.glob("*.md"))
                        if md_files:
                            skill_md = md_files[0]
                        else:
                            continue
                    content = skill_md.read_text(encoding="utf-8", errors="replace")
                except (PermissionError, OSError):
                    continue
                except Exception:
                    continue

                try:
                    keywords = _extract_keywords_from_md(content)
                    if keywords:
                        self._skills[name] = SkillFingerprint(
                            name=name, keywords=keywords, source="filesystem",
                            source_path=str(skill_md),
                        )
                    # Path A: derive capabilities from frontmatter allowed-tools
                    self._seed_frontmatter_capabilities(name, content)
                except Exception:
                    pass

        # Also scan skill_cache for previously parsed skills
        cache_dir = Path(os.getenv("ENGINE_HOME", Path.home() / ".engine")) / "skill_cache"
        if cache_dir.exists():
            for cache_file in cache_dir.glob("*.json"):
                try:
                    data = json.loads(cache_file.read_text())
                    name = data.get("name", "")
                    if name and name not in self._skills:
                        # Extract keywords from cached steps
                        kws = set()
                        kws.add(name.lower())
                        for step in data.get("steps", []):
                            for field in ("name", "description", "command", "script"):
                                val = step.get(field, "")
                                if val:
                                    for w in re.findall(r"[\w\u4e00-\u9fff]{2,}", val):
                                        kws.add(w.lower())
                        if kws:
                            self._skills[name] = SkillFingerprint(
                                name=name, keywords=kws, source="skill_cache"
                            )
                except Exception:
                    pass

        # Load persisted reporter skills
        self._load_reporter_cache()

        self._scanned = True
        self._idf_dirty = True
        print(f"[skill_registry] Scanned {len(self._skills)} skills")

    def _seed_frontmatter_capabilities(self, skill_name: str, skill_md_content: str):
        """Path A: parse SKILL.md frontmatter `allowed-tools` and write to
        the AUTHORITATIVE declared_caps_json column. This is the contract the
        author published — admin can extend or restrict the *effective* caps
        derived from it, but cannot rewrite this declaration.
        """
        try:
            from auditor.capabilities import (
                parse_allowed_tools, capabilities_from_allowed_tools,
            )
            from event_store import event_store
            tools = parse_allowed_tools(skill_md_content)
            if not tools:
                return
            declared = capabilities_from_allowed_tools(tools)
            event_store.set_declared_capabilities(skill_name, declared)
        except Exception as e:
            print(f"[skill_registry] frontmatter caps seed failed for {skill_name}: {e}")

    def _load_reporter_cache(self):
        """Load previously reporter-uploaded skills from disk."""
        try:
            if self._REPORTER_CACHE.exists():
                data = json.loads(self._REPORTER_CACHE.read_text())
                for name, kws_list in data.items():
                    if name not in self._skills:
                        self._skills[name] = SkillFingerprint(
                            name=name,
                            keywords=set(kws_list),
                            source="reporter",
                        )
        except Exception as e:
            print(f"[skill_registry] reporter cache load error: {e}")

    def _save_reporter_cache(self):
        """Persist reporter-uploaded skills to disk."""
        try:
            reporter_skills = {
                name: sorted(fp.keywords)
                for name, fp in self._skills.items()
                if fp.source == "reporter"
            }
            self._REPORTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
            self._REPORTER_CACHE.write_text(json.dumps(reporter_skills, ensure_ascii=False))
        except Exception as e:
            print(f"[skill_registry] reporter cache save error: {e}")

    def register_skills(self, skills_data: List[Dict]) -> int:
        """Register skills uploaded by a reporter.

        Each item: {"name": str, "keywords": list[str]}
        or {"name": str, "content": str} (raw SKILL.md content).

        Returns count of newly registered skills.
        """
        added = 0
        for item in skills_data:
            name = item.get("name", "")
            if not name:
                continue

            raw_content = item.get("content", "")
            if "keywords" in item:
                kws = set(k.lower() for k in item["keywords"] if k)
            elif raw_content:
                kws = _extract_keywords_from_md(raw_content)
            else:
                continue

            if kws:
                self._skills[name] = SkillFingerprint(
                    name=name, keywords=kws, source="reporter",
                    raw_content=raw_content,
                )
                added += 1

        if added > 0:
            self._save_reporter_cache()
            self._idf_dirty = True

        return added

    def _compute_idf(self):
        """Compute IDF weights for all keywords across all skills.

        Keywords that appear in many skills get low weight (generic).
        Keywords unique to one skill get high weight (distinctive).
        IDF = log(N / df) where N = total skills, df = skills containing keyword.
        """
        import math
        n = len(self._skills)
        if n == 0:
            self._idf_weights = {}
            self._idf_dirty = False
            return

        # Count document frequency for each keyword
        df: Dict[str, int] = {}
        for fp in self._skills.values():
            for kw in fp.keywords:
                df[kw] = df.get(kw, 0) + 1

        # IDF = log(N / df) + 1 (smoothed)
        self._idf_weights = {
            kw: math.log(n / count) + 1.0
            for kw, count in df.items()
        }
        self._idf_dirty = False
        # Stats for debugging
        unique = sum(1 for c in df.values() if c == 1)
        print(f"[skill_registry] IDF computed: {len(df)} keywords, {unique} unique to one skill")

    def match_events(
        self, events: List[Dict], exclude: Optional[Set[str]] = None
    ) -> Optional[Tuple[str, float]]:
        """Match a batch of events to the best-matching skill.

        Uses IDF-weighted scoring so distinctive keywords (unique to
        a skill) count much more than generic ones shared across many.

        Returns (skill_name, score) or None if no match above threshold.
        """
        if not self._scanned:
            self.scan_local()

        if not self._skills:
            return None

        if self._idf_dirty:
            self._compute_idf()

        # Build text corpus from events
        parts = []
        for ev in events:
            if ev.get("event_type") != "tool_call":
                continue
            tool = ev.get("tool_name", "")
            parts.append(tool)

            args = ev.get("tool_input", ev.get("args", {}))
            if isinstance(args, dict):
                for key in ("command", "file_path", "path", "pattern",
                            "content", "new_string", "description"):
                    val = args.get(key, "")
                    if isinstance(val, str) and val:
                        parts.append(val[:500])

            # Also include result text for richer matching
            result = ev.get("result", "")
            if isinstance(result, str) and result:
                parts.append(result[:300])

        if not parts:
            return None

        event_text = " ".join(parts)

        # Score against all skills (IDF-weighted)
        best_name = None
        best_score = 0.0

        for name, fp in self._skills.items():
            if exclude and name in exclude:
                continue
            score = fp.score_events(event_text, self._idf_weights)
            if score > best_score:
                best_score = score
                best_name = name

        if best_name and best_score >= self.MATCH_THRESHOLD:
            return (best_name, best_score)

        return None

    def score_skill(self, skill_name: str, events: List[Dict]) -> float:
        """Score a specific skill against events. Used for stickiness checks."""
        if not self._scanned:
            self.scan_local()
        fp = self._skills.get(skill_name)
        if not fp:
            return 0.0
        if self._idf_dirty:
            self._compute_idf()

        # Build text corpus (same as match_events)
        parts = []
        for ev in events:
            if ev.get("event_type") != "tool_call":
                continue
            tool = ev.get("tool_name", "")
            parts.append(tool)
            args = ev.get("tool_input", ev.get("args", {}))
            if isinstance(args, dict):
                for key in ("command", "file_path", "path", "pattern",
                            "content", "new_string", "description"):
                    val = args.get(key, "")
                    if isinstance(val, str) and val:
                        parts.append(val[:500])
            result = ev.get("result", "")
            if isinstance(result, str) and result:
                parts.append(result[:300])

        if not parts:
            return 0.0
        event_text = " ".join(parts)
        return fp.score_events(event_text, self._idf_weights)

    def get_known_skills(self) -> List[str]:
        """Return list of known skill names."""
        if not self._scanned:
            self.scan_local()
        return sorted(self._skills.keys())

    def get_skill_content(self, skill_name: str) -> Optional[str]:
        """Return SKILL.md content for a known skill, or None.

        Checks in order: filesystem path, in-memory raw_content, reporter cache.
        """
        if not self._scanned:
            self.scan_local()
        fp = self._skills.get(skill_name)
        if not fp:
            return None
        # 1. Read from filesystem
        if fp.source_path:
            try:
                return Path(fp.source_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        # 2. In-memory raw content (from reporter upload)
        if fp.raw_content:
            return fp.raw_content
        return None

    def stats(self) -> Dict:
        return {
            "total_skills": len(self._skills),
            "scanned": self._scanned,
            "sources": {
                s: sum(1 for fp in self._skills.values() if fp.source == s)
                for s in ("filesystem", "skill_cache", "reporter")
            },
        }


# ── Skill Intent Classification ────────────────────────────────────

# Intent types:
#   read_only  — only reads data, generates reports (e.g. backtest analysis)
#   modify     — modifies config/code/parameters (e.g. factor research, auto-iterate)
#   execute    — executes trades or external actions (e.g. AI CTA, publishing)
#   unknown    — not classified yet, use default scoring

SKILL_INTENTS: Dict[str, str] = {
    # Known read-only skills (analysis, reporting)
    "回测分析": "read_only",
    "backtest-analysis": "read_only",

    # Known modify skills (change code/config)
    "因子研究": "modify",
    "factor-research": "modify",
    "自动迭代": "modify",
    "auto-iterate": "modify",

    # Known execute skills (trading, publishing, external actions)
    "AI CTA": "execute",
    "ai-cta": "execute",
    "postall": "execute",
}


def get_skill_intent(skill_name: str) -> str:
    """Get the intent classification for a skill.

    Checks explicit registry first, then infers from skill name keywords.
    Returns: "read_only", "modify", "execute", or "unknown".
    """
    if not skill_name:
        return "unknown"

    # Exact match
    intent = SKILL_INTENTS.get(skill_name)
    if intent:
        return intent

    # Case-insensitive match
    name_lower = skill_name.lower()
    for registered, registered_intent in SKILL_INTENTS.items():
        if registered.lower() == name_lower:
            return registered_intent

    # Keyword-based inference
    READ_KEYWORDS = {"分析", "analysis", "report", "review", "诊断", "对比", "查看", "检查"}
    MODIFY_KEYWORDS = {"研究", "迭代", "优化", "生成", "创建", "modify", "create", "generate", "iterate"}
    EXECUTE_KEYWORDS = {"交易", "发布", "publish", "trade", "execute", "deploy", "cta"}

    read_score = sum(1 for kw in READ_KEYWORDS if kw in name_lower)
    modify_score = sum(1 for kw in MODIFY_KEYWORDS if kw in name_lower)
    execute_score = sum(1 for kw in EXECUTE_KEYWORDS if kw in name_lower)

    max_score = max(read_score, modify_score, execute_score)
    if max_score == 0:
        return "unknown"
    if read_score == max_score:
        return "read_only"
    if execute_score == max_score:
        return "execute"
    return "modify"


def register_skill_intent(skill_name: str, intent: str):
    """Register or update intent for a skill. Persists in memory only."""
    if intent in ("read_only", "modify", "execute", "unknown"):
        SKILL_INTENTS[skill_name] = intent


# Module-level singleton
skill_registry = SkillRegistry()
