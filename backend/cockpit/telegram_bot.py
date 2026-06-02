"""Cockpit Telegram bot — UX A primary entry (2026-05-10).

Long-polls Telegram for incoming text messages, treats each one as a wish,
runs it through Planner + skill_executor, and streams progress back to the
user via editMessageText.

Run as a long-running asyncio task launched from app.py startup, OR as a
standalone script: `python -m cockpit.telegram_bot`.

Env vars:
    COCKPIT_TELEGRAM_BOT_TOKEN     bot token (REQUIRED)
    COCKPIT_TELEGRAM_ALLOWED_USERS comma-separated Telegram user_ids (REQUIRED;
                                   refuse messages from anyone else)
    COCKPIT_TELEGRAM_LLM_BASE_URL  default LLM endpoint (per allowed user, OR
    COCKPIT_TELEGRAM_LLM_API_KEY   set per-user via /llm command — see below)
    COCKPIT_TELEGRAM_LLM_MODEL

Bot commands:
    /start          show greeting + register user
    /help           usage info
    /budget         today's token + USD usage
    /llm <base_url> <api_key> <model>
                    set per-user LLM override (stored in profile)
    /cancel         cancel last running wish (TODO; not yet implemented)
    <any text>      treated as a wish

The bot is one-bot-per-engine (single token), but allow-list of user_ids
gates access. For Cockpit Edition cloud SaaS, this is per-tenant and each
tenant runs their own bot token (or the engine multiplexes via tenant id).

Why not python-telegram-bot lib: keeping deps lean. httpx + getUpdates is
~80 lines for what we need.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .models import LLMConfig, WishTask
from .planner import Planner
from .profile_writer import get_profile_writer
from .store import get_store
from .token_budget import BudgetConfig, format_cost_summary
from .wish_runner import run_wish_async

logger = logging.getLogger("cockpit.telegram")

API_BASE = "https://api.telegram.org/bot{token}"
LONG_POLL_TIMEOUT = 25  # seconds — Telegram allows up to 50

PROGRESS_THROTTLE_S = 1.5  # min seconds between editMessageText calls


def _allowed_users() -> set[str]:
    raw = os.environ.get("COCKPIT_TELEGRAM_ALLOWED_USERS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def _default_llm_config() -> Optional[LLMConfig]:
    base = os.environ.get("COCKPIT_TELEGRAM_LLM_BASE_URL")
    key = os.environ.get("COCKPIT_TELEGRAM_LLM_API_KEY")
    model = os.environ.get("COCKPIT_TELEGRAM_LLM_MODEL")
    if not (base and key and model):
        return None
    return LLMConfig(base_url=base, api_key=key, model=model)


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------


class TelegramAPI:
    """Thin async wrapper over Telegram Bot API."""

    def __init__(self, token: str):
        self.token = token
        self.base = API_BASE.format(token=token)

    async def get_updates(self, offset: int) -> list[dict]:
        params = {"offset": offset, "timeout": LONG_POLL_TIMEOUT, "allowed_updates": '["message"]'}
        async with httpx.AsyncClient(timeout=LONG_POLL_TIMEOUT + 5) as c:
            r = await c.get(f"{self.base}/getUpdates", params=params)
        if r.status_code != 200:
            logger.warning("getUpdates HTTP %d: %s", r.status_code, r.text[:200])
            return []
        return r.json().get("result", [])

    async def send_message(self, chat_id: str, text: str, **kwargs) -> dict:
        payload = {"chat_id": chat_id, "text": text[:4000], **kwargs}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.base}/sendMessage", json=payload)
        return r.json() if r.status_code == 200 else {}

    async def edit_message_text(self, chat_id: str, message_id: int, text: str, **kwargs) -> dict:
        payload = {
            "chat_id": chat_id, "message_id": message_id,
            "text": text[:4000], **kwargs,
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.base}/editMessageText", json=payload)
        return r.json() if r.status_code == 200 else {}


# ---------------------------------------------------------------------------
# Wish processing
# ---------------------------------------------------------------------------


def _format_progress(events: list[dict], wish_text: str) -> str:
    """Render a progress board from accumulated events."""
    lines = [f"💭 {wish_text[:120]}", "", "**进度**"]
    for e in events[-12:]:  # show last 12 to avoid Telegram length limit
        t = e.get("type", "")
        if t == "wish_started":
            lines.append(f"🚀 开始执行（{e.get('chain_len', '?')} 个 skill）")
        elif t == "skill_dispatch":
            lines.append(f"📦 [{e.get('step_idx', 0)+1}/{e.get('step_count', '?')}] {e.get('skill', '')}")
        elif t == "skill_started":
            lines.append(f"  ▶️ {e.get('skill', '')}")
        elif t == "tool_call":
            tool = e.get("tool", "")
            lines.append(f"  🔧 {tool}")
        elif t == "skill_completed":
            lines.append(f"  ✅ {e.get('skill', '')} ({e.get('iterations', 0)} steps)")
        elif t == "skill_error":
            lines.append(f"  ❌ {e.get('skill', '')}: {e.get('error', '')[:60]}")
        elif t == "skill_truncated":
            lines.append(f"  ⏱️ {e.get('skill', '')} 超过迭代上限")
        elif t == "budget_exceeded":
            lines.append(f"  💸 超预算 ({e.get('kind', '')}: {e.get('current', 0)}/{e.get('limit', 0)})")
        elif t == "wish_completed":
            lines.append(
                f"\n{'✅' if e.get('success') else '⚠️'} 完成（{e.get('duration_s', 0)}s）"
                f"\n{e.get('cost_summary', '')}"
            )
    return "\n".join(lines)


async def _process_wish(
    api: TelegramAPI,
    chat_id: str,
    user_id: str,
    wish_text: str,
    progress_message_id: int,
    cfg: LLMConfig,
) -> None:
    """Run one wish: plan → execute → stream progress → final report."""
    task = WishTask(wish=wish_text, user_id=user_id, context={})
    store = get_store()
    store.save_wish(task.to_dict())

    profile = get_profile_writer().get(user_id)
    planner = Planner(llm_config=cfg)
    store.update_wish_status(task.task_id, "planning")

    await api.edit_message_text(
        chat_id, progress_message_id,
        f"💭 {wish_text[:200]}\n\n🧠 正在规划...",
    )

    try:
        plan = planner.plan(task, profile=profile)
    except Exception as e:
        logger.exception("planner failed")
        await api.edit_message_text(
            chat_id, progress_message_id,
            f"💭 {wish_text[:200]}\n\n❌ 规划失败：{e}",
        )
        store.update_wish_status(task.task_id, "failed")
        return

    store.save_plan(plan.to_dict(), raw_llm_response=plan.raw_llm_response or "")

    chain_summary = "\n".join(
        f"  {i+1}. {step.get('skill', '?')} — {step.get('sub_goal', step.get('rationale', ''))[:80]}"
        for i, step in enumerate(plan.skill_chain)
    )
    await api.edit_message_text(
        chat_id, progress_message_id,
        f"💭 {wish_text[:200]}\n\n📋 计划：\n{chain_summary}\n\n开始执行...",
    )

    # Progress streaming via on_event
    events_buffer: list[dict] = []
    last_edit_at = [datetime.now().timestamp()]

    async def edit_with_throttle(text: str) -> None:
        now = datetime.now().timestamp()
        if now - last_edit_at[0] < PROGRESS_THROTTLE_S:
            return
        last_edit_at[0] = now
        try:
            await api.edit_message_text(chat_id, progress_message_id, text, parse_mode="Markdown")
        except Exception:
            logger.exception("edit_message_text failed; ignoring")

    def on_event(e: dict) -> None:
        events_buffer.append(e)
        # Schedule a throttled edit. Use create_task but don't await.
        try:
            loop = asyncio.get_event_loop()
            text = _format_progress(events_buffer, wish_text)
            asyncio.run_coroutine_threadsafe(edit_with_throttle(text), loop)
        except RuntimeError:
            # No running event loop in this thread (executor) — best-effort skip.
            pass

    # Run wish
    try:
        result = await run_wish_async(
            task=task, plan=plan, cfg=cfg,
            on_event=on_event,
            budget_config=BudgetConfig.from_env(),
        )
    except Exception as e:
        logger.exception("wish_runner crashed")
        await api.edit_message_text(
            chat_id, progress_message_id,
            f"💭 {wish_text[:200]}\n\n💥 执行崩溃：{type(e).__name__}: {e}",
        )
        store.update_wish_status(task.task_id, "failed")
        return

    # Final message
    final_text = result.get("final_output") or "(no output)"
    cost = format_cost_summary(result.get("token_usage", {}), model=cfg.model)
    duration = result.get("duration_s", 0)

    body = (
        f"💭 {wish_text[:200]}\n\n"
        f"{'✅' if result['skill_results'] and all(r.get('success') for r in result['skill_results']) else '⚠️'} 完成（{duration}s）\n\n"
        f"{final_text[:3000]}\n\n"
        f"---\n💰 {cost}"
    )
    await api.edit_message_text(chat_id, progress_message_id, body)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def _handle_command(api: TelegramAPI, chat_id: str, user_id: str, text: str) -> bool:
    """Handle /-commands. Returns True if message was a command (handled)."""
    if not text.startswith("/"):
        return False

    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/start", "/help"):
        await api.send_message(chat_id, _help_text())
        return True

    if cmd == "/budget":
        store = get_store()
        from datetime import datetime as _dt
        today = _dt.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        sums = store.sum_user_tokens_since(user_id, today)
        msg = (
            f"📊 今日用量\n\n"
            f"  Wishes: {sums['wish_count']}\n"
            f"  Tokens: {sums['total_tokens']:,} (in={sums['prompt_tokens']:,} out={sums['completion_tokens']:,})\n"
            f"  ≈ ${sums['estimated_cost_usd']:.4f}"
        )
        await api.send_message(chat_id, msg)
        return True

    if cmd == "/llm":
        # /llm <base_url> <api_key> <model>
        if len(parts) < 4:
            await api.send_message(chat_id, "用法: `/llm <base_url> <api_key> <model>`", parse_mode="Markdown")
            return True
        prof_writer = get_profile_writer()
        prof = prof_writer.get(user_id)
        prof.setdefault("llm_override", {})
        prof["llm_override"] = {
            "base_url": parts[1],
            "api_key": parts[2],
            "model": parts[3],
        }
        get_store().save_profile(user_id, prof)
        await api.send_message(chat_id, "✅ 已保存你的 LLM 配置。注意：API key 持久化在 engine 端 SQLite。")
        return True

    if cmd == "/cancel":
        await api.send_message(chat_id, "TODO: cancel 还没做。当前 wish 会跑到完成或预算 cap。")
        return True

    await api.send_message(chat_id, f"未知命令：{cmd}\n\n{_help_text()}")
    return True


def _help_text() -> str:
    return (
        "🚀 Cockpit Telegram 入口\n\n"
        "直接发消息 = 提交 wish\n"
        "/budget = 今日用量\n"
        "/llm <base_url> <key> <model> = 设置 LLM 配置\n"
        "/cancel = 取消当前 wish (待实现)\n\n"
        "注意：每个 wish 都按 BYOK 走你的 LLM key 计费。"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _resolve_user_cfg(user_id: str) -> Optional[LLMConfig]:
    """Per-user LLM config. Falls back to env defaults."""
    profile = get_profile_writer().get(user_id)
    override = (profile or {}).get("llm_override") or {}
    if override.get("base_url") and override.get("api_key") and override.get("model"):
        return LLMConfig(
            base_url=override["base_url"],
            api_key=override["api_key"],
            model=override["model"],
        )
    return _default_llm_config()


async def _handle_message(api: TelegramAPI, msg: dict) -> None:
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    from_user = msg.get("from") or {}
    user_id = str(from_user.get("id", ""))
    text = (msg.get("text") or "").strip()

    if not chat_id or not user_id or not text:
        return

    if user_id not in _allowed_users():
        logger.warning("rejected message from unauthorized user_id=%s", user_id)
        await api.send_message(chat_id, "⛔ 你不在 cockpit 的允许列表里。")
        return

    if await _handle_command(api, chat_id, user_id, text):
        return

    cfg = _resolve_user_cfg(user_id)
    if not cfg:
        await api.send_message(
            chat_id,
            "❌ 没找到 LLM 配置。请用 `/llm <base_url> <api_key> <model>` 设一个。",
            parse_mode="Markdown",
        )
        return

    # Acknowledge wish receipt with a placeholder message (we'll edit it as progress flows)
    ack = await api.send_message(chat_id, f"💭 {text[:200]}\n\n⏳ 排队中...")
    progress_msg_id = ack.get("result", {}).get("message_id")
    if not progress_msg_id:
        logger.error("could not get message_id from ack send; aborting")
        return

    # Process in background so the bot loop can keep polling
    asyncio.create_task(
        _process_wish(api, chat_id, user_id, text, progress_msg_id, cfg)
    )


async def run_bot(stop_event: Optional[asyncio.Event] = None) -> None:
    """Main long-polling loop. Cancel via the stop_event or task cancellation."""
    token = os.environ.get("COCKPIT_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("COCKPIT_TELEGRAM_BOT_TOKEN not set; bot cannot start")
        return

    api = TelegramAPI(token)
    offset = 0
    logger.info("cockpit telegram bot starting (allowed=%d)", len(_allowed_users()))

    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            updates = await api.get_updates(offset)
        except Exception:
            logger.exception("getUpdates raised; sleeping 5s and retrying")
            await asyncio.sleep(5)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg:
                continue
            try:
                await _handle_message(api, msg)
            except Exception:
                logger.exception("handle_message raised; continuing")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_bot())
