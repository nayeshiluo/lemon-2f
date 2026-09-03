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
from backend.models.tg_bind import TG_BIND_CODE_TTL_MINUTES
from backend.clients.tmdb import tmdb_client
from backend.clients.emby import emby_client
from backend.qb_client import qb_client
from backend.services.points_service import PointsService
from backend.services.submission_service import SubmissionService
from backend.services.tg_bind_service import TgBindService
from backend.repositories.submission_repo import SubmissionRepository

logger = logging.getLogger("lemon_2f.bot")

# 未绑定用户看到的统一引导文案。
# 关键安全设计：Bot 绝不再按 Telegram ID 自动建立独立经济账户。
# 历史实现会让同一个真人在 Web(Emby) 与 TG 各有一个账号、各领一份初始币，
# 且 TG 兑换 Emby VIP 时没有可靠的履约对象。现在 Emby 账号是唯一权威身份。
UNBOUND_HINT = (
    "🔗 **您还没有绑定二楼账号**\n\n"
    "本 Bot 是 Emby 账号的一个接入端，不再单独建号发币。\n"
    "请按以下两步完成绑定：\n\n"
    "1️⃣ 在此发送 `/link` 获取一次性绑定码\n"
    "2️⃣ 打开二楼 Web 端 → 用 **Emby 账号密码登录** → 个人中心提交绑定码\n\n"
    "绑定完成后即可在 Bot 中签到、投稿、查询软妹币。"
)


async def get_bound_user(tg_id: int) -> Optional[User]:
    """
    获取已绑定的账号；未绑定返回 None。

    与历史 get_or_create_tg_user() 的本质区别：绝不自动建号、绝不自动发币。
    """
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.tg_user_id == tg_id))
        return res.scalar_one_or_none()


async def require_bound_user(update: Update) -> Optional[User]:
    """
    绑定闸门：未绑定则回复引导文案并返回 None。
    所有涉及经济系统（签到/投稿/余额/商城）的指令都必须先过这道闸。
    """
    tg_user = update.effective_user
    if not tg_user:
        return None

    user = await get_bound_user(tg_user.id)
    if user:
        return user

    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_text(UNBOUND_HINT, parse_mode="Markdown")
    return None


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/link 生成一次性绑定码，供 Web 端 Emby 已登录会话兑换"""
    tg_user = update.effective_user
    if not update.message or not tg_user:
        return

    existing = await get_bound_user(tg_user.id)
    if existing:
        await update.message.reply_text(
            f"✅ 您已绑定二楼账号 `{existing.username}`\n"
            f"🪙 当前余额：`{existing.balance}` 软妹币\n\n"
            f"如需解绑请在 Web 端个人中心操作。",
            parse_mode="Markdown"
        )
        return

    async with AsyncSessionLocal() as session:
        service = TgBindService(session)
        try:
            code, expires_at = await service.issue_code(tg_user.id, tg_user.username)
        except ValueError as e:
            await update.message.reply_text(f"⚠️ {str(e)}")
            return

    await update.message.reply_text(
        f"🔑 **您的一次性绑定码**\n\n"
        f"`{code}`\n\n"
        f"⏱️ 有效期：**{TG_BIND_CODE_TTL_MINUTES} 分钟**（过期请重新发送 /link）\n\n"
        f"**接下来：**\n"
        f"1️⃣ 打开二楼 Web 端，用 **Emby 账号密码**登录\n"
        f"2️⃣ 进入个人中心 → Telegram 绑定 → 粘贴上方绑定码\n\n"
        f"⚠️ 请勿将此码转发给任何人 —— 持有该码者可将您的 Telegram 绑到自己账号上。",
        parse_mode="Markdown"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 指令（未绑定时只给绑定引导，不建号不发币）"""
    tg_user = update.effective_user
    if not update.message or not tg_user:
        return

    user = await get_bound_user(tg_user.id)
    if not user:
        await update.message.reply_text(
            "✨ **欢迎来到【二楼有请】影视众包管理中心** ✨\n\n" + UNBOUND_HINT,
            parse_mode="Markdown"
        )
        return

    welcome_text = (
        f"✨ **欢迎回来，【二楼有请】影视众包管理中心** ✨\n\n"
        f"👤 **用户身份**：`{user.username}` ({user.role.upper()})\n"
        f"🪙 **软妹币余额**：`{user.balance}` 币\n"
        f"🔥 **连签天数**：`{user.sign_in_streak}` 天\n\n"
        f"📌 **常用指令**：\n"
        f"• `/find <片名>` —— TMDB & Emby 穿透查重\n"
        f"• `/upload <TMDB_ID> [S01E07] <磁力>` —— 提交指定影视/单集入库\n"
        f"• `/sign` —— 每日签到赚软妹币\n"
        f"• `/points` —— 查看软妹币明细\n"
        f"• `/shop` —— 兑换 Emby VIP / 专线特权\n"
        f"• `/link` —— 查看账号绑定状态\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("🎁 每日签到", callback_data="btn_sign"),
            InlineKeyboardButton("🪙 我的软妹币", callback_data="btn_points")
        ],
        [
            InlineKeyboardButton("🛍️ 二楼商城", callback_data="btn_shop"),
            InlineKeyboardButton("📦 我的投稿", callback_data="btn_tasks")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def cmd_sign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sign 签到指令（必须已绑定 Emby 账号）"""
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

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
            f"💰 获得奖励：`+{total}` 软妹币 (基础 {base} + 连签 {bonus})\n"
            f"🔥 连续签到：`{streak}` 天\n"
            f"🪙 最新资产：`{db_user.balance}` 软妹币"
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
    user = await require_bound_user(update)
    if not user:
        return

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
                f"🎁 预计奖励：`{sub.reward_points}` 软妹币\n\n"
                f"系统将在下载完成后自动执行 **FFprobe 质检**与 **规范化落盘入库**，入库成功后软妹币将秒级自动入账！",
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
    user = await get_bound_user(tg_user.id)
    if not user:
        # 未绑定：所有按钮一律拒绝，不建号不发币
        await query.edit_message_text(UNBOUND_HINT, parse_mode="Markdown")
        return

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
                await query.edit_message_text(f"⚠️ 您今天已经签过到了，明天再来哦！当前余额：`{db_user.balance}` 软妹币", parse_mode="Markdown")
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
                f"💰 获得奖励：`+{total}` 软妹币 (基础 {base} + 连签 {bonus})\n"
                f"🔥 连续签到：`{streak}` 天\n"
                f"🪙 最新资产：`{db_user.balance}` 软妹币",
                parse_mode="Markdown"
            )

    elif data == "btn_points":
        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, user.id)
            balance = db_user.balance if db_user else 0
            await query.edit_message_text(
                f"🪙 **【二楼有请】我的软妹币资产**\n\n"
                f"👤 用户：`{user.username}`\n"
                f"💰 当前可用余额：`{balance}` 软妹币\n"
                f"🔥 连续签到：`{user.sign_in_streak}` 天\n\n"
                f"💡 每日签到、投稿补片均可赚取软妹币，可在商城兑换 Emby VIP 与高速通道！",
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
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("sign", cmd_sign))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    return app
