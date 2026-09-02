import logging
import uuid
from typing import Optional
from contextlib import asynccontextmanager
import redis.asyncio as aioredis
from backend.config import settings

logger = logging.getLogger("lemon_2f.redis")

class RedisLock:
    """Lua 脚本安全释放的 Redis 分布式锁"""
    def __init__(self, client: Optional[aioredis.Redis], key: str, timeout_seconds: int = 30):
        self.client = client
        self.key = key
        self.timeout = timeout_seconds
        self.token = str(uuid.uuid4())
        self.acquired = False

    async def __aenter__(self) -> bool:
        if not self.client:
            if settings.APP_ENV == "production" and settings.REQUIRE_REDIS_IN_PROD:
                logger.error("Production requires Redis but Redis is unavailable. Rejecting lock acquisition (Fail-Closed).")
                return False
            logger.warning("Redis unavailable in non-prod, fallback allow.")
            return True

        try:
            res = await self.client.set(self.key, self.token, nx=True, ex=self.timeout)
            self.acquired = bool(res)
            return self.acquired
        except Exception as e:
            logger.error(f"Redis lock acquisition error for key [{self.key}]: {e}")
            if settings.APP_ENV == "production" and settings.REQUIRE_REDIS_IN_PROD:
                return False
            return True

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if not self.client or not self.acquired:
            return

        lua_release = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            await self.client.eval(lua_release, 1, self.key, self.token)
        except Exception as e:
            logger.error(f"Failed to release Redis lock for key [{self.key}]: {e}")

class RedisManager:
    """Redis 连接与生命周期管理器"""
    def __init__(self):
        self.client: Optional[aioredis.Redis] = None
        self._available = False

    async def connect(self):
        try:
            self.client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self.client.ping()
            self._available = True
            logger.info("Redis connected successfully")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}")
            self._available = False
            self.client = None

    async def ping(self) -> bool:
        """实时 PING 探测"""
        if not self.client:
            return False
        try:
            return bool(await self.client.ping())
        except Exception:
            self._available = False
            return False

    async def close(self):
        if self.client:
            await self.client.close()
            logger.info("Redis connection closed")

    def lock(self, key: str, timeout_seconds: int = 30) -> RedisLock:
        return RedisLock(self.client, key, timeout_seconds)

    @property
    def is_available(self) -> bool:
        return self._available

redis_manager = RedisManager()
