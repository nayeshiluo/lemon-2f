from fastapi import APIRouter, Depends, HTTPException, Request, status
import logging
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.repositories.user_repo import UserRepository
from backend.services.points_service import PointsService
from backend.services.tg_bind_service import TgBindService
from backend.schemas import (
    EmbyLoginRequest, Token, UserProfile, ApiResponse,
    TgBindRedeemRequest, TgBindStatusResponse,
    TokenLoginRequest, TgLoginRequest,
)
from backend.security import create_access_token, get_password_hash, verify_password
from backend.clients.emby import EmbyClient, emby_client
from backend.auth import get_current_user
from backend.services.tg_auth_service import TgAuthService
from backend.rate_limit import login_guard, LoginBlocked
from backend.config import settings

class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=6, max_length=64)

router = APIRouter(prefix="/auth", tags=["Auth"])

@router.post("/login", response_model=Token)
async def login(req: EmbyLoginRequest, db: AsyncSession = Depends(get_db), request: Request = None):
    """支持 Emby 原生账号密码穿透登录，或本地用户名密码登录（限速防暴破）"""
    username = req.username.strip()
    password = req.password

    # 登录限速：以 用户名|客户端IP 为维度，失败累计超阈值锁定
    client_ip = request.client.host if request and request.client else "unknown"
    guard_key = f"{username}|{client_ip}"
    try:
        login_guard.check(guard_key)
    except LoginBlocked as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after_seconds)},
        )

    user_repo = UserRepository(db)
    points_service = PointsService(db)

    # 1. Emby 服务器穿透鉴权
    emby_auth = await emby_client.authenticate_user(username, password)
    user = await user_repo.get_by_username(username)

    if emby_auth:
        login_guard.on_success(guard_key)
        emby_id = emby_auth.get("emby_user_id")
        is_admin = emby_auth.get("is_administrator", False)
        target_role = "admin" if is_admin else "user"

        if not user:
            # 关键修复：初始 balance=0，必须严格由 PointsService 入账
            user = User(
                username=username,
                emby_user_id=emby_id,
                emby_username=username,
                role=target_role,
                balance=0
            )
            await user_repo.create(user)
            await points_service.add_points(
                user_id=user.id,
                amount=settings.INITIAL_USER_COINS,
                event_type="init",
                idempotency_key=f"init_user_{user.id}",
                description="新用户首次登录赠送软妹币"
            )
        else:
            user.emby_user_id = emby_id
            user.emby_username = username
            if is_admin and user.role == "user":
                user.role = "admin"
        
        await db.commit()
        await db.refresh(user)

        token = create_access_token(subject=user.id, role=user.role)
        return Token(
            access_token=token,
            role=user.role,
            username=user.username,
            balance=user.balance,
            is_whitelisted=user.is_whitelisted
        )

    # 2. 本地密码校验 (开发/管理员)
    if user and user.password_hash and verify_password(password, user.password_hash):
        login_guard.on_success(guard_key)
        token = create_access_token(subject=user.id, role=user.role)
        return Token(
            access_token=token,
            role=user.role,
            username=user.username,
            balance=user.balance,
            is_whitelisted=user.is_whitelisted
        )

    # 3. 初始体验默认用户 (开发模式)
    if not user and password == "123456" and settings.APP_ENV != "production":
        login_guard.on_success(guard_key)
        user = User(
            username=username,
            password_hash=get_password_hash(password),
            role="owner" if username.lower() == "admin" else "user",
            balance=0
        )
        await user_repo.create(user)
        await points_service.add_points(
            user_id=user.id,
            amount=settings.INITIAL_USER_COINS,
            event_type="init",
            idempotency_key=f"init_user_{user.id}",
            description="初始化赠送软妹币"
        )
        await db.commit()
        await db.refresh(user)

        token = create_access_token(subject=user.id, role=user.role)
        return Token(
            access_token=token,
            role=user.role,
            username=user.username,
            balance=user.balance,
            is_whitelisted=user.is_whitelisted
        )

    # 4. 校验失败：记录一次失败（累计达阈值触发锁定）
    login_guard.record_failure(guard_key)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="账号密码校验失败"
    )


def _token_response(user: User) -> Token:
    """统一会话签发出口：登录成功后用它换取 JWT 会话"""
    token = create_access_token(subject=user.id, role=user.role)
    return Token(
        access_token=token,
        role=user.role,
        username=user.username,
        balance=user.balance,
        is_whitelisted=user.is_whitelisted
    )


@router.post("/token-login", response_model=Token)
async def token_login(
    req: TokenLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    EMOS 式个人 Token 免密登录。

    Token 由 Telegram Bot 私聊 /token 获取（形如 `7_9f2c...`），
    每次获取即滚动 —— 旧 Token 立即失效。全程无需 Emby 账号密码。
    """
    service = TgAuthService(db)
    client_ip = request.client.host if request.client else None
    try:
        user = await service.login_with_token(req.token, ip_address=client_ip)
    except ValueError as e:
        # 触发登录限速
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效或已被重置，请在 Telegram Bot 中重新发送 /token"
        )

    logger = logging.getLogger("lemon_2f.auth")
    logger.info(f"Token login success: user #{user.id} ({user.username})")
    return _token_response(user)


@router.post("/tg-login", response_model=Token)
async def tg_login(
    req: TgLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Telegram 验证码免密登录。

    面板输入 TG 标识（数字 ID 或 @用户名）+ Bot 私聊 /login 获取的一次性验证码，
    校验通过即换取会话 —— 全程无需 Emby 账号密码。
    """
    service = TgAuthService(db)
    client_ip = request.client.host if request.client else None

    try:
        user, err = await service.resolve_tg_user(req.identifier)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if user is None or user.tg_user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=err or "该 Telegram 账号尚未开通二楼账号，请先私聊 Bot 发送 /start")

    try:
        user = await service.verify_login_code(user.tg_user_id, req.code, ip_address=client_ip)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="验证码校验失败，请确认后重试")

    return _token_response(user)

@router.get("/me", response_model=UserProfile)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/tg-bind/status", response_model=TgBindStatusResponse)
async def get_tg_bind_status(current_user: User = Depends(get_current_user)):
    """查询当前账号的 Telegram 绑定状态"""
    if current_user.tg_user_id:
        return TgBindStatusResponse(
            bound=True,
            tg_user_id=current_user.tg_user_id,
            tg_username=current_user.tg_username,
            message=f"已绑定 Telegram 账号 @{current_user.tg_username or current_user.tg_user_id}"
        )
    return TgBindStatusResponse(
        bound=False,
        message="尚未绑定 Telegram。请在 Bot 中发送 /link 获取绑定码后在此提交"
    )


@router.post("/tg-bind/redeem", response_model=TgBindStatusResponse)
async def redeem_tg_bind_code(
    req: TgBindRedeemRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    兑换 Telegram 绑定码，将 Bot 身份并入当前 Emby 账号。

    必须由已通过 Emby 鉴权的 Web 会话发起 —— Emby 账号是权威身份，
    TG 只是它的一个接入端，绝不允许反向由 TG 侧决定归属。
    """
    service = TgBindService(db)
    client_ip = request.client.host if request.client else None
    try:
        user = await service.redeem_code(req.code, current_user, ip_address=client_ip)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return TgBindStatusResponse(
        bound=True,
        tg_user_id=user.tg_user_id,
        tg_username=user.tg_username,
        message=f"绑定成功！Telegram @{user.tg_username or user.tg_user_id} 已并入账号 {user.username}"
    )


@router.post("/tg-bind/unbind", response_model=TgBindStatusResponse)
async def unbind_telegram(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """解绑当前账号的 Telegram（软妹币与流水不受影响）"""
    service = TgBindService(db)
    client_ip = request.client.host if request.client else None
    try:
        await service.unbind(current_user, actor=current_user, ip_address=client_ip)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return TgBindStatusResponse(
        bound=False,
        message="已解除 Telegram 绑定。软妹币余额与历史流水均不受影响"
    )


@router.get("/devices")
async def list_user_devices(
    current_user: User = Depends(get_current_user)
):
    """获取当前用户在 Emby 上的所有在线设备会话列表"""
    emby = EmbyClient()
    if not current_user.emby_user_id:
        return []
    return await emby.get_user_sessions(current_user.emby_user_id)


@router.post("/devices/{session_id}/logout")
async def logout_user_device(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """远程下线指定的 Emby 播放设备"""
    emby = EmbyClient()
    success = await emby.logout_session(session_id)
    if not success:
        raise HTTPException(status_code=400, detail="下线设备失败或设备已离线")
    return {"success": True, "message": "已成功将该设备远程强制下线！"}


@router.post("/reset-password")
async def reset_password(
    req: ResetPasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改当前账号登录密码与 Emby 播放密码"""
    current_user.password_hash = get_password_hash(req.new_password)
    if current_user.emby_user_id:
        emby = EmbyClient()
        await emby.reset_user_password(current_user.emby_user_id, req.new_password)
    await db.commit()
    return {"success": True, "message": "密码修改成功！新密码已同步生效。"}

