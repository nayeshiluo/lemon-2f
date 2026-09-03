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

    # 流水线唤醒队列：qB webhook 等事件源 LPUSH 一个 token，
    # Worker 用 BRPOP 阻塞等待 —— 既实现"睡眠"又实现"被事件唤醒"，
    # 且天然跨进程/跨副本工作（不像 asyncio.Event 只在单进程内有效）。
    WAKE_QUEUE_KEY = "lemon2f:pipeline:wake"

    # redis-py 8.x 的坑：from_url() 不显式传 socket_timeout 时，
    # 连接实际带着 orig_socket_timeout=5 —— 也就是 5 秒读超时。
    # 阻塞命令 BRPOP 只要等待超过 5 秒就会被误判为读超时抛 TimeoutError，
    # 事件驱动会静默退化成纯轮询（实测：BRPOP(20) 在 5.01s 抛 TimeoutError）。
    #
    # 因此必须区分两类连接：
    #   - 普通命令（SET/GET/EVAL/PING）：短读超时，Redis 卡住时快速失败；
    #   - 阻塞命令（BRPOP）：读超时必须大于阻塞时长，否则永远等不满。
    # 混用同一个客户端做不到这两点，标准做法就是给阻塞命令单独一条连接。
    FAST_SOCKET_TIMEOUT_SECONDS = 5
    # BRPOP 单次最长阻塞时长上限（同时决定阻塞连接的读超时）
    MAX_BLOCKING_WAIT_SECONDS = 300
    # 阻塞连接读超时留出的余量，确保 Redis 先返回、socket 后超时
    BLOCKING_SOCKET_TIMEOUT_MARGIN = 15

    def __init__(self):
        self.client: Optional[aioredis.Redis] = None
        # 专用于 BRPOP 等阻塞命令，读超时已放宽
        self._blocking_client: Optional[aioredis.Redis] = None
        self._available = False

    async def connect(self):
        try:
            self.client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=self.FAST_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=self.FAST_SOCKET_TIMEOUT_SECONDS,
            )
            await self.client.ping()

            # 阻塞命令专用连接：读超时 = 最长阻塞时长 + 余量
            self._blocking_client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=self.MAX_BLOCKING_WAIT_SECONDS + self.BLOCKING_SOCKET_TIMEOUT_MARGIN,
                socket_connect_timeout=self.FAST_SOCKET_TIMEOUT_SECONDS,
            )
            await self._blocking_client.ping()

            self._available = True
            logger.info(
                f"Redis connected successfully "
                f"(fast_timeout={self.FAST_SOCKET_TIMEOUT_SECONDS}s, "
                f"blocking_timeout={self.MAX_BLOCKING_WAIT_SECONDS + self.BLOCKING_SOCKET_TIMEOUT_MARGIN}s)"
            )
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}")
            self._available = False
            self.client = None
            self._blocking_client = None

    @property
    def blocking_client(self) -> Optional[aioredis.Redis]:
        """
        阻塞命令客户端。未单独建立时退回主客户端
        （测试替身场景只注入 client，需保持可用）。
        """
        return self._blocking_client or self.client

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
        for name, cli in (("blocking", self._blocking_client), ("main", self.client)):
            if cli:
                try:
                    await cli.close()
                except Exception as e:
                    logger.warning(f"Error closing {name} Redis client: {e}")
        if self.client or self._blocking_client:
            logger.info("Redis connection closed")

    def lock(self, key: str, timeout_seconds: int = 30) -> RedisLock:
        return RedisLock(self.client, key, timeout_seconds)

    async def signal_wake(self, reason: str = "event") -> bool:
        """
        投递一个流水线唤醒信号，让 Worker 立刻醒来推进状态机，
        而不必等满一个轮询周期。返回是否成功投递。
        """
        if not self.client:
            return False
        try:
            # 设 60 秒过期，避免 Redis 里堆积陈旧唤醒 token
            await self.client.lpush(self.WAKE_QUEUE_KEY, reason)
            await self.client.expire(self.WAKE_QUEUE_KEY, 60)
            return True
        except Exception as e:
            logger.warning(f"Failed to signal pipeline wake: {e}")
            return False

    async def wait_for_wake(self, timeout_seconds: int) -> Optional[str]:
        """
        阻塞等待唤醒信号，最多等 timeout_seconds 秒。

        返回唤醒原因；超时返回 None（等价于一次正常的轮询节拍）。
        Redis 不可用返回 None；连接异常时抛出，由调用方退化为 asyncio.sleep。

        走 blocking_client：主客户端的短读超时会把长时间 BRPOP 误判为读超时。
        """
        client = self.blocking_client
        if not client:
            return None

        # 阻塞时长必须受上限约束，否则会超过阻塞连接的 socket 读超时
        wait = max(1, min(int(timeout_seconds), self.MAX_BLOCKING_WAIT_SECONDS))
        try:
            res = await client.brpop(self.WAKE_QUEUE_KEY, timeout=wait)
            if res:
                raw = res[1]
                return raw.decode() if isinstance(raw, bytes) else str(raw)
            return None
        except Exception as e:
            logger.warning(f"wait_for_wake failed, falling back to sleep: {e}")
            raise

    @property
    def is_available(self) -> bool:
        return self._available

redis_manager = RedisManager()
