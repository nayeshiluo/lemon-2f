from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models.user import User
from backend.repositories.user_repo import UserRepository
from backend.services.points_service import PointsService
from backend.schemas import EmbyLoginRequest, Token, UserProfile, ApiResponse
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
                description="新用户首次登录赠送二楼币"
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
            description="初始化赠送二楼币"
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
