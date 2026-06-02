# handlers/menu.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove


REPLY_KB = ReplyKeyboardMarkup(
    [
        ["☰ 功能選單", "📝 Telegraph 清單"],
        ["⌨️ 隱藏鍵盤"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 提醒", callback_data="menu:提醒清單"),
            InlineKeyboardButton("📌 追蹤", callback_data="menu:追蹤清單"),
        ],
        [
            InlineKeyboardButton("🧠 記憶", callback_data="menu:記憶清單"),
            InlineKeyboardButton("📍 地點", callback_data="menu:地點清單"),
        ],
        [
            InlineKeyboardButton("💳 每月支出", callback_data="menu:每月支出"),
            InlineKeyboardButton("🎨 貼圖", callback_data="menu:貼圖轉換"),
        ],
        [
            InlineKeyboardButton("⚙️ 設定", callback_data="menu:設定中心"),
            InlineKeyboardButton("❓ 說明", callback_data="menu:說明"),
        ],
    ])


async def send_main_menu(update, ctx):
    if update.callback_query:
        await update.callback_query.message.reply_text("☰ 選擇要打開的功能：", reply_markup=main_menu_kb())
    else:
        await update.message.reply_text("☰ 選擇要打開的功能：", reply_markup=main_menu_kb())


async def cmd_hide_keyboard(update, ctx):
    await update.message.reply_text(
        "⌨️ 已隱藏快捷鍵盤。輸入 /start 可以重新顯示。",
        reply_markup=ReplyKeyboardRemove(),
    )
