"""米其林指南 v2 site builder — multi-page architecture.

Replaces build_site.py's single-HTML model. Renders:

  index.html                              ← main landing with 10 category tiles
  category/<top-id>/index.html            ← category overview + subcategory + skill list
  skill/<slug>/index.html                 ← individual skill audit report
  leaderboard.html                        ← top + bottom scoring skills

Reads from a batch_audit_v2.py output directory (per-skill .md + .meta.json).

Output structure inside `--output`:
  index.html
  category/
    engineering/
      index.html
    data-ai/
      index.html
    ...
  skill/
    ai-evals/
      index.html
    ...
  leaderboard.html
  assets/style.css           ← shared stylesheet
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).parent
TAXONOMY_PATH = HERE / "config" / "taxonomy.yaml"


DEFAULT_LANG = "en"


# ── Shared CSS (one stylesheet under /assets/) ─────────────────────────
CSS = """
:root {
  --bg: #0a0e14;
  --bg-card: #131820;
  --bg-card-alt: #1a2030;
  --text: #e6edf3;
  --text-dim: #8b97a8;
  --text-quiet: #5a6478;
  --accent: #5eba7d;
  --accent-dim: #3a8c5b;
  --warn: #d9a64a;
  --danger: #d94a4a;
  --border: #1e2630;
  --link: #6cb9f0;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter",
               "Segoe UI", "PingFang SC", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 28px 22px 60px; }

.lang-toggle {
  position: fixed; top: 14px; right: 14px;
  display: flex; background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 999px; padding: 3px; z-index: 50;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}
.lang-toggle a {
  font-size: 12px; font-weight: 600; color: var(--text-dim);
  text-decoration: none; padding: 5px 12px; border-radius: 999px;
}
.lang-toggle a.active { background: var(--accent-dim); color: var(--bg); }
@media (max-width: 480px) {
  .lang-toggle { top: 10px; right: 10px; }
  .lang-toggle a { font-size: 11px; padding: 4px 10px; }
}

.crumbs {
  font-size: 13px; color: var(--text-quiet);
  margin-bottom: 18px;
}
.crumbs a { color: var(--text-dim); text-decoration: none; }
.crumbs a:hover { color: var(--text); }

.eyebrow {
  font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--accent); font-weight: 600; margin: 0 0 12px 0;
}
h1.title {
  font-size: 32px; font-weight: 700; letter-spacing: -0.01em;
  margin: 0 0 8px 0; line-height: 1.2;
}
.subtitle {
  color: var(--text-dim); font-size: 16px; margin: 0 0 22px 0;
}

.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px; margin: 24px 0 36px 0;
}
.stat {
  background: var(--bg-card); border: 1px solid var(--border);
  padding: 14px 16px; border-radius: 8px;
}
.stat-label {
  font-size: 11px; letter-spacing: 0.08em; color: var(--text-quiet);
  text-transform: uppercase; margin: 0 0 6px 0;
}
.stat-value { font-size: 22px; font-weight: 700; color: var(--text); }
.stat-value.accent { color: var(--accent); }

h2.section-title {
  font-size: 22px; font-weight: 700; margin: 38px 0 14px 0;
}

.category-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 14px; margin: 16px 0 24px 0;
}
.cat-tile {
  display: block; background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 18px 16px 18px;
  text-decoration: none; color: inherit;
  transition: border-color 0.15s, transform 0.1s;
}
.cat-tile:hover { border-color: var(--accent-dim); transform: translateY(-1px); }
.cat-tile h3 { margin: 0 0 6px 0; font-size: 17px; color: var(--text); }
.cat-tile p { margin: 0 0 12px 0; font-size: 13px; color: var(--text-dim); line-height: 1.5; }
.cat-tile .cat-meta {
  font-size: 12px; color: var(--text-quiet); display: flex; gap: 14px;
}
.cat-tile .cat-meta strong { color: var(--accent); font-weight: 600; }

.skill-table {
  width: 100%; border-collapse: collapse; font-size: 14px;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden; margin: 16px 0;
}
.skill-table th {
  text-align: left; background: var(--bg-card-alt); color: var(--text-dim);
  font-weight: 600; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.04em; padding: 11px 14px; border-bottom: 1px solid var(--border);
}
.skill-table td {
  padding: 12px 14px; border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.skill-table tr:last-child td { border-bottom: none; }
.skill-table tr:hover td { background: var(--bg-card-alt); }
.skill-table .grade-badge {
  display: inline-block; font-weight: 700; padding: 3px 8px;
  border-radius: 4px; font-size: 13px; font-variant-numeric: tabular-nums;
}
.grade-A { background: rgba(94, 186, 125, 0.15); color: var(--accent); }
.grade-B { background: rgba(94, 186, 125, 0.10); color: var(--accent-dim); }
.grade-C { background: rgba(217, 166, 74, 0.18); color: var(--warn); }
.grade-D { background: rgba(217, 74, 74, 0.15); color: var(--danger); }
.grade-F { background: rgba(217, 74, 74, 0.22); color: var(--danger); }
.skill-link {
  color: var(--link); text-decoration: none; font-weight: 600;
}
.skill-link:hover { text-decoration: underline; }
.findings-pill {
  display: inline-block; font-size: 12px; font-variant-numeric: tabular-nums;
  padding: 2px 7px; border-radius: 12px; color: var(--text-dim);
}
.findings-pill.crit { color: var(--danger); }
.findings-pill.high { color: var(--warn); }

.subcat-section { margin-top: 28px; }
.subcat-section h3 { margin: 18px 0 8px 0; font-size: 16px; }
.subcat-meta { font-size: 13px; color: var(--text-quiet); }

.report-body h1 { font-size: 26px; margin: 0 0 12px 0; }
.report-body h2 {
  font-size: 18px; margin: 28px 0 10px;
  padding-bottom: 6px; border-bottom: 1px solid var(--border);
}
.report-body h3 { font-size: 15px; margin: 22px 0 8px; }
.report-body p { margin: 8px 0; }
.report-body ul, .report-body ol { padding-left: 22px; }
.report-body li { margin: 4px 0; }
.report-body code {
  font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, monospace;
  background: var(--bg-card-alt); padding: 1px 6px; border-radius: 3px;
  font-size: 0.92em;
}
.report-body pre {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 6px; padding: 12px 14px; overflow-x: auto;
  font-size: 13px;
}
.report-body pre code { background: transparent; padding: 0; font-size: 13px; }
.report-body table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }
.report-body th, .report-body td {
  text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
}
.report-body th { background: var(--bg-card-alt); }
.report-body blockquote {
  border-left: 3px solid var(--accent-dim); margin: 12px 0; padding: 4px 14px;
  color: var(--text-dim); background: var(--bg-card); border-radius: 0 4px 4px 0;
}
.report-body a { color: var(--link); }

footer {
  margin-top: 60px; padding-top: 24px; border-top: 1px solid var(--border);
  font-size: 13px; color: var(--text-quiet); text-align: center;
}
footer a { color: var(--link); }

@media (max-width: 600px) {
  .wrap { padding: 20px 14px 48px; }
  h1.title { font-size: 24px; }
  .skill-table td, .skill-table th { padding: 9px 10px; font-size: 13px; }
}
"""


# ── Lang strings (UI chrome only; per-skill markdown already i18n'd) ────
STRINGS = {
    "en": {
        "html_lang": "en",
        "site_title": "TAR Engine 米其林指南",
        "eyebrow": "TAR Engine · Skill Security Audit Guide",
        "tagline": "Security & quality audits for AI agent skills. By reader persona.",
        "stat_skills": "Skills audited",
        "stat_categories": "Categories covered",
        "stat_avg_score": "Average score",
        "stat_clean_passes": "Grade A passes",
        "sec_categories": "Browse by reader persona",
        "sec_recent": "Recent audits",
        "sec_subcategories": "Subcategories",
        "th_skill": "Skill", "th_grade": "Grade", "th_score": "Score",
        "th_findings": "Findings", "th_source": "Source",
        "no_findings": "no findings",
        "crumbs_home": "Home", "crumbs_category": "Category",
        "footer_text": "Each report uses the same TAR Engine static + semantic + adversarial audit pipeline. Rule registry and methodology are open at github.com/qingxuantang/tar-engine.",
        "found_n_skills": "{n} skills in this category",
        "no_skills_yet": "No skills audited in this category yet — coming soon.",
        "back_to_home": "← All categories",
        "view_report": "View report",
    },
    "zh": {
        "html_lang": "zh-CN",
        "site_title": "TAR Engine 米其林指南",
        "eyebrow": "TAR Engine · 技能安全审计指南",
        "tagline": "AI agent skill 的安全与质量审计，按读者画像组织。",
        "stat_skills": "已审计 skill",
        "stat_categories": "覆盖类别",
        "stat_avg_score": "平均得分",
        "stat_clean_passes": "A 级通过",
        "sec_categories": "按读者画像浏览",
        "sec_recent": "最近审计",
        "sec_subcategories": "子类别",
        "th_skill": "Skill", "th_grade": "等级", "th_score": "得分",
        "th_findings": "命中", "th_source": "来源",
        "no_findings": "无 finding",
        "crumbs_home": "首页", "crumbs_category": "类别",
        "footer_text": "所有报告均使用同一套 TAR Engine 静态 + 语义 + 对抗审计流水线。规则 registry 与方法学公开在 github.com/qingxuantang/tar-engine。",
        "found_n_skills": "本类别共 {n} 个 skill",
        "no_skills_yet": "本类别暂无 audit — 后续会补齐。",
        "back_to_home": "← 全部类别",
        "view_report": "查看报告",
    },
}


def s(key: str, lang: str, **fmt) -> str:
    bundle = STRINGS.get(lang) or STRINGS[DEFAULT_LANG]
    val = bundle.get(key) or STRINGS[DEFAULT_LANG].get(key, key)
    return val.format(**fmt) if fmt else val


GRADE_BADGES = {"A": "🟢 A", "B": "🟢 B", "C": "🟡 C", "D": "🟠 D", "F": "🔴 F"}


def grade_class(g: str) -> str:
    return f"grade-{g}" if g in "ABCDF" else "grade-B"


def safe_slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "_", name.strip().lower())
    return s or "unnamed"


def base_html_head(*, title: str, lang: str, css_relative_path: str,
                   description: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="{s('html_lang', lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
<meta name="theme-color" content="#0a0e14">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="stylesheet" href="{css_relative_path}">
</head>
<body data-lang="{lang}">"""


def lang_toggle_html(*, current_lang: str, sibling_paths: dict[str, str]) -> str:
    """`sibling_paths` maps "en" and "zh" to the relative URL of the same
    page in that language (or just to the language root if unavailable)."""
    en_class = "active" if current_lang == "en" else ""
    zh_class = "active" if current_lang == "zh" else ""
    return f"""
<nav class="lang-toggle" aria-label="Language">
  <a href="{sibling_paths.get('en', '../en/')}" data-lang-switch="en" class="{en_class}">EN</a>
  <a href="{sibling_paths.get('zh', '../zh/')}" data-lang-switch="zh" class="{zh_class}">中</a>
</nav>"""


def page_footer(lang: str) -> str:
    return f"""
<footer>
  <p>{html.escape(s('footer_text', lang))}</p>
</footer>"""


def lang_toggle_script() -> str:
    """JS sets a cookie + flips active so nginx and SPA-style switching work."""
    return """
<script>
(function() {
  function setLangCookie(lang) {
    var maxAge = 60 * 60 * 24 * 365;
    document.cookie = 'michelin_lang=' + lang + '; path=/michelin/; max-age=' + maxAge + '; SameSite=Lax';
  }
  document.querySelectorAll('.lang-toggle a[data-lang-switch]').forEach(function(btn) {
    btn.addEventListener('click', function(ev) {
      var target = btn.dataset.langSwitch;
      if (!target) return;
      setLangCookie(target);
      // navigation proceeds via href
    });
  });
})();
</script>"""


def render_findings_pill(sev: dict) -> str:
    parts = []
    if sev.get("critical"):
        parts.append(f'<span class="findings-pill crit">🔴 {sev["critical"]}</span>')
    if sev.get("high"):
        parts.append(f'<span class="findings-pill high">🟠 {sev["high"]}</span>')
    if sev.get("warning"):
        parts.append(f'<span class="findings-pill">🟡 {sev["warning"]}</span>')
    return " ".join(parts) or "<span class=\"findings-pill\">—</span>"


def render_skill_row(*, skill: dict, lang: str, link_prefix: str) -> str:
    grade = skill.get("grade", "?")
    score = skill.get("score", 0)
    sev = skill.get("severity_counts", {})
    name = skill.get("skill_name") or "unnamed"
    slug = safe_slug(name)
    src = skill.get("source_platform", "?")
    return f"""
<tr>
  <td><a class="skill-link" href="{link_prefix}skill/{slug}/">{html.escape(name)}</a></td>
  <td><span class="grade-badge {grade_class(grade)}">{grade}</span></td>
  <td>{score}</td>
  <td>{render_findings_pill(sev)}</td>
  <td><small style="color:var(--text-quiet)">{html.escape(src)}</small></td>
</tr>"""


# ── Markdown → HTML for per-skill report body ──────────────────────────
# Lightweight (avoid extra dependency on marked.js client-side). Uses Python
# markdown via existing renderer if available; else minimal converter.


def md_to_html(md: str) -> str:
    """Convert markdown to HTML using `markdown` lib if available, else
    a very basic fallback. Skill reports are well-formed markdown so
    `markdown` lib produces clean output.
    """
    try:
        import markdown  # type: ignore
        return markdown.markdown(
            md,
            extensions=["extra", "tables", "fenced_code", "sane_lists", "toc"],
        )
    except ImportError:
        pass
    # Crude fallback: wrap paragraphs, leave headers / code blocks as-is.
    return f"<pre style='white-space:pre-wrap'>{html.escape(md)}</pre>"


# ── Pages ───────────────────────────────────────────────────────────────


def write_landing(*, lang: str, out_root: Path, taxonomy: dict,
                  skills: list[dict]) -> None:
    """index.html — main landing with 10 category tiles."""
    page_dir = out_root / lang
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / "index.html"

    # Group skills by top_level
    by_top: dict[str, list[dict]] = defaultdict(list)
    for sk in skills:
        if sk.get("top_level"):
            by_top[sk["top_level"]].append(sk)

    total_n = len(skills)
    scored = [int(sk.get("score") or 0) for sk in skills if sk.get("score") is not None]
    avg = round(sum(scored) / len(scored), 1) if scored else 0
    a_passes = sum(1 for sk in skills if sk.get("grade") == "A")
    n_categories = sum(1 for top in taxonomy.get("taxonomy", []) if by_top.get(top["id"]))

    # Build category tiles
    tiles = []
    for top in taxonomy.get("taxonomy", []):
        tid = top["id"]
        cnt = len(by_top.get(tid, []))
        title = top.get(f"title_{lang}") or top.get("title_en") or tid
        desc = top.get(f"description_{lang}") or top.get("description_en") or ""
        tiles.append(f"""
<a class="cat-tile" href="category/{tid}/">
  <h3>{html.escape(title)}</h3>
  <p>{html.escape(desc)}</p>
  <div class="cat-meta">
    <span><strong>{cnt}</strong> {s('stat_skills', lang).lower()}</span>
  </div>
</a>""")

    # Recent audits table (top 10 by most-recent audited_at)
    recent = sorted(skills, key=lambda x: x.get("audited_at") or "", reverse=True)[:10]
    recent_rows = "".join(render_skill_row(skill=sk, lang=lang, link_prefix="")
                          for sk in recent)

    head = base_html_head(title=s("site_title", lang) + " · " + s("eyebrow", lang),
                          lang=lang, css_relative_path="../assets/style.css",
                          description=s("tagline", lang))

    html_out = f"""{head}
{lang_toggle_html(current_lang=lang, sibling_paths={'en': '../en/', 'zh': '../zh/'})}
<div class="wrap">

<p class="eyebrow">{s('eyebrow', lang)}</p>
<h1 class="title">{s('site_title', lang)}</h1>
<p class="subtitle">{s('tagline', lang)}</p>

<div class="stats">
  <div class="stat"><p class="stat-label">{s('stat_skills', lang)}</p><p class="stat-value">{total_n}</p></div>
  <div class="stat"><p class="stat-label">{s('stat_categories', lang)}</p><p class="stat-value">{n_categories} / {len(taxonomy.get('taxonomy', []))}</p></div>
  <div class="stat"><p class="stat-label">{s('stat_avg_score', lang)}</p><p class="stat-value accent">{avg}/100</p></div>
  <div class="stat"><p class="stat-label">{s('stat_clean_passes', lang)}</p><p class="stat-value">{a_passes} / {total_n}</p></div>
</div>

<h2 class="section-title">{s('sec_categories', lang)}</h2>
<div class="category-grid">
{''.join(tiles)}
</div>

<h2 class="section-title">{s('sec_recent', lang)}</h2>
<table class="skill-table">
<thead><tr>
<th>{s('th_skill', lang)}</th><th>{s('th_grade', lang)}</th>
<th>{s('th_score', lang)}</th><th>{s('th_findings', lang)}</th>
<th>{s('th_source', lang)}</th>
</tr></thead>
<tbody>{recent_rows}</tbody>
</table>

{page_footer(lang)}
</div>
{lang_toggle_script()}
</body>
</html>"""
    page_path.write_text(html_out, encoding="utf-8")


def write_category_page(*, lang: str, out_root: Path, taxonomy: dict,
                         top: dict, skills_in_top: list[dict]) -> None:
    """category/<tid>/index.html — subcategory grouped skill list."""
    tid = top["id"]
    page_dir = out_root / lang / "category" / tid
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / "index.html"

    title = top.get(f"title_{lang}") or top.get("title_en") or tid
    desc = top.get(f"description_{lang}") or top.get("description_en") or ""

    # Group by subcategory
    subs = {sub["id"]: sub for sub in top.get("subcategories", [])}
    by_sub: dict[str, list[dict]] = defaultdict(list)
    for sk in skills_in_top:
        by_sub[sk.get("subcategory") or "_other"].append(sk)

    sub_sections = []
    for sub_id, sub in subs.items():
        sk_list = by_sub.get(sub_id, [])
        sub_title = sub.get(f"title_{lang}") or sub.get("title_en") or sub_id
        if not sk_list:
            continue
        rows = "".join(render_skill_row(skill=sk, lang=lang, link_prefix="../../")
                       for sk in sk_list)
        sub_sections.append(f"""
<div class="subcat-section">
  <h3>{html.escape(sub_title)}</h3>
  <p class="subcat-meta">{len(sk_list)} {s('stat_skills', lang).lower()}</p>
  <table class="skill-table">
    <thead><tr>
      <th>{s('th_skill', lang)}</th><th>{s('th_grade', lang)}</th>
      <th>{s('th_score', lang)}</th><th>{s('th_findings', lang)}</th>
      <th>{s('th_source', lang)}</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>""")

    if not sub_sections:
        sub_sections.append(f"<p style='color:var(--text-quiet)'>{s('no_skills_yet', lang)}</p>")

    head = base_html_head(title=f"{title} · {s('site_title', lang)}",
                          lang=lang, css_relative_path="../../../assets/style.css",
                          description=desc)

    crumbs = f"""
<div class="crumbs">
  <a href="../../">{s('crumbs_home', lang)}</a> · {html.escape(title)}
</div>"""

    html_out = f"""{head}
{lang_toggle_html(current_lang=lang, sibling_paths={'en': f'../../../en/category/{tid}/', 'zh': f'../../../zh/category/{tid}/'})}
<div class="wrap">
{crumbs}
<p class="eyebrow">{s('eyebrow', lang)}</p>
<h1 class="title">{html.escape(title)}</h1>
<p class="subtitle">{html.escape(desc)}</p>
<p class="subcat-meta">{s('found_n_skills', lang, n=len(skills_in_top))}</p>

<h2 class="section-title">{s('sec_subcategories', lang)}</h2>
{''.join(sub_sections)}

<p style="margin-top:30px"><a class="skill-link" href="../../">{s('back_to_home', lang)}</a></p>
{page_footer(lang)}
</div>
{lang_toggle_script()}
</body>
</html>"""
    page_path.write_text(html_out, encoding="utf-8")


def write_skill_page(*, lang: str, out_root: Path, skill: dict,
                     report_md: str, top: dict | None) -> None:
    name = skill.get("skill_name") or "unnamed"
    slug = safe_slug(name)
    page_dir = out_root / lang / "skill" / slug
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / "index.html"

    desc = skill.get("description", "")
    title_main = top.get(f"title_{lang}") if top else None
    tid = top["id"] if top else None
    crumbs_top = (f"<a href='../../category/{tid}/'>{html.escape(title_main)}</a> · "
                  if top and title_main else "")
    crumbs = f"""
<div class="crumbs">
  <a href="../../">{s('crumbs_home', lang)}</a> · {crumbs_top}{html.escape(name)}
</div>"""

    body_html = md_to_html(report_md)

    head = base_html_head(
        title=f"{name} · {s('site_title', lang)}",
        lang=lang, css_relative_path="../../../assets/style.css",
        description=desc,
    )

    html_out = f"""{head}
{lang_toggle_html(current_lang=lang, sibling_paths={'en': f'../../../en/skill/{slug}/', 'zh': f'../../../zh/skill/{slug}/'})}
<div class="wrap">
{crumbs}
<div class="report-body">{body_html}</div>
{page_footer(lang)}
</div>
{lang_toggle_script()}
</body>
</html>"""
    page_path.write_text(html_out, encoding="utf-8")


def write_root_index(*, out_root: Path) -> None:
    """Top-level out_root/index.html → simple landing that redirects via cookie/JS."""
    out_root.mkdir(parents=True, exist_ok=True)
    out_root.joinpath("index.html").write_text("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>TAR Engine 米其林指南</title>
<meta http-equiv="refresh" content="0; url=en/"></head>
<body><p>Redirecting to <a href="en/">English</a> / <a href="zh/">中文</a></p>
</body></html>""", encoding="utf-8")


def write_assets(*, out_root: Path) -> None:
    assets = out_root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "style.css").write_text(CSS, encoding="utf-8")


# ── Driver ──────────────────────────────────────────────────────────────


def load_inputs(input_dir: Path) -> list[tuple[dict, str]]:
    """Return list of (meta, markdown_body) tuples in input_dir."""
    out = []
    for meta_path in sorted(input_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        md_path = meta_path.with_suffix("").with_suffix(".md")
        if not md_path.exists():
            continue
        out.append((meta, md_path.read_text(encoding="utf-8")))
    return out


def build_lang(*, lang: str, input_dir: Path, out_root: Path, taxonomy: dict) -> int:
    items = load_inputs(input_dir)
    if not items:
        print(f"  no inputs in {input_dir}", file=sys.stderr)
        return 0
    skills = [m for m, _ in items]
    # 1. landing
    write_landing(lang=lang, out_root=out_root, taxonomy=taxonomy, skills=skills)
    # 2. per top-level category page
    by_top: dict[str, list[dict]] = defaultdict(list)
    for sk in skills:
        if sk.get("top_level"):
            by_top[sk["top_level"]].append(sk)
    for top in taxonomy.get("taxonomy", []):
        write_category_page(
            lang=lang, out_root=out_root, taxonomy=taxonomy,
            top=top, skills_in_top=by_top.get(top["id"], []),
        )
    # 3. per-skill pages
    top_by_id = {t["id"]: t for t in taxonomy.get("taxonomy", [])}
    for meta, md in items:
        top = top_by_id.get(meta.get("top_level"))
        write_skill_page(lang=lang, out_root=out_root,
                         skill=meta, report_md=md, top=top)
    return len(items)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--en-input", help="batch_audit_v2 output dir for EN")
    parser.add_argument("--zh-input", help="batch_audit_v2 output dir for ZH")
    parser.add_argument("--output", required=True, help="Site root (e.g. /opt/michelin/v2)")
    args = parser.parse_args()

    taxonomy = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))
    out_root = Path(args.output)
    write_assets(out_root=out_root)
    write_root_index(out_root=out_root)

    total = 0
    if args.en_input:
        n = build_lang(lang="en", input_dir=Path(args.en_input),
                       out_root=out_root, taxonomy=taxonomy)
        print(f"  EN: {n} skill pages built", file=sys.stderr)
        total += n
    if args.zh_input:
        n = build_lang(lang="zh", input_dir=Path(args.zh_input),
                       out_root=out_root, taxonomy=taxonomy)
        print(f"  ZH: {n} skill pages built", file=sys.stderr)
        total += n
    print(f"\n✅ Built site at {out_root} (total skill pages: {total})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
