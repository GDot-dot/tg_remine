# handlers/memory.py
import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from db import (
    Memory as MemModel,
    SessionLocal,
    delete_memory_by_id,
    forget_memory,
    get_memory_by_id,
    list_memories,
    query_memory,
    save_memory,
    update_memory_by_id,
)

MEMORY_HTML_PREFIX = "__TG_MEMORY_HTML__\n"


def format_memory_content(content: str) -> str:
    placeholders: list[tuple[str, str]] = []

    def stash(pattern: str, repl):
        nonlocal content

        def _replace(match):
            token = f"\u0000MEMFMT{len(placeholders)}\u0000"
            placeholders.append((token, repl(match)))
            return token

        content = re.sub(pattern, _replace, content, flags=re.S)

    stash(r"```(.+?)```", lambda m: f"<pre>{html.escape(m.group(1).strip())}</pre>")
    stash(r"`([^`\n]+?)`", lambda m: f"<code>{html.escape(m.group(1))}</code>")
    escaped = html.escape(content)
    rules = [
        (r"\*\*(.+?)\*\*", r"<b>\1</b>"),
        (r"__(.+?)__", r"<i>\1</i>"),
        (r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>"),
        (r"(?<!_)_([^_\n]+?)_(?!_)", r"<i>\1</i>"),
        (r"~~(.+?)~~", r"<s>\1</s>"),
        (r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>"),
    ]
    for pattern, replacement in rules:
        escaped = re.sub(pattern, replacement, escaped, flags=re.S)
    for token, value in placeholders:
        escaped = escaped.replace(html.escape(token), value)
    return escaped


def stored_memory_content(content: str) -> str:
    if content.startswith(MEMORY_HTML_PREFIX):
        return content[len(MEMORY_HTML_PREFIX):]
    return format_memory_content(content)


def extract_message_html(update: Update, plain_content: str, prefix: str = "") -> str:
    msg = update.message
    if not msg:
        return format_memory_content(plain_content)

    html_text = getattr(msg, "text_html", None)
    if html_text and msg.entities and (not prefix or html_text.startswith(prefix)):
        return html_text[len(prefix):]

    return format_memory_content(plain_content)


def extract_memory_content_html(update: Update, keyword: str, plain_content: str) -> str:
    return extract_message_html(update, plain_content, f"記住 {keyword} ")


def memory_text(keyword: str, content: str) -> str:
    return f"🧠 <b>{html.escape(keyword)}</b>\n{stored_memory_content(content)}"


def memory_kb(mem_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ 編輯", callback_data=f"mem:edit:{mem_id}"),
        InlineKeyboardButton("🗑️ 刪除", callback_data=f"mem:del:{mem_id}"),
    ]])


async def _reply(update: Update, text: str, **kwargs):
    if update.message:
        await update.message.reply_text(text, **kwargs)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, **kwargs)


async def handle_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id

    if text.startswith("記住"):
        parts = text[2:].strip().split(" ", 1)
        if len(parts) < 2:
            await _reply(
                update,
                "格式：<code>記住 [關鍵字] [內容]</code>\n"
                "內容可用：<code>**粗體**</code>、<code>__斜體__</code>、"
                "<code>~~刪除線~~</code>、<code>||防劇透||</code>、<code>`等寬`</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        keyword, content = parts
        keyword = keyword.strip().replace("\n", "").replace("\r", "")
        content_html = extract_memory_content_html(update, keyword, content)
        stored_content = MEMORY_HTML_PREFIX + content_html
        existing = query_memory(user_id, keyword)
        exact = next((m for m in existing if m.keyword == keyword), None)
        if save_memory(user_id, keyword, stored_content):
            verb = "🔄 已更新" if exact else "🧠 已記住"
            await _reply(
                update,
                f"{verb}：<b>{html.escape(keyword)}</b>\n{content_html}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await _reply(update, "❌ 儲存失敗。", parse_mode=ParseMode.HTML)

    elif text.startswith("查詢"):
        keyword = text[2:].strip()
        if not keyword:
            await _reply(update, "格式：<code>查詢 [關鍵字]</code>", parse_mode=ParseMode.HTML)
            return
        results = query_memory(user_id, keyword)
        if not results:
            await _reply(update, f"🔍 找不到「{html.escape(keyword)}」的記憶。", parse_mode=ParseMode.HTML)
        elif len(results) == 1:
            await _reply(
                update,
                memory_text(results[0].keyword, results[0].content),
                parse_mode=ParseMode.HTML,
                reply_markup=memory_kb(results[0].id),
            )
        else:
            buttons = [[InlineKeyboardButton(m.keyword, callback_data=f"mem:view:{m.id}")] for m in results]
            await _reply(
                update,
                f"🔍 找到 {len(results)} 筆關於「{html.escape(keyword)}」的記憶，請選擇：",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )

    elif text.startswith("忘記"):
        keyword = text[2:].strip()
        if forget_memory(user_id, keyword):
            await _reply(update, f"🗑️ 已忘記「{html.escape(keyword)}」。", parse_mode=ParseMode.HTML)
        else:
            await _reply(update, f"❌ 找不到「{html.escape(keyword)}」。", parse_mode=ParseMode.HTML)

    elif text == "記憶清單":
        memories = list_memories(user_id)
        if not memories:
            await _reply(update, "🧠 記憶庫是空的。", parse_mode=ParseMode.HTML)
        else:
            valid = [m for m in memories if m.keyword and m.keyword.strip()]
            lines = ["🧠 <b>記憶清單</b>\n"] + [f"• {html.escape(m.keyword.strip())}" for m in valid]
            await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cb_mem_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    mem = db.query(MemModel).filter(MemModel.id == mem_id).first()
    db.close()
    if mem:
        await query.edit_message_text(
            memory_text(mem.keyword, mem.content),
            parse_mode=ParseMode.HTML,
            reply_markup=memory_kb(mem.id),
        )
    else:
        await query.edit_message_text("❌ 找不到。")


async def cb_mem_edit_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_states: dict, mem_id: int):
    query = update.callback_query
    await query.answer()
    mem = get_memory_by_id(update.effective_user.id, mem_id)
    if not mem:
        await query.edit_message_text("❌ 找不到這筆記憶。")
        return
    user_states[update.effective_user.id] = {
        "action": "edit_memory_content",
        "memory_id": mem_id,
        "keyword": mem.keyword,
    }
    await query.edit_message_text(
        f"✏️ 請輸入「{html.escape(mem.keyword)}」的新內容：\n"
        f"{stored_memory_content(mem.content)}",
        parse_mode=ParseMode.HTML,
    )


async def cb_mem_delete_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int, confirm_kb):
    query = update.callback_query
    await query.answer()
    mem = get_memory_by_id(update.effective_user.id, mem_id)
    if not mem:
        await query.edit_message_text("❌ 找不到這筆記憶。")
        return
    await query.edit_message_text(
        f"⚠️ 確定要刪除記憶「{html.escape(mem.keyword)}」？",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_kb([("✅ 確認刪除", f"mem:delok:{mem_id}"), ("❌ 取消", "cancel")]),
    )


async def cb_mem_delete_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int):
    query = update.callback_query
    await query.answer()
    ok = delete_memory_by_id(update.effective_user.id, mem_id)
    await query.edit_message_text("🗑️ 已刪除記憶。" if ok else "❌ 找不到這筆記憶。")


async def handle_memory_edit_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_states: dict, text: str):
    user_id = update.effective_user.id
    state = user_states.pop(user_id)
    content_html = extract_message_html(update, text.strip())
    stored_content = MEMORY_HTML_PREFIX + content_html
    if update_memory_by_id(user_id, state["memory_id"], stored_content):
        await update.message.reply_text(
            f"✅ 已更新記憶：<b>{html.escape(state['keyword'])}</b>\n{content_html}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("❌ 更新失敗，找不到這筆記憶。", parse_mode=ParseMode.HTML)
