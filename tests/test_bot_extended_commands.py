import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import Base
from backend.models.user import User
from backend.models.wanted import WantedTask
from backend.models.watch import WatchRecord
from backend.models.social import RedPacket
from backend.bot import cmd_wanted, cmd_watch, cmd_wheel, cmd_redpacket

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


class MockMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.replies.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})


class MockUpdate:
    def __init__(self, user_id=123456):
        self.effective_user = MagicMock(id=user_id, username="tg_test_user")
        self.message = MockMessage()


@pytest.mark.asyncio
async def test_bot_commands_unbound_guard(monkeypatch):
    """验证：未绑定用户调用任何新扩展指令均被统一安全拦截"""
    # 模拟未找到绑定用户
    async def mock_get_bound_user(tg_id):
        return None

    monkeypatch.setattr("backend.bot.get_bound_user", mock_get_bound_user)

    update = MockUpdate(999999)
    context = MagicMock()

    # 依次调用 4 个指令
    await cmd_wanted(update, context)
    await cmd_watch(update, context)
    await cmd_wheel(update, context)
    await cmd_redpacket(update, context)

    # 验证全部被拦截且给出了绑定指引
    for reply in update.message.replies:
        assert "您还没有绑定二楼账号" in reply["text"]
