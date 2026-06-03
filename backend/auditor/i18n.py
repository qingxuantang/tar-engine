"""i18n for audit rule strings.

The rule registry in risk_guardrail.py stores English as the canonical source
(message / description / fix_template). This module provides translations for
other supported languages, keyed by stable `rule_id`.

Why keyed by rule_id (not rule.name):
- rule.name can be renamed; rule_id is permanent.
- A finding cites rule_id; translation lookup uses the same key.

Adding a new language:
- Append to SUPPORTED_LANGUAGES
- Add the language key under each rule's translations dict below

Adding a new rule:
- Add a new entry keyed by its rule_id in RULE_TRANSLATIONS
- Each entry must have at least the languages in SUPPORTED_LANGUAGES, otherwise
  get_translated_rule() falls back to DEFAULT_LANG for missing fields.
"""
from __future__ import annotations

from typing import Optional


DEFAULT_LANG = "en"
SUPPORTED_LANGUAGES = ("en", "zh")


# Display labels for severity / categories / section headers — used by the
# report formatter and the website chrome.
UI_STRINGS = {
    "en": {
        "severity_critical": "Critical",
        "severity_high": "High",
        "severity_warning": "Warning",
        "severity_info": "Info",
        "severity_none": "None",
        "risk_class_critical": "Critical risk",
        "risk_class_high": "High risk",
        "risk_class_medium": "Medium risk",
        "risk_class_low": "Low risk",
    },
    "zh": {
        "severity_critical": "严重",
        "severity_high": "高",
        "severity_warning": "警告",
        "severity_info": "提示",
        "severity_none": "无",
        "risk_class_critical": "严重风险",
        "risk_class_high": "高风险",
        "risk_class_medium": "中等风险",
        "risk_class_low": "低风险",
    },
}


# Category display names per language. Universal categories first; domains
# can add their own via DomainConfig.categories (those should likewise be
# localized at the domain layer when we get there).
CATEGORY_DISPLAY_NAMES_I18N = {
    "en": {
        "prompt_injection": "Prompt injection / scope override",
        "shell_safety": "Shell safety",
        "file_access": "Sensitive file access",
        "data_exfil": "Data exfiltration",
        "credential_exposure": "Credential exposure",
        "malicious_payload": "Malicious payload signatures",
        "capability_drift": "Capability drift (runtime only)",
        "uncategorized": "Uncategorized",
    },
    "zh": {
        "prompt_injection": "Prompt 注入 / 越权指令",
        "shell_safety": "Shell 安全",
        "file_access": "敏感文件访问",
        "data_exfil": "数据外泄",
        "credential_exposure": "凭据泄露",
        "malicious_payload": "恶意 payload 特征",
        "capability_drift": "权限漂移（仅运行时）",
        "uncategorized": "未分类",
    },
}


# Per-rule translations. The "en" entry should match what's in risk_guardrail.py.
# When the LLM-side default differs from English, the rule's source-of-truth is
# English (as set in risk_guardrail.py) and this dict mirrors it for completeness.
RULE_TRANSLATIONS = {
    # ── file_access ────────────────────────────────────────────────
    "FA-001": {
        "en": {
            "message": "Access to sensitive configuration files",
            "description": "Reads or writes files commonly used to hold secrets (.env, .ssh, *.key, *.pem)",
            "fix_template": "Remove direct references to .env / .ssh / *.key / *.pem; load secrets from a runtime config service or environment variable instead of naming the file in the skill body.",
        },
        "zh": {
            "message": "访问了敏感配置文件",
            "description": "读写常用于存放 secret 的文件（.env / .ssh / *.key / *.pem 等）",
            "fix_template": "不要在 skill 正文里直接引用 .env / .ssh / *.key / *.pem 这类文件；改为从运行时配置服务或环境变量加载 secret，不要把文件名写死。",
        },
    },
    # ── shell_safety ───────────────────────────────────────────────
    "SS-001": {
        "en": {
            "message": "Potentially destructive bash command detected",
            "description": "Commands that can irreversibly drop tables, wipe filesystems, or rewrite git history",
            "fix_template": "Replace `rm -rf` with `trash` or `mv` to a tombstone directory. For SQL, require explicit confirmation before DROP/TRUNCATE. Never instruct the LLM to use `--force` on a git push.",
        },
        "zh": {
            "message": "检测到可能的破坏性 shell 命令",
            "description": "可能造成不可逆数据丢失的命令（drop / truncate 表、rm -rf、git push --force 等）",
            "fix_template": "把 `rm -rf` 换成 `trash` 或 `mv` 到墓碑目录。SQL 的 DROP / TRUNCATE 一定要先做显式二次确认。永远不要让 LLM 用 `git push --force`。",
        },
    },
    "SS-002": {
        "en": {
            "message": "Use of --force / --no-verify flags that bypass safety checks",
            "description": "Force flags that skip pre-commit hooks, verification steps, or permission checks",
            "fix_template": "Drop `--force` / `--no-verify` from the skill body. If a hook is failing, fix the hook — don't tell the LLM to skip it. For chmod, use minimum-needed mode (e.g. 600/644) instead of 777.",
        },
        "zh": {
            "message": "使用了绕过安全检查的强制标志",
            "description": "类似 --force / --no-verify 这种跳过 hook、跳过验证、跳过权限检查的标志",
            "fix_template": "把 `--force` / `--no-verify` 从 skill 正文里移除。如果 hook 报错，去修 hook 而不是叫 LLM 跳过。chmod 用够用的权限（600/644），不要 777。",
        },
    },
    "SS-003": {
        "en": {
            "message": "Piping remote content directly to shell execution",
            "description": "Curl/wget piped into bash/sh/python — the upstream can serve different payload on the next request",
            "fix_template": "Download to a file, checksum it against a published hash, then execute. Never `curl … | sh` — the upstream may serve a different payload on the next request.",
        },
        "zh": {
            "message": "检测到远程内容直接管道到 shell 执行（高危）",
            "description": "curl/wget 直接管道到 bash/sh/python——上游服务方下次请求可以换 payload",
            "fix_template": "下载到本地文件，对公开 hash 校验后再执行。永远不要 `curl … | sh`，上游服务方可以随时换 payload。",
        },
    },
    "SS-004": {
        "en": {
            "message": "Use of sudo for privilege escalation",
            "description": "Sudo invocation inside the skill body suggests it needs elevated permissions at runtime",
            "fix_template": "Skills should run as a user with the privileges they need. If sudo is required, surface it as a one-time setup step in `## Prerequisites`, not in the runtime body.",
        },
        "zh": {
            "message": "检测到 sudo 提权操作",
            "description": "skill 正文出现 sudo，意味着需要运行时提权",
            "fix_template": "Skill 应该以已经具备所需权限的用户运行。如果确实要 sudo，应作为 `## Prerequisites` 段的一次性 setup，不要放在运行时执行的步骤里。",
        },
    },
    # ── data_exfil ─────────────────────────────────────────────────
    "DE-001": {
        "en": {
            "message": "Sending data to external URL via POST/upload",
            "description": "Outbound POST or multipart upload to an external endpoint",
            "fix_template": "If the POST is intentional (webhook, API integration), declare its destination in SKILL.md `## Network Egress` section so audit can allowlist it. Otherwise remove.",
        },
        "zh": {
            "message": "向外部 URL 发送数据",
            "description": "向外部 endpoint 发起 POST 或 multipart 上传",
            "fix_template": "如果这是有意的（webhook / API 集成），请在 SKILL.md `## Network Egress` 段声明目的地，让 audit 可以白名单。否则就删掉。",
        },
    },
    "DE-002": {
        "en": {
            "message": "Known C2 / exfiltration staging domain detected",
            "description": "Domain matches a known list of out-of-band callback / exfil services (Burp Collaborator, oastify, webhook.site, etc.)",
            "fix_template": "These are known C2/exfil staging domains. If you're using one for a legitimate test, replace it with a self-hosted echo endpoint and document the purpose in `## Network Egress`.",
        },
        "zh": {
            "message": "命中已知的 C2 / 数据外泄域名",
            "description": "域名匹配已知的 OOB callback / 数据外泄服务（Burp Collaborator / oastify / webhook.site 等）",
            "fix_template": "这些是已知的 C2 / 数据外泄域名。如果是做合法测试，改用自建 echo endpoint，并在 `## Network Egress` 段说明用途。",
        },
    },
    "DE-003": {
        "en": {
            "message": "Broad system info collection pattern",
            "description": "Reads /etc/passwd, env vars, shell history, or browsing data — classic exfil precursor",
            "fix_template": "Skills should read only what they specifically need. Bulk dumps of /etc/passwd, environ, or shell history piped to network is the classic exfil pattern — narrow the scope or remove.",
        },
        "zh": {
            "message": "检测到广泛的系统信息收集模式",
            "description": "读 /etc/passwd / env / shell 历史 / 浏览数据——经典的外泄前置动作",
            "fix_template": "Skill 应该只读它真正需要的东西。把 /etc/passwd、环境变量、shell 历史这种批量数据通过网络送出去是典型外泄套路——缩小范围或者删掉。",
        },
    },
    # ── credential_exposure ────────────────────────────────────────
    "CE-001": {
        "en": {
            "message": "Hardcoded API key, secret, or password",
            "description": "Literal credential value embedded in the skill body (api_key, secret, password, token, etc.)",
            "fix_template": "Replace hardcoded secrets with `${VAR_NAME}` placeholders and document the env var in SKILL.md `## Required Environment`. Rotate any secret that touched git history.",
        },
        "zh": {
            "message": "疑似 API key 或密码硬编码",
            "description": "skill 正文里写死了 credential 字面值（api_key / secret / password / token 等）",
            "fix_template": "用 `${VAR_NAME}` 这样的占位符替换硬编码 secret，并在 SKILL.md `## Required Environment` 段记录环境变量。任何进过 git 历史的 secret 必须立刻轮换。",
        },
    },
    # ── prompt_injection ───────────────────────────────────────────
    "PI-001": {
        "en": {
            "message": "Prompt injection bypass detected (override of prior instructions)",
            "description": "Telling the agent to ignore / disregard / forget previous instructions",
            "fix_template": "Remove the instruction. If you legitimately need the LLM to deprioritize an earlier directive, rephrase as a positive constraint (`use the latest config from X`) instead of telling it to ignore prior rules.",
        },
        "zh": {
            "message": "检测到 Prompt 注入（要求 Agent 忽略前置指令）",
            "description": "让 Agent 忽略 / 跳过 / 忘记之前的指令",
            "fix_template": "把这条指令删掉。如果确实需要让 LLM 暂时降级一个早先的指令，改成正向约束（如「使用 X 的最新配置」），不要叫它「ignore previous」。",
        },
    },
    "PI-002": {
        "en": {
            "message": "Hidden instruction — telling the agent to hide actions from the user",
            "description": "Instructions like `do not mention`, `keep this secret`, `user should not see` — these short-circuit user trust",
            "fix_template": "Skills must not hide actions from the user. If the goal is to suppress verbose output, declare it as `## Output: summary only`. Anything done on the user's behalf must be reportable.",
        },
        "zh": {
            "message": "检测到隐藏指令（要求 Agent 对用户隐瞒操作）",
            "description": "类似「do not mention」「keep secret」「user should not see」这种让 Agent 对用户隐藏操作的指令",
            "fix_template": "Skill 绝对不能对用户隐藏自己做了什么。如果是想让输出更简洁，应当声明 `## Output: summary only`。代用户做的任何动作都必须可上报。",
        },
    },
    "PI-003": {
        "en": {
            "message": "Excessive permission claim — `full access` / `no restrictions`",
            "description": "Claims of unrestricted access; these confuse the model and don't actually elevate capabilities",
            "fix_template": "Replace blanket permission claims with the specific tool/scope the skill actually needs (e.g. `Read access to ./data/`). The cockpit enforces capabilities — claiming `no restrictions` only confuses the LLM.",
        },
        "zh": {
            "message": "声称拥有无限制权限或绕过安全约束",
            "description": "声称「full access」「no restrictions」——这种声明只会让模型困惑，不会真的提升权限",
            "fix_template": "把笼统的权限声明换成 skill 真正需要的具体工具 / 范围（比如「Read access to ./data/」）。Cockpit 是真正的权限边界，写「no restrictions」只会让 LLM 误判。",
        },
    },
    "PI-004": {
        "en": {
            "message": "Disable safety / logging / verification instruction",
            "description": "Telling the agent to disable safety features, logging, or verification",
            "fix_template": "Remove. Safety/logging are infrastructural; skills don't get to turn them off. If a specific check is too noisy, surface that as feedback to engine maintainers instead of telling the LLM to skip it.",
        },
        "zh": {
            "message": "要求禁用安全特性 / 日志 / 验证机制",
            "description": "让 Agent 关掉安全保护、日志、验证机制等基础设施级别的能力",
            "fix_template": "删掉。安全 / 日志是基础设施层面的，skill 没有权限关。如果某个检查太吵，应该作为反馈反馈给 engine 维护者，而不是让 LLM 跳过。",
        },
    },
    "PI-005": {
        "en": {
            "message": "Impersonation instruction — `pretend to be the user / admin / root`",
            "description": "Telling the agent to impersonate the user or other identities — opens reply-to-self loops and forged messages",
            "fix_template": "Replace impersonation with a role description (`act as a code reviewer`). Never `pretend to be the user` — that's how reply-to-self loops and forged messages happen.",
        },
        "zh": {
            "message": "要求 Agent 冒充用户或其他身份",
            "description": "让 Agent 假扮用户 / 管理员 / root 等身份——会引发自我回复循环、伪造消息等问题",
            "fix_template": "把冒充改成角色描述（「以 code reviewer 视角处理」）。永远不要写「pretend to be the user」——这是自我回复循环和消息伪造的源头。",
        },
    },
    # ── malicious_payload ──────────────────────────────────────────
    "MP-001": {
        "en": {
            "message": "Encoded payload pattern (base64 decode + eval)",
            "description": "Base64/hex payload followed by eval, atob, or Buffer.from — classic obfuscation",
            "fix_template": "If the encoding is for a legitimate reason (binary data, image), use a well-known library API instead of inline `eval(atob(...))`. The `eval+decode` pattern is almost always exploit-pattern.",
        },
        "zh": {
            "message": "检测到编码 payload（base64 解码后 eval）",
            "description": "base64 / hex payload 后接 eval、atob、Buffer.from——经典混淆套路",
            "fix_template": "如果是合理需求（处理二进制 / 图片），用成熟的库 API，不要用 inline `eval(atob(...))`。「eval+decode」组合 99% 是 exploit 模式。",
        },
    },
    "MP-002": {
        "en": {
            "message": "Cryptocurrency miner reference detected",
            "description": "Strings matching known miners (xmrig, cryptonight, stratum+tcp pools, etc.)",
            "fix_template": "Remove. Skills should not run miners. If you're auditing miner infrastructure as part of a research project, gate the skill behind explicit `--allow-malware-strings` and document the research context.",
        },
        "zh": {
            "message": "检测到加密货币挖矿相关内容",
            "description": "命中已知挖矿工具特征（xmrig / cryptonight / stratum+tcp 矿池等）",
            "fix_template": "删掉。Skill 不应该跑矿机。如果是研究项目里审计挖矿基础设施，应该用显式的 `--allow-malware-strings` 开关 gate 住，并在 skill 里说明研究背景。",
        },
    },
    "MP-003": {
        "en": {
            "message": "Reverse shell payload detected",
            "description": "Patterns for opening a callback connection back to attacker-controlled infrastructure",
            "fix_template": "Remove. If you need remote access for ops, use a managed service (Tailscale, SSH with explicit keys) — not raw reverse shells in a skill body.",
        },
        "zh": {
            "message": "检测到反向 shell payload（高危）",
            "description": "向攻击者基础设施发起回连的 payload 模式",
            "fix_template": "删掉。运维需要远程访问的话，用托管服务（Tailscale / 明示 key 的 SSH 等）——不要把裸 reverse shell 写在 skill 正文里。",
        },
    },
}


def get_translated_rule(rule_id: str, lang: str = DEFAULT_LANG) -> dict[str, str]:
    """Return the localized message / description / fix_template for a rule.

    Falls back to DEFAULT_LANG for missing fields, then to empty string.
    Lookup is by rule_id (stable across rule.name renames).

    Returns a dict with at least {"message", "description", "fix_template"};
    callers can rely on those keys always being present.
    """
    entry = RULE_TRANSLATIONS.get(rule_id) or {}
    bundle = entry.get(lang) or entry.get(DEFAULT_LANG) or {}
    return {
        "message": bundle.get("message", ""),
        "description": bundle.get("description", ""),
        "fix_template": bundle.get("fix_template", ""),
    }


def get_ui_string(key: str, lang: str = DEFAULT_LANG) -> str:
    """Return a UI/chrome string (severity labels, risk class names, ...)."""
    bundle = UI_STRINGS.get(lang) or UI_STRINGS.get(DEFAULT_LANG, {})
    return bundle.get(key) or UI_STRINGS.get(DEFAULT_LANG, {}).get(key, key)


def get_category_display(category: str, lang: str = DEFAULT_LANG) -> str:
    """Return the localized display name for a category id."""
    bundle = CATEGORY_DISPLAY_NAMES_I18N.get(lang) or CATEGORY_DISPLAY_NAMES_I18N.get(DEFAULT_LANG, {})
    return bundle.get(category) or CATEGORY_DISPLAY_NAMES_I18N.get(DEFAULT_LANG, {}).get(category, category)


def negotiate_language(requested: Optional[str], accept_language: Optional[str] = None) -> str:
    """Pick the response language.

    Order of precedence:
      1. Explicit `requested` parameter (e.g. body field `lang`)
      2. Accept-Language HTTP header — first supported language match
      3. DEFAULT_LANG

    Returns a value in SUPPORTED_LANGUAGES.
    """
    if requested and requested in SUPPORTED_LANGUAGES:
        return requested
    if requested:
        # Common variants: "zh-CN" / "zh-TW" / "en-US"
        prefix = requested.split("-")[0].lower()
        if prefix in SUPPORTED_LANGUAGES:
            return prefix
    if accept_language:
        # Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
        for token in accept_language.split(","):
            tag = token.strip().split(";")[0].strip().lower()
            if not tag:
                continue
            if tag in SUPPORTED_LANGUAGES:
                return tag
            prefix = tag.split("-")[0]
            if prefix in SUPPORTED_LANGUAGES:
                return prefix
    return DEFAULT_LANG
