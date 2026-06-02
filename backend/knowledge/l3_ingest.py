"""L3 (Knowledge) ingest tool — terminology + default parameters from approved sources.

Implementation of L3_INGEST_SPEC.md hard rules:
  Rule 1: only ingest from Mark-approved source paths
  Rule 2: light ingest v0 — no LLM, just word freq + numeric defaults + source line tracing
  Rule 3: 0 PII (two-layer filter, see pii_filter.py)
  Rule 4: every output entry has source[] (file + line + excerpt up to 200 char)

Output: 4 files in OUTPUT_DIR
  - terminology_candidates.yaml
  - defaults_candidates.yaml
  - ingest_audit_<date>.md
  - pii_audit_<date>.md

Run: python3 -m backend.knowledge.l3_ingest [--dry-run]
"""

from __future__ import annotations

import os
import argparse
import datetime as _dt
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

try:
    import jieba  # Chinese segmentation
except ImportError:
    jieba = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from .pii_filter import PIIFilter


# ─────────────────────────────────────────────────────────────────────────────
# Configuration (per L3_INGEST_SPEC Rule 1 — source whitelist)
# ─────────────────────────────────────────────────────────────────────────────

APPROVED_SOURCES = [
    {
        "name": "邢不行 position-mgmt",
        "path": os.environ.get("L3_INGEST_PRIMARY_PATH", "./data/sources/primary"),
        "framework": "邢不行 position-mgmt",
        "ext_whitelist": [".py", ".md", ".yaml", ".yml", ".json"],
        "exclude_dirs": ["__pycache__", ".git", "data/cache", ".venv", "node_modules"],
    },
    {
        "name": "user_transcripts",
        "path": os.environ.get("L3_INGEST_TRANSCRIPTS_PATH", "./data/sources/transcripts"),
        "framework": "user_transcripts",
        "ext_whitelist": [".pdf", ".txt", ".md"],
        "exclude_dirs": [],
    },
]

FORBIDDEN_PATHS = [
    os.environ.get("L3_INGEST_EXPERIMENT_BLOCKLIST", "./data/sources/_experiment_blocklist"),  # AI 实验代码污染
]

OUTPUT_DIR = Path("/opt/doc-index/tar-engine-docs/ingest")
PII_BLOCKLIST_PATH = Path("/opt/postall/private/pii_blocklist.yaml")

# Term extraction params
MIN_TERM_LEN_CN = 2
MAX_TERM_LEN_CN = 8
MIN_TERM_OCCURRENCES = 10  # 出现 10 次以上才进候选 (v0; balance review burden)
MAX_OUTPUT_TERMS = 1500  # 候选上限 — 给 Mark 人工 review 的可承受量
EXCERPT_MAX_CHARS = 200

# Stopwords for Chinese (very minimal — just to filter most-common nonsense bigrams)
CN_STOPWORDS = {
    # Particles / structural
    "的", "是", "了", "在", "和", "我", "也", "都", "就", "这", "那", "有", "不",
    "吗", "啊", "吧", "呢", "嗯", "对", "你", "他", "她", "它", "我们", "你们",
    "他们", "什么", "怎么", "可以", "如果", "因为", "所以", "但是", "然后", "现在",
    "这个", "那个", "一个", "一些", "今天", "昨天", "时候", "需要", "可能", "会",
    "要", "把", "说", "做", "去", "来", "比如", "其实", "应该", "知道", "看到",
    "感觉", "觉得", "觉", "认为", "已经", "还有", "还是",
    # High-freq filler in transcripts / docs (jieba over-segments these)
    "就是", "大家", "可能", "如果", "因此", "因为", "所以", "或者", "比如说",
    "也就是", "其实", "确实", "比较", "非常", "之后", "之前", "之间", "包括",
    "不过", "但是", "如果说", "实际上", "基本上", "差不多", "大概", "刚才",
    "刚刚", "已经", "正在", "马上", "立刻", "好像", "似乎", "可能性", "情况",
    "时候", "地方", "东西", "事情", "问题", "原因", "结果", "方法", "方式",
    "意思", "希望", "觉得", "认为", "感觉", "知道", "明白", "理解", "考虑",
    "确认", "确定", "决定", "选择", "建议", "推荐", "可以", "应该", "需要",
    "必须", "一定", "肯定", "也许", "或许", "大概", "应该说", "可以说",
    "讲一下", "说一下", "看一下", "等等", "之类", "什么的", "诸如", "譬如",
    "如此", "这样", "那样", "这么", "那么", "怎样", "怎么样", "什么样",
    # Project-specific generic chrome
    "一下", "一会", "一遍", "一种", "一定", "几个", "好的", "好像", "整个",
    "里面", "外面", "上面", "下面", "前面", "后面", "里头", "外头", "中间",
    "左边", "右边", "旁边", "周围", "附近",
    # Numbers spelled out
    "第一", "第二", "第三", "一二", "二三", "三四", "三个", "四个", "五个",
    "百分", "百分之", "千万", "万元", "美元", "人民币",
    # Generic verbs
    "看到", "听到", "学到", "想到", "拿到", "用到", "找到", "回到", "得到",
    "进入", "离开", "开始", "结束", "完成", "进行", "经过", "过程", "阶段",
    "继续", "停止", "等待", "等到",
    # Specific noise from transcripts
    "好嘛", "对吧", "对吗", "是吧", "是吗", "好啊", "可以", "OK", "ok",
    "okay", "嗯嗯", "对的", "好的", "行的", "懂了", "了解", "明白",
    "谢谢", "客气", "辛苦", "抱歉", "不好意思", "麻烦",
}


# ─────────────────────────────────────────────────────────────────────────────
# Source citation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Citation:
    file: str
    line: int
    excerpt: str  # max EXCERPT_MAX_CHARS

    def to_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "excerpt": self.excerpt}


def _excerpt(text: str, max_chars: int = EXCERPT_MAX_CHARS) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


# ─────────────────────────────────────────────────────────────────────────────
# File reading (text + PDF)
# ─────────────────────────────────────────────────────────────────────────────


def _iter_lines_text(path: Path) -> Iterable[tuple[int, str]]:
    """Yield (lineno_1based, line) from a text-like file."""
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                yield i, line.rstrip("\n")
    except UnicodeDecodeError:
        try:
            with open(path, encoding="gbk") as f:
                for i, line in enumerate(f, start=1):
                    yield i, line.rstrip("\n")
        except Exception:
            return


def _iter_lines_pdf(path: Path) -> Iterable[tuple[int, str]]:
    """Yield (pseudo_lineno, line) from a PDF. PDFs don't have stable line numbers
    so we use accumulated line counter across pages, recording "page" hint
    in the line content for context."""
    if pdfplumber is None:
        print(f"  ⚠️  pdfplumber not installed, skipping {path.name}", file=sys.stderr)
        return
    try:
        with pdfplumber.open(path) as pdf:
            line_counter = 0
            for page_num, page in enumerate(pdf.pages, start=1):
                txt = page.extract_text() or ""
                for line in txt.split("\n"):
                    line_counter += 1
                    yield line_counter, line
    except Exception as e:
        print(f"  ⚠️  PDF read error on {path.name}: {e}", file=sys.stderr)


def _iter_lines(path: Path) -> Iterable[tuple[int, str]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        yield from _iter_lines_pdf(path)
    else:
        yield from _iter_lines_text(path)


# ─────────────────────────────────────────────────────────────────────────────
# Term extraction
# ─────────────────────────────────────────────────────────────────────────────


# Snake / camel / kebab identifiers (English code terms)
EN_IDENT_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9_-]{2,}\b")
# Chinese characters block
CN_RUN_RE = re.compile(r"[一-鿿]+")

# Built-in / very common Python tokens to skip
EN_STOPWORDS = {
    "self", "true", "false", "none", "import", "from", "return", "def", "class",
    "if", "else", "elif", "for", "while", "in", "and", "or", "not", "is", "as",
    "with", "try", "except", "finally", "raise", "lambda", "global", "nonlocal",
    "pass", "break", "continue", "yield", "async", "await",
    "the", "this", "that", "these", "those", "have", "has", "had", "been",
    "being", "are", "was", "were", "will", "would", "could", "should",
    "str", "int", "float", "list", "dict", "tuple", "set", "bool", "bytes",
    "object", "type", "len", "range", "enumerate", "zip", "map", "filter",
    "print", "open", "read", "write", "close",
    "todo", "fixme", "note", "see", "also", "etc",
}


def _extract_terms_en(line: str) -> list[str]:
    out = []
    for m in EN_IDENT_RE.findall(line):
        if m.lower() in EN_STOPWORDS:
            continue
        # Skip pure-digit-suffixed identifiers like x123 (unless camelCase)
        if m.isdigit():
            continue
        # Single-letter or 2-letter usually noise
        if len(m) < 3:
            continue
        out.append(m)
    return out


def _extract_terms_cn(line: str) -> list[str]:
    """Use jieba to segment Chinese; fallback to char n-grams if jieba missing."""
    out = []
    for run in CN_RUN_RE.findall(line):
        if jieba is not None:
            tokens = list(jieba.cut(run))
        else:
            # n-gram fallback: 2 to 4 chars
            tokens = []
            for n in range(MIN_TERM_LEN_CN, min(MAX_TERM_LEN_CN, len(run)) + 1):
                for i in range(0, len(run) - n + 1):
                    tokens.append(run[i : i + n])

        for tok in tokens:
            tok = tok.strip()
            if not tok or tok in CN_STOPWORDS:
                continue
            if len(tok) < MIN_TERM_LEN_CN or len(tok) > MAX_TERM_LEN_CN:
                continue
            # Skip pure-numeric or single-char repetitions
            if all(c == tok[0] for c in tok):
                continue
            out.append(tok)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Default-value extraction (numeric defaults in code)
# ─────────────────────────────────────────────────────────────────────────────


# x = 1.5    → key=x, value=1.5
PY_ASSIGN_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*(?:#|$)")
# 'cap_ratios': [1/3, 1/3, 1/3]  or  "cap_ratios": 0.5  → captures key + first numeric token in rhs
DICT_ENTRY_RE = re.compile(
    r"""['"]([a-zA-Z_][a-zA-Z0-9_]*)['"]\s*:\s*(?:\[\s*([^\]]+)\]|([+-]?\d+(?:\.\d+)?))"""
)


def _extract_defaults(line: str) -> list[tuple[str, str]]:
    """Returns list of (key_name, raw_value_repr)."""
    out: list[tuple[str, str]] = []

    m = PY_ASSIGN_RE.match(line)
    if m:
        out.append((m.group(1), m.group(2)))

    for m in DICT_ENTRY_RE.finditer(line):
        key = m.group(1)
        if m.group(2) is not None:
            # list value, take first token
            first = m.group(2).split(",")[0].strip()
            out.append((key, first))
        elif m.group(3) is not None:
            out.append((key, m.group(3)))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Main ingest
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TermInfo:
    occurrences: int = 0
    sources: list[Citation] = field(default_factory=list)

    def add(self, cite: Citation, max_sources: int = 5) -> None:
        self.occurrences += 1
        if len(self.sources) < max_sources:
            self.sources.append(cite)


@dataclass
class DefaultInfo:
    """Single observed value for a key, plus citations."""

    value: str
    occurrences: int = 0
    sources: list[Citation] = field(default_factory=list)

    def add(self, cite: Citation, max_sources: int = 5) -> None:
        self.occurrences += 1
        if len(self.sources) < max_sources:
            self.sources.append(cite)


class L3Ingest:
    def __init__(
        self,
        sources: list[dict] = APPROVED_SOURCES,
        forbidden: list[str] = FORBIDDEN_PATHS,
        output_dir: Path = OUTPUT_DIR,
        blocklist_path: Path = PII_BLOCKLIST_PATH,
    ):
        self.sources = sources
        self.forbidden = [Path(p).resolve() for p in forbidden]
        self.output_dir = Path(output_dir)
        self.pii = PIIFilter(blocklist_path)

        self.term_info: dict[str, TermInfo] = defaultdict(TermInfo)
        self.term_framework: dict[str, str] = {}  # term -> first-seen framework

        # default candidates: framework -> term -> value -> DefaultInfo
        self.defaults: dict[str, dict[str, dict[str, DefaultInfo]]] = defaultdict(
            lambda: defaultdict(dict)
        )

        # PII audit samples (capped)
        self.pii_l1_samples: list[tuple[str, int, str, str]] = []  # (file, line, excerpt, name)
        self.pii_l2_samples: list[tuple[str, int, str, str]] = []  # (file, line, excerpt, pattern)

        # Stats
        self.stats = {
            "files_scanned": 0,
            "lines_scanned": 0,
            "lines_pii_filtered_l1": 0,
            "lines_pii_filtered_l2": 0,
            "lines_kept": 0,
            "by_source": {},
        }

    # ─────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        for source in self.sources:
            self._ingest_source(source)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_terminology()
        self._write_defaults()
        self._write_ingest_audit()
        self._write_pii_audit()

        print()
        print(f"✅ L3 ingest complete. Output: {self.output_dir}")
        print(f"  Files scanned:     {self.stats['files_scanned']}")
        print(f"  Lines scanned:     {self.stats['lines_scanned']}")
        print(f"  PII filtered (L1): {self.stats['lines_pii_filtered_l1']}")
        print(f"  PII filtered (L2): {self.stats['lines_pii_filtered_l2']}")
        print(f"  Lines kept:        {self.stats['lines_kept']}")
        print(f"  Term candidates:   {sum(1 for t in self.term_info.values() if t.occurrences >= MIN_TERM_OCCURRENCES)}")
        print(f"  Default candidates:{sum(len(v) for src in self.defaults.values() for v in src.values())}")

    # ─────────────────────────────────────────────────────────────────────

    def _ingest_source(self, source: dict) -> None:
        path = Path(source["path"]).resolve()
        if not path.exists():
            print(f"⚠️  Skipping missing source: {path}", file=sys.stderr)
            return

        # Forbidden-path guard (Rule 1)
        for forbidden in self.forbidden:
            if path == forbidden or forbidden in path.parents:
                raise PermissionError(f"Source {path} is on the forbidden list (Rule 1)")

        print(f"📂 Scanning: {source['name']}  ({path})")
        framework = source["framework"]
        ext_whitelist = set(source["ext_whitelist"])
        exclude = set(source["exclude_dirs"])

        files_in_source = 0
        lines_in_source = 0

        for fpath in self._iter_files(path, ext_whitelist, exclude):
            # Forbidden guard at file level too
            forbidden_hit = False
            for forbidden in self.forbidden:
                if forbidden in fpath.parents or fpath == forbidden:
                    forbidden_hit = True
                    break
            if forbidden_hit:
                continue

            files_in_source += 1
            self.stats["files_scanned"] += 1

            for lineno, line in _iter_lines(fpath):
                lines_in_source += 1
                self.stats["lines_scanned"] += 1
                self._process_line(framework, fpath, lineno, line)

        self.stats["by_source"][source["name"]] = {
            "files": files_in_source,
            "lines": lines_in_source,
        }

    def _iter_files(
        self, root: Path, ext_whitelist: set[str], exclude_dirs: set[str]
    ) -> Iterable[Path]:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            # Check excluded dirs anywhere in path
            if any(part in exclude_dirs or part.startswith(".") and part != "." for part in p.relative_to(root).parts):
                continue
            if p.suffix.lower() not in ext_whitelist:
                continue
            yield p

    # ─────────────────────────────────────────────────────────────────────

    def _process_line(self, framework: str, fpath: Path, lineno: int, line: str) -> None:
        if not line.strip():
            return

        # PII filter
        result = self.pii.check(line)
        if not result.kept:
            if result.layer == "L1":
                self.stats["lines_pii_filtered_l1"] += 1
                if len(self.pii_l1_samples) < 50:
                    self.pii_l1_samples.append(
                        (str(fpath), lineno, _excerpt(line), result.reason or "")
                    )
            else:
                self.stats["lines_pii_filtered_l2"] += 1
                if len(self.pii_l2_samples) < 50:
                    self.pii_l2_samples.append(
                        (str(fpath), lineno, _excerpt(line), result.reason or "")
                    )
            return

        self.stats["lines_kept"] += 1

        cite = Citation(file=str(fpath), line=lineno, excerpt=_excerpt(line))

        # Terms
        for term in _extract_terms_en(line):
            self.term_info[term].add(cite)
            self.term_framework.setdefault(term, framework)
        for term in _extract_terms_cn(line):
            self.term_info[term].add(cite)
            self.term_framework.setdefault(term, framework)

        # Defaults (only meaningful in code-ish files)
        if fpath.suffix.lower() in {".py", ".yaml", ".yml", ".json"}:
            for key, value in _extract_defaults(line):
                # Skip clearly trivial keys
                if key.lower() in EN_STOPWORDS or len(key) < 3:
                    continue
                bucket = self.defaults[framework][key]
                if value not in bucket:
                    bucket[value] = DefaultInfo(value=value)
                bucket[value].add(cite)

    # ─────────────────────────────────────────────────────────────────────
    # Outputs
    # ─────────────────────────────────────────────────────────────────────

    def _write_terminology(self) -> None:
        out_path = self.output_dir / "terminology_candidates.yaml"
        items = []
        for term, info in sorted(
            self.term_info.items(), key=lambda kv: -kv[1].occurrences
        ):
            if info.occurrences < MIN_TERM_OCCURRENCES:
                continue
            items.append(
                {
                    "canonical": term,
                    "framework": self.term_framework.get(term, "unknown"),
                    "type": "term",
                    "occurrences": info.occurrences,
                    "sources": [c.to_dict() for c in info.sources],
                    "status": "pending_review",
                }
            )

        truncated_total = len(items)
        if truncated_total > MAX_OUTPUT_TERMS:
            items = items[:MAX_OUTPUT_TERMS]

        with open(out_path, "w", encoding="utf-8") as f:
            if truncated_total > MAX_OUTPUT_TERMS:
                f.write(
                    f"# Truncated: kept top {MAX_OUTPUT_TERMS} of {truncated_total} candidates "
                    f"(min_occurrences={MIN_TERM_OCCURRENCES}). Adjust MIN_TERM_OCCURRENCES or "
                    f"MAX_OUTPUT_TERMS in l3_ingest.py to widen.\n\n"
                )
            yaml.safe_dump(items, f, allow_unicode=True, sort_keys=False, width=200)
        if truncated_total > MAX_OUTPUT_TERMS:
            print(
                f"  📝 {out_path}  ({len(items)} of {truncated_total} terms, truncated)"
            )
        else:
            print(f"  📝 {out_path}  ({len(items)} terms)")

    def _write_defaults(self) -> None:
        out_path = self.output_dir / "defaults_candidates.yaml"
        items = []
        for framework, by_term in self.defaults.items():
            for term, by_value in by_term.items():
                values = sorted(by_value.values(), key=lambda d: -d.occurrences)
                if not values:
                    continue
                # Suggest the most-frequent value
                suggested = values[0].value
                items.append(
                    {
                        "term": term,
                        "framework": framework,
                        "observed_values": [
                            {
                                "value": v.value,
                                "occurrences": v.occurrences,
                                "sources": [c.to_dict() for c in v.sources],
                            }
                            for v in values
                        ],
                        "suggested_default": suggested,
                        "status": "pending_review",
                    }
                )

        # Sort by total occurrences desc
        items.sort(
            key=lambda x: -sum(v["occurrences"] for v in x["observed_values"])
        )

        with open(out_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(items, f, allow_unicode=True, sort_keys=False, width=200)
        print(f"  📝 {out_path}  ({len(items)} default keys)")

    def _write_ingest_audit(self) -> None:
        date = _dt.date.today().isoformat()
        out_path = self.output_dir / f"ingest_audit_{date}.md"

        total_lines = self.stats["lines_scanned"]
        pii_total = self.stats["lines_pii_filtered_l1"] + self.stats["lines_pii_filtered_l2"]
        pii_pct = (pii_total / total_lines * 100) if total_lines else 0.0

        term_count = sum(
            1 for t in self.term_info.values() if t.occurrences >= MIN_TERM_OCCURRENCES
        )
        default_count = sum(len(v) for src in self.defaults.values() for v in src.values())

        lines = [
            f"# Ingest Audit Report {date}",
            "",
            "## Sources scanned",
        ]
        for name, st in self.stats["by_source"].items():
            lines.append(f"- {name}: {st['files']} files, {st['lines']} lines")
        lines += [
            "",
            "## PII filter stats",
            f"- Layer 1 (blocklist) 剔除: {self.stats['lines_pii_filtered_l1']} lines",
            f"- Layer 2 (regex) 剔除: {self.stats['lines_pii_filtered_l2']} lines",
            f"- 合计 PII 剔除占比: {pii_pct:.2f}%",
            "",
            "## Term candidates",
            f"- 总抽取 (含低频): {len(self.term_info)}",
            f"- 出现 ≥{MIN_TERM_OCCURRENCES} 次, 待 Mark review: {term_count}",
            "",
            "## Defaults candidates",
            f"- 总抽取 (key×value 组合): {default_count}",
            f"- 待 Mark review: {default_count}",
            "",
            "## Mark 你需要做的",
            "1. 打开 `terminology_candidates.yaml`，逐行 review 每条",
            "2. 打开 `defaults_candidates.yaml`，确认 `suggested_default` 是否合理",
            "3. 修改 `status: pending_review → approved / rejected / merged`",
            "4. approved 的会被 ingest 工具写入 L3 主库 (terminology.yaml + defaults.yaml)",
        ]

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"  📝 {out_path}")

    def _write_pii_audit(self) -> None:
        date = _dt.date.today().isoformat()
        out_path = self.output_dir / f"pii_audit_{date}.md"

        lines = [
            f"# PII Filter Audit {date}",
            "",
            "Total dropped lines (Layer 1 + Layer 2 combined). Sample shows up to 50 per layer.",
            "",
            "## 黑名单命中样本 (Layer 1, 前 50 条)",
        ]
        if not self.pii_l1_samples:
            lines.append("(none)")
        else:
            for f_, ln, exc, reason in self.pii_l1_samples:
                lines.append(f"- `{f_}:{ln}`  reason=`{reason}`  excerpt: `{exc}`")

        lines += [
            "",
            "## 正则命中样本 (Layer 2, 前 50 条)",
        ]
        if not self.pii_l2_samples:
            lines.append("(none)")
        else:
            for f_, ln, exc, reason in self.pii_l2_samples:
                lines.append(f"- `{f_}:{ln}`  reason=`{reason}`  excerpt: `{exc}`")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"  📝 {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L3 Knowledge ingest tool")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run scan but skip output writes (for debugging)",
    )
    args = parser.parse_args(argv)

    ingest = L3Ingest()
    if args.dry_run:
        # Replace output writers with no-ops
        ingest._write_terminology = lambda: print("  (dry-run, skip terminology)")
        ingest._write_defaults = lambda: print("  (dry-run, skip defaults)")
        ingest._write_ingest_audit = lambda: print("  (dry-run, skip audit)")
        ingest._write_pii_audit = lambda: print("  (dry-run, skip pii audit)")

    ingest.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
