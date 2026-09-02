import os
import re
import json
import logging
import asyncio
from typing import Optional, Tuple
from sqlalchemy import select
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ContextTypes, MessageHandler, filters
)

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models.user import User
from backend.models.ledger import PointsLedger, SignInRecord
from backend.models.submission import Submission
from backend.models.wanted import WantedTask
from backend.models.shop import ShopItem
from backend.clients.tmdb import tmdb_client
from backend.clients.emby import emby_client
from backend.qb_client import qb_client
from backend.services.points_service import PointsService
from backend.services.submission_service import SubmissionService
from backend.repositories.submission_repo import SubmissionRepository

logger = logging.getLogger("lemon_2f.bot")

async def get_or_create_tg_user(tg_id: int, tg_username: Optional[str]) -> User:
    """根据 Telegram User ID 获取或自动建档用户 (初始余额 0 + 严格流水入账)"""
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.tg_user_id == tg_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            is_admin = tg_id in settings.TG_ADMIN_IDS
            role = "owner" if is_admin else "user"
            uname = tg_username or f"tg_{tg_id}"
            
            user = User(
                username=uname,
                tg_user_id=tg_id,
                tg_username=tg_username,
                role=role,
                balance=0
            )
            session.add(user)
            await session.flush()

            points_service = PointsService(session)
            await points_service.add_points(
                user_id=user.id,
                amount=settings.INITIAL_USER_COINS,
                event_type="init",
                idempotency_key=f"tg_init_{user.id}",
                description="Telegram 首次进入自动建档赠送二楼币"
            )
            await session.commit()
            await session.refresh(user)

        return user

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 指令"""
    tg_user = update.effective_user
    if not update.message or not tg_user:
        return
    user = await get_or_create_tg_user(tg_user.id, tg_user.username)

    welcome_text = (
        f"✨ **欢迎来到【二楼有请】影视众包管理中心** ✨\n\n"
        f"👤 **用户身份**：`{user.username}` ({user.role.upper()})\n"
        f"🪙 **二楼币余额**：`{user.balance}` 币\n"
        f"🔥 **连签天数**：`{user.sign_in_streak}` 天\n\n"
        f"📌 **常用指令**：\n"
        f"• `/find <片名>` —— TMDB & Emby 穿透查重\n"
        f"• `/upload <TMDB_ID> [S01E07] <磁力>` —— 提交指定影视/单集入库\n"
        f"• `/sign` —— 每日签到赚二楼币\n"
        f"• `/points` —— 查看二楼币明细\n"
        f"• `/shop` —— 兑换 Emby VIP / 专线特权\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("🎁 每日签到", callback_data="btn_sign"),
            InlineKeyboardButton("🪙 我的二楼币", callback_data="btn_points")
        ],
        [
            InlineKeyboardButton("🛍️ 二楼商城", callback_data="btn_shop"),
            InlineKeyboardButton("📦 我的投稿", callback_data="btn_tasks")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def cmd_sign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sign 签到指令"""
    tg_user = update.effective_user
    if not update.message or not tg_user:
        return
    user = await get_or_create_tg_user(tg_user.id, tg_user.username)

    from datetime import date, datetime, timezone
    import random
    from sqlalchemy.exc import IntegrityError

    today = date.today()

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        if not db_user:
            return

        streak = db_user.sign_in_streak + 1 if (db_user.last_sign_in and (today - db_user.last_sign_in.date()).days == 1) else 1
        base = random.randint(settings.SIGN_IN_MIN_COINS, settings.SIGN_IN_MAX_COINS)
        bonus = min(streak * 2, 20)
        total = base + bonus

        record = SignInRecord(user_id=db_user.id, sign_date=today, reward_coins=total, streak=streak)
        session.add(record)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            await update.message.reply_text("⚠️ 您今天已经签过到了，明天再来哦！")
            return

        points_service = PointsService(session)
        await points_service.add_points(
            user_id=db_user.id,
            amount=total,
            event_type="sign_in",
            idempotency_key=f"tg_sign_{db_user.id}_{today.isoformat()}",
            description=f"Telegram 签到奖励 (基础 {base} + 连签 {bonus})"
        )

        db_user.sign_in_streak = streak
        db_user.last_sign_in = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(db_user)

        msg = (
            f"🎉 **签到成功！**\n\n"
            f"💰 获得奖励：`+{total}` 二楼币 (基础 {base} + 连签 {bonus})\n"
            f"🔥 连续签到：`{streak}` 天\n"
            f"🪙 最新资产：`{db_user.balance}` 二楼币"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find <片名> 穿透查重"""
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("💡 请输入要搜索的片名，例如：`/find 庆余年`", parse_mode="Markdown")
        return

    query = " ".join(context.args)
    results = await tmdb_client.search_candidates(query)
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

        emby_item = await emby_client.find_by_tmdb_id(tmdb_id, m_type)
        if emby_item:
            status_text = "🟢 **Emby 库内已收录**"
        else:
            status_text = f"🔴 **Emby 缺失** (投稿: `/upload {tmdb_id} <磁力>` 或 `/upload {tmdb_id} S01E01 <磁力>`)"

        msg += (
            f"**{idx}. {title}** ({year}) [{m_type.upper()}]\n"
            f"• TMDB ID: `{tmdb_id}`\n"
            f"• 状态: {status_text}\n\n"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/upload <TMDB_ID> [S01E07] <磁力链接> 统一调用 SubmissionService (支持精准单集预占)"""
    if not update.message:
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "💡 投稿指令格式：\n"
            "• 电影/全季: `/upload <TMDB_ID> <magnet:...>`\n"
            "• 指定剧集: `/upload <TMDB_ID> S01E07 <magnet:...>`\n"
            "（可先使用 `/find 片名` 查询 TMDB ID）",
            parse_mode="Markdown"
        )
        return

    tmdb_str = context.args[0].strip()
    if not tmdb_str.isdigit():
        await update.message.reply_text("❌ 第一个参数必须为纯数字 TMDB ID，例如：`/upload 1363974 magnet:...`")
        return
    tmdb_id = int(tmdb_str)

    target_season: Optional[int] = None
    target_episode: Optional[int] = None
    magnet: str = ""

    if len(context.args) >= 3:
        # 形如 /upload 12345 S01E07 magnet:...
        se_arg = context.args[1].strip()
        se_match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", se_arg)
        if se_match:
            target_season = int(se_match.group(1))
            target_episode = int(se_match.group(2))
        magnet = context.args[2].strip()
    else:
        magnet = context.args[1].strip()

    tg_user = update.effective_user
    if not tg_user:
        return
    user = await get_or_create_tg_user(tg_user.id, tg_user.username)

    detail_movie = await tmdb_client.get_details(tmdb_id, "movie")
    media_type = "movie" if detail_movie else "tv"

    async with AsyncSessionLocal() as session:
        submission_service = SubmissionService(session)
        try:
            sub = await submission_service.create_submission(
                user_id=user.id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                magnet_uri=magnet,
                season=target_season,
                episode=target_episode
            )
            se_info = f" (目标: `S{target_season:02d}E{target_episode:02d}` 已预占锁定)" if (target_season and target_episode) else ""
            await update.message.reply_text(
                f"✅ **投稿已受理并进入下载队列！**{se_info}\n\n"
                f"🆔 任务编号：`#{sub.id}`\n"
                f"🎬 作品标题：`{sub.title}`\n"
                f"🔑 种子 Hash：`{sub.torrent_hash}`\n"
                f"⚙️ 当前状态：`排队下载 (Pending)`\n"
                f"🎁 预计奖励：`{sub.reward_points}` 二楼币\n\n"
                f"系统将在下载完成后自动执行 **FFprobe 质检**与 **规范化落盘入库**，入库成功后二楼币将秒级自动入账！",
                parse_mode="Markdown"
            )
        except ValueError as e:
            await update.message.reply_text(f"⚠️ 提交被拦截：{str(e)}")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 Inline Keyboard 按钮点击回调"""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    tg_user = query.from_user
    user = await get_or_create_tg_user(tg_user.id, tg_user.username)

    data = query.data
    if data == "btn_sign":
        from datetime import date, datetime, timezone
        import random
        from sqlalchemy.exc import IntegrityError
        today = date.today()

        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, user.id)
            if not db_user:
                return

            streak = db_user.sign_in_streak + 1 if (db_user.last_sign_in and (today - db_user.last_sign_in.date()).days == 1) else 1
            base = random.randint(settings.SIGN_IN_MIN_COINS, settings.SIGN_IN_MAX_COINS)
            bonus = min(streak * 2, 20)
            total = base + bonus

            record = SignInRecord(user_id=db_user.id, sign_date=today, reward_coins=total, streak=streak)
            session.add(record)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                await query.edit_message_text(f"⚠️ 您今天已经签过到了，明天再来哦！当前余额：`{db_user.balance}` 二楼币", parse_mode="Markdown")
                return

            points_service = PointsService(session)
            await points_service.add_points(
                user_id=db_user.id,
                amount=total,
                event_type="sign_in",
                idempotency_key=f"tg_sign_{db_user.id}_{today.isoformat()}",
                description=f"Telegram 签到奖励 (基础 {base} + 连签 {bonus})"
            )
            db_user.sign_in_streak = streak
            db_user.last_sign_in = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(db_user)

            await query.edit_message_text(
                f"🎉 **签到成功！**\n\n"
                f"💰 获得奖励：`+{total}` 二楼币 (基础 {base} + 连签 {bonus})\n"
                f"🔥 连续签到：`{streak}` 天\n"
                f"🪙 最新资产：`{db_user.balance}` 二楼币",
                parse_mode="Markdown"
            )

    elif data == "btn_points":
        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, user.id)
            balance = db_user.balance if db_user else 0
            await query.edit_message_text(
                f"🪙 **【二楼有请】我的二楼币资产**\n\n"
                f"👤 用户：`{user.username}`\n"
                f"💰 当前可用余额：`{balance}` 二楼币\n"
                f"🔥 连续签到：`{user.sign_in_streak}` 天\n\n"
                f"💡 每日签到、投稿补片均可赚取二楼币，可在商城兑换 Emby VIP 与高速通道！",
                parse_mode="Markdown"
            )

    elif data == "btn_shop":
        async with AsyncSessionLocal() as session:
            items_res = await session.execute(select(ShopItem).where(ShopItem.is_active == True))
            items = items_res.scalars().all()
            if not items:
                await query.edit_message_text("🛍️ 当前二楼商城暂无上架商品，请稍候再来！")
                return

            msg = "🛍️ **【二楼有请】权益商城商品列表**：\n\n"
            for it in items:
                msg += f"• **{it.title}** —— 价格: `{it.cost_points}` 🪙\n  _{it.description}_\n\n"
            msg += "💡 兑换请前往 Web 网页端控制台操作。"
            await query.edit_message_text(msg, parse_mode="Markdown")

    elif data == "btn_tasks":
        async with AsyncSessionLocal() as session:
            sub_repo = SubmissionRepository(session)
            subs, total = await sub_repo.list_user_submissions(user.id, offset=0, limit=5)
            if not subs:
                await query.edit_message_text("📦 您暂无任何影视投稿记录，快使用 `/upload` 投稿赚币吧！", parse_mode="Markdown")
                return

            msg = f"📦 **【二楼有请】我的最近 5 条投稿动态** (共 {total} 条)：\n\n"
            for s in subs:
                msg += f"• **{s.title}** [{s.media_type.upper()}] —— 状态: `{s.status}` (奖励: `+{s.reward_points}` 🪙)\n"
            await query.edit_message_text(msg, parse_mode="Markdown")

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
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    return app
