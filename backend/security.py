import base64
import binascii
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, Any
from jose import jwt, JWTError
from backend.config import settings

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000


def get_password_hash(password: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 生成自适应密码哈希。"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt_b64}${digest_b64}"


def _decode_b64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def is_legacy_password_hash(hashed_password: str) -> bool:
    """识别旧版 salt$sha256 格式，供登录成功后无感升级。"""
    return bool(hashed_password) and not hashed_password.startswith(f"${PASSWORD_SCHEME}$") and hashed_password.count("$") == 1


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not hashed_password:
        return False

    try:
        if hashed_password.startswith(f"${PASSWORD_SCHEME}$"):
            scheme, iterations, salt_b64, expected_b64 = hashed_password.split("$", 3)
            if scheme != PASSWORD_SCHEME:
                return False
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                plain_password.encode("utf-8"),
                _decode_b64(salt_b64),
                int(iterations),
            )
            return hmac.compare_digest(
                base64.urlsafe_b64encode(actual).decode("ascii").rstrip("="),
                expected_b64,
            )

        # 兼容旧版 salt$sha256 哈希，避免现有账号被迫重置密码。
        salt, expected_hash = hashed_password.split("$", 1)
        actual_hash = hashlib.sha256((salt + plain_password).encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual_hash, expected_hash)
    except (TypeError, ValueError, binascii.Error):
        return False


def create_access_token(subject: Union[str, Any], role: str = "user", expires_delta: Optional[timedelta] = None) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "role": role
    }
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None
