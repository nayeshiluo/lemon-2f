import os
import re
import json
import random
import secrets
import logging
import asyncio
from typing import Optional, Tuple, List
from datetime import datetime, timezone, timedelta, date
from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError
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
from backend.models.wanted import WantedTask, WantedBacker
from backend.models.watch import WatchRecord, DailyWatchReward
from backend.models.social import RedPacket, RedPacketClaim, LuckyWheelRecord
from backend.routes.v1.social import WHEEL_PRIZES, WHEEL_COST
from backend.models.shop import ShopItem
from backend.models.tg_bind import TG_BIND_CODE_TTL_MINUTES
from backend.clients.tmdb import tmdb_client
from backend.clients.emby import emby_client
from backend.qb_client import qb_client
from backend.services.points_service import PointsService
from backend.services.submission_service import SubmissionService
from backend.services.tg_bind_service import TgBindService
from backend.repositories.submission_repo import SubmissionRepository
from backend.repositories.wanted_repo import WantedRepository
from backend.repositories.watch_repo import WatchRepository
from backend.repositories.social_repo import SocialRepository

logger = logging.getLogger("lemon_2f.bot")

UNBOUND_HINT = (
    "🔗 **您还没有绑定二楼账号**\n\n"
    "本 Bot 是 Emby 账号的一个接入端，不再单独建号发币。\n"
    "请按以下两步完成绑定：\n\n"
    "1️⃣ 在此发送 `/link` 获取一次性绑定码\n"
    "2️⃣ 打开二楼 Web 端 → 用 **Emby 账号密码登录** → 个人中心提交绑定码\n\n"
    "绑定完成后即可在 Bot 中签到、投稿、求片、抢红包、查询软妹币。"
)


async def get_bound_user(tg_id: int) -> Optional[User]:
    """获取已绑定的账号；未绑定返回 None"""
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.tg_user_id == tg_id)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()


async def require_bound_user(update: Update) -> Optional[User]:
    """快捷守卫：未绑定用户阻断并提示绑定引导"""
    tg_user = update.effective_user
    if not tg_user:
        return None
    user = await get_bound_user(tg_user.id)
    if not user:
        if update.message:
            await update.message.reply_text(UNBOUND_HINT, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.answer("⚠️ 请先绑定二楼账号", show_alert=True)
            await update.callback_query.edit_message_text(UNBOUND_HINT, parse_mode="Markdown")
        return None
    return user


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/link 签发一次性绑定码"""
    tg_user = update.effective_user
    if not update.message or not tg_user:
        return

    async with AsyncSessionLocal() as session:
        bound = await session.execute(select(User).where(User.tg_user_id == tg_user.id))
        existing_user = bound.scalar_one_or_none()
        if existing_user:
            await update.message.reply_text(
                f"✅ **您已成功绑定二楼账号**\n\n"
                f"• Emby 用户名：`{existing_user.username}`\n"
                f"• 角色权限：`{existing_user.role.upper()}`\n"
                f"• 软妹币余额：`{existing_user.balance}` 🪙\n\n"
                f"如需解绑请登录 Web 端【个人中心】操作。",
                parse_mode="Markdown"
            )
            return

        service = TgBindService(session)
        code = await service.issue_code(
            tg_user_id=tg_user.id,
            tg_username=tg_user.username,
            tg_first_name=tg_user.first_name,
        )
        await session.commit()

    ttl = TG_BIND_CODE_TTL_MINUTES
    msg = (
        f"🔐 **您的二楼账号一次性绑定码**\n\n"
        f"```\n{code}\n```\n"
        f"（点击上方代码块可直接复制）\n\n"
        f"⏳ **有效期**：{ttl} 分钟（单次使用即毁）\n\n"
        f"📌 **使用方法**：\n"
        f"1. 打开二楼 Web 管理端\n"
        f"2. 用您的 **Emby 账号密码** 登录\n"
        f"3. 前往【个人中心】→ 粘贴该绑定码确认\n\n"
        f"⚠️ **安全提示**：请勿将此码转发给任何人！"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 指令"""
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
        f"• `/wanted` —— 查看众筹求片悬赏大厅\n"
        f"• `/watch` —— 查看今日观影打卡足迹\n"
        f"• `/wheel` —— 赛博幸运大轮盘抽奖\n"
        f"• `/redpacket <金额> <份数> [口令]` —— 塞红包到群里\n"
        f"• `/sign` —— 每日签到赚软妹币\n"
        f"• `/points` —— 查看软妹币明细\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("🎁 每日签到", callback_data="btn_sign"),
            InlineKeyboardButton("🪙 我的软妹币", callback_data="btn_points")
        ],
        [
            InlineKeyboardButton("🎯 求片大厅", callback_data="btn_wanted"),
            InlineKeyboardButton("📅 观影足迹", callback_data="btn_watch")
        ],
        [
            InlineKeyboardButton("🎡 幸运轮盘", callback_data="btn_wheel"),
            InlineKeyboardButton("🛍️ 二楼商城", callback_data="btn_shop")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")


async def cmd_sign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sign 签到指令"""
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

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
            await update.message.reply_text(f"⚠️ 您今天已经签过到了，明天再来哦！当前余额：`{db_user.balance}` 软妹币", parse_mode="Markdown")
            return

        points_service = PointsService(session)
        await points_service.add_points(
            user_id=db_user.id,
            amount=total,
            event_type="sign_in",
            idempotency_key=f"tg_sign_{db_user.id}_{today.isoformat()}",
            description=f"Telegram 签到基础奖励 {base} + 连签 {streak} 天奖励 {bonus}"
        )
        db_user.last_sign_in = datetime.now(timezone.utc)
        db_user.sign_in_streak = streak
        await session.commit()
        await session.refresh(db_user)

        res_text = (
            f"🎉 **签到成功！**\n\n"
            f"• 基础奖励：`+{base}` 软妹币\n"
            f"• 连签加成：`+{bonus}` 软妹币 (已连续签到 {streak} 天)\n"
            f"• 本次获得：`+{total}` 软妹币 🪙\n"
            f"• 最新余额：`{db_user.balance}` 软妹币"
        )
        await update.message.reply_text(res_text, parse_mode="Markdown")


async def cmd_wanted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wanted 查看众筹求片大厅最高悬赏条目"""
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

    async with AsyncSessionLocal() as session:
        repo = WantedRepository(session)
        items, total = await repo.list_open(limit=5, sort_by="bounty")

    if not items:
        await update.message.reply_text("🎯 当前求片大厅暂无开放中的悬赏，快去 Web 端发起全站首个求片吧！")
        return

    msg = f"🎯 **【二楼有请】求片众筹大厅 TOP 5** (共 {total} 部在求)：\n\n"
    for idx, item in enumerate(items, 1):
        ep_str = f" S{item.season:02d}E{item.episode:02d}" if item.episode is not None else (" (电影)" if item.media_type == "movie" else "")
        status_tag = "⏳ 认领中" if item.status == "claimed" else "🎯 悬赏中"
        msg += f"{idx}. **《{item.title}》**{ep_str}\n"
        msg += f"   • 赏金总池：`🪙 {item.bounty_points}` 软妹币\n"
        msg += f"   • 热度：`{item.backer_count} 人众筹支持` · 状态: `{status_tag}`\n\n"

    msg += "💡 前往二楼 Web 网页端可一键认领、加码众筹或交付入库！"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watch 查看今日观影打卡进度与月度统计"""
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    year_month = now.strftime("%Y-%m")

    async with AsyncSessionLocal() as session:
        repo = WatchRepository(session)
        today_sec = await repo.get_daily_seconds(user.id, today_str)
        has_reward = await repo.has_claimed_daily_reward(user.id, today_str)
        summary = await repo.get_monthly_summary(user.id, year_month)

    today_min = today_sec // 60
    pct = min(100, int((today_sec / 1800) * 100))
    progress_bar = "█" * (pct // 10) + "░" * (10 - (pct // 10))

    msg = (
        f"📅 **【二楼有请】我的 Emby 观影足迹看板**\n\n"
        f"⏱️ **今日看片打卡**：\n"
        f"• 进度：`[{progress_bar}] {pct}%` ({today_min}/30 分钟)\n"
        f"• 状态：{'✅ 已领今日打卡奖励 +5 🪙' if has_reward else '⏳ 满30分钟自动到账 5 🪙'}\n\n"
        f"📊 **本月累计 ({year_month})**：\n"
        f"• 观影总时长：`{summary['total_seconds'] // 3600}小时 {(summary['total_seconds'] % 3600) // 60}分`\n"
        f"• 剧集集数：`{summary['total_episodes']}` 集\n"
        f"• 电影部数：`{summary['total_movies']}` 部\n"
        f"• 活跃天数：`{summary['active_days_count']}` 天\n"
    )
    if summary["top_watched"]:
        msg += f"\n🏆 **本月常看 TOP 3**：\n"
        for i, it in enumerate(summary["top_watched"][:3], 1):
            msg += f" {i}. {it['title']} ({it['seconds'] // 60}m)\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_wheel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wheel 转动赛博幸运轮盘"""
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        if not db_user or db_user.balance < WHEEL_COST:
            await update.message.reply_text(f"⚠️ 软妹币余额不足！每次转动轮盘需要 `{WHEEL_COST}` 🪙，当前余额 `{db_user.balance if db_user else 0}` 🪙")
            return

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        points_service = PointsService(session)
        social_repo = SocialRepository(session)

        # 扣币
        await points_service.deduct_points(
            user_id=db_user.id,
            amount=WHEEL_COST,
            event_type="wheel_spin",
            idempotency_key=f"wheel_spin_{db_user.id}_{now_ms}",
            description=f"Telegram 幸运轮盘抽奖消耗 ({WHEEL_COST}🪙)",
            ref_type="lucky_wheel"
        )

        weights = [p["weight"] for p in WHEEL_PRIZES]
        chosen = random.choices(WHEEL_PRIZES, weights=weights, k=1)[0]

        prize_code = None
        if chosen["type"] == "code":
            prize_code = f"VIP-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
        elif chosen["type"] == "points":
            await points_service.add_points(
                user_id=db_user.id,
                amount=chosen["points"],
                event_type="wheel_win",
                idempotency_key=f"wheel_win_{db_user.id}_{now_ms}",
                description=f"幸运轮盘中奖: {chosen['name']}",
                ref_type="lucky_wheel"
            )

        rec = LuckyWheelRecord(
            user_id=db_user.id,
            cost_points=WHEEL_COST,
            prize_name=chosen["name"],
            prize_type=chosen["type"],
            prize_points=chosen["points"],
            prize_code=prize_code
        )
        await social_repo.create_wheel_record(rec)
        await session.commit()
        await session.refresh(db_user)

    res_msg = f"🎡 **轮盘飞速旋转中……**\n\n"
    if chosen["type"] != "none":
        res_msg += f"🎉 **恭喜抽中：【{chosen['name']}】！**\n"
        if prize_code:
            res_msg += f"\n🔑 **卡密直发**：`{prize_code}` (请长按复制)\n"
    else:
        res_msg += f"💨 差一点就中大奖了，再接再厉！\n"

    res_msg += f"\n🪙 最新余额：`{db_user.balance}` 软妹币"
    await update.message.reply_text(res_msg, parse_mode="Markdown")


async def cmd_redpacket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /redpacket <金额> <份数> [口令]
    直接在群聊或私聊中发红包
    """
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "💡 **发红包格式**：\n"
            "• 拼手气红包：`/redpacket <总金额> <总份数>`\n"
            "• 口令红包：`/redpacket <总金额> <总份数> <暗号口令>`\n\n"
            "例如：`/redpacket 50 5 二楼有请`",
            parse_mode="Markdown"
        )
        return

    try:
        points = int(args[0])
        count = int(args[1])
        passcode = args[2].strip() if len(args) >= 3 else None
    except ValueError:
        await update.message.reply_text("⚠️ 金额与份数必须为正整数")
        return

    if points < count:
        await update.message.reply_text(f"⚠️ 红包金额 ({points} 🪙) 不能少于份数 ({count} 份)")
        return

    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        if not db_user or db_user.balance < points:
            await update.message.reply_text(f"⚠️ 软妹币余额不足！当前余额 `{db_user.balance if db_user else 0}` 🪙")
            return

        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        points_service = PointsService(session)
        social_repo = SocialRepository(session)

        # 扣款
        p_type = "password" if passcode else "random"
        await points_service.deduct_points(
            user_id=db_user.id,
            amount=points,
            event_type="redpacket_send",
            idempotency_key=f"tg_rp_send_{db_user.id}_{now_ms}",
            description=f"Telegram 塞入红包 ({points}🪙/{count}份)",
            ref_type="red_packet"
        )

        packet = RedPacket(
            sender_id=db_user.id,
            packet_type=p_type,
            passcode=passcode,
            title=f"{db_user.username} 的二楼福利红包",
            total_points=points,
            remaining_points=points,
            total_count=count,
            remaining_count=count,
            status="active",
            expires_at=now + timedelta(hours=24)
        )
        packet = await social_repo.create_red_packet(packet)
        await session.commit()
        await session.refresh(packet)

    card_text = (
        f"🧧 **【二楼有请】福利红包来袭！**\n\n"
        f"👤 发起人：`{user.username}`\n"
        f"🪙 总金额：`{points}` 软妹币 · 共 `{count}` 份\n"
        f"🎲 类型：{'🔐 口令红包 (口令: ' + passcode + ')' if passcode else '🎲 拼手气随机红包'}\n\n"
        f"快点击下方按钮开抢！"
    )
    keyboard = [
        [InlineKeyboardButton("🧧 戳我拆红包！", callback_data=f"btn_claim_rp:{packet.id}")]
    ]
    await update.message.reply_text(card_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find 片名 检索 TMDB 与 Emby 查重"""
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("💡 请输入要搜索的片名，例如：`/find 庆余年`", parse_mode="Markdown")
        return

    await update.message.reply_text(f"🔍 正在穿透 TMDB 与二楼 Emby 库检索：`{query}`...", parse_mode="Markdown")

    tmdb_results = await tmdb_client.search_multi(query)
    if not tmdb_results:
        await update.message.reply_text(f"❌ 未在 TMDB 检索到相关作品：`{query}`")
        return

    for item in tmdb_results[:3]:
        tmdb_id = item["id"]
        title = item["title"]
        media_type = item["media_type"]
        year = item.get("year", "未知")
        overview = item.get("overview", "暂无简介")[:100] + "..."

        emby_item = await emby_client.find_by_tmdb_id(tmdb_id, media_type)
        if emby_item:
            status_text = "🟢 **Emby 已完整收录**"
        else:
            status_text = "🔴 **Emby 缺失 · 投稿有奖**"

        caption = (
            f"🎬 **{title}** ({year})\n"
            f"• TMDB ID: `{tmdb_id}` [{media_type.upper()}]\n"
            f"• 库内状态: {status_text}\n\n"
            f"📝 简介: {overview}"
        )
        keyboard = [
            [InlineKeyboardButton("📥 投稿此影视", callback_data=f"btn_quick_upload:{tmdb_id}:{media_type}")]
        ]
        await update.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/upload <TMDB_ID> [S01E07] <磁力链接>"""
    if not update.message:
        return
    user = await require_bound_user(update)
    if not user:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "💡 **使用格式**：\n"
            "• 电影/全季：`/upload <TMDB_ID> <磁力链接>`\n"
            "• 剧集单集：`/upload <TMDB_ID> S01E05 <磁力链接>`\n\n"
            "例如：`/upload 112191 S01E05 magnet:?xt=urn:btih:...`",
            parse_mode="Markdown"
        )
        return

    try:
        tmdb_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ TMDB ID 必须为数字")
        return

    season = None
    episode = None
    magnet_uri = None

    if len(args) == 2:
        magnet_uri = args[1]
    else:
        ep_match = re.match(r"^s(\d+)e(\d+)$", args[1].lower())
        if ep_match:
            season = int(ep_match.group(1))
            episode = int(ep_match.group(2))
            magnet_uri = args[2]
        else:
            magnet_uri = args[1]

    if not magnet_uri.startswith("magnet:?"):
        await update.message.reply_text("⚠️ 请提供有效的磁力链接 (以 `magnet:?` 开头)")
        return

    details = await tmdb_client.get_details(tmdb_id, "tv" if episode is not None else "movie")
    if not details:
        details = await tmdb_client.get_details(tmdb_id, "movie")
    title = details.get("title", f"TMDB-{tmdb_id}") if details else f"TMDB-{tmdb_id}"
    media_type = details.get("media_type", "tv" if episode is not None else "movie") if details else "tv"

    async with AsyncSessionLocal() as session:
        sub_service = SubmissionService(session)
        try:
            sub = await sub_service.create_submission(
                user_id=user.id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=title,
                magnet_uri=magnet_uri,
                year=details.get("year") if details else None,
                season=season,
                episode=episode
            )
            await session.commit()
            ep_info = f" S{season:02d}E{episode:02d}" if episode is not None else ""
            await update.message.reply_text(
                f"✅ **入库任务已开启！**\n\n"
                f"• 影视：`{title}`{ep_info}\n"
                f"• 任务 ID：`#{sub.id}`\n"
                f"• 当前状态：`排队离线下载中`\n\n"
                f"系统正通过 qBittorrent 拉取，质检合格后将自动发币并入库 Emby！",
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
        await query.edit_message_text(UNBOUND_HINT, parse_mode="Markdown")
        return

    data = query.data
    if data == "btn_sign":
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
                description=f"Telegram 签到基础奖励 {base} + 连签 {streak} 天奖励 {bonus}"
            )
            db_user.last_sign_in = datetime.now(timezone.utc)
            db_user.sign_in_streak = streak
            await session.commit()
            await session.refresh(db_user)

            await query.edit_message_text(
                f"🎉 **签到成功！**\n\n"
                f"• 基础奖励：`+{base}` 软妹币\n"
                f"• 连签加成：`+{bonus}` 软妹币 (已连签 {streak} 天)\n"
                f"• 最新余额：`{db_user.balance}` 软妹币 🪙",
                parse_mode="Markdown"
            )

    elif data == "btn_points":
        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, user.id)
            stmt = select(PointsLedger).where(PointsLedger.user_id == user.id).order_by(desc(PointsLedger.created_at)).limit(5)
            ledgers = (await session.execute(stmt)).scalars().all()

        text = f"🪙 **【二楼有请】软妹币资产卡**\n\n• 当前余额：`{db_user.balance if db_user else 0}` 软妹币\n\n📜 **最近 5 笔账单**：\n"
        for l in ledgers:
            sym = "+" if l.amount > 0 else ""
            text += f"• `{sym}{l.amount}` 币 —— {l.description}\n"
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "btn_wanted":
        async with AsyncSessionLocal() as session:
            repo = WantedRepository(session)
            items, total = await repo.list_open(limit=5, sort_by="bounty")

        msg = f"🎯 **【二楼有请】求片悬赏大厅** (共 {total} 部待补)：\n\n"
        for i, item in enumerate(items, 1):
            ep_str = f" S{item.season:02d}E{item.episode:02d}" if item.episode is not None else ""
            msg += f"{i}. **{item.title}**{ep_str} —— 悬赏 `🪙 {item.bounty_points}` ({item.backer_count}人支持)\n"
        msg += "\n💡 前往 Web 端可一键加码众筹或认领交付！"
        await query.edit_message_text(msg, parse_mode="Markdown")

    elif data == "btn_watch":
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as session:
            repo = WatchRepository(session)
            today_sec = await repo.get_daily_seconds(user.id, today_str)
            has_reward = await repo.has_claimed_daily_reward(user.id, today_str)

        today_min = today_sec // 60
        pct = min(100, int((today_sec / 1800) * 100))
        msg = (
            f"📅 **今日观影打卡进度**：\n\n"
            f"• 今日观看：`{today_min}` 分钟 / 满 30 分钟赠 5 🪙\n"
            f"• 完成度：`{pct}%`\n"
            f"• 状态：{'✅ 今日打卡奖励已到账' if has_reward else '⏳ 正在累计时长中'}\n\n"
            f"去 Emby 看部好片继续积累吧！"
        )
        await query.edit_message_text(msg, parse_mode="Markdown")

    elif data == "btn_wheel":
        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, user.id)
            if not db_user or db_user.balance < WHEEL_COST:
                await query.edit_message_text(f"⚠️ 软妹币余额不足！每次需要 {WHEEL_COST} 🪙，当前只有 {db_user.balance if db_user else 0} 🪙")
                return

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            points_service = PointsService(session)
            social_repo = SocialRepository(session)

            await points_service.deduct_points(
                user_id=db_user.id,
                amount=WHEEL_COST,
                event_type="wheel_spin",
                idempotency_key=f"wheel_spin_{db_user.id}_{now_ms}",
                description=f"Telegram 幸运轮盘抽奖 ({WHEEL_COST}🪙)",
                ref_type="lucky_wheel"
            )

            weights = [p["weight"] for p in WHEEL_PRIZES]
            chosen = random.choices(WHEEL_PRIZES, weights=weights, k=1)[0]

            prize_code = None
            if chosen["type"] == "code":
                prize_code = f"VIP-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
            elif chosen["type"] == "points":
                await points_service.add_points(
                    user_id=db_user.id,
                    amount=chosen["points"],
                    event_type="wheel_win",
                    idempotency_key=f"wheel_win_{db_user.id}_{now_ms}",
                    description=f"幸运轮盘中奖: {chosen['name']}",
                    ref_type="lucky_wheel"
                )

            rec = LuckyWheelRecord(
                user_id=db_user.id,
                cost_points=WHEEL_COST,
                prize_name=chosen["name"],
                prize_type=chosen["type"],
                prize_points=chosen["points"],
                prize_code=prize_code
            )
            await social_repo.create_wheel_record(rec)
            await session.commit()
            await session.refresh(db_user)

        res_text = f"🎡 **轮盘转动停在【{chosen['name']}】！**\n\n"
        if prize_code:
            res_text += f"🔑 **卡密**：`{prize_code}`\n"
        res_text += f"🪙 最新余额：`{db_user.balance}` 软妹币"
        await query.edit_message_text(res_text, parse_mode="Markdown")

    elif data == "btn_shop":
        async with AsyncSessionLocal() as session:
            stmt = select(ShopItem).where(ShopItem.is_active == True)
            items = (await session.execute(stmt)).scalars().all()

        text = "🛍️ **【二楼商城】可兑换特权列表**：\n\n"
        for it in items:
            text += f"• **{it.title}** —— 价格: `{it.cost_points}` 软妹币 (库存: `{it.stock}`)\n  _{it.description}_\n\n"
        text += "💡 请登录二楼 Web 网页端点击一键兑换！"
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data and data.startswith("btn_claim_rp:"):
        packet_id = int(data.split(":")[-1])
        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as session:
            social_repo = SocialRepository(session)
            points_service = PointsService(session)

            packet = await social_repo.get_packet_by_id(packet_id, for_update=True)
            if not packet:
                await query.answer("⚠️ 红包不存在", show_alert=True)
                return

            if packet.status == "empty" or packet.remaining_count <= 0 or packet.remaining_points <= 0:
                await query.answer("😭 手慢了，红包已被抢光！", show_alert=True)
                return

            if packet.expires_at and packet.expires_at < now:
                await query.answer("⚠️ 该红包已过期", show_alert=True)
                return

            if packet.packet_type == "password":
                await query.answer("🔐 此为口令红包，请在聊天框直接发送口令或前往 Web 端抢！", show_alert=True)
                return

            already_claimed = await social_repo.has_user_claimed(packet.id, user.id)
            if already_claimed:
                await query.answer("⚠️ 您已经领过这个红包啦！", show_alert=True)
                return

            if packet.remaining_count == 1:
                got_points = packet.remaining_points
            else:
                max_possible = packet.remaining_points - (packet.remaining_count - 1) * 1
                avg = packet.remaining_points // packet.remaining_count
                upper = min(max_possible, max(1, avg * 2))
                got_points = random.randint(1, upper)

            packet.remaining_points -= got_points
            packet.remaining_count -= 1
            if packet.remaining_count <= 0 or packet.remaining_points <= 0:
                packet.status = "empty"

            claim = RedPacketClaim(packet_id=packet.id, user_id=user.id, points=got_points)
            await social_repo.create_claim(claim)

            idempotency_key = f"tg_rp_claim_{packet.id}_{user.id}"
            await points_service.add_points(
                user_id=user.id,
                amount=got_points,
                event_type="redpacket_claim",
                idempotency_key=idempotency_key,
                description=f"Telegram 抢得红包: 《{packet.title}》+{got_points}🪙",
                ref_type="red_packet",
                ref_id=str(packet.id)
            )
            await session.commit()

        await query.answer(f"🎉 恭喜抢得 {got_points} 软妹币！", show_alert=True)


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
    app.add_handler(CommandHandler("wanted", cmd_wanted))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("wheel", cmd_wheel))
    app.add_handler(CommandHandler("spin", cmd_wheel))
    app.add_handler(CommandHandler("redpacket", cmd_redpacket))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    return app
