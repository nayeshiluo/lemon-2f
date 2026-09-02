import logging
import uuid
from typing import Optional
from contextlib import asynccontextmanager
import redis.asyncio as aioredis
from backend.config import settings

logger = logging.getLogger("lemon_2f.redis")

class RedisManager:
    def __init__(self, url: str = settings.REDIS_URL):
        self.url = url
        self.client: Optional[aioredis.Redis] = None
        self._available = False

    async def connect(self):
        try:
            self.client = aioredis.from_url(
                self.url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=3.0,
                socket_connect_timeout=3.0
            )
            await self.client.ping()
            self._available = True
            logger.info("Redis connected successfully")
        except Exception as e:
            logger.warning(f"Redis connection failed ({e}), falling back to local non-distributed mode")
            self._available = False

    async def close(self):
        if self.client:
            await self.client.close()

    @property
    def is_available(self) -> bool:
        return self._available and self.client is not None

    @asynccontextmanager
    async def lock(self, key: str, timeout_seconds: int = 60):
        """安全分布式锁：带 Token 与 Lua 释放脚本"""
        token = str(uuid.uuid4())
        lock_key = f"lock:{key}"
        acquired = False
        
        if self.is_available and self.client:
            try:
                res = await self.client.set(lock_key, token, ex=timeout_seconds, nx=True)
                acquired = bool(res)
            except Exception as e:
                logger.warning(f"Redis lock error: {e}")
                acquired = True # 降级放行
        else:
            acquired = True # 无 Redis 时降级单机放行

        try:
            yield acquired
        finally:
            if acquired and self.is_available and self.client:
                # 仅当 token 一致时释放锁，防误删他人锁
                lua_script = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                try:
                    await self.client.eval(lua_script, 1, lock_key, token)
                except Exception as e:
                    logger.warning(f"Redis unlock error: {e}")

redis_manager = RedisManager()
