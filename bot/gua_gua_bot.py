# gua_gua_bot.py
import os
import re
import json
import pytz
#import deepl
import base64
import discord
import aiohttp
import requests
import asyncio
import firebase_admin
import logging
import sys
import functools

from wcwidth import wcswidth
from dotenv import load_dotenv
from discord import app_commands
from googletrans import Translator
from discord.ext import commands, tasks
from discord.ui import View, Button, Select, Modal, TextInput
from datetime import datetime, timedelta
from firebase_admin import credentials, firestore
from aiohttp import ClientError, ClientTimeout
# === Health Check HTTP Server for Cloud Run Ping ===
from flask import Flask, request
from threading import Thread

http_app = Flask("gua_gua_bot_health")

@http_app.route("/")
def health_check():
    return "✅ Bot is alive", 200

def run_http_server():
    import os
    port = int(os.environ.get("PORT", 8080))
    http_app.run(host="0.0.0.0", port=port)

# 啟動 ping 服務
Thread(target=run_http_server, daemon=True).start()

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # ✅ 全域設為 DEBUG
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.propagate = False

# === ENV ===
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
REDEEM_API_URL = os.getenv("REDEEM_API_URL")
redeem_submit_url = f"{REDEEM_API_URL}/redeem_submit"
retry_failed_url = f"{REDEEM_API_URL}/retry_failed"
tz = pytz.timezone("Asia/Taipei")
LANG_CHOICES = [
    app_commands.Choice(name="繁體中文", value="zh"),
    app_commands.Choice(name="English", value="en"),
]

# === Firebase Init ===
cred_env = os.getenv("FIREBASE_CREDENTIALS") or ""
cred_dict = json.loads(base64.b64decode(cred_env).decode("utf-8")) if not cred_env.startswith("{") else json.loads(cred_env)
cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()
#2025/07/01 解決Discord interaction過期問題
def interaction_guard(func):
    @functools.wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        try:
            if interaction.is_expired():
                logger.warning(f"[{func.__name__}] ⚠️ Interaction 已過期（is_expired），跳過")
                return
            return await func(interaction, *args, **kwargs)
        except discord.NotFound:
            logger.warning(f"[{func.__name__}] ⚠️ Interaction 已過期或無效（NotFound），跳過")
            return
        except Exception as e:
            logger.exception(f"[{func.__name__}] ❌ 發生例外錯誤：{e}")
            try:
                await safe_send(interaction, f"❌ 發生錯誤：{e}")
            except Exception:
                pass
    return wrapper

# === Firestore Async Wrapper ===
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

# === Discord Init ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# 放在 gua_gua_bot.py 裡或單獨開一個 aiohttp endpoint
@http_app.route("/internal_push_notify", methods=["POST"])
def http_push_notify():
    secret = request.json.get("secret")
    if secret != os.getenv("INTERNAL_SECRET"):
        return "Unauthorized", 403

    asyncio.run_coroutine_threadsafe(run_push_notify(), bot.loop)
    return "Triggered", 200

async def run_push_notify():
    now = datetime.now(tz).replace(second=0, microsecond=0)
    future = now + timedelta(seconds=30)
    docs = await firestore_stream(
        db.collection("notifications")
        .where("datetime", ">=", now)
        .where("datetime", "<", future)
        .order_by("datetime")
        .limit(10)
    )
    logger.info(f"[run_push_notify] 執行中，通知筆數：{len(docs)}")
    for doc in docs:
        data = doc.to_dict()
        logger.info(f"[run_push_notify] 準備推播通知：{data}")
        logger.info(f"[run_push_notify] 通知 guild_id：{data.get('guild_id')} channel_id：{data.get('channel_id')}")
        try:
            channel = bot.get_channel(int(data["channel_id"]))
            if not channel:
                continue
            msg = f'{data.get("mention", "")}\n⏰ **活動提醒 / Reminder** ⏰\n{data["message"]}'
            logger.info(f"[run_push_notify] 發送 Discord 頻道 ID：{data['channel_id']}")
            await channel.send(msg)

            # ✅ 新增：LINE 同步推播
            logger.info(f"[run_push_notify] 發送 LINE 群組內容：{data['message']}")
            line_msg = f"⏰ 活動提醒 / Reminder ⏰\n{data['message']}"
            await send_to_line_group(line_msg)
            logger.info(f"[run_push_notify] 刪除 Firestore 通知紀錄：{doc.id}")
            await firestore_delete(db.collection("notifications").document(doc.id))
        except Exception as e:
            logger.warning(f"[http_push_notify] 發送失敗：{e}")

# === ID 管理 ===
@tree.command(name="add_id", description="新增一個或多個玩家 ID / Add one or multiple player IDs")
@app_commands.describe(player_ids="可以用逗號(,)分隔的玩家 ID / Player IDs separated by comma(,)")
@interaction_guard
async def add_id(interaction: discord.Interaction, player_ids: str):
    try:
        error_ids = []  # 確保初始化，避免未定義
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild_id = str(interaction.guild_id)
        ids = [pid.strip() for pid in player_ids.split(",") if pid.strip()]

        # 驗證每個玩家 ID 是否為 9 位數字
        valid_ids = []
        invalid_ids = []
        for pid in ids:
            if re.match(r'^\d{9}$', pid):  # 檢查是否為 9 位數字
                valid_ids.append(pid)
            else:
                invalid_ids.append(pid)

        if invalid_ids:
            msg = f"⚠️ 無效 ID（非 9 位數字） / Invalid ID(s) (not 9 digits):`{', '.join(invalid_ids)}`"
            await safe_send(interaction, "\n".join(msg))
            return

        success = []
        exists = []
        for pid in valid_ids:
            ref = db.collection("ids").document(guild_id).collection("players").document(pid)
            if (await firestore_get(ref)).exists:
                exists.append(pid)
            else:
                # 這裡直接查 nickname 並儲存
                async with aiohttp.ClientSession() as session:
                    async with session.post(f"{REDEEM_API_URL}/add_id", json={
                        "guild_id": guild_id,
                        "player_id": pid
                    }) as resp:
                        if resp.status == 200:
                            success.append(pid)
                        elif resp.status == 409:
                            exists.append(pid)
                        else:
                            error_ids.append(pid)  # 可另設一類

        msg = []
        if success:
            msg.append(f"✅ 已新增 / Added：`{', '.join(success)}`")
        if exists:
            msg.append(f"⚠️ 已存在 / Already exists：`{', '.join(exists)}`")
        if not msg:
            msg = ["⚠️ 沒有有效的 ID 輸入 / No valid ID input"]
        
        await safe_send(interaction, "\n".join(msg))
    except Exception as e:
        await interaction.followup.send(f"❌ 錯誤：{e}", ephemeral=True)

@tree.command(name="remove_id", description="移除玩家ID / Remove a player ID")
@app_commands.describe(player_id="要移除的 ID / ID to remove")
@interaction_guard
async def remove_id(interaction: discord.Interaction, player_id: str):
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild_id = str(interaction.guild_id)
        ref = db.collection("ids").document(guild_id).collection("players").document(player_id)
        doc = await firestore_get(ref)

        if doc.exists:
            info = doc.to_dict()
            await firestore_delete(ref)
            msg = f"✅ 已移除 / Removed player_id `{player_id}`"
            await safe_send(interaction, msg)
            # === 傳送到監控頻道 ===
            log_channel = bot.get_channel(1356431597150408786)
            if log_channel:
                nickname = info.get("name", "")
                await log_channel.send(
                    f"🗑️ **ID 被移除**\n"
                    f"👤 操作者：{interaction.user} ({interaction.user.id})\n"
                    f"🌐 伺服器：{interaction.guild.name} ({interaction.guild.id})\n"
                    f"📌 移除 ID：{player_id} {f'({nickname})' if nickname else ''}"
                )
        else:
            await safe_send(interaction, f"❌ 找不到該 ID / ID not found `{player_id}`")
    except Exception as e:
        await safe_send(interaction, f"❌ 錯誤：{e}")

@tree.command(name="list_ids", description="列出所有玩家 ID / List all player IDs")
async def list_ids(interaction: discord.Interaction):
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild_id = str(interaction.guild_id)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{REDEEM_API_URL}/list_ids?guild_id={guild_id}") as resp:
                result = await resp.json()

        players = result.get("players", [])
        if not players:
            await interaction.response.send_message("📭 沒有任何 ID / No player ID found", ephemeral=True)
            return

        PAGE_SIZE = 20
        total_pages = (len(players) + PAGE_SIZE - 1) // PAGE_SIZE

        def format_page(page):
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_players = players[start:end]
            lines = [
                f"- `{p.get('id', '未知ID')}` ({p.get('name', '未知名稱')})（王國 {p.get('kingdom', '未知')}）"
                for p in page_players
            ]
            return f"📋 玩家清單（第 {page}/{total_pages} 頁） / Player List (Page {page}/{total_pages})\n" + "\n".join(lines)

        class PageView(View):
            def __init__(self, players):
                super().__init__(timeout=600)
                self.page = 1
                self.players = players
                self.PAGE_SIZE = 20
                self.total_pages = (len(players) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
                self.update_buttons()
                self.max_name_width = max(wcswidth(p.get("name", "未知名稱")) for p in players)

            def update_buttons(self):
                for item in self.children:
                    if isinstance(item, Button):
                        if item.label == "⬅️ 上一頁":
                            item.disabled = self.page == 1
                        elif item.label == "➡️ 下一頁":
                            item.disabled = self.page >= self.total_pages

            def format_page(self):
                start = (self.page - 1) * self.PAGE_SIZE
                end = start + self.PAGE_SIZE
                page_players = self.players[start:end]

                lines = [
                    f"{'ID':<10}  {'王國':<5}  名稱",
                    "-" * 30
                ]
                for p in page_players:
                    pid = p.get("id", "未知ID")
                    kingdom = str(p.get("kingdom", "未知"))
                    name = p.get("name", "未知名稱")

                    # 清理名稱（去除換行、空白符、特殊符號）
                    clean_name = re.sub(r"[^\S\r\n]+", " ", name).replace("\n", " ")

                    # 不截斷名稱，讓其自然延伸，但固定前面兩欄寬度
                    lines.append(f"{pid:<10}  {kingdom:<4}  {clean_name}")

                return (
                    f"📋 玩家清單（第 {self.page}/{self.total_pages} 頁） / Player List (Page {self.page}/{self.total_pages})\n"
                    + "```text\n" + "\n".join(lines) + "\n```"
                )

            async def update_message(self, interaction):
                self.update_buttons()
                await interaction.response.edit_message(content=self.format_page(), view=self)

            @discord.ui.button(label="⬅️ 上一頁", style=discord.ButtonStyle.gray)
            async def prev_button(self, interaction: discord.Interaction, button: Button):
                self.page -= 1
                await self.update_message(interaction)

            @discord.ui.button(label="➡️ 下一頁", style=discord.ButtonStyle.gray)
            async def next_button(self, interaction: discord.Interaction, button: Button):
                self.page += 1
                await self.update_message(interaction)

            @discord.ui.button(label="🔍 搜尋 / Search", style=discord.ButtonStyle.blurple)
            async def search_button(self, interaction: discord.Interaction, button: Button):
                await interaction.response.send_modal(SearchModal(self.players))


        class SearchModal(Modal, title="🔍 搜尋玩家 / Search Player"):
            keyword = TextInput(label="請輸入 ID 或名稱片段 / Enter part of ID or name", required=True)

            def __init__(self, players):
                super().__init__()
                self.players = players

            async def on_submit(self, interaction: discord.Interaction):
                keyword_lower = self.keyword.value.lower()
                matches = []
                for p in self.players:
                    pid = p.get("id", "")
                    name = p.get("name", "")
                    kingdom = str(p.get("kingdom", "未知"))
                    if keyword_lower in pid.lower() or keyword_lower in name.lower() or keyword_lower in kingdom.lower():
                        clean_name = re.sub(r"[^\S\r\n]+", " ", name).replace("\n", " ")
                        matches.append((pid, kingdom, clean_name))

                if not matches:
                    await interaction.response.send_message("📭 沒有找到符合條件的 ID / No matching IDs found", ephemeral=True)
                    return

                lines = [
                    f"{'ID':<10}  {'王國':<4}  名稱",
                    "-" * 26
                ]
                for pid, kingdom, name in matches[:20]:
                    lines.append(f"{pid:<10}  {kingdom:<4}  {name}")

                content = (
                    f"🔍 搜尋結果 / Search Results (最多顯示 20 筆)：\n"
                    + "```text\n" + "\n".join(lines) + "\n```"
                )
                await interaction.response.send_message(content, ephemeral=True)


        view = PageView(players)
        await interaction.followup.send(content=view.format_page(), view=view, ephemeral=True)

    except Exception as e:
        await safe_send(interaction, f"❌ 錯誤：{e}")

@tree.command(name="search_ids", description="搜尋玩家 ID 或名稱 / Search player ID or name")
@app_commands.describe(keyword="輸入玩家 ID 或名稱片段 / Enter part of ID or name")
@interaction_guard
async def search_ids(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    guild_id = str(interaction.guild_id)

    docs = await firestore_stream(db.collection("ids").document(guild_id).collection("players"))

    keyword_lower = keyword.lower()
    players = []
    for doc in docs:
        data = doc.to_dict()
        pid = doc.id
        name = data.get("name", "")
        kingdom = data.get("kingdom", "")
        if keyword_lower in pid.lower() or keyword_lower in name.lower() or keyword_lower in str(kingdom).lower():
            players.append({
                "id": pid,
                "name": name,
                "kingdom": kingdom
            })

    if not players:
        await interaction.followup.send("📭 找不到符合條件的 ID / No matching IDs found", ephemeral=True)
        return

    lines = [
        f"{'ID':<10}  {'王國':<5}  名稱",
        "-" * 30
    ]
    for p in players[:20]:
        pid = p.get("id", "未知ID")
        kingdom = str(p.get("kingdom", "未知"))
        name = p.get("name", "未知名稱")
        clean_name = re.sub(r"[^\S\r\n]+", " ", name).replace("\n", " ")
        lines.append(f"{pid:<10}  {kingdom:<4}  {clean_name}")

    content = (
        f"🔍 搜尋結果 / Search Results (最多顯示 20 筆)：\n"
        + "```" + "\n".join(lines) + "```"
    )
    await interaction.followup.send(content, ephemeral=True)

# === Redeem 兌換 ===
@tree.command(name="redeem_submit", description="提交兌換碼 / Submit redeem code")
@app_commands.describe(code="要兌換的禮包碼", player_id="選填：指定兌換的玩家 ID（單人兌換）")
@interaction_guard
async def redeem_submit(interaction: discord.Interaction, code: str, player_id: str = None):
    try:
        # 先檢查 interaction 是否已過期（建議二）
        if interaction.expires_at and datetime.now(tz) > interaction.expires_at:
            logger.warning("[redeem_submit] ⚠️ Interaction 已過期（expires_at），略過")
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
    except discord.NotFound:
        logger.warning("[redeem_submit] ⚠️ Interaction 已過期或無效（NotFound），略過 defer")
        return

    await safe_send(interaction, "🎁 兌換已開始處理 / Redemption started. 系統稍後會回報結果 / Result will be reported shortly.")
    if player_id:
        asyncio.create_task(trigger_backend_redeem(interaction, code, [player_id]))
    else:
        asyncio.create_task(trigger_backend_redeem(interaction, code))

async def get_player_ids(guild_id):
    docs = await firestore_stream(db.collection("ids").document(guild_id).collection("players"))
    return [doc.id for doc in docs]

async def trigger_backend_redeem(interaction: discord.Interaction, code: str, player_ids: list = None):
    guild_id = str(interaction.guild_id)

    if player_ids is None:
        player_ids = await get_player_ids(guild_id)

    if not player_ids:
        await interaction.followup.send("⚠️ 沒有找到任何玩家 ID / No player ID found", ephemeral=True)
        return

    try:
        payload = {
            "code": code,
            "player_ids": player_ids,
            "guild_id": str(interaction.guild_id),
            "debug": False
        }
        logger.info(f"[redeem_submit] 發送至 API：{REDEEM_API_URL} payload={payload}")
        logger.info(f"[trigger_backend_redeem] 來源頻道：{interaction.channel_id} 來源 guild：{interaction.guild_id}")
        async with aiohttp.ClientSession() as session:
            try:
                logger.info(f"[trigger_backend_redeem] 發送 Redeem 請求中，payload：{payload}")
                async with session.post(redeem_submit_url, json=payload, timeout=30) as resp:
                    logger.info(f"[trigger_backend_redeem] 後端回應狀態：{resp.status}")
                    if resp.status == 200:
                        logger.info(f"[{guild_id}] ✅ 成功觸發後端兌換流程（未等待完成）")
                    else:
                        logger.error(f"[{guild_id}] ❌ API 回傳錯誤狀態：{resp.status}")
            except (asyncio.TimeoutError, ClientError) as e:
                logger.warning(f"[{guild_id}] 發送請求超時 / Request timeout：{e}")
    except Exception as e:
        logger.exception(f"[Critical Error] trigger_backend_redeem 發生錯誤（guild_id: {guild_id}）")

@tree.command(name="retry_failed", description="重新兌換失敗的 ID / Retry failed ID")
@app_commands.describe(code="禮包碼 / Redeem code")
@interaction_guard
async def retry_failed(interaction: discord.Interaction, code: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await safe_send(interaction, "🎁 重新兌換開始 / Retrying redemption. 系統稍後會回報結果 / System will report back shortly.")
    
    guild_id = str(interaction.guild_id)

    # 從 Firestore 找到失敗的 ID
    failed_docs = await firestore_stream(
        db.collection("failed_redeems")
        .document(f"{guild_id}_{code}")
        .collection("players")
    )
    player_ids = [doc.id for doc in failed_docs]

    if not player_ids:
        await safe_send(interaction, "⚠️ 沒有找到失敗的 ID / No failed IDs found")
        return

    # 呼叫後端 API（這裡直接進行兌換）
    try:
        payload = {
            "code": code,
            "player_ids": player_ids,
            "guild_id": guild_id,
            "debug": False
        }
        await safe_send(interaction, f"🎁 重新兌換 {len(player_ids)} 個失敗的 ID 已發送到後端進行處理")
        async with aiohttp.ClientSession() as session:
            async def fire_and_forget_retry(payload):
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.post(retry_failed_url, json=payload):
                            pass
                    except Exception as e:
                        logger.warning(f"[fire_and_forget_retry] 發送失敗：{e}")

    except Exception as e:
        logger.exception(f"[retry_failed] 發送 API 時出錯")
        await safe_send(interaction, f"❌ 發生錯誤 / Error: {e}")

# === 活動提醒 ===
@tree.command(name="add_notify", description="新增提醒 / Add reminder")
@app_commands.describe(
    date="提醒日期（YYYY-MM-DD，可多個用 , 分隔）",
    time="提醒時間（HH:MM，可多個用 , 分隔）",
    message="提醒內容（使用 \\n 換行）",
    target_channel="提醒要送出的頻道",
    mention="要標記的對象（可空）"
)
@interaction_guard
async def add_notify(
    interaction: discord.Interaction,
    date: str,
    time: str,
    message: str,
    target_channel: discord.TextChannel,
    mention: str = ""
):
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild_id = str(interaction.guild_id)
        logger.info(f"[add_notify] guild_id={guild_id} channel={target_channel.id} mention={mention} date={date} time={time}")
        dates = [d.strip() for d in date.split(",")]
        times = [t.strip() for t in time.split(",")]
        message = message.replace("\\n", "\n")  # ✅ 支援換行
        count = 0

        for d in dates:
            for t in times:
                try:
                    dt = tz.localize(datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M"))
                except Exception as dt_err:
                    await safe_send(interaction, f"❌ 日期或時間格式錯誤 / Invalid date or time: {d} {t}\n{dt_err}")
                    return

                try:
                    logger.info(f"[add_notify] 準備新增提醒：{d} {t} 至頻道 {target_channel.id} mention={mention}")
                    await run_in_executor(db.collection("notifications").add, {
                        "channel_id": str(target_channel.id),
                        "guild_id": guild_id,
                        "datetime": dt,
                        "message": message,
                        "mention": mention
                    })
                    logger.info(f"[add_notify] 成功新增提醒：{dt} 至 {target_channel.id}")
                    count += 1
                except Exception as db_err:
                    logger.error(f"❌ Firestore 寫入失敗：{db_err}")

        await interaction.followup.send(
            f"✅ 已新增 {count} 筆提醒至 {target_channel.mention} / Added {count} reminders to {target_channel.mention}",
            ephemeral=True
        )

    except Exception as e:
        await safe_send(interaction, f"❌ 發生錯誤 / Error: {e}")

@tree.command(name="list_notify", description="查看提醒列表 / View reminder list")
async def list_notify(interaction: discord.Interaction):
    try:
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except discord.errors.NotFound:
            await safe_send(interaction, "⚠️ 互動已過期 / Interaction expired. 請重新嘗試。")
            return
        docs = await firestore_stream(
            db.collection("notifications")
            .where("guild_id", "==", str(interaction.guild_id))
            .order_by("datetime")
        )
        rows = []
        for i, doc in enumerate(docs):
            data = doc.to_dict()
            try:
                fire_dt = data["datetime"]
                dt = fire_dt.astimezone(tz) if hasattr(fire_dt, 'astimezone') else datetime.fromtimestamp(fire_dt.timestamp(), tz)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                time_str = "❓ 時間解析錯誤 / Time error"

            mention = data.get("mention", "")
            channel_id = data.get("channel_id", "")
            try:
                channel = bot.get_channel(int(channel_id))
                channel_name = f"<#{channel_id}>" if channel else f"未知頻道 / Unknown channel ({channel_id})"
            except Exception:
                channel_name = f"未知頻道 / Unknown channel ({channel_id})"

            rows.append(f"{i+1}. {time_str} - {data.get('message')} {mention} → {channel_name}")

        await safe_send(interaction, "\n".join(rows) if rows else "📭 沒有提醒資料 / No reminders found")

    except Exception as e:
        await safe_send(interaction, f"❌ 錯誤：{e}")

@tree.command(name="remove_notify", description="移除提醒 / Remove reminder")
@app_commands.describe(index="提醒編號 / Reminder index")
@interaction_guard
async def remove_notify(interaction: discord.Interaction, index: int):
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        docs = await firestore_stream(
            db.collection("notifications")
            .where("guild_id", "==", str(interaction.guild_id))
            .order_by("datetime")
        )
        real_index = index - 1
        if real_index < 0 or real_index >= len(docs):
            await safe_send(interaction, "❌ index 無效 / Invalid index")
            return
        doc = docs[real_index]
        data = doc.to_dict()
        await firestore_delete(db.collection("notifications").document(doc.id))
        await interaction.followup.send(f"🗑️ 已刪除 / Removed reminder #{index}: {data['message']}", ephemeral=True)

        # 推送到監控頻道
        log_channel = bot.get_channel(1356431597150408786)
        if log_channel:
            await log_channel.send(
                f"🗑️ **提醒被刪除**\n"
                f"👤 操作者：{interaction.user} ({interaction.user.id})\n"
                f"🌐 伺服器：{interaction.guild.name} ({interaction.guild.id})\n"
                f"📌 原提醒：{data['datetime']} - {data['message']}"
            )

    except Exception as e:
        await safe_send(interaction, "❌ index 無效 / Invalid index")

@tree.command(name="edit_notify", description="編輯提醒 / Edit reminder")
@app_commands.describe(
    index="提醒編號 / Reminder index",
    date="新日期 YYYY-MM-DD / New date",
    time="新時間 HH:MM / New time",
    message="新訊息（使用 \\n 換行）/ New message",
    mention="新標記 / New mention",
    target_channel="提醒要送出的頻道 / Target channel to send the reminder"
)
@interaction_guard
async def edit_notify(
    interaction: discord.Interaction,
    index: int,
    date: str = None,
    time: str = None,
    message: str = None,
    mention: str = None,
    target_channel: discord.TextChannel = None
):
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        docs = await firestore_stream(
            db.collection("notifications")
            .where("guild_id", "==", str(interaction.guild_id))
            .order_by("datetime")
        )

        real_index = index - 1
        if real_index < 0 or real_index >= len(docs):
            await safe_send(interaction, "❌ index 無效 / Invalid index")
            return

        doc = docs[real_index]
        old_data = doc.to_dict()

        try:
            firestore_dt = old_data["datetime"]
            orig = datetime.fromtimestamp(firestore_dt.timestamp(), tz)
        except Exception:
            await interaction.followup.send("❌ 時間格式錯誤，無法修改 / Invalid original time format, cannot edit.", ephemeral=True)
            return

        if date:
            try:
                y, mo, d = map(int, date.split("-"))
                orig = orig.replace(year=y, month=mo, day=d)
            except ValueError as ve:
                await interaction.followup.send(f"❌ 日期錯誤：{ve}", ephemeral=True)
                return
        if time:
            try:
                h, m = map(int, time.split(":"))
                orig = orig.replace(hour=h, minute=m)
            except ValueError as ve:
                await interaction.followup.send(f"❌ 時間錯誤：{ve}", ephemeral=True)
                return

        if orig.tzinfo is None:
            orig = tz.localize(orig)
        else:
            orig = orig.astimezone(tz)

        if message:
            message = message.replace("\\n", "\n")  # ✅ 支援換行

        channel_id_value = old_data.get("channel_id")
        new_data = {
            "channel_id": str(target_channel.id) if target_channel else channel_id_value,
            "guild_id": str(interaction.guild_id),
            "datetime": orig,
            "message": message if message is not None else old_data.get("message"),
            "mention": mention if mention is not None else old_data.get("mention", "")
        }

        await run_in_executor(db.collection("notifications").add, new_data)
        await firestore_delete(db.collection("notifications").document(doc.id))

        await interaction.followup.send(f"✏️ 已更新提醒 / Updated reminder #{index}", ephemeral=True)

        log_channel = bot.get_channel(1356431597150408786)
        if log_channel:
            await log_channel.send(
                f"📝 **提醒被編輯**\n"
                f"👤 操作者：{interaction.user} ({interaction.user.id})\n"
                f"🌐 伺服器：{interaction.guild.name} ({interaction.guild.id})\n"
                f"📌 原提醒：{old_data['datetime'].astimezone(tz).strftime('%Y-%m-%d %H:%M')} - {old_data['message']}\n"
                f"🆕 新提醒：{new_data['datetime'].astimezone(tz).strftime('%Y-%m-%d %H:%M')} - {new_data['message']}"
            )

    except Exception as e:
        await safe_send(interaction, f"❌ 錯誤：{e}")

# === Help 指令 ===
@tree.command(name="help", description="查看機器人指令說明 / View command help")
@app_commands.describe(lang="選擇語言 / Please choose a language")
@app_commands.choices(lang=LANG_CHOICES)
@interaction_guard
async def help_command(interaction: discord.Interaction, lang: app_commands.Choice[str]):
    try:
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except discord.errors.InteractionResponded:
            pass  # 已回應的互動略過 defer，不報錯

        if lang.value == "en":
            content = (
                "**GuaGuaBOT Help (English)**\n"
                "`/add_id` - Add one or more player IDs (comma-separated)\n"
                "`/remove_id` - Remove a player ID\n"
                "`/list_ids` - List all saved player IDs\n"
                "`/redeem_submit` - Submit a redeem code\n"
                "`/retry_failed` - Retry failed ID redemptions\n"
                "`/update_names` - Refresh and update all player ID names\n"
                "`/add_notify` - Add reminders (supports multiple dates and times)\n"
                "`/list_notify` - View reminder list\n"
                "`/remove_notify` - Remove a reminder\n"
                "`/edit_notify` - Edit a reminder\n"
                "`/help` - View the list of available commands\n"
                "`Translation` - Mention the bot and reply to a message to auto-translate, or use the right-click menu 'Translate Message'"
            )
        else:
            content = (
                "**呱呱BOT 指令說明（繁體中文）**\n"
                "`/add_id` - 新增一個或多個玩家 ID（用逗號分隔）\n"
                "`/remove_id` - 移除玩家 ID\n"
                "`/list_ids` - 顯示所有已儲存的 ID\n"
                "`/redeem_submit` - 提交兌換碼\n"
                "`/retry_failed` - 重新兌換失敗的 ID\n"
                "`/update_names` - 重新查詢並更新所有 ID 的角色名稱\n"
                "`/add_notify` - 新增提醒（支援多個日期與時間）\n"
                "`/list_notify` - 查看提醒列表\n"
                "`/remove_notify` - 移除提醒\n"
                "`/edit_notify` - 編輯提醒\n"
                "`/help` - 查看指令列表\n"
                "`翻譯功能` - 標記機器人並回覆訊息即可自動翻譯中英文，或使用右鍵選單「翻譯此訊息」"
            )

        await safe_send(interaction, content)

    except Exception as e:
        await interaction.followup.send(
            f"❌ 錯誤：{e}\n⚠️ 發送說明時發生錯誤 / Help command failed.", ephemeral=True)

@tree.command(name="line_quota", description="查看本月 LINE 推播用量 / Check LINE push message quota")
@interaction_guard
async def line_quota(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{REDEEM_API_URL}/line_quota") as resp:
                if resp.status != 200:
                    text = await resp.text()
                    await interaction.followup.send(f"❌ API 錯誤：{resp.status}\n{text}", ephemeral=True)
                    return
                result = await resp.json()

        if result.get("success"):
            count = result.get("quota", 0)
            await interaction.followup.send(f"📊 當月 LINE 推播用量：{count} 則（免費額度 200 則）", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ 查詢失敗：{result.get('reason')}", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ 發生錯誤：{e}", ephemeral=True)

async def send_to_line_group(message: str):
    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    group_id = os.getenv("LINE_NOTIFY_GROUP_ID")

    if not line_token or not group_id:
        logger.warning("[send_to_line_group] ⚠️ LINE Token 或 Group ID 未設定")
        return

    headers = {
        "Authorization": f"Bearer {line_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "to": group_id,
        "messages": [{
            "type": "text",
            "text": message
        }]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.line.me/v2/bot/message/push",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status == 200:
                    logger.info("[send_to_line_group] ✅ LINE Message API 發送成功")
                else:
                    error = await resp.text()
                    logger.warning(f"[send_to_line_group] ❌ 發送失敗：{resp.status} {error}")
    except Exception as e:
        logger.warning(f"[send_to_line_group] ❌ 發送失敗：{e}")

# === 提醒失敗時通報 webhook（選用） ===
async def report_notify_failure(data, error_detail: str):
    webhook_url = os.getenv("ADD_ID_WEBHOOK_URL")
    if not webhook_url:
        return

    content = (
        f"⚠️ 發送提醒失敗 / Reminder send failed\n"
        f"📛 Channel ID: `{data.get('channel_id')}`\n"
        f"📅 時間: {data.get('datetime')}\n"
        f"💬 訊息: {data.get('message')}\n"
        f"🔗 Mention: {data.get('mention')}\n"
        f"❗ 錯誤：{error_detail}"
    )
    try:
        logger.warning(f"[report_notify_failure] 準備傳送錯誤通報，內容：{content}")
        async with aiohttp.ClientSession() as session:
            await session.post(webhook_url, json={"content": content})
    except Exception as e:
        logger.warning(f"[Webhook] 發送錯誤通報失敗：{e}")

# === 上線後同步 ===
@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # === 發送 webhook 啟動通知（僅一次）===
    logger.info("[on_ready] Bot 已啟動，準備完成。")
    logger.info(f"[on_ready] Guild IDs：{[g.id for g in bot.guilds]}")
    logger.info(f"[on_ready] TOKEN 前五碼：{TOKEN[:5]}")
    logger.info(f"[on_ready] ADD_ID_WEBHOOK_URL 存在：{bool(os.getenv('ADD_ID_WEBHOOK_URL'))}")
    logger.info(f"[on_ready] LINE_NOTIFY_GROUP_ID：{os.getenv('LINE_NOTIFY_GROUP_ID')}")
    await send_webhook_message(
        "📡 GuaGuaBOT 已成功啟動！\n✅ 雙語指令模式已啟用，等待使用者互動中。\n🔄 機器人狀態穩定運作中。\n\n"
        "📡 GuaGuaBOT has started successfully!\n✅ Bilingual command mode enabled, standing by.\n🔄 Bot status: stable and ready."
    )

    try:
        synced = await tree.sync()
        logger.info(f"✅ Synced {len(synced)} global commands: {[c.name for c in synced]}")
    except Exception as e:
        logger.info(f"❌ Failed to sync commands: {e}")

# === Webhook 發送函式（啟動通知） ===
async def send_webhook_message(content: str):
    url = os.getenv("ADD_ID_WEBHOOK_URL")
    if not url:
        logger.warning("⚠️ ADD_ID_WEBHOOK_URL 未設定，發送已略過")
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"content": content})
            logger.info("✅ Webhook 發送成功 / Webhook sent successfully")
    except Exception as e:
        logger.warning(f"❌ Webhook 發送失敗 / Failed to send webhook: {e}")

# ✅ 更新過的 safe_send，不再使用 extras，避免 40060 重複回應錯誤
async def safe_send(interaction: discord.Interaction, content: str):
    try:
        if interaction.is_expired():
            logger.warning("[safe_send] ⚠️ Interaction 已過期（is_expired），無法發送")
            return
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.NotFound:
        logger.warning("[safe_send] ⚠️ Interaction 已過期（NotFound），無法發送")
    except discord.errors.InteractionResponded:
        logger.warning("[safe_send] ⚠️ 嘗試發送已回應的互動訊息，略過")
    except Exception as e:
        logger.warning(f"[safe_send] ❌ 傳送訊息失敗：{e}")

translator = Translator()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if bot.user in message.mentions and message.reference:
        try:
            original_msg = await message.channel.fetch_message(message.reference.message_id)
            text = original_msg.content.strip()

            detected = translator.detect(text).lang.lower()

            if detected == "th":
                target_langs = [("en", "English"), ("zh-tw", "繁體中文")]
            elif detected in ["zh-cn", "zh-tw", "zh"]:
                target_langs = [("en", "English")]
            else:
                target_langs = [("zh-tw", "繁體中文")]

            embeds = []
            for lang_code, lang_label in target_langs:
                result = translator.translate(text, dest=lang_code)
                embed = discord.Embed(
                    title=f"🌐 翻譯完成 / Translation Result ({lang_label})",
                    color=discord.Color.blue()
                )
                embed.add_field(name="📤 原文 / Original", value=text[:1024], inline=False)
                embed.add_field(name="📥 翻譯 / Translated", value=result.text[:1024], inline=False)
                embed.set_footer(text=f"語言偵測 / Detected: {detected} → {lang_label}")
                embeds.append(embed)

            for embed in embeds:
                await message.reply(embed=embed)
            return
        except Exception as e:
            await message.reply(f"⚠️ 翻譯失敗：{e}")
            return

    await bot.process_commands(message)

@tree.context_menu(name="翻譯此訊息 / Translate Message")
async def context_translate(interaction: discord.Interaction, message: discord.Message):
    try:
        await interaction.response.defer(ephemeral=True)

        text = message.content.strip()
        if not text:
            await safe_send(interaction, "⚠️ 原文為空 / The original message is empty.")
            return

        target_lang = "en" if any(u'\u4e00' <= ch <= u'\u9fff' for ch in text) else "zh-tw"
        result = translator.translate(text, dest=target_lang)

        embed = discord.Embed(
            title="🌐 翻譯完成 / Translation Result",
            color=discord.Color.green()
        )
        embed.add_field(name="📤 原文 / Original", value=text[:1024], inline=False)
        embed.add_field(name="📥 翻譯 / Translated", value=result.text[:1024], inline=False)
        embed.set_footer(text=f"目標語言 / Target: {target_lang}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await safe_send(interaction, f"⚠️ 翻譯失敗：{e}")

@tree.command(name="update_names", description="重新查詢所有 ID 並更新名稱 / Refresh all player names")
async def update_names(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    guild_id = str(interaction.guild_id)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{REDEEM_API_URL}/update_names_api", json={
                "guild_id": guild_id
            }) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    await interaction.followup.send(f"❌ API 回傳錯誤 / API error:{resp.status}\n{text}", ephemeral=True)
                    return

                result = await resp.json()
                updated = result.get("updated", [])

                if updated:
                    lines = [f"{u['id']}（王國 {u['new_kingdom']}）\n{u['old_name']}（{u['old_kingdom']}） ➜ {u['new_name']}（{u['new_kingdom']}）" for u in updated]
                    summary = "\n".join(lines)
                    logger.info(f"[update_names] 共更新 {len(updated)} 筆名稱：\n{summary}")
                    await interaction.followup.send(
                        f"✨ 共更新 {len(updated)} 筆名稱 / Updated {len(updated)} names：\n\n{summary}", ephemeral=True
                    )

                else:
                    logger.info(f"[update_names] 無任何名稱需要更新 / No names to update")
                    await interaction.followup.send("✅ 沒有任何名稱需要更新 / No name updates required.", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ 發生錯誤：{e}", ephemeral=True)

bot.run(TOKEN)