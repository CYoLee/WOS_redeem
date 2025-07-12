# redeem_web.py
import asyncio
import base64
import json
import os
import io
import re
import traceback
import hashlib
import requests
import time
import contextlib
import sys
import logging
import aiohttp
import threading
import textwrap
from textwrap import indent
#Remove preprocess_image_for_2captcha()
#from io import BytesIO
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, TimeoutError
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
#Remove preprocess_image_for_2captcha()
#from PIL import Image
import subprocess
import nest_asyncio
import functools
from datetime import datetime
from pytz import timezone
from datetime import datetime, timedelta
tz = timezone("Asia/Taipei")
from googletrans import Translator
translator = Translator()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
nest_asyncio.apply()
loop = asyncio.get_event_loop_policy().get_event_loop()

def build_summary_block(code, success, fail, skipped, duration, is_retry=False):
    return (
        f"=== {'Retry ' if is_retry else ''}Summary ===\n"
        f"Giftcode : {code}\n"
        f"Success  : {success}\n"
        f"Failed   : {fail}\n"
        f"Skipped  : {skipped}\n"
        f"Duration : {duration:.1f}s"
    )

@contextlib.contextmanager
def suppress_stdout():
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout

def get_webhook_url_by_guild(guild_id: str) -> str:
    key = f"WEBHOOK_{guild_id}"
    return os.getenv(key)

def send_long_webhook(webhook_url, content):
    max_length = 1900
    chunks = [content[i:i + max_length] for i in range(0, len(content), max_length)]
    for chunk in chunks:
        try:
            resp = requests.post(webhook_url, json={"content": chunk})
            if resp.status_code >= 400:
                logger.warning(f"[Webhook] 發送失敗：{resp.status_code} {resp.text}")
            else:
                logger.info(f"[Webhook] 發送成功：{resp.status_code}")
        except Exception as e:
            logger.warning(f"[Webhook] 發送失敗：{e}")

# === 初始化 ===
app = Flask(__name__)

# === 設定 ===
OCR_MAX_RETRIES = 3
PAGE_LOAD_TIMEOUT = 60000
DEBUG_MODE = True

# === Firebase Init ===
load_dotenv()
cred_json = json.loads(base64.b64decode(
    os.environ.get("FIREBASE_KEY_BASE64") or os.environ.get("FIREBASE_CREDENTIALS", "{}")
).decode("utf-8"))
if "private_key" in cred_json:
    cred_json["private_key"] = cred_json["private_key"].replace("\\n", "\n")
if not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(cred_json))
db = firestore.client()
async def run_in_executor(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    partial_func = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, partial_func)

async def firestore_get(ref):
    return await run_in_executor(ref.get)

async def firestore_set(ref, data, merge=False):
    return await run_in_executor(ref.set, data, merge=merge)

async def firestore_update(ref, data):
    return await run_in_executor(ref.update, data)

async def firestore_delete(ref):
    return await run_in_executor(ref.delete)

async def firestore_stream(ref):
    return await run_in_executor(lambda: list(ref.stream()))

FAILURE_KEYWORDS = ["請先輸入", "不存在", "錯誤", "無效", "超出", "無法", "類型"]
RETRY_KEYWORDS = ["驗證碼錯誤", "伺服器繁忙", "請稍後再試", "系統異常", "請重試", "處理中"]
SUCCESS_KEYWORDS = ["您已領取", "已兌換", "已領取過", "已經兌換", "超出兌換時間", "已使用", "已過期", "兌換成功，請在信件中領取獎勵！", "您已領取過", "暫不符合兌換要求"
]

def is_success_reason(reason, message=""):
    combined_msg = (reason or "") + (message or "")
    return any(k in combined_msg for k in SUCCESS_KEYWORDS)

REDEEM_RETRIES = 3
# === 主流程 ===
async def process_redeem(payload):
    start_time = time.time()
    code = payload.get("code")
    player_ids = payload.get("player_ids")
    debug = payload.get("debug", False)
    guild_id = payload.get("guild_id")
    is_retry = payload.get("retry", False)

    header = "Retry 兌換完成 / Retry Redemption Complete" if is_retry else "兌換完成 / Redemption Completed"
    MAX_BATCH_SIZE = 1
    all_success = []
    all_fail = []

    await asyncio.gather(*(fetch_and_store_if_missing(guild_id, pid) for pid in player_ids))

    success_docs = await firestore_stream(
        db.collection("success_redeems").document(f"{guild_id}_{code}").collection("players")
    )
    already_redeemed_ids = {doc.id for doc in success_docs}

    failed_docs = await firestore_stream(
        db.collection("failed_redeems").document(f"{guild_id}_{code}").collection("players")
    )
    failed_ids = {doc.id for doc in failed_docs}

    captcha_failed_ids = {
        doc.id for doc in failed_docs
        if "驗證碼三次辨識皆失敗" in (doc.to_dict() or {}).get("reason", "") or
           "CAPTCHA failed 3 times" in (doc.to_dict() or {}).get("reason", "")
    }

    filtered_player_ids = []

    for pid in player_ids:
        if pid in already_redeemed_ids:
            continue
        if not is_retry:
            if pid in captcha_failed_ids:
                continue
        if is_retry:
            if pid in failed_ids:
                filtered_player_ids.append(pid)
        else:
            if pid in failed_ids:
                continue
            filtered_player_ids.append(pid)
    skipped_count = len(player_ids) - len(filtered_player_ids)

    if debug:
        for pid in filtered_player_ids:
            assert pid not in already_redeemed_ids, f"過濾失敗：{pid} 應已在 success_redeems 中"

    if not filtered_player_ids:
        summary_block = build_summary_block(
            code=code,
            success=0,
            fail=0,
            skipped=skipped_count,
            duration=time.time() - start_time,
            is_retry=is_retry
        )
        full_block = f"{summary_block}\n\n所有 ID 皆已兌換成功或已領取過，無需再處理"
        msg = f"{header}\n```text\n{textwrap.indent(full_block, '  ')}\n```"

        webhook_url = get_webhook_url_by_guild(guild_id)
        if webhook_url:
            send_long_webhook(webhook_url, msg)
        return

    for i in range(0, len(filtered_player_ids), MAX_BATCH_SIZE):
        batch = filtered_player_ids[i:i + MAX_BATCH_SIZE]
        tasks = [run_redeem_with_retry(pid, code, debug=debug) for pid in batch]
        results = await asyncio.gather(*tasks)
        await asyncio.sleep(1)

        for r in results:
            if isinstance(r, Exception):
                logger.error(f"[process_all] 任務發生例外，自動包裝：{r}")
                r = {
                    "player_id": "Unknown",
                    "success": False,
                    "reason": str(r),
                    "debug_logs": []
                }

            if not isinstance(r, dict):
                logger.error(f"[process_all] 任務回傳非 dict，自動包裝：{r}")
                r = {
                    "player_id": "Unknown",
                    "success": False,
                    "reason": str(r) if r else "None or invalid return",
                    "debug_logs": []
                }

            reason = str(r.get("reason", "") if isinstance(r, dict) else r or "")
            message = str(r.get("message", "") if isinstance(r, dict) else "")

            if is_success_reason(reason, message):
                all_success.append(r)
                await firestore_set(
                    db.collection("success_redeems").document(f"{guild_id}_{code}").collection("players").document(r["player_id"]),
                    {
                        "message": reason or message or "成功但無訊息",
                        "timestamp": datetime.utcnow()
                    }
                )
                await firestore_delete(
                    db.collection("failed_redeems").document(f"{guild_id}_{code}").collection("players").document(r["player_id"])
                )
            else:
                doc = await firestore_get(db.collection("ids").document(guild_id).collection("players").document(r["player_id"]))
                name = doc.to_dict().get("name", "未知名稱") if doc.exists else "未知"
                await firestore_set(
                    db.collection("failed_redeems").document(f"{guild_id}_{code}").collection("players").document(r["player_id"]),
                    {
                        "name": name,
                        "reason": reason or "未知錯誤",
                        "updated_at": datetime.utcnow()
                    }
                )
                all_fail.append(r)

    summary_block = build_summary_block(
        code=code,
        success=len(all_success),
        fail=len(all_fail),
        skipped=skipped_count,
        duration=time.time() - start_time,
        is_retry=is_retry
    )

    failures_block = await format_failures_block(guild_id, all_fail)
    full_block = f"{summary_block}\n\n{failures_block.strip() or '無錯誤資料 / No error data'}"
    webhook_message = f"{header}\n```text\n{textwrap.indent(full_block, '  ')}\n```"

    webhook_url = get_webhook_url_by_guild(guild_id)
    if webhook_url:
        try:
            send_long_webhook(webhook_url, webhook_message)
        except Exception as e:
            logger.warning(f"[Webhook] 發送失敗：{e}")

async def run_redeem_with_retry(player_id, code, debug=False):
    debug_logs = []

    for redeem_retry in range(REDEEM_RETRIES + 1):
        try:
            result = await asyncio.wait_for(
                _redeem_once(player_id, code, debug_logs, redeem_retry, debug=debug),
                timeout=90  # 每次單人兌換最多 90 秒
            )
        except asyncio.TimeoutError:
            logger.error(f"[{player_id}] 第 {redeem_retry + 1} 次：超過 90 秒 timeout")
            return {
                "success": False,
                "reason": "Timeout：單人兌換超過 90 秒",
                "player_id": player_id,
                "debug_logs": debug_logs
            }

        if result is None or not isinstance(result, dict):
            logger.error(f"[{player_id}] 第 {redeem_retry + 1} 次：_redeem_once 回傳 None 或格式錯誤 → {result}")
            result = {
                "player_id": player_id,
                "success": False,
                "reason": str(result) if result else "無效回傳（None 或錯誤格式）",
                "debug_logs": debug_logs
            }

        result["reason"] = result.get("reason") or "未知錯誤"

        if result["reason"].startswith("_try"):
            continue

        if is_success_reason(result.get("reason", ""), result.get("message", "")):
            return result

        if "登入失敗" in (result.get("reason") or "") or "請先登入" in (result.get("reason") or ""):
            return result

        if any(k in (result.get("reason") or "") for k in RETRY_KEYWORDS):
            debug_logs.append({
                "retry": redeem_retry + 1,
                "info": f"Retry due to: {result.get('reason')}"
            })
            await asyncio.sleep(2 + redeem_retry)
        else:
            return result


    return result

async def _redeem_once(player_id, code, debug_logs, redeem_retry, debug=False):
    logger.info(f"[{player_id}] _redeem_once() 進入，開始兌換流程")
    browser = None

    def log_entry(attempt, **kwargs):
        entry = {"redeem_retry": redeem_retry, "attempt": attempt}
        entry.update(kwargs)
        debug_logs.append(entry)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-gpu"])
            context = await browser.new_context(locale="zh-TW")
            page = await context.new_page()
            await page.goto("https://wos-giftcode.centurygame.com/", timeout=PAGE_LOAD_TIMEOUT)
            await page.fill('input[type="text"]', player_id)
            await page.click(".login_btn")

            # 嘗試等待錯誤 modal
            try:
                await page.wait_for_selector(".message_modal", timeout=5000)
                modal_text = await page.inner_text(".message_modal .msg")
                log_entry(0, error_modal=modal_text)
                if any(k in modal_text for k in FAILURE_KEYWORDS):
                    logger.info(f"[{player_id}] 登入失敗：{modal_text}")
                    return await _package_result(page, False, f"登入失敗：{modal_text}", player_id, debug_logs, debug=debug)
            except TimeoutError:
                pass  # 無 modal 則繼續檢查登入成功

            # 加強：等待 .name 與兌換欄位都出現才視為成功
            try:
                await page.wait_for_selector(".name", timeout=5000)
                await page.wait_for_selector('input[placeholder="請輸入兌換碼"]', timeout=5000)
            except TimeoutError:
                return await _package_result(page, False, "登入失敗（未成功進入兌換頁） / Login failed (did not reach redeem page)", player_id, debug_logs, debug=debug)

            await page.fill('input[placeholder="請輸入兌換碼"]', code)

            for attempt in range(1, OCR_MAX_RETRIES + 1):
                try:
                    logger.info(f"[{player_id}] CAPTCHA_API_KEY 存在檢查: {bool(CAPTCHA_API_KEY)}")
                    captcha_text, method_used = await _solve_captcha(page, attempt, player_id)
                    log_entry(attempt, captcha_text=captcha_text, method=method_used)

                    await page.fill('input[placeholder="請輸入驗證碼"]', captcha_text or "")

                    try:
                        await page.click(".exchange_btn", timeout=3000)
                        await page.wait_for_timeout(1000)

                        for _ in range(10):
                            modal = await page.query_selector(".message_modal")
                            if modal:
                                msg_el = await modal.query_selector("p.msg")
                                if msg_el:
                                    message = await msg_el.inner_text()
                                    log_entry(attempt, server_message=message)
                                    logger.info(f"[{player_id}] 第 {attempt} 次：伺服器回應：{message}")

                                    confirm_btn = await modal.query_selector(".confirm_btn")
                                    if confirm_btn and await confirm_btn.is_visible():
                                        await confirm_btn.click()
                                        await page.wait_for_timeout(500)

                                    if "驗證碼錯誤" in message or "驗證碼已過期" in message:
                                        await _refresh_captcha(page, player_id=player_id)
                                        break

                                    if any(k in message for k in FAILURE_KEYWORDS):
                                        return await _package_result(page, False, message, player_id, debug_logs, debug=debug)

                                    if "成功" in message:
                                        return await _package_result(page, True, message, player_id, debug_logs, debug=debug)

                                    return await _package_result(page, False, f"未知錯誤：{message}", player_id, debug_logs, debug=debug)

                            await page.wait_for_timeout(300)

                        else:
                            log_entry(attempt, server_message="未出現 modal 回應（點擊被遮蔽或失敗）")
                            await _refresh_captcha(page, player_id=player_id)
                            continue

                    except Exception as e:
                        log_entry(attempt, error=f"點擊或等待 modal 時失敗: {str(e)}")
                        await _refresh_captcha(page, player_id=player_id)
                        await page.wait_for_timeout(1000)
                        continue

                except Exception:
                    log_entry(attempt, error=traceback.format_exc())
                    await _refresh_captcha(page, player_id=player_id)
                    await page.wait_for_timeout(1000)

            log_entry(attempt, info="驗證碼三次辨識皆失敗，放棄兌換")
            logger.info(f"[{player_id}] 最終失敗：驗證碼三次辨識皆失敗 / Final failure: CAPTCHA failed 3 times")
            return await _package_result(page, False, "驗證碼三次辨識皆失敗，放棄兌換", player_id, debug_logs, debug=debug)

    except Exception as e:
        logger.exception(f"[{player_id}] 發生例外錯誤：{e}")
        html, img = None, None
        if debug:
            try:
                html = await page.content() if 'page' in locals() else "<no page>"
                img = await page.screenshot() if 'page' in locals() else None
            except:
                pass
        return {
            "player_id": player_id,
            "success": False,
            "reason": "例外錯誤",
            "debug_logs": debug_logs,
            "debug_html_base64": base64.b64encode(html.encode("utf-8")).decode() if html else None,
            "debug_img_base64": base64.b64encode(img).decode() if img else None
        }

    finally:
        if browser:
            await browser.close()

    return {
        "player_id": player_id,
        "success": False,
        "reason": "未知錯誤（流程未命中任何 return）",
        "debug_logs": debug_logs
    }

async def _solve_captcha(page, attempt, player_id):
    fallback_text = f"_try{attempt}"
    method_used = "none"
    def log_entry(attempt, **kwargs):
        entry = {"attempt": attempt}
        entry.update(kwargs)
        logger.info(f"[{player_id}] DebugLog: {entry}")

    try:
        captcha_img = await page.query_selector(".verify_pic")
        if not captcha_img:
            logger.info(f"[{player_id}] 第 {attempt} 次：未找到驗證碼圖片")
            return fallback_text, method_used

        await page.wait_for_timeout(500)
        try:
            captcha_bytes = await asyncio.wait_for(captcha_img.screenshot(), timeout=10)
        except Exception as e:
            logger.warning(f"[{player_id}] 第 {attempt} 次：captcha screenshot timeout 或錯誤 → {e}")
            return fallback_text, method_used

        # ✅ 圖片過小則自動刷新，避免 2Captcha 拒收
        if not captcha_bytes or len(captcha_bytes) < 1024:
            logger.warning(f"[{player_id}] 第 {attempt} 次：驗證碼圖太小（{len(captcha_bytes) if captcha_bytes else 0} bytes），自動刷新")
            await _refresh_captcha(page, player_id=player_id)
            return fallback_text, method_used

        # 強化圖片 → base64 編碼
        #b64_img = preprocess_image_for_2captcha(captcha_bytes)
        b64_img = base64.b64encode(captcha_bytes).decode("utf-8")

        logger.info(f"[{player_id}] 第 {attempt} 次：使用 2Captcha 辨識")
        result = await solve_with_2captcha(b64_img)
        if result == "UNSOLVABLE":
            logger.warning(f"[{player_id}] 第 {attempt} 次：2Captcha 回傳無解 → 自動刷新圖")
            log_entry(attempt, info="2Captcha 回傳 UNSOLVABLE")
            await _refresh_captcha(page, player_id=player_id)
            return fallback_text, method_used

        if result:
            result = result.strip()
            if len(result) == 4 and result.isalnum():
                method_used = "2captcha"
                logger.info(f"[{player_id}] 第 {attempt} 次：2Captcha 成功辨識 → {result}")
                return result, method_used
            else:
                logger.warning(f"[{player_id}] 第 {attempt} 次：2Captcha 回傳長度不符（{len(result)}字 → {result}），強制刷新")
                await _refresh_captcha(page, player_id=player_id)
                return fallback_text, method_used

    except Exception as e:
        logger.exception(f"[{player_id}] 第 {attempt} 次：例外錯誤：{e}")
        return fallback_text, method_used

# def preprocess_image_for_2captcha(img_bytes, scale=2.5):
#     """轉灰階、二值化、放大並轉 base64 編碼"""
#     img = Image.open(BytesIO(img_bytes)).convert("L")  # 灰階
#     img = img.point(lambda x: 0 if x < 140 else 255, '1')  # 二值化
#     new_size = (int(img.width * scale), int(img.height * scale))
#     img = img.resize(new_size, Image.LANCZOS)
#     buffer = BytesIO()
#     img.save(buffer, format="PNG")
#     return base64.b64encode(buffer.getvalue()).decode("utf-8")

def _clean_ocr_text(text):
    """替換常見誤判字元並移除非字母數字"""
    corrections = {
        "0": "O", "1": "I", "5": "S", "8": "B", "$": "S", "6": "G",
        "l": "I", "|": "I", "2": "Z", "9": "g", "§": "S", "£": "E",
        "4": "A", "@": "A"
    }
    for wrong, correct in corrections.items():
        text = text.replace(wrong, correct)
    return ''.join(filter(str.isalnum, text))

def _save_debug_captcha_image(img_np, label, player_id, attempt):
    date_folder = f"debug/{datetime.now().strftime('%Y%m%d')}"
    os.makedirs(date_folder, exist_ok=True)
    filename = f"captcha_{player_id}_attempt{attempt}_{label}.png"
    Image.fromarray(img_np).save(os.path.join(date_folder, filename))


def _save_blank_captcha_image(player_id, attempt):
    date_folder = f"debug/{datetime.now().strftime('%Y%m%d')}"
    os.makedirs(date_folder, exist_ok=True)
    Image.new("RGB", (200, 50), "white").save(
        os.path.join(date_folder, f"captcha_{player_id}_attempt{attempt}_blank_none.png")
    )

CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY")
logger.info(f"CAPTCHA_API_KEY 設定檢查: {bool(CAPTCHA_API_KEY)}")

async def solve_with_2captcha(b64_img):
    api_key = os.getenv("CAPTCHA_API_KEY")
    payload = {
        "key": api_key,
        "method": "base64",
        "body": b64_img,
        "json": 1,
        "numeric": 0,
        "min_len": 4,
        "max_len": 5,
        "language": 2
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("http://2captcha.com/in.php", data=payload) as resp:
                if resp.content_type != "application/json":
                    text = await resp.text()
                    logger.error(f"2Captcha 提交回傳非 JSON（{resp.status}）：{text}")
                    return None

                res = await resp.json()
                if res.get("status") != 1:
                    logger.warning(f"2Captcha 提交失敗：{res}")
                    return None

                request_id = res["request"]
        except Exception as e:
            logger.exception(f"提交 2Captcha 發生錯誤：{e}")
            return None

        # 等待辨識結果
        for _ in range(12):
            await asyncio.sleep(5)
            try:
                async with session.get(f"http://2captcha.com/res.php?key={api_key}&action=get&id={request_id}&json=1") as resp:
                    if resp.content_type != "application/json":
                        text = await resp.text()
                        logger.error(f"2Captcha 查詢回傳非 JSON（{resp.status}）：{text}")
                        return None

                    result = await resp.json()
                    if result.get("status") == 1:
                        return result.get("request")
                    if result.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                        logger.warning(f"2Captcha 回傳無法解碼錯誤 → {result}")
                        return "UNSOLVABLE"
                    elif result.get("request") != "CAPCHA_NOT_READY":
                        logger.warning(f"2Captcha 回傳錯誤結果：{result}")
                        return None

            except Exception as e:
                logger.exception(f"查詢 2Captcha 結果發生錯誤：{e}")
                return None

    return None

async def _refresh_captcha(page, player_id=None):
    try:
        refresh_btn = await page.query_selector('.reload_btn')
        captcha_img = await page.query_selector('.verify_pic')
        if not refresh_btn or not captcha_img:
            logger.info(f"[{player_id}] 無法定位驗證碼圖片或刷新按鈕")
            return

        # 先確保 modal 已經關閉
        for _ in range(10):
            modal = await page.query_selector('.message_modal')
            if not modal:
                break
            confirm_btn = await modal.query_selector('.confirm_btn')
            if confirm_btn and await confirm_btn.is_visible():
                await confirm_btn.click()
            await page.wait_for_timeout(1000)

        try:
            original_bytes = await asyncio.wait_for(captcha_img.screenshot(), timeout=10)
        except Exception as e:
            logger.warning(f"[{player_id}] captcha 原圖 screenshot timeout 或錯誤 → {e} / original captcha screenshot timeout or error")
            return
        original_hash = hashlib.md5(original_bytes).hexdigest() if original_bytes else ""

        # 點擊刷新按鈕
        await refresh_btn.click()
        await page.wait_for_timeout(1500)

        # 處理 modal（如果彈出錯誤訊息）
        for _ in range(8):
            modal = await page.query_selector('.message_modal')
            if modal:
                msg_el = await modal.query_selector('p.msg')
                if msg_el:
                    msg_text = await msg_el.inner_text()
                    logger.info(f"[{player_id}] Captcha Modal：{msg_text.strip()}")
                    if any(k in msg_text for k in ["過於頻繁", "伺服器繁忙", "請稍後再試"]):
                        confirm_btn = await modal.query_selector('.confirm_btn')
                        if confirm_btn:
                            await confirm_btn.click()
                        await page.wait_for_timeout(1500)
                        return
            await page.wait_for_timeout(300)

        # 等待圖刷新
        for i in range(30):
            await page.wait_for_timeout(150)
            try:
                new_bytes = await asyncio.wait_for(captcha_img.screenshot(), timeout=10)
            except Exception as e:
                logger.warning(f"[{player_id}] captcha 新圖 screenshot timeout 或錯誤（第 {i+1} 次）→ {e}")
                continue

            if not new_bytes or len(new_bytes) < 1024:
                continue
            new_hash = hashlib.md5(new_bytes).hexdigest()
            if new_hash != original_hash:
                box = await captcha_img.bounding_box()
                if box and box["height"] > 10:
                    logger.info(f"[{player_id}] 成功刷新驗證碼 (hash 第 {i+1} 次變化)")
                    return
        else:
            logger.info(f"[{player_id}] 刷新失敗：圖片內容未更新 / Refresh failed: Captcha image did not update")

    except Exception as e:
        logger.info(f"[{player_id}] Captcha 刷新例外：{str(e)} / Refresh captcha exception: {str(e)}")

async def _package_result(page, success, message, player_id, debug_logs, debug=False):
    result = {
        "player_id": player_id,
        "success": success,
        "reason": message if not success else None,
        "message": message if success else None,
        "debug_logs": debug_logs
    }

    if debug and page:
        try:
            html = await page.content()
            screenshot = await page.screenshot()
            result["debug_html_base64"] = base64.b64encode(html.encode("utf-8")).decode("utf-8")
            result["debug_img_base64"] = base64.b64encode(screenshot).decode("utf-8")
        except Exception as e:
            result["debug_html_base64"] = None
            result["debug_img_base64"] = None
            debug_logs.append({"error": f"[{player_id}] 無法擷取 debug 畫面: {str(e)}"})
    result["reason"] = result.get("reason") or "未知錯誤"
    return result

# === 共用函式：透過 Playwright 取得玩家名稱與王國 ===
async def fetch_name_and_kingdom_common(pid):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="zh-TW")
        page = await context.new_page()
        name = "未知名稱"
        kingdom = None

        for attempt in range(3):
            try:
                await page.goto("https://wos-giftcode.centurygame.com/")
                await page.fill('input[type="text"]', pid)
                await page.click(".login_btn")
                await page.wait_for_selector('input[placeholder="請輸入兌換碼"]', timeout=5000)
                await page.wait_for_selector(".name", timeout=5000)

                name_el = await page.query_selector(".name")
                name = await name_el.inner_text() if name_el else "未知名稱"

                try:
                    other_els = await page.query_selector_all(".other")
                    for el in other_els:
                        text = await el.inner_text()
                        match = re.search(r"王國[:：]\s*(\d+)", text)
                        if match:
                            kingdom = match.group(1)
                            break
                except Exception as e:
                    logger.warning(f"[{pid}][Warn] 擷取王國失敗：{e}")
                break
            except:
                await page.wait_for_timeout(1000 + attempt * 500)

        await browser.close()
        return name, kingdom

async def fetch_and_store_if_missing(guild_id, pid):
    ref = db.collection("ids").document(guild_id).collection("players").document(pid)
    doc = await firestore_get(ref)
    if doc.exists:
        return
    name, kingdom = await fetch_name_and_kingdom_common(pid)
    if is_valid_player_data(name, kingdom):
        await firestore_set(ref, {
            "name": name,
            "kingdom": kingdom,
            "updated_at": datetime.utcnow()
        }, merge=True)
        logger.info(f"[{pid}][Info]已自動新增：{name}")
    else:
        logger.warning(f"[{pid}][Warn]名稱或王國未知，未寫入")

def is_valid_player_data(name: str, kingdom: str) -> bool:
    return name != "未知名稱" and kingdom != "未知"

async def format_failures_block(guild_id, all_fail):
    lines = []
    for r in all_fail:
        pid = r["player_id"]
        doc = await firestore_get(
            db.collection("ids").document(guild_id).collection("players").document(pid)
        )
        data = doc.to_dict() if doc.exists else {}
        name = data.get("name", "未知名稱")
        kingdom = data.get("kingdom", "未知")
        lines.append(f"- {pid}｜{kingdom}｜{name}")
    return "\n".join(lines)

# === Flask API ===
@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/run_notify", methods=["POST"])
def run_notify():
    try:
        secret = os.getenv("INTERNAL_SECRET")
        url = "https://wosredeem-production-2f18.up.railway.app/internal_push_notify"
        resp = requests.post(url, json={"secret": secret}, timeout=10)
        return f"✅ 結果：{resp.text}", 200
    except Exception as e:
        return f"❌ 發生錯誤：{e}", 500

@app.route("/add_id", methods=["POST"])
def add_id():
    try:
        data = request.json
        guild_id = data.get("guild_id")
        player_id = data.get("player_id")

        if not guild_id or not player_id:
            return jsonify({"success": False, "reason": "缺少 guild_id 或 player_id / Missing guild_id or player_id"}), 400

        async def run_all():
            player_name, kingdom = await fetch_name_and_kingdom_common(player_id)

            ref = db.collection("ids").document(guild_id).collection("players").document(player_id)
            existing_doc = await firestore_get(ref)
            doc_data = existing_doc.to_dict() if existing_doc.exists else {}

            return player_name, kingdom, ref, existing_doc, doc_data

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        player_name, kingdom, ref, existing_doc, doc_data = loop.run_until_complete(run_all())

        name_changed = doc_data.get("name") != player_name
        kingdom_changed = doc_data.get("kingdom") != kingdom

        if player_name == "未知名稱" or kingdom == "未知":
            logger.warning(f"[{player_id}][Warn]名稱或王國為未知，未更新 Firestore")
            return jsonify({
                "success": False,
                "reason": "名稱或王國為未知，未寫入資料庫"
            }), 400

        if name_changed or kingdom_changed:
            loop.run_until_complete(firestore_set(ref, {
                "name": player_name,
                "kingdom": kingdom,
                "updated_at": datetime.utcnow()
            }, merge=True))

        webhook_url = os.getenv("ADD_ID_WEBHOOK_URL")
        if webhook_url and (not existing_doc.exists or name_changed or kingdom_changed):
            try:
                if not existing_doc.exists:
                    content = (
                        f"[Info]新增 ID 通知 / Add ID Notification\n"
                        f"🆔 Guild ID: `{guild_id}`\n"
                        f"👤 Player ID: `{player_id}`\n"
                        f"📛 Name: `{player_name}`\n"
                        f"🏰 Kingdom: `{kingdom}`"
                    )
                else:
                    content = (
                        f"🔁 資料更新通知 / Info Updated\n"
                        f"🆔 Guild ID: `{guild_id}`\n"
                        f"👤 Player ID: `{player_id}`\n"
                        f"📛 Name: `{player_name}`\n"
                        f"🏰 Kingdom: `{kingdom}`"
                    )
                send_long_webhook(webhook_url, content)
                logger.info(f"[Webhook] 已發送新增或更新通知")
            except Exception as e:
                logger.warning(f"[Webhook] 發送通知失敗：{e}")

        return jsonify({
            "success": True,
            "message": f"已新增或更新 {player_id} 至 guild {guild_id}",
            "name": player_name,
            "kingdom": kingdom
        })

    except Exception as e:
        return jsonify({"success": False, "reason": str(e)}), 500

@app.route("/list_ids", methods=["GET"])
def list_ids():
    try:
        guild_id = request.args.get("guild_id")
        if not guild_id:
            return jsonify({"success": False, "reason": "缺少 guild_id"}), 400

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        docs = loop.run_until_complete(
            firestore_stream(db.collection("ids").document(guild_id).collection("players"))
        )
        players = [{"id": doc.id, **doc.to_dict()} for doc in docs]
        return jsonify({"success": True, "players": players})
    except Exception as e:
        return jsonify({"success": False, "reason": str(e)}), 500

@app.route("/redeem_submit", methods=["POST"])
def redeem_submit():
    data = request.json
    logger.info(f"[REDEEM SUBMIT] 收到請求：{data}")

    code = data.get("code")
    player_ids = data.get("player_ids")
    debug = data.get("debug", False)
    guild_id = data.get("guild_id")

    if not guild_id or not code or not isinstance(player_ids, list) or not player_ids:
        return jsonify({"success": False, "reason": "缺少必要參數"}), 400

    payload = {
        "code": code,
        "player_ids": player_ids,
        "debug": debug,
        "guild_id": guild_id,
        "retry": False
    }
    loop.create_task(process_redeem(payload))
    return jsonify({"message": "兌換任務已提交，背景處理中"}), 200

@app.route("/update_names_api", methods=["POST"])
def update_names_api():
    try:
        data = request.json
        guild_id = data.get("guild_id")
        if not guild_id:
            return jsonify({"success": False, "reason": "缺少 guild_id / Missing guild_id"}), 400

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            player_docs = loop.run_until_complete(
                firestore_stream(db.collection("ids").document(guild_id).collection("players"))
            )
        except Exception as e:
            logger.error(f"[Firestore] 讀取 IDs 出錯：{e}")
            return jsonify({"success": False, "reason": str(e)}), 500

        player_ids = [doc.id for doc in player_docs]
        updated = []

        async def fetch_all():
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    context = await browser.new_context(locale="zh-TW")
                    page = await context.new_page()

                    for pid in player_ids:
                        try:
                            name, kingdom = await fetch_name_and_kingdom_common(pid)

                            doc_ref = db.collection("ids").document(guild_id).collection("players").document(pid)
                            existing_doc = await firestore_get(doc_ref)
                            doc_data = existing_doc.to_dict() if existing_doc.exists else {}
                            existing_name = doc_data.get("name")
                            existing_kingdom = doc_data.get("kingdom")

                            if name == "未知名稱" or not kingdom or kingdom == "未知":
                                logger.warning(f"[{pid}][Warn]名稱或王國為未知，跳過更新")
                                continue

                            if existing_name != name or existing_kingdom != kingdom:
                                updated.append({
                                    "id": pid,
                                    "old_name": existing_name or "未知",
                                    "new_name": name,
                                    "old_kingdom": existing_kingdom or "未知",
                                    "new_kingdom": kingdom
                                })
                                await firestore_set(doc_ref, {
                                    "name": name,
                                    "kingdom": kingdom,
                                    "updated_at": datetime.utcnow()
                                }, merge=True)
                            else:
                                logger.info(f"[{pid}] 無變更，保留原資料")
                        except Exception as e:
                            logger.error(f"[{pid}] 抓取或更新失敗：{e}")

                    await browser.close()
            except Exception as e:
                logger.error(f"[Playwright] 瀏覽器錯誤：{e}")
                raise

        try:
            loop.run_until_complete(fetch_all())
        except Exception as e:
            logger.error(f"[UpdateNames] fetch_all 執行失敗：{e}")
            return jsonify({"success": False, "reason": str(e)}), 500

        # ✅ Webhook
        if updated and os.getenv("ADD_ID_WEBHOOK_URL"):
            try:
                lines = []
                for u in updated:
                    pid = u["id"]
                    line = f"{pid}（王國 {u['new_kingdom']}）"
                    if u["old_name"] != u["new_name"] and u["old_kingdom"] != u["new_kingdom"]:
                        line += f"\n{u['old_name']}（{u['old_kingdom']}） ➜ {u['new_name']}（{u['new_kingdom']}）"
                    elif u["old_name"] != u["new_name"]:
                        line += f"\n{u['old_name']} ➜ {u['new_name']}"
                    elif u["old_kingdom"] != u["new_kingdom"]:
                        line += f"\n王國 {u['old_kingdom']} ➜ {u['new_kingdom']}"
                    lines.append(line)

                content = (
                    f"🔁 共更新 {len(updated)} 筆名稱 / Updated {len(updated)} records:\n\n"
                    + "\n\n".join(lines)
                )
                send_long_webhook(os.getenv("ADD_ID_WEBHOOK_URL"), content)
                logger.info(f"[Webhook] 已發送更新通知")
            except Exception as e:
                logger.warning(f"[Webhook] 發送失敗：{e}")

        return jsonify({
            "success": True,
            "guild_id": guild_id,
            "updated": updated
        })

    except Exception as e:
        logger.error(f"[UpdateNames] 發生嚴重錯誤：{e}")
        return jsonify({"success": False, "reason": str(e)}), 500

@app.route("/retry_failed", methods=["POST"])
def retry_failed():
    data = request.json
    code = data.get("code")
    debug = data.get("debug", False)
    guild_id = data.get("guild_id")
    if not guild_id:
        return jsonify({"success": False, "reason": "缺少 guild_id"}), 400
    if not code:
        return jsonify({"success": False, "reason": "缺少 code"}), 400

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    failed_docs = loop.run_until_complete(
        firestore_stream(db.collection("failed_redeems").document(f"{guild_id}_{code}").collection("players"))
    )

    player_ids = [doc.id for doc in failed_docs]

    if not player_ids:
        return jsonify({"success": False, "reason": f"找不到 failed_redeems 清單：{code} / Cannot find failed_redeems list for code: {code}"}), 404

    logger.info(f"[{guild_id}] 🔁 Retry {len(player_ids)} failed ID(s) for giftcode {code}")

    try:
        payload = {
            "code": code,
            "player_ids": player_ids,
            "debug": debug,
            "guild_id": guild_id,
            "retry": True,
        }
        loop.run_until_complete(process_redeem(payload))
        return jsonify({"success": True, "message": f"已針對 {len(player_ids)} 筆失敗紀錄重新兌換"}), 200
    except Exception as e:
        return jsonify({"success": False, "reason": str(e)}), 500

def send_to_discord(channel_id, mention, message):
    if "discord.com/api/webhooks/" in channel_id:
        content = f"{mention}\n⏰ **活動提醒 / Reminder** ⏰\n{message}"
        send_long_webhook(channel_id, content)
    else:
        logger.warning(f"[Notify] 非 webhook URL，未處理：{channel_id}")

@app.route("/")
def health():
    return "Worker ready for redeeming!"

async def self_ping_loop():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://gua-gua-bot-worker-649040484909.asia-east1.run.app") as resp:
                    logger.info(f"[Self Ping] Worker 回應狀態 / Status: {resp.status}")
        except Exception as e:
            logger.warning(f"[Self Ping] Worker 失敗 / Failed: {e}")
        await asyncio.sleep(240)  # 每 4 分鐘 ping 一次

from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from flask import abort
import time

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")  # ← 你要把你的 Secret 存進環境變數

async def get_translate_setting(group_id):
    try:
        ref = db.collection("line_groups").document(group_id).collection("config").document("settings")
        doc = await firestore_get(ref)
        if doc.exists:
            return doc.to_dict().get("translate_enabled", True)  # 預設為開
        return True
    except Exception as e:
        logger.warning(f"[LINE] 無法讀取翻譯設定：{e}")
        return True

async def set_translate_setting(group_id, enabled: bool):
    try:
        ref = db.collection("line_groups").document(group_id).collection("config").document("settings")
        await firestore_set(ref, {
            "translate_enabled": enabled
        }, merge=True)
        return True
    except Exception as e:
        logger.warning(f"[LINE] 無法寫入翻譯設定：{e}")
        return False

async def check_and_send_notify():
    now = datetime.now(tz).replace(second=0, microsecond=0)
    future = now + timedelta(seconds=30)
    docs = await firestore_stream(
        db.collection("notifications")
        .where("datetime", ">=", now)
        .where("datetime", "<", future)
        .order_by("datetime")
        .limit(10)
    )
    for doc in docs:
        data = doc.to_dict()
        try:
            channel_id = data.get("channel_id")
            mention = data.get("mention", "")
            message = data.get("message", "")

            send_to_discord(channel_id, mention, message)  # 透過 webhook 發送
            send_to_line_group(message)

            await firestore_delete(db.collection("notifications").document(doc.id))
        except Exception as e:
            logger.warning(f"[check_and_send_notify] 發送失敗：{e}")

@app.route("/line_webhook", methods=["POST"])
def line_webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    hash = hmac_new(CHANNEL_SECRET.encode(), body.encode(), sha256).digest()
    encoded_hash = base64.b64encode(hash).decode()
    if not compare_digest(encoded_hash, signature):
        abort(403)

    payload = request.json
    events = payload.get("events", [])
    for event in events:
        if event.get("type") != "message" or event["message"]["type"] != "text":
            continue

        user_id = event["source"].get("userId")
        group_id = event["source"].get("groupId")
        text = event["message"]["text"].strip()
        reply_token = event["replyToken"]

        if not group_id:
            reply_to_line(reply_token, "⚠️ 請在群組中使用本功能 / Please use this command in a group.")
            return "OK", 200

        if group_id == "C58bd3b35d69cb4514c002ff78ba1a49e":
            translate_enabled = asyncio.run(get_translate_setting(group_id))
            if translate_enabled:
                if not text.startswith("/"):
                    try:
                        detected = translator.detect(text).lang.lower()
                        logger.info(f"[LINE] 偵測語言：{detected}")

                        if detected == "th":
                            target_lang = "zh-tw"
                        elif detected in ["zh-cn", "zh-tw", "zh"]:
                            target_lang = "en"
                        elif detected == "en":
                            target_lang = "zh-tw"
                        else:
                            target_lang = None

                        if target_lang:
                            result = translator.translate(text, dest=target_lang)
                            reply_message = f"🌐 {result.text}"
                            reply_to_line(reply_token, reply_message)
                            continue  # 翻譯完成，跳過其他指令
                    except Exception as e:
                        reply_to_line(reply_token, f"❌ 翻譯失敗 / Translation failed：{e}")
                        continue

        profile_name = "Unknown"
        try:
            headers = {"Authorization": f"Bearer {os.getenv('LINE_CHANNEL_ACCESS_TOKEN')}"}
            resp = requests.get(f"https://api.line.me/v2/bot/profile/{user_id}", headers=headers, timeout=10)
            if resp.ok:
                profile_name = resp.json().get("displayName", "Unknown")
        except:
            pass

        col_ref = db.collection("line_groups").document(group_id).collection("users_data")
        docs = list(col_ref.stream())
        reply_message = ""

        if text.startswith("/新增") or text.startswith("/add"):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                reply_message = "❗請輸入 `/新增 遊戲名稱 遊戲ID`"
            else:
                game_name, game_id = parts[1], parts[2]
                if not game_id.isdigit():
                    reply_message = "❗遊戲 ID 只能是純數字"
                elif any(
                    d.to_dict().get("game_name") == game_name or
                    d.to_dict().get("game_id") == game_id
                    for d in docs
                ):
                    reply_message = "⚠️ 此遊戲名稱或 ID 已被其他人登記"
                else:
                    col_ref.add({
                        "user_id": user_id,
                        "line_name": profile_name,
                        "game_name": game_name,
                        "game_id": game_id,
                        "updated_at": datetime.utcnow()
                    })
                    reply_message = f"✅ 已新增紀錄：\n📛 {profile_name}\n🎮 {game_name}\n🆔 {game_id}"

        elif text.startswith("/查看清單") or text.startswith("/清單"):
            if docs:
                lines = [f"{i+1}. {d.to_dict().get('line_name')}｜{d.to_dict().get('game_name')}｜{d.to_dict().get('game_id')}" for i, d in enumerate(docs)]
                reply_message = "📋 當前清單（編號. 暱稱｜遊戲｜ID）：\n" + "\n".join(lines)
            else:
                reply_message = "⚠️ 尚無任何登記紀錄\n您可以使用 `/新增` 來新增資料。"

        elif text.startswith("/刪除") or text.startswith("/remove"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].isdigit():
                reply_message = "❗請輸入 `/刪除 編號`（如：/刪除 2）"
            else:
                idx = int(parts[1]) - 1
                if 0 <= idx < len(docs):
                    doc = docs[idx]
                    data = doc.to_dict()
                    try:
                        col_ref.document(doc.id).delete()
                        reply_message = f"🗑️ 已刪除第 {idx+1} 筆紀錄：\n📛 {data.get('line_name')}\n🎮 {data.get('game_name')}\n🆔 {data.get('game_id')}\n\n📌 可輸入 `/查看清單` 查看剩餘資料"
                    except Exception as e:
                        reply_message = f"❗刪除失敗：{str(e)}"
                else:
                    reply_message = "⚠️ 無效的編號"

        elif text.startswith("/修改") or text.startswith("/edit"):
            parts = text.split(maxsplit=3)
            if len(parts) < 4 or not parts[1].isdigit():
                reply_message = "❗請輸入 `/修改 編號 新遊戲名稱 新ID`"
            else:
                idx = int(parts[1]) - 1
                new_game_name, new_game_id = parts[2], parts[3]
                if not new_game_id.isdigit():
                    reply_message = "❗遊戲 ID 只能是純數字"
                elif any(
                    (d.to_dict().get("game_name") == new_game_name or
                     d.to_dict().get("game_id") == new_game_id)
                    for i, d in enumerate(docs) if i != idx
                ):
                    reply_message = "⚠️ 此遊戲名稱或 ID 已存在，無法修改為重複資料"
                elif 0 <= idx < len(docs):
                    doc = docs[idx]
                    try:
                        col_ref.document(doc.id).update({
                            "game_name": new_game_name,
                            "game_id": new_game_id,
                            "updated_at": datetime.utcnow()
                        })
                        reply_message = f"✏️ 已修改第 {idx+1} 筆紀錄：\n📛 {profile_name}\n🎮 {new_game_name}\n🆔 {new_game_id}"
                    except Exception as e:
                        reply_message = f"❗修改失敗：{str(e)}"
                else:
                    reply_message = "⚠️ 無效的編號"

        elif text == "/我誰":
            user_lines = [f"{i+1}. {d.to_dict().get('game_name')}｜{d.to_dict().get('game_id')}" for i, d in enumerate(docs) if d.to_dict().get("user_id") == user_id]
            if user_lines:
                reply_message = f"📛 {profile_name} 的紀錄如下：\n" + "\n".join(user_lines)
            else:
                reply_message = "🔍 查無您的紀錄，請先使用 `/新增` 建立資料。"
        elif text.lower() in ["/翻譯開", "/開", "/open"]:
            if group_id == "C58bd3b35d69cb4514c002ff78ba1a49e":
                if asyncio.run(set_translate_setting(group_id, True)):
                    reply_to_line(reply_token, "🌐 已開啟本群組的自動翻譯功能")
                else:
                    reply_to_line(reply_token, "⚠️ 開啟失敗，請稍後再試")
            else:
                reply_to_line(reply_token, "⚠️ 此指令僅限指定群組使用")

        elif text.lower() in ["/翻譯關", "/關", "/close"]:
            if group_id == "C58bd3b35d69cb4514c002ff78ba1a49e":
                if asyncio.run(set_translate_setting(group_id, False)):
                    reply_to_line(reply_token, "🌐 已關閉本群組的自動翻譯功能")
                else:
                    reply_to_line(reply_token, "⚠️ 關閉失敗，請稍後再試")
            else:
                reply_to_line(reply_token, "⚠️ 此指令僅限指定群組使用")

        if reply_message:
            reply_to_line(reply_token, reply_message)

    return "OK", 200

def reply_to_line(reply_token, message):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.getenv('LINE_CHANNEL_ACCESS_TOKEN')}"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{
            "type": "text",
            "text": message
        }]
    }
    try:
        resp = requests.post(url, headers=headers, json=payload)  # ✅ 把回應存進 resp
        print("[LINE] reply_to_line 回應：", resp.status_code, resp.text)
    except Exception as e:
        logger.warning(f"[LINE] 回覆失敗：{e}")

def send_to_line_group(message):
    LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    LINE_GROUP_ID = os.getenv("LINE_NOTIFY_GROUP_ID")

    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("[LINE] ❌ LINE_CHANNEL_ACCESS_TOKEN 未設定，無法推播")
        return
    if not LINE_GROUP_ID or not LINE_GROUP_ID.startswith("C"):
        logger.warning(f"[LINE] ❌ LINE_NOTIFY_GROUP_ID 格式錯誤或未設定：{LINE_GROUP_ID}")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_GROUP_ID,
        "messages": [{
            "type": "text",
            "text": message
        }]
    }

    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json=payload,
            timeout=10
        )
        if resp.status_code != 200:
            logger.warning(f"[LINE] ❌ 推播失敗：{resp.status_code} {resp.text} | Payload: {payload}")
        else:
            logger.info(f"[LINE] ✅ 推播成功：{resp.status_code} | Message: {message}")
    except Exception as e:
        logger.warning(f"[LINE] ❌ 推播發生例外：{e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    loop.create_task(self_ping_loop())
    app.run(host="0.0.0.0", port=port)