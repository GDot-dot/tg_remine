# handlers/locations.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from db import (
    Location as LocModel,
    SessionLocal,
    add_location,
    delete_location,
    get_location_by_name,
    get_locations,
)


async def handle_location_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_states: dict):
    user_id = update.effective_user.id
    loc = update.message.location
    user_states[user_id] = {"action": "await_location_name", "lat": loc.latitude, "lng": loc.longitude}
    await update.message.reply_text(
        "📍 收到位置！請輸入地點名稱（如：公司、家）：",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_location_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_states: dict, text: str) -> bool:
    user_id = update.effective_user.id
    state = user_states.pop(user_id)
    name = text.strip()
    if add_location(user_id, name, state["lat"], state["lng"]):
        await update.message.reply_text(f"✅ 地點「{name}」已儲存！", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ 地點「{name}」已存在。", parse_mode=ParseMode.HTML)
    return True


async def handle_location_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    locs = get_locations(user_id)
    if not locs:
        await update.message.reply_text("📍 沒有儲存地點。請傳送位置訊息來新增！", parse_mode=ParseMode.HTML)
        return
    lines = ["📍 <b>我的地點</b>\n"]
    buttons = []
    for loc in locs:
        lines.append(f"• {loc.name}")
        buttons.append([
            InlineKeyboardButton(f"📌 {loc.name}", callback_data=f"loc:send:{loc.id}"),
            InlineKeyboardButton("🗑️", callback_data=f"loc:del:{loc.id}"),
        ])
    if update.callback_query:
        await update.callback_query.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def handle_find_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE, name: str):
    loc = get_location_by_name(update.effective_user.id, name)
    if loc:
        await ctx.bot.send_location(update.effective_chat.id, latitude=loc.latitude, longitude=loc.longitude)
        await update.message.reply_text(f"📍 {loc.name}", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"❌ 找不到「{name}」。", parse_mode=ParseMode.HTML)


async def handle_delete_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE, name: str):
    if delete_location(update.effective_user.id, name):
        await update.message.reply_text(f"🗑️ 地點「{name}」已刪除。", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"❌ 找不到「{name}」。", parse_mode=ParseMode.HTML)


async def cb_loc_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE, loc_id: int):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    loc = db.query(LocModel).filter(LocModel.id == loc_id).first()
    db.close()
    if not loc:
        await query.edit_message_text("❌ 找不到該地點。")
        return
    await ctx.bot.send_location(update.effective_chat.id, latitude=loc.latitude, longitude=loc.longitude)
    await ctx.bot.send_message(update.effective_chat.id, f"📍 {loc.name}")


async def cb_loc_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE, loc_id: int):
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    loc = db.query(LocModel).filter(LocModel.id == loc_id).first()
    if loc:
        name = loc.name
        db.delete(loc)
        db.commit()
        db.close()
        await query.edit_message_text(f"🗑️ 地點「{name}」已刪除。")
    else:
        db.close()
        await query.edit_message_text("❌ 找不到該地點。")
