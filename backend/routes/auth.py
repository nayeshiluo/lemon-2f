from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.database import get_db
from backend.models import User, PointsLedger
from backend.schemas import EmbyLoginRequest, Token, UserProfile
from backend.security import create_access_token, get_password_hash, verify_password
from backend.emby_client import emby_client
from backend.auth import get_current_user
from backend.config import settings

router = APIRouter(prefix="/api/auth", tags=["Auth"])

@router.post("/login", response_model=Token)
async def login(req: EmbyLoginRequest, db: AsyncSession = Depends(get_db)):
    """支持 Emby 原生账号密码穿透登录，或本地用户名密码登录"""
    username = req.username.strip()
    password = req.password

    # 1. 首先尝试 Emby 服务器穿透鉴权
    emby_auth = await emby_client.authenticate_user(username, password)
    
    stmt = select(User).where(User.username == username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if emby_auth:
        emby_id = emby_auth.get("emby_user_id")
        is_admin = emby_auth.get("is_administrator", False)
        target_role = "admin" if is_admin else "user"

        if not user:
            # 首次通过 Emby 登录，自动初始化用户与赠送二楼币
            user = User(
                username=username,
                emby_user_id=emby_id,
                emby_username=username,
                role=target_role,
                balance=settings.INITIAL_USER_COINS
            )
            db.add(user)
            await db.flush()

            # 初始流水记录
            ledger = PointsLedger(
                user_id=user.id,
                amount=settings.INITIAL_USER_COINS,
                balance_after=user.balance,
                event_type="init",
                description="新用户注册初始赠送二楼币"
            )
            db.add(ledger)
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
            balance=user.balance
        )

    # 2. Emby 鉴权未通过或未配置 Emby，尝试本地账号登录
    if user and user.password_hash and verify_password(password, user.password_hash):
        token = create_access_token(subject=user.id, role=user.role)
        return Token(
            access_token=token,
            role=user.role,
            username=user.username,
            balance=user.balance
        )

    # 3. 若本地首次登录体验 (支持默认 demo/admin 密码)
    if not user and password == "123456":
        user = User(
            username=username,
            password_hash=get_password_hash(password),
            role="owner" if username.lower() == "admin" else "user",
            balance=settings.INITIAL_USER_COINS
        )
        db.add(user)
        await db.flush()
        ledger = PointsLedger(
            user_id=user.id,
            amount=settings.INITIAL_USER_COINS,
            balance_after=user.balance,
            event_type="init",
            description="初始化赠送二楼币"
        )
        db.add(ledger)
        await db.commit()
        await db.refresh(user)

        token = create_access_token(subject=user.id, role=user.role)
        return Token(
            access_token=token,
            role=user.role,
            username=user.username,
            balance=user.balance
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Emby 或本地账号密码校验失败"
    )

@router.get("/me", response_model=UserProfile)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
