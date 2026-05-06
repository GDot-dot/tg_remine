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
from curl_cffi import requests as chrome_requests  # 模擬 Chrome TLS 指紋
from bs4 import BeautifulSoup
from PIL import Image
from telegram import InputSticker
from telegram.error import RetryAfter, TimedOut, NetworkError

logger = logging.getLogger(__name__)

# 代理設定（從環境變數讀取，兩種方案擇一）
# 方案A：ScrapingBee（住宅IP代理，免費1000次/月）→ fly secrets set SCRAPINGBEE_KEY=xxx
# 方案B：自架 CF Worker（已確認被 LINE 封鎖，保留供參考）
_SCRAPINGBEE_KEY = os.environ.get("SCRAPINGBEE_KEY", "")
_PROXY_URL       = os.environ.get("LINE_PROXY_URL", "")
_PROXY_SECRET    = os.environ.get("LINE_PROXY_SECRET", "")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

_API_HEADERS = {
    **_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://store.line.me/",
    "Origin": "https://store.line.me",
}

_MAX_WEBM_BYTES = 256_000  # Telegram 動態貼圖上限


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
    """
    APNG → WEBM。
    強制碼率控制在 300k，轉出後若仍超過 256KB 回傳 None，
    由呼叫端自動 fallback 靜態貼圖。
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(image_bytes)
        src = f.name
    dst = src + ".webm"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", src,
                "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p",
                "-vf", "scale='if(gt(iw,ih),512,-2)':'if(gt(iw,ih),-2,512)'",
                "-r", "20",
                "-b:v", "300k", "-maxrate", "300k", "-bufsize", "600k",
                "-cpu-used", "4", "-row-mt", "1",
                "-an", "-t", "2.9",
                dst,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error(f"ffmpeg 失敗: {result.stderr[-300:]}")
            return None
        with open(dst, "rb") as f:
            raw = f.read()
        if len(raw) > _MAX_WEBM_BYTES:
            logger.warning(f"webm 超過 256KB ({len(raw)} bytes)，將 fallback 靜態")
            return None
        out = io.BytesIO(raw)
        out.name = "sticker.webm"
        return out
    except Exception as e:
        logger.error(f"convert_to_webm 例外: {e}")
        return None
    finally:
        for p in (src, dst):
            if os.path.exists(p): os.remove(p)


# ── 下載工具 ──────────────────────────────────────────────────────────────────

def _download(url: str, index: int) -> "bytes | None":
    for try_url in dict.fromkeys([url, url.replace("@2x", "")]):
        try:
            res = requests.get(try_url, headers=_HEADERS, timeout=20)
            if res.status_code == 200 and res.content:
                logger.debug(f"[{index}] 下載成功 ({len(res.content)} bytes)")
                return res.content
            logger.warning(f"[{index}] HTTP {res.status_code}: {try_url}")
        except Exception as e:
            logger.warning(f"[{index}] 下載例外: {e}")
    logger.error(f"[{index}] 下載失敗，放棄此張")
    return None

def _head_ok(url: str) -> bool:
    try:
        return requests.head(url, headers=_HEADERS, timeout=8).status_code == 200
    except Exception:
        return False


def _sticker_urls_from_meta_item(sticker_id: int, resource_type: str, has_animation: bool) -> dict:
    base = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{sticker_id}/iPhone"
    static_url = f"{base}/sticker@2x.png"
    rtype = (resource_type or "").upper()

    if "POPUP" in rtype:
        return {
            "url": f"{base}/popup@2x.png",
            "static_url": static_url,
            "is_animated": True,
        }
    if has_animation or "ANIMATION" in rtype:
        return {
            "url": f"{base}/sticker_animation@2x.png",
            "static_url": static_url,
            "is_animated": True,
        }
    return {"url": static_url, "static_url": static_url, "is_animated": False}


# ── Telegram API 發送（含 retry）─────────────────────────────────────────────

async def _tg_upload(bot, action: str, status_msg, **kwargs) -> bool:
    """
    action: 'create' | 'add'
    自動處理 RetryAfter / TimedOut / NetworkError，最多 retry 3 次。
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if action == "create":
                await bot.create_new_sticker_set(
                    read_timeout=60, write_timeout=60, **kwargs
                )
            else:
                await bot.add_sticker_to_set(
                    read_timeout=60, write_timeout=60, **kwargs
                )
            return True

        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(f"RetryAfter {wait}s，暫停...")
            await status_msg.edit_text(f"⚠️ 伺服器限速，暫停 {wait} 秒後自動繼續...")
            await asyncio.sleep(wait)

        except (TimedOut, NetworkError) as e:
            if attempt == max_retries - 1:
                logger.error(f"Telegram API 失敗 (已重試 {max_retries} 次): {e}")
                return False
            logger.warning(f"Telegram API 暫時失敗，3 秒後 retry ({attempt+1}/{max_retries}): {e}")
            await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Telegram API 非預期錯誤: {e}")
            return False

    return False


# ── stickershop 解析 ───────────────────────────────────────────────────────────

def _fetch_stickershop(url: str) -> "list[dict] | None":
    m = re.search(r"/product/(\d+)", url)
    product_id = m.group(1) if m else None

    # 方法零：productInfo.meta
    # 新版/動態貼圖常讓 stickers.json、store API 回 404，但 meta 仍可列出 sticker id。
    if product_id:
        for platform in ("iphone", "iPhone"):
            meta_url = f"https://stickershop.line-scdn.net/stickershop/v1/product/{product_id}/{platform}/productInfo.meta"
            try:
                res = requests.get(meta_url, headers=_API_HEADERS, timeout=15)
                logger.info(f"productInfo.meta {meta_url} -> {res.status_code}")
                if res.status_code != 200:
                    continue
                data = res.json()
                items = data.get("stickers") or []
                resource_type = data.get("stickerResourceType") or ""
                has_animation = bool(data.get("hasAnimation"))
                stickers = []
                for item in items:
                    sticker_id = item.get("id") if isinstance(item, dict) else None
                    if sticker_id:
                        stickers.append(_sticker_urls_from_meta_item(
                            int(sticker_id), resource_type, has_animation
                        ))
                if stickers:
                    logger.info(
                        "productInfo.meta 成功: %s 張, type=%s, animation=%s",
                        len(stickers), resource_type, has_animation,
                    )
                    return stickers
                logger.warning(f"productInfo.meta 200 但無 stickers，keys={list(data.keys())}")
            except Exception as e:
                logger.warning(f"productInfo.meta {meta_url} 例外: {e}")

    # 方法一：CDN JSON（需帶 Referer 否則 LINE CDN 回 403）
    if product_id:
        cdn_candidates = [
            f"https://stickershop.line-scdn.net/stickershop/v1/product/{product_id}/iPhone/stickers.json",
            f"https://stickershop.line-scdn.net/stickershop/v1/product/{product_id}/iPhone/stickerSets.json",
            f"https://stickershop.line-scdn.net/stickershop/v1/product/{product_id}/iPhone/main.json",
            f"https://store.line.me/api/v1/stickershop/products/{product_id}/stickers",
            f"https://store.line.me/api/v1/stickershop/products/{product_id}",
        ]
        for cdn_url in cdn_candidates:
            try:
                res = requests.get(cdn_url, headers=_API_HEADERS, timeout=15)
                logger.info(f"CDN JSON {cdn_url} -> {res.status_code}")
                if res.status_code == 200:
                    data = res.json()
                    # LINE CDN JSON 有多種結構
                    items = (data if isinstance(data, list)
                             else data.get("stickers")
                             or data.get("stickerList")
                             or (data.get("package") or {}).get("stickers")
                             or [])
                    stickers = []
                    for s in items:
                        ani  = s.get("animationUrl") or s.get("popupUrl")
                        stat = s.get("staticUrl") or s.get("staticImage") or s.get("fallbackStaticUrl")
                        img_url = ani or stat
                        if img_url:
                            stickers.append({"url": img_url.split(";")[0],
                                             "is_animated": bool(ani)})
                    if stickers:
                        logger.info(f"CDN JSON 成功: {len(stickers)} 張 ({cdn_url})")
                        return stickers
                    logger.warning(f"CDN JSON 200 但無資料，keys={list(data.keys()) if isinstance(data, dict) else 'list'}")
            except Exception as e:
                logger.warning(f"CDN JSON {cdn_url} 例外: {e}")

    # 方法二：HTML data-preview
    # fly.io 機房 IP 被 LINE 過濾，優先透過 Cloudflare Worker 代理
    def _fetch_html(target_url: str) -> str | None:
        # 方案A：curl_cffi 模擬 Chrome TLS 指紋（最優先，不需代理）
        try:
            r = chrome_requests.get(
                target_url,
                impersonate="chrome120",   # 完整模擬 Chrome 120 的 TLS ClientHello
                timeout=20,
            )
            logger.info(f"curl_cffi chrome120 {r.status_code} for {target_url}")
            if r.status_code == 200:
                from bs4 import BeautifulSoup as _BS
                # 快速確認是否有 data-preview，避免無效回應
                if "data-preview" in r.text:
                    return r.text
                logger.warning("curl_cffi 回傳 200 但無 data-preview，嘗試備用方案")
        except Exception as e:
            logger.warning(f"curl_cffi 失敗: {e}")

        # 方案B：ScrapingBee 住宅 IP（備用）
        if _SCRAPINGBEE_KEY:
            try:
                r = requests.get(
                    "https://app.scrapingbee.com/api/v1/",
                    params={
                        "api_key": _SCRAPINGBEE_KEY,
                        "url": target_url,
                        "render_js": "false",
                        "country_code": "tw",
                    },
                    timeout=30,
                )
                logger.info(f"ScrapingBee {r.status_code} for {target_url}")
                if r.status_code == 200:
                    return r.text
            except Exception as e:
                logger.warning(f"ScrapingBee 失敗: {e}")

        # 方案C：直連（本機測試用）
        try:
            r = requests.get(target_url, headers=_HEADERS, timeout=20)
            logger.info(f"直連 {r.status_code}")
            return r.text if r.status_code == 200 else None
        except Exception as e:
            logger.error(f"直連失敗: {e}")
            return None

    html = _fetch_html(url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        items = soup.find_all(attrs={"data-preview": True})
        logger.info(f"data-preview count={len(items)}")
        if items:
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
            if stickers:
                return stickers

        logger.warning("HTML 無 data-preview")
        return None



# ── emojishop 解析 ────────────────────────────────────────────────────────────

def _fetch_emojishop(url: str) -> "list[dict] | None":
    m = re.search(r"/emojishop/product/([^/?#]+)", url)
    if not m:
        logger.error("emojishop: 無法解析 product ID")
        return None
    product_id = m.group(1)
    logger.info(f"emojishop product_id={product_id}")

    # 方法一：LINE API
    for api_url in [
        f"https://store.line.me/api/v1/emojishop/products/{product_id}",
        f"https://store.line.me/api/v1/emojishop/products/{product_id}/stickers",
        f"https://store.line.me/api/v1/stickershop/products/{product_id}/info",
    ]:
        try:
            res = requests.get(api_url, headers=_API_HEADERS, timeout=15)
            logger.info(f"API {api_url} -> {res.status_code}, body: {res.text[:300]}")
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

    # 方法二：CDN 流水號 + 偵測動態 variant
    base = f"https://stickershop.line-scdn.net/sticonshop/v1/sticon/{product_id}/iPhone"
    logger.info(f"emojishop CDN fallback base={base}")
    try:
        fmt = None
        for first in [f"{base}/001.png", f"{base}/1.png"]:
            if _head_ok(first):
                fmt = "001" if "001" in first else "1"
                logger.info(f"CDN 有效，格式={fmt}")
                break
        if fmt:
            stickers = []
            for n in range(1, 61):
                name   = f"{n:03d}" if fmt == "001" else str(n)
                static = f"{base}/{name}.png"
                ani    = f"{base}/{name}_animation.png"
                if not _head_ok(static):
                    break
                is_ani = _head_ok(ani)
                stickers.append({"url": ani if is_ani else static,
                                  "static_url": static,
                                  "is_animated": is_ani})
            if stickers:
                logger.info(f"CDN 找到 {len(stickers)} 張 "
                            f"(動態 {sum(1 for s in stickers if s['is_animated'])} 張)")
                return stickers
    except Exception as e:
        logger.error(f"CDN fallback 例外: {e}")

    logger.error("emojishop: 所有方法均失敗")
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
        # 下載
        content = _download(s["url"], i)
        if not content:
            static_url = s.get("static_url")
            if s["is_animated"] and static_url and static_url != s["url"]:
                logger.info(f"[{i}] 動態素材下載失敗，改抓靜態圖")
                content = _download(static_url, i)
                s["is_animated"] = False
            if not content:
                continue

        # 處理圖片
        if s["is_animated"]:
            processed = await loop.run_in_executor(None, convert_to_webm, content)
            fmt = "video"
            # 超過 256KB 或轉檔失敗 → fallback 靜態
            if not processed:
                logger.info(f"[{i}] 動圖 fallback 靜態")
                static_bytes = _download(s.get("static_url", s["url"]), i) or content
                processed = await loop.run_in_executor(None, resize_for_telegram, static_bytes)
                fmt = "static"
        else:
            processed = await loop.run_in_executor(None, resize_for_telegram, content)
            fmt = "static"

        if not processed:
            logger.error(f"[{i}] 圖片處理最終失敗，跳過")
            continue

        # 上傳（含 retry）
        sticker = InputSticker(sticker=processed, emoji_list=["✨"], format=fmt)
        if not first_done:
            ok = await _tg_upload(bot, "create", status_msg,
                                  user_id=user_id, name=pack_name,
                                  title=pack_title, stickers=[sticker],
                                  sticker_type="regular")
            if ok:
                first_done = True
        else:
            ok = await _tg_upload(bot, "add", status_msg,
                                  user_id=user_id, name=pack_name, sticker=sticker)

        if ok:
            success += 1

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
