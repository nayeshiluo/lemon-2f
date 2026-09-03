from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.repositories.user_repo import UserRepository
from backend.services.points_service import PointsService
from backend.services.tg_bind_service import TgBindService
from backend.schemas import (
    EmbyLoginRequest, Token, UserProfile, ApiResponse,
    TgBindRedeemRequest, TgBindStatusResponse,
)
from backend.security import create_access_token, get_password_hash, verify_password
from backend.clients.emby import emby_client
from backend.auth import get_current_user
from backend.config import settings

router = APIRouter(prefix="/auth", tags=["Auth"])

@router.post("/login", response_model=Token)
async def login(req: EmbyLoginRequest, db: AsyncSession = Depends(get_db)):
    """支持 Emby 原生账号密码穿透登录，或本地用户名密码登录"""
    username = req.username.strip()
    password = req.password

    user_repo = UserRepository(db)
    points_service = PointsService(db)

    # 1. Emby 服务器穿透鉴权
    emby_auth = await emby_client.authenticate_user(username, password)
    user = await user_repo.get_by_username(username)

    if emby_auth:
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

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="账号密码校验失败"
    )

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
