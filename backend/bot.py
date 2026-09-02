import os
import json
import logging
import asyncio
from typing import Optional
from sqlalchemy import select
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes, MessageHandler, filters
)

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models import User, PointsLedger, Submission, WantedEpisode, ShopItem
from backend.tmdb_client import tmdb_client
from backend.emby_client import emby_client
from backend.qb_client import qb_client

logger = logging.getLogger("lemon_2f.bot")

async def get_or_create_tg_user(tg_id: int, tg_username: Optional[str]) -> User:
    """根据 Telegram User ID 获取或自动建档用户"""
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.tg_user_id == tg_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            # 检查是否为配置的超级管理员
            is_admin = tg_id in settings.TG_ADMIN_IDS
            role = "owner" if is_admin else "user"
            uname = tg_username or f"tg_{tg_id}"
            
            user = User(
                username=uname,
                tg_user_id=tg_id,
                tg_username=tg_username,
                role=role,
                balance=settings.INITIAL_USER_COINS
            )
            session.add(user)
            await session.flush()

            ledger = PointsLedger(
                user_id=user.id,
                amount=settings.INITIAL_USER_COINS,
                balance_after=user.balance,
                event_type="init",
                description="Telegram 首次进入自动建档赠送二楼币"
            )
            session.add(ledger)
            await session.commit()
            await session.refresh(user)

        return user

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 指令"""
    tg_user = update.effective_user
    user = await get_or_create_tg_user(tg_user.id, tg_user.username)

    welcome_text = (
        f"✨ **欢迎来到【二楼有请】影视众包管理中心** ✨\n\n"
        f"👤 **用户身份**：`{user.username}` ({user.role.upper()})\n"
        f"🪙 **二楼币余额**：`{user.balance}` 币\n"
        f"🔥 **连签天数**：`{user.sign_in_streak}` 天\n\n"
        f"📌 **常用指令**：\n"
        f"• `/find <片名>` —— TMDB & Emby 穿透查重\n"
        f"• `/upload <磁力>` —— 提交下载与入库质检\n"
        f"• `/sign` —— 每日签到赚二楼币\n"
        f"• `/points` —— 查看二楼币明细\n"
        f"• `/shop` —— 兑换 Emby VIP / 专线特权\n"
        f"• `/tasks` —— 查看当前下载与质检流水线\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("🎁 每日签到", callback_data="btn_sign"),
            InlineKeyboardButton("🪙 我的二楼币", callback_data="btn_points")
        ],
        [
            InlineKeyboardButton("🛍️ 二楼商城", callback_data="btn_shop"),
            InlineKeyboardButton("📥 任务进度", callback_data="btn_tasks")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def cmd_sign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sign 签到指令"""
    tg_user = update.effective_user
    user = await get_or_create_tg_user(tg_user.id, tg_user.username)

    # 导入签到逻辑
    from datetime import datetime, timezone
    import random
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        if db_user.last_sign_in and db_user.last_sign_in.date() == now.date():
            await update.message.reply_text("⚠️ 爸爸/亲爱的，您今天已经签过到了，明天再来哦！")
            return

        if db_user.last_sign_in and (now.date() - db_user.last_sign_in.date()).days == 1:
            streak = db_user.sign_in_streak + 1
        else:
            streak = 1

        base = random.randint(settings.SIGN_IN_MIN_COINS, settings.SIGN_IN_MAX_COINS)
        bonus = min(streak * 2, 20)
        total = base + bonus

        db_user.balance += total
        db_user.sign_in_streak = streak
        db_user.last_sign_in = now

        ledger = PointsLedger(
            user_id=db_user.id,
            amount=total,
            balance_after=db_user.balance,
            event_type="sign_in",
            description=f"Telegram 签到奖励 (基础 {base} + 连签 {bonus})"
        )
        session.add(ledger)
        await session.commit()

        msg = (
            f"🎉 **签到成功！**\n\n"
            f"💰 获得奖励：`+{total}` 二楼币 (基础 {base} + 连签 {bonus})\n"
            f"🔥 连续签到：`{streak}` 天\n"
            f"🪙 最新资产：`{db_user.balance}` 二楼币"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find <片名> 穿透查重"""
    if not context.args:
        await update.message.reply_text("💡 请输入要搜索的片名，例如：`/find 庆余年`", parse_mode="Markdown")
        return

    query = " ".join(context.args)
    results = await tmdb_client.search_multi(query)
    if not results:
        await update.message.reply_text(f"🔍 未在 TMDB 找到与 `{query}` 匹配的影视条目", parse_mode="Markdown")
        return

    top = results[:3]
    msg = f"🔎 **【二楼有请】TMDB & Emby 查重检索结果**：\n\n"

    for idx, item in enumerate(top, 1):
        tmdb_id = item["tmdb_id"]
        m_type = item["media_type"]
        title = item["title"]
        year = item.get("year", "未知")

        # 查 Emby
        emby_item = await emby_client.find_by_tmdb_id(tmdb_id, m_type)
        if emby_item:
            status_text = "🟢 **Emby 库内已收录** (无需重复投稿)"
        else:
            status_text = f"🔴 **Emby 库内缺失** (可投稿赚取 `{settings.MOVIE_UPLOAD_REWARD if m_type == 'movie' else settings.EPISODE_UPLOAD_REWARD}` 二楼币)"

        msg += (
            f"**{idx}. {title}** ({year}) [{m_type.upper()}]\n"
            f"• TMDB ID: `{tmdb_id}`\n"
            f"• 状态: {status_text}\n\n"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/upload <磁力链接> 提交任务"""
    if not context.args:
        await update.message.reply_text("💡 请提供磁力链接，例如：`/upload magnet:?xt=urn:btih:...`", parse_mode="Markdown")
        return

    magnet = context.args[0].strip()
    t_hash = qb_client.extract_hash_from_magnet(magnet)
    if not t_hash:
        await update.message.reply_text("❌ 无效的磁力链接，未检测到有效 info_hash")
        return

    tg_user = update.effective_user
    user = await get_or_create_tg_user(tg_user.id, tg_user.username)

    async with AsyncSessionLocal() as session:
        # 查重
        stmt = select(Submission).where(
            Submission.torrent_hash == t_hash,
            Submission.status.in_(["pending", "downloading", "inspecting", "mounting", "completed"])
        )
        res = await session.execute(stmt)
        if res.scalar_one_or_none():
            await update.message.reply_text("⚠️ 该资源已有人提交或已完成入库，请勿重复提交！")
            return

        sub = Submission(
            user_id=user.id,
            tmdb_id=0, # 待后台解析或补充
            media_type="movie",
            title=f"TG提交_{t_hash[:8]}",
            magnet_uri=magnet,
            torrent_hash=t_hash,
            status="pending",
            reward_points=settings.MOVIE_UPLOAD_REWARD
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        await update.message.reply_text(
            f"✅ **投稿已受理并进入下载队列！**\n\n"
            f"🆔 任务编号：`#{sub.id}`\n"
            f"🔑 种子 Hash：`{t_hash}`\n"
            f"⚙️ 当前状态：`排队下载 (Pending)`\n"
            f"🎁 预计奖励：`{settings.MOVIE_UPLOAD_REWARD}` 二楼币\n\n"
            f"系统将在下载完成后自动执行 **FFprobe 质检**与 **规范化落盘入库**，入库成功后二楼币将秒级自动入账！",
            parse_mode="Markdown"
        )

def create_bot_app() -> Optional[Application]:
    """构建 Telegram Bot 应用实例"""
    token = settings.TG_BOT_TOKEN
    if not token or token.startswith("your_"):
        logger.info("Telegram Bot token not configured. Skipping bot service.")
        return None

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sign", cmd_sign))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("upload", cmd_upload))

    return app
