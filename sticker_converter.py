# sticker_converter.py
# LINE → Telegram 貼圖轉換邏輯（從 stick.py 提取，移除 bot 初始化）

import io
import os
import json
import time
import asyncio
import tempfile
import subprocess
import requests
import logging
from bs4 import BeautifulSoup
from PIL import Image
from telegram import InputSticker

logger = logging.getLogger(__name__)


# ── 圖片處理 ──────────────────────────────────────────────────────────────────

def _strip_transparent(img: Image.Image) -> Image.Image:
    """把完全透明的像素改成透明白，防止黑邊污染"""
    data = img.getdata()
    img.putdata([(255, 255, 255, 0) if p[3] == 0 else p for p in data])
    return img

def resize_for_telegram(image_bytes: bytes) -> io.BytesIO:
    """靜態貼圖：512px，保持比例"""
    img = _strip_transparent(Image.open(io.BytesIO(image_bytes)).convert("RGBA"))
    w, h = img.size
    if w > h:
        img = img.resize((512, int(h / w * 512)), Image.Resampling.LANCZOS)
    else:
        img = img.resize((int(w / h * 512), 512), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="WEBP", lossless=True, quality=100)
    out.name = "sticker.webp"
    out.seek(0)
    return out

def resize_for_emoji(image_bytes: bytes) -> io.BytesIO:
    """表情貼：強制 100x100px"""
    img = _strip_transparent(Image.open(io.BytesIO(image_bytes)).convert("RGBA"))
    img = img.resize((100, 100), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="WEBP", lossless=True, quality=100)
    out.name = "emoji.webp"
    out.seek(0)
    return out

def convert_to_webm(image_bytes: bytes) -> io.BytesIO | None:
    """動態貼圖：APNG → WEBM（VP9，透明背景，最多 3 秒）"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(image_bytes)
        src = f.name
    dst = src + ".webm"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src,
             "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
             "-vf", "scale='if(gt(iw,ih),512,-2)':'if(gt(iw,ih),-2,512)'",
             "-an", "-t", "3", dst],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(dst, "rb") as f:
            out = io.BytesIO(f.read())
        out.name = "sticker.webm"
        return out
    except Exception as e:
        logger.error(f"webm 轉換失敗: {e}")
        return None
    finally:
        for p in (src, dst):
            if os.path.exists(p):
                os.remove(p)


# ── 抓取 LINE 商店資料 ────────────────────────────────────────────────────────

def _fetch_emojishop(url: str, soup: BeautifulSoup) -> list[dict] | None:
    """
    emojishop 頁面結構和 stickershop 不同。
    先嘗試 data-preview，再從 CDN 直接建構網址。
    """
    import re as _re

    # 方法一：data-preview（部分版本有效）
    items = soup.find_all(attrs={"data-preview": True})
    stickers = []
    for item in items:
        try:
            data = json.loads(item["data-preview"])
            img_url = data.get("staticUrl") or data.get("fallbackStaticUrl")
            if img_url:
                stickers.append({"url": img_url.split(";")[0], "is_animated": False})
        except Exception:
            continue
    if stickers:
        return stickers

    # 方法二：從頁面 HTML 抓 product ID → CDN 直接建構
    # emojishop CDN: https://stickershop.line-scdn.net/sticonshop/v1/sticon/{id}/iPhone/{n}.png
    m = _re.search(r"/emojishop/product/([a-f0-9]+)", url)
    if not m:
        return None
    product_id = m.group(1)

    # 從頁面找貼圖 ID 列表（li[data-id] 或 script 內的 stickerIds）
    ids = [tag.get("data-id") for tag in soup.find_all(attrs={"data-id": True}) if tag.get("data-id")]
    if not ids:
        # 嘗試從 script 內找 JSON
        for script in soup.find_all("script"):
            if "stickerIds" in (script.string or ""):
                found = _re.findall(r'"stickerIds"\s*:\s*\[([^\]]+)\]', script.string)
                if found:
                    ids = [i.strip().strip('"') for i in found[0].split(",")]
                    break

    if not ids:
        return None

    base = f"https://stickershop.line-scdn.net/sticonshop/v1/sticon/{product_id}/iPhone"
    return [{"url": f"{base}/{sid}.png", "is_animated": False} for sid in ids if sid]


def fetch_line_stickers(url: str) -> list[dict] | None:
    """
    回傳 [{'url': str, 'is_animated': bool}, ...]
    失敗回傳 None
    """
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        # emojishop 走專用解析
        if "emojishop" in url:
            return _fetch_emojishop(url, soup)

        items = soup.find_all(attrs={"data-preview": True})
        if not items:
            return None

        stickers = []
        for item in items:
            data = json.loads(item["data-preview"])
            ani_url   = data.get("animationUrl")
            popup_url = data.get("popupUrl")
            stat_url  = data.get("staticUrl")
            fallback  = data.get("animation")
            img_url   = ani_url or popup_url or fallback or stat_url
            if not img_url:
                continue
            img_url = (img_url
                       .replace("animation.png", "animation@2x.png")
                       .replace("popup.png",     "popup@2x.png")
                       .replace("sticker.png",   "sticker@2x.png")
                       .split(";")[0])
            stickers.append({"url": img_url, "is_animated": bool(ani_url or popup_url or fallback)})
        return stickers or None
    except Exception as e:
        logger.error(f"fetch_line_stickers: {e}")
        return None


# ── 主流程（可從 bot.py 直接 await）─────────────────────────────────────────

async def convert_and_upload(bot, user_id: int, chat_id: int | str,
                              url: str, status_msg) -> str | None:
    """
    爬取 LINE 商店 URL，轉換並上傳成 Telegram 貼圖包。
    status_msg：一個可 edit_text 的 Message 物件（用於進度更新）。
    成功回傳貼圖包連結，失敗回傳 None。
    """
    is_emoji = "emojishop" in url
    stickers_data = fetch_line_stickers(url)
    if not stickers_data:
        return None

    has_animated   = any(s["is_animated"] for s in stickers_data)
    file_type_text = "動態" if has_animated else "靜態"
    # custom_emoji 已鎖付費，emoji 包也轉成普通貼圖
    pack_type_name = "表情貼（轉貼圖）" if is_emoji else "貼圖"

    await status_msg.edit_text(
        f"✅ 找到 {len(stickers_data)} 張 {file_type_text}{pack_type_name}！開始轉換...\n"
        "（動圖較慢，請耐心等候 ⏳）"
    )

    bot_username = (await bot.get_me()).username
    pack_name  = f"{'emoji_' if is_emoji else 'sticker_'}{int(time.time())}_by_{bot_username}"
    pack_title = f"來自 LINE 的{file_type_text}{pack_type_name}"

    loop = asyncio.get_event_loop()

    for i, s in enumerate(stickers_data):
        res = requests.get(s["url"], timeout=15)
        if res.status_code != 200:
            res = requests.get(s["url"].replace("@2x", ""), timeout=15)

        content = res.content
        if s["is_animated"]:
            # ffmpeg 是 CPU 密集，用 executor 避免阻塞 event loop
            processed = await loop.run_in_executor(None, convert_to_webm, content)
            fmt = "video"
        else:
            processed = await loop.run_in_executor(None, resize_for_telegram, content)
            fmt = "static"

        if not processed:
            continue

        sticker = InputSticker(sticker=processed, emoji_list=["✨"], format=fmt)

        if i == 0:
            await bot.create_new_sticker_set(
                user_id=user_id, name=pack_name, title=pack_title,
                stickers=[sticker], sticker_type="regular",
            )
        else:
            await bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=sticker)

        await asyncio.sleep(0.3)

        if (i + 1) % 5 == 0:
            await status_msg.edit_text(
                f"⏳ {pack_type_name}轉換中：{i + 1} / {len(stickers_data)} 張..."
            )

    return f"https://t.me/addstickers/{pack_name}"
