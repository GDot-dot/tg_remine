# sticker_converter.py
# LINE → Telegram 貼圖轉換邏輯

import io
import os
import re
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

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ── 圖片處理 ──────────────────────────────────────────────────────────────────

def _strip_transparent(img: Image.Image) -> Image.Image:
    data = img.getdata()
    img.putdata([(255, 255, 255, 0) if p[3] == 0 else p for p in data])
    return img

def resize_for_telegram(image_bytes: bytes) -> "io.BytesIO | None":
    try:
        img = _strip_transparent(Image.open(io.BytesIO(image_bytes)).convert("RGBA"))
        w, h = img.size
        if w > h:
            img = img.resize((512, max(1, int(h / w * 512))), Image.Resampling.LANCZOS)
        else:
            img = img.resize((max(1, int(w / h * 512)), 512), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="WEBP", lossless=True, quality=100)
        out.name = "sticker.webp"
        out.seek(0)
        return out
    except Exception as e:
        logger.error(f"resize_for_telegram 失敗: {e}")
        return None

def convert_to_webm(image_bytes: bytes) -> "io.BytesIO | None":
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(image_bytes)
        src = f.name
    dst = src + ".webm"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src,
             "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
             "-vf", "scale='if(gt(iw,ih),512,-2)':'if(gt(iw,ih),-2,512)'",
             "-an", "-t", "3", dst],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error(f"ffmpeg 失敗: {result.stderr[-300:]}")
            return None
        with open(dst, "rb") as f:
            out = io.BytesIO(f.read())
        out.name = "sticker.webm"
        return out
    except Exception as e:
        logger.error(f"convert_to_webm 失敗: {e}")
        return None
    finally:
        for p in (src, dst):
            if os.path.exists(p): os.remove(p)


# ── 下載（含 retry + log）────────────────────────────────────────────────────

def _download(url: str, index: int) -> "bytes | None":
    for try_url in [url, url.replace("@2x", "")]:
        try:
            res = requests.get(try_url, headers=_HEADERS, timeout=15)
            if res.status_code == 200:
                logger.debug(f"[{index}] 下載成功: {try_url}")
                return res.content
            logger.warning(f"[{index}] HTTP {res.status_code}: {try_url}")
        except Exception as e:
            logger.warning(f"[{index}] 下載例外: {try_url} -> {e}")
    logger.error(f"[{index}] 下載失敗，放棄此張")
    return None


# ── emojishop 解析 ────────────────────────────────────────────────────────────

def _fetch_emojishop(url: str) -> "list[dict] | None":
    m = re.search(r"/emojishop/product/([^/?#]+)", url)
    if not m:
        logger.error("emojishop: 無法解析 product ID")
        return None
    product_id = m.group(1)
    logger.info(f"emojishop product_id={product_id}")

    # 方法一：LINE JSON API（多個端點嘗試）
    api_candidates = [
        f"https://store.line.me/api/v1/sticconshop/packages/{product_id}/stickers",
        f"https://store.line.me/api/v1/stickershop/products/{product_id}/info",
        f"https://store.line.me/api/v1/emojishop/products/{product_id}/info",
    ]
    for api_url in api_candidates:
        try:
            res = requests.get(api_url, headers={**_HEADERS, "Accept": "application/json",
                               "Referer": "https://store.line.me/"}, timeout=15)
            logger.info(f"API {api_url} -> {res.status_code}, body: {res.text[:400]}")
            if res.status_code == 200:
                data = res.json()
                raw = (data.get("stickers")
                       or data.get("stickerList")
                       or (data.get("package") or {}).get("stickers") or [])
                stickers = []
                for s in raw:
                    img_url = (s.get("animationUrl") or s.get("popupUrl")
                               or s.get("staticUrl") or s.get("fallbackStaticUrl"))
                    if img_url:
                        stickers.append({"url": img_url.split(";")[0],
                                         "is_animated": bool(s.get("animationUrl"))})
                if stickers:
                    logger.info(f"emojishop API 成功: {len(stickers)} 張")
                    return stickers
                logger.warning(f"API 回 200 但無貼圖，keys={list(data.keys())}")
        except Exception as e:
            logger.warning(f"API {api_url} 例外: {e}")

    # 方法二：CDN 流水號（sticonshop）
    base = f"https://stickershop.line-scdn.net/sticonshop/v1/sticon/{product_id}/iPhone"
    logger.info(f"emojishop CDN fallback base={base}")
    try:
        for first in [f"{base}/001.png", f"{base}/1.png"]:
            chk = requests.head(first, headers=_HEADERS, timeout=8)
            logger.info(f"CDN head {first} -> {chk.status_code}")
            if chk.status_code == 200:
                stickers = []
                for n in range(1, 61):
                    r = requests.head(f"{base}/{n:03d}.png", headers=_HEADERS, timeout=5)
                    if r.status_code != 200:
                        break
                    stickers.append({"url": f"{base}/{n:03d}.png", "is_animated": False})
                if stickers:
                    logger.info(f"CDN fallback 找到 {len(stickers)} 張")
                    return stickers
    except Exception as e:
        logger.error(f"CDN fallback 例外: {e}")

    logger.error("emojishop: 所有方法均失敗")
    return None


# ── stickershop 解析 ───────────────────────────────────────────────────────────

def _fetch_stickershop(url: str) -> "list[dict] | None":
    try:
        res = requests.get(url, headers=_HEADERS, timeout=15)
        logger.info(f"stickershop HTTP {res.status_code}, url={url}")
        soup = BeautifulSoup(res.text, "html.parser")
        items = soup.find_all(attrs={"data-preview": True})
        logger.info(f"stickershop data-preview count={len(items)}")
        if not items:
            return None
        stickers = []
        for item in items:
            data = json.loads(item["data-preview"])
            ani   = data.get("animationUrl")
            popup = data.get("popupUrl")
            stat  = data.get("staticUrl")
            fallb = data.get("animation")
            img_url = ani or popup or fallb or stat
            if not img_url:
                continue
            img_url = (img_url
                       .replace("animation.png", "animation@2x.png")
                       .replace("popup.png",     "popup@2x.png")
                       .replace("sticker.png",   "sticker@2x.png")
                       .split(";")[0])
            stickers.append({"url": img_url, "is_animated": bool(ani or popup or fallb)})
        logger.info(f"stickershop 解析到 {len(stickers)} 張")
        return stickers or None
    except Exception as e:
        logger.error(f"_fetch_stickershop: {e}")
        return None


def fetch_line_stickers(url: str) -> "list[dict] | None":
    if "emojishop" in url:
        return _fetch_emojishop(url)
    return _fetch_stickershop(url)


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def convert_and_upload(bot, user_id: int, chat_id,
                              url: str, status_msg) -> "str | None":
    is_emoji      = "emojishop" in url
    stickers_data = fetch_line_stickers(url)
    if not stickers_data:
        return None

    has_animated   = any(s["is_animated"] for s in stickers_data)
    file_type_text = "動態" if has_animated else "靜態"
    pack_type_name = "表情貼（轉貼圖）" if is_emoji else "貼圖"
    total          = len(stickers_data)

    await status_msg.edit_text(
        f"✅ 找到 {total} 張 {file_type_text}{pack_type_name}！開始轉換...\n"
        "（動圖較慢，請耐心等候 ⏳）"
    )

    bot_username = (await bot.get_me()).username
    pack_name    = f"{'emoji_' if is_emoji else 'sticker_'}{int(time.time())}_by_{bot_username}"
    pack_title   = f"來自 LINE 的{file_type_text}{pack_type_name}"
    loop         = asyncio.get_event_loop()
    success      = 0
    first_done   = False

    for i, s in enumerate(stickers_data):
        content = _download(s["url"], i)
        if not content:
            continue

        if s["is_animated"]:
            processed = await loop.run_in_executor(None, convert_to_webm, content)
            fmt = "video"
        else:
            processed = await loop.run_in_executor(None, resize_for_telegram, content)
            fmt = "static"

        if not processed:
            logger.error(f"[{i}] 圖片處理失敗，跳過")
            continue

        try:
            sticker = InputSticker(sticker=processed, emoji_list=["✨"], format=fmt)
            if not first_done:
                await bot.create_new_sticker_set(
                    user_id=user_id, name=pack_name, title=pack_title,
                    stickers=[sticker], sticker_type="regular",
                )
                first_done = True
            else:
                await bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=sticker)
            success += 1
        except Exception as e:
            logger.error(f"[{i}] Telegram API 失敗: {e}")

        await asyncio.sleep(0.3)
        if (i + 1) % 5 == 0:
            await status_msg.edit_text(
                f"⏳ {pack_type_name}轉換中：{i + 1} / {total} 張（成功 {success} 張）..."
            )

    if not first_done:
        logger.error("沒有任何貼圖上傳成功")
        return None

    logger.info(f"轉換完成: {success}/{total} 張，pack={pack_name}")
    return f"https://t.me/addstickers/{pack_name}"
