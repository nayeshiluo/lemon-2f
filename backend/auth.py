import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from fastapi import HTTPException, Security, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from config import settings

security_bearer = HTTPBearer()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")

async def authenticate_with_emby(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    通过 Emby Server 原生接口鉴权
    """
    headers = {
        "X-Emby-Client": "LemonEmos-Web",
        "X-Emby-Device-Name": "LemonEmos-Browser",
        "X-Emby-Device-Id": "lemon-emos-web-client",
        "X-Emby-Client-Version": "1.0.0",
        "Content-Type": "application/json"
    }
    payload = {"Username": username, "Pw": password}
    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        try:
            resp = await client.post(f"{settings.EMBY_SERVER_URL}/emby/Users/AuthenticateByName", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                user_info = data.get("User", {})
                user_id = user_info.get("Id")
                name = user_info.get("Name")
                is_admin = user_info.get("Policy", {}).get("IsAdministrator", False)
                return {
                    "user_id": user_id,
                    "username": name,
                    "is_admin": is_admin,
                    "emby_token": data.get("AccessToken")
                }
        except Exception as e:
            print(f"Emby auth error: {e}")
    return None

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security_bearer)) -> Dict[str, Any]:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        role: str = payload.get("role", "user")
        if username is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return {"username": username, "role": role, "user_id": payload.get("user_id")}
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")

def require_role(allowed_roles: list):
    async def role_checker(user: Dict[str, Any] = Depends(get_current_user)):
        if user.get("role") not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
        return user
    return role_checker
