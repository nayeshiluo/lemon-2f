"""Security regression tests for authentication and device isolation."""

import hashlib

import pytest
from fastapi import HTTPException

from backend.models.user import User
from backend.routes.v1 import auth as auth_routes
from backend.security import (
    get_password_hash,
    is_legacy_password_hash,
    verify_password,
)


def test_password_hash_is_adaptive_and_verifiable():
    hashed = get_password_hash("correct horse battery staple")

    assert hashed.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)
    assert not is_legacy_password_hash(hashed)


def test_legacy_password_hash_verifies_for_login_upgrade():
    salt = "legacy-salt"
    digest = hashlib.sha256((salt + "old-password").encode("utf-8")).hexdigest()
    legacy = f"${salt}${digest}"

    assert is_legacy_password_hash(legacy)
    assert verify_password("old-password", legacy)
    assert not verify_password("wrong-password", legacy)


@pytest.mark.asyncio
async def test_device_logout_rejects_a_session_owned_by_another_user(monkeypatch):
    class FakeEmbyClient:
        async def get_user_sessions(self, emby_user_id):
            return [{"session_id": "own-session"}]

        async def logout_session(self, session_id):
            raise AssertionError("越权会话不应调用 Emby 登出接口")

    monkeypatch.setattr(auth_routes, "EmbyClient", FakeEmbyClient)

    current_user = User(
        id=7,
        username="alice",
        emby_user_id="emby-alice",
        is_active=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth_routes.logout_user_device("other-users-session", current_user)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_device_logout_allows_a_session_owned_by_current_user(monkeypatch):
    class FakeEmbyClient:
        async def get_user_sessions(self, emby_user_id):
            assert emby_user_id == "emby-alice"
            return [{"session_id": "own-session"}]

        async def logout_session(self, session_id):
            assert session_id == "own-session"
            return True

    monkeypatch.setattr(auth_routes, "EmbyClient", FakeEmbyClient)

    current_user = User(
        id=7,
        username="alice",
        emby_user_id="emby-alice",
        is_active=True,
    )

    result = await auth_routes.logout_user_device("own-session", current_user)

    assert result["success"] is True
