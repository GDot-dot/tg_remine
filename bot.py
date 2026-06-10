# bot.py - Telegram 智慧管家（Flask + Gunicorn）
# 邏輯對齊原 LINE 版本

import os
import html
import threading
import asyncio
import logging
from flask import Flask, Response, request

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from db import init_db
from scheduler import scheduler, safe_start
from handlers.tracker import (
    handle_tracker_input, handle_tracker_list,
    handle_monthly_cost, handle_tracker_delete,
    handle_tracker_detail, handle_tracker_edit_prompt,
    handle_tracker_toggle_notify, handle_tracker_edit_value,
    TRIGGER_MAP as TRACKER_TRIGGER_MAP,
)
from handlers.locations import (
    cb_loc_del, cb_loc_send, handle_delete_location, handle_find_location,
    handle_location_list, handle_location_msg, handle_location_state,
)
from handlers.menu import REPLY_KB, cmd_hide_keyboard, send_main_menu
from handlers.reminders import (
    bind_user_states, is_group, kb, reply,
    cb_confirm, cb_custom_reminder_prompt, cb_delete_ok, cb_delete_prompt,
    cb_edit_content_prompt, cb_edit_priority_prompt, cb_edit_prompt, cb_edit_time_prompt,
    cb_priority_custom_reminder_prompt, cb_priority_early, cb_priority_level,
    cb_rec_settime, cb_rec_toggle, cb_set_reminder, cb_snooze, cb_snooze_custom_prompt,
    handle_priority_reminder, handle_recurring, handle_reminder, handle_reminder_list,
    handle_reminder_state, looks_like_natural_reminder,
)
from handlers.memory import (
    cb_mem_delete_ok, cb_mem_delete_prompt, cb_mem_edit_prompt, cb_mem_view,
    handle_memory, handle_memory_edit_state,
)
from handlers.settings import (
    cmd_settings, handle_settings_callback, handle_settings_state,
    show_settings,
)
from handlers.stickers import handle_sticker_toggle, handle_sticker_url
from dashboard_pages import ensure_dashboard_url, render_dashboard_page
from telegraph_pages import publish_telegraph_list_page

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT        = int(os.environ.get("PORT", 8080))

app = Flask(__name__)
_loop: asyncio.AbstractEventLoop = None
_ptb_app: Application = None
user_states: dict[int, dict] = {}
bind_user_states(user_states)


# ── asyncio bridge ────────────────────────────────────────────────────────────

def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def _async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=30)


# ── /start & /help ────────────────────────────────────────────────────────────

HELP_TEXT = """🤖 <b>Telegram 智慧管家</b>

<b>📅 提醒功能</b>
<code>提醒 [誰] [日期] [時間] [事件]</code>
<i>日期可用：今天 / 明天 / 後天 / MM/DD / YYYY-MM-DD（空格可省略）</i>
也可說：<code>明天下午三點提醒我繳費</code>
<code>重要提醒 [誰] [日期] [時間] [事件]</code>
<code>週期提醒</code> — 設定每週重複
<code>提醒清單</code> — 管理所有提醒

<b>📍 地點功能</b>
<code>找地點 [名稱]</code> / <code>地點清單</code>
<code>刪除地點 [名稱]</code>
（傳送位置訊息可儲存）

<b>🧠 記憶功能</b>
<code>記住 [關鍵字] [內容]</code>
<code>查詢 [關鍵字]</code>
<code>忘記 [關鍵字]</code> / <code>記憶清單</code>
內容格式：<code>**粗體**</code>、<code>__斜體__</code>、<code>||防劇透||</code>、<code>`等寬`</code>

<b>📌 追蹤功能</b>
<code>訂閱 Netflix 每月15號 390元</code>
<code>合約 租約 2026/12/31 提前30天</code>
<code>紀念日 媽媽生日 0520</code>
<code>藥物 魚油 60顆 每天2顆</code>
<code>追蹤清單</code> / <code>訂閱清單</code> / <code>紀念日清單</code>
<code>每月支出</code> — 訂閱費用統計
<code>刪除追蹤 [名稱]</code>

<b>🎨 LINE 貼圖轉換</b>
<code>貼圖轉換</code> — 開啟／關閉轉換模式
開啟後貼上 LINE 商店網址即自動轉換至 Telegram

<b>⚙️ 設定中心</b>
<code>設定</code> / <code>/settings</code>
可設定地區/城市、早上今日摘要、晚上明日預告、是否附上天氣、常用延後按鈕。
天氣來源只使用中央氣象署 CWA；包含天氣狀態、最高/最低溫、降雨機率、出門建議。

<b>通用</b>：<code>取消</code> — 中斷操作"""

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 歡迎使用 Telegram 智慧管家！\n\n{HELP_TEXT}",
        parse_mode=ParseMode.HTML,
        reply_markup=REPLY_KB,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_TEXT)

async def send_web_lists_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("📝 正在產生 Telegraph 快照...")
    try:
        url = publish_telegraph_list_page(update.effective_user.id)
    except Exception as e:
        logger.error("publish telegraph list failed: %s", e, exc_info=True)
        await status.edit_text(f"❌ Telegraph 快照產生失敗：{html.escape(str(e))}")
        return
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📝 開啟 Telegraph 快照", url=url)
    ]])
    await status.edit_text("📝 Telegraph 快照已產生：", reply_markup=markup)


async def send_dashboard_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = ensure_dashboard_url(update.effective_user.id)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 開啟 Web 儀表板", url=url)
    ]])
    await update.message.reply_text("🌐 Web 儀表板：", reply_markup=markup)


async def handle_location_msg_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_location_msg(update, ctx, user_states)


# ── 主訊息 Handler ────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text    = update.message.text.strip()
    user_id = update.effective_user.id
    grp     = is_group(update)

    # 取消
    if text == "取消":
        if user_id in user_states:
            del user_states[user_id]
            await reply(update, "✅ 操作已取消。")
        else:
            await reply(update, "目前沒有進行中的操作。")
        return

    # 狀態機
    if user_id in user_states:
        action = user_states[user_id].get("action")

        if action == "await_location_name":
            await handle_location_state(update, ctx, user_states, text)
            return

        elif action.startswith("setting_"):
            await handle_settings_state(update, ctx, action, user_states, text)
            return

        elif await handle_reminder_state(update, ctx, user_states, action, text):
            return

        elif action == "edit_tracker_field":
            await handle_tracker_edit_value(update, ctx, user_states, text)
            return

        elif action == "edit_memory_content":
            await handle_memory_edit_state(update, ctx, user_states, text)
            return

    # 固定指令路由
    if text in ("功能選單", "☰ 功能選單", "主選單", "選單", "menu"):
        await send_main_menu(update, ctx)
        return
    if text in ("隱藏鍵盤", "⌨️ 隱藏鍵盤", "收起鍵盤", "關閉鍵盤"):
        await cmd_hide_keyboard(update, ctx)
        return
    if text in ("顯示鍵盤", "快捷鍵盤"):
        await update.message.reply_text("⌨️ 已顯示快捷鍵盤。", reply_markup=REPLY_KB)
        return
    if text in ("Telegraph 快照", "📝 Telegraph 快照", "Telegraph 清單", "📝 Telegraph 清單", "Web 清單", "🌐 Web 清單", "網頁清單"):
        await send_web_lists_link(update, ctx)
        return
    if text in ("Web 儀表板", "🌐 Web 儀表板", "儀表板", "Dashboard", "dashboard"):
        await send_dashboard_link(update, ctx)
        return
    if text in ("貼圖轉換", "🎨 貼圖轉換"):
        await handle_sticker_toggle(update, ctx)
        return
    if await handle_sticker_url(update, ctx, text):
        return
    # ── Tracker 追蹤功能 ──────────────────────────────────────────────────────
    tracker_text = text.removeprefix("📌 ").removeprefix("💳 ")
    if tracker_text in ("追蹤清單", "訂閱清單", "合約清單", "紀念日清單", "藥物清單"):
        await handle_tracker_list(update, ctx, tracker_text); return
    if tracker_text in ("每月支出", "月費總計", "訂閱費用"):
        await handle_monthly_cost(update, ctx); return
    if text.startswith("刪除追蹤"):
        await handle_tracker_delete(update, ctx, text[4:].strip()); return
    if any(text.startswith(p + " ") or text.startswith(p + "\u3000")
           for p in TRACKER_TRIGGER_MAP):
        await handle_tracker_input(update, ctx, text); return

    if text in ("提醒清單", "📋 提醒清單"):
        await handle_reminder_list(update, ctx); return
    if text in ("設定", "設定中心", "⚙️ 設定中心"):
        await show_settings(update, ctx); return
    if text.startswith("重要提醒"):
        await handle_priority_reminder(update, ctx, text); return
    if text.startswith("提醒"):
        await handle_reminder(update, ctx, text); return
    if looks_like_natural_reminder(text):
        await handle_reminder(update, ctx, text); return
    if text == "週期提醒":
        await handle_recurring(update, ctx); return
    if text in ("地點", "地點清單", "📍 地點清單"):
        await handle_location_list(update, ctx); return
    if text.startswith("找地點"):
        name = text[3:].strip()
        await handle_find_location(update, ctx, name)
        return
    if text.startswith("刪除地點"):
        name = text[4:].strip()
        await handle_delete_location(update, ctx, name)
        return
    if any(text.startswith(k) for k in ["記住", "查詢", "忘記"]) or text in ("記憶清單", "🧠 記憶清單"):
        # 把 emoji 前綴去掉再傳入
        clean = text.removeprefix("🧠 ")
        await handle_memory(update, ctx, clean); return
    if text.lower() in ("help", "說明", "幫助", "❓ 說明"):
        await cmd_help(update, ctx); return

    # 群組靜默，私訊才提示
    if not grp:
        await reply(update,
            "🤔 我聽不太懂，您可以試著說：\n"
            "「提醒 我 明天 09:00 開會」\n"
            "「明天下午三點提醒我繳費」\n"
            "或輸入「說明」查看指令。")


# ── Callback 統一入口 ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    try:
        if data == "cancel":
            user_states.pop(update.effective_user.id, None)
            await q.answer("已取消")
            await q.edit_message_text("❌ 操作已取消。")
            return

        parts = data.split(":")

        # ── 主選單按鈕（模擬使用者輸入對應指令）
        if parts[0] == "menu":
            await q.answer()
            cmd = parts[1]
            if cmd == "提醒清單":
                await handle_reminder_list(update, ctx)
            elif cmd == "追蹤清單":
                await handle_tracker_list(update, ctx, "追蹤清單")
            elif cmd == "每月支出":
                await handle_monthly_cost(update, ctx)
            elif cmd == "記憶清單":
                await handle_memory(update, ctx, "記憶清單")
            elif cmd == "地點清單":
                await handle_location_list(update, ctx)
            elif cmd == "設定中心":
                await show_settings(update, ctx)
            elif cmd == "Web儀表板":
                await send_dashboard_link(update, ctx)
            elif cmd == "貼圖轉換":
                await handle_sticker_toggle(update, ctx)
            elif cmd == "說明":
                await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)
            return

        # ── 提醒確認 / 延後
        if parts[0] == "cr":
            await cb_confirm(update, ctx, int(parts[1]))
        elif parts[0] == "sn":
            await cb_snooze(update, ctx, int(parts[1]), int(parts[2]))
        elif parts[0] == "snc":
            await cb_snooze_custom_prompt(update, ctx, int(parts[1]))

        # ── 一般提醒：選提早時間  sr:event_id:minutes
        elif parts[0] == "sr":
            await cb_set_reminder(update, ctx, int(parts[1]), int(parts[2]))
        elif parts[0] == "src":
            await cb_custom_reminder_prompt(update, ctx, int(parts[1]))

        elif parts[0] == "set":
            await handle_settings_callback(update, ctx, parts[1], user_states)

        # ── 重要提醒：選提早時間  pe:minutes
        elif parts[0] == "pe":
            await cb_priority_early(update, ctx, int(parts[1]))
        elif parts[0] == "pec":
            await cb_priority_custom_reminder_prompt(update, ctx)

        # ── 重要提醒：選等級  pl:level
        elif parts[0] == "pl":
            await cb_priority_level(update, ctx, int(parts[1]))

        # ── 提醒清單操作
        elif parts[0] == "re":
            action = parts[1]
            if action == "page":         await handle_reminder_list(update, ctx, int(parts[2]))
            elif action == "del":        await cb_delete_prompt(update, ctx, int(parts[2]))
            elif action == "delok":      await cb_delete_ok(update, ctx, int(parts[2]))
            elif action == "edit":       await cb_edit_prompt(update, ctx, int(parts[2]))
            elif action == "edit_content": await cb_edit_content_prompt(update, ctx, int(parts[2]))
            elif action == "edit_time":  await cb_edit_time_prompt(update, ctx, int(parts[2]))
            elif action == "edit_priority": await cb_edit_priority_prompt(update, ctx, int(parts[2]))

        # ── 週期提醒
        elif parts[0] == "rec":
            if parts[1] == "toggle":    await cb_rec_toggle(update, ctx, parts[2])
            elif parts[1] == "settime": await cb_rec_settime(update, ctx)

        # ── 追蹤清單操作
        elif parts[0] == "tr":
            action = parts[1]
            if action == "view":
                await handle_tracker_detail(update, ctx, int(parts[2]))
            elif action == "edit":
                await handle_tracker_edit_prompt(update, ctx, user_states, parts[2], int(parts[3]))
            elif action == "notify":
                await handle_tracker_toggle_notify(update, ctx, int(parts[2]))

        # ── 地點
        elif parts[0] == "loc":
            if parts[1] == "send": await cb_loc_send(update, ctx, int(parts[2]))
            elif parts[1] == "del":await cb_loc_del(update, ctx, int(parts[2]))

        # ── 記憶庫
        elif parts[0] == "mem":
            if parts[1] == "view": await cb_mem_view(update, ctx, int(parts[2]))
            elif parts[1] == "edit": await cb_mem_edit_prompt(update, ctx, user_states, int(parts[2]))
            elif parts[1] == "del": await cb_mem_delete_prompt(update, ctx, int(parts[2]), kb)
            elif parts[1] == "delok": await cb_mem_delete_ok(update, ctx, int(parts[2]))

        else:
            await q.answer("❓ 未知操作", show_alert=True)

    except Exception as e:
        logger.error(f"Callback error ({data}): {e}", exc_info=True)
        try:
            await q.answer("❌ 發生錯誤", show_alert=True)
        except Exception:
            pass


# ── PTB Application ───────────────────────────────────────────────────────────

def build_ptb_app() -> Application:
    a = Application.builder().token(BOT_TOKEN).build()
    a.add_handler(CommandHandler("start", cmd_start))
    a.add_handler(CommandHandler("help",  cmd_help))
    a.add_handler(CommandHandler("settings", cmd_settings))
    a.add_handler(CommandHandler("hide_keyboard", cmd_hide_keyboard))
    a.add_handler(MessageHandler(filters.LOCATION, handle_location_msg_entry))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    a.add_handler(CallbackQueryHandler(handle_callback))
    return a


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return {"service": "TG Reminder Bot", "status": "running"}, 200

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "scheduler": scheduler.running}, 200

@app.route("/dashboard/<token>", methods=["GET"])
def dashboard(token):
    page = render_dashboard_page(token, notice=request.args.get("notice"))
    if page is None:
        return Response("Not found", status=404, mimetype="text/plain; charset=utf-8")
    return Response(page, mimetype="text/html; charset=utf-8")

@app.route("/webhook", methods=["POST"])
def webhook():
    data   = request.get_json(force=True)
    update = Update.de_json(data, _ptb_app.bot)
    asyncio.run_coroutine_threadsafe(
        _ptb_app.process_update(update), _loop
    ).result(timeout=30)
    return "OK", 200

@app.route("/set_webhook", methods=["GET"])
def set_webhook_route():
    if not WEBHOOK_URL or not BOT_TOKEN:
        return {"error": "WEBHOOK_URL 或 BOT_TOKEN 未設定"}, 400
    try:
        _async(_ptb_app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        ))
        return {"result": f"✅ Webhook 已設定：{WEBHOOK_URL}"}, 200
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/webhook_info", methods=["GET"])
def webhook_info():
    try:
        info = _async(_ptb_app.bot.get_webhook_info())
        return {"url": info.url, "pending": info.pending_update_count,
                "last_error": info.last_error_message}, 200
    except Exception as e:
        return {"error": str(e)}, 500


# ── 啟動 ─────────────────────────────────────────────────────────────────────

def start():
    global _loop, _ptb_app

    logger.info("=" * 50)
    logger.info(f"  TELEGRAM_BOT_TOKEN: {'✅' if BOT_TOKEN else '❌ MISSING'}")
    logger.info(f"  DATABASE_URL: {'✅' if os.environ.get('DATABASE_URL') else '❌ MISSING'}")
    logger.info(f"  WEBHOOK_URL: {WEBHOOK_URL or '❌ MISSING'}")
    logger.info("=" * 50)

    init_db()
    safe_start()
    from scheduler import start_daily_summary_scan, start_tracker_scan
    start_daily_summary_scan()
    start_tracker_scan()

    _loop = asyncio.new_event_loop()
    threading.Thread(target=_run_loop, args=(_loop,), daemon=True).start()

    _ptb_app = build_ptb_app()
    _async(_ptb_app.initialize())
    _async(_ptb_app.start())

    if WEBHOOK_URL:
        try:
            _async(_ptb_app.bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            ))
            logger.info(f"✅ Webhook 設定成功：{WEBHOOK_URL}")
        except Exception as e:
            logger.warning(f"⚠️ Webhook 設定失敗（可用 /set_webhook 手動補設）：{e}")
    else:
        logger.warning("⚠️ WEBHOOK_URL 未設定")

    logger.info("🤖 Bot 啟動完成")


start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
