"""
进程内轻量限流守卫（防滥用边界的第一道防线）。

设计原则：
  1. 纯内存实现，零外部依赖（Redis 故障时依然生效）；
  2. 多 worker 部署下每个进程独立计数 —— 边界被放大 N 倍，
     因此阈值按"单进程最坏情况"设定偏紧，并配合 DB 层唯一约束兜底；
  3. 所有守卫可被 reset() 清空，测试隔离友好。

当前用途：
  - LoginRateGuard：账号密码登录限速（5 次失败 → 锁 10 分钟）；
  - BytesWindowLimiter：直传上传的会话累计字节额度；
  - RateLimiter / RateLimit：通用端点频率限制（高危经济接口）。
"""
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request, status


class RateLimiter:
    """通用滑动窗口频率守卫（按 key 计数）"""

    def __init__(self, max_hits: int, window_seconds: float):
        self.max_hits = max_hits
        self.window_seconds = window_seconds
        self._hits: Dict[str, List[float]] = {}

    def allow(self, key: str) -> Tuple[bool, int]:
        """
        尝试放行一次。返回 (是否放行, 若拒绝则 建议 Retry-After 秒数)。
        放行时立即计数；拒绝时正好是窗口第 max_hits 次之后。
        """
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if t > now - self.window_seconds]
        if len(hits) >= self.max_hits:
            return False, int(self.window_seconds - (now - hits[0])) + 1
        hits.append(now)
        self._hits[key] = hits
        return True, 0

    def reset(self) -> None:
        self._hits.clear()


def _request_key(request: Request) -> str:
    """
    限流键：已登录用户按 uid（多设备/共享出口 IP 互不挤兑），
    匿名请求按客户端 IP（兜底）。从 JWT 提取 sub，不查库。
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = decode_jwt_sub(auth[7:].strip())
        if payload:
            return f"u:{payload}"
    ip = request.client.host if request.client else "unknown"
    return f"ip:{ip}"


def decode_jwt_sub(token: str) -> Optional[str]:
    """轻量解码 JWT 的 sub（不透传非法 token 的错误）"""
    try:
        from backend.security import decode_access_token
        payload = decode_access_token(token)
        if payload and payload.get("sub"):
            return str(payload["sub"])
    except Exception:
        pass
    return None


def RateLimit(max_hits: int, window_seconds: float):
    """
    FastAPI 依赖工厂：给高频端点加频率限制。

    用法：
        submit_rate = RateLimit(10, 60)          # 10 次 / 60 秒
        @router.post("/")
        async def handler(..., _rl: None = submit_rate):
            ...

    返回 429 + Retry-After 头。进程内存活，多 worker 各自计数。
    """
    limiter = RateLimiter(max_hits, window_seconds)

    async def dependency(request: Request):
        ok, retry = limiter.allow(_request_key(request))
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="操作过于频繁，请稍后再试",
                headers={"Retry-After": str(retry)},
            )

    dependency.limiter = limiter  # 测试可访问/调参
    return Depends(dependency)


class LoginRateGuard:
    """
    登录暴力破解守卫。

    策略：滑动窗口内累计失败次数，达到阈值后锁定一段时间。
    成功登录立即清零（避免合法用户被历史失败误伤）。

    注意：阈值按"每用户名+IP 维度"计算，同一真人多设备正常使用不受影响；
    分布式多 worker 下阈值被进程数放大，暴力者需付出 N 倍成本，
    但任何单进程内超过阈值即被拒，仍能有效拖慢爆破速度。
    """

    MAX_FAILS = 5           # 窗口内最多失败次数
    WINDOW_SECONDS = 300.0  # 失败窗口：5 分钟
    LOCK_SECONDS = 600.0    # 触发后锁定：10 分钟

    def __init__(self):
        self._fails: Dict[str, List[float]] = {}
        self._locks: Dict[str, float] = {}

    def _get_lock_until(self, key: str, now: float) -> Optional[float]:
        until = self._locks.get(key)
        if until and until > now:
            return until
        if until:  # 已过期，清除
            self._locks.pop(key, None)
        return None

    def check(self, key: str) -> None:
        """
        检查是否允许一次登录尝试。

        抛出 LoginBlocked 表示当前被锁定（调用方应返回 429）。
        """
        now = time.time()
        until = self._get_lock_until(key, now)
        if until:
            raise LoginBlocked(int(until - now) + 1)

        fails = [t for t in self._fails.get(key, []) if t > now - self.WINDOW_SECONDS]
        if len(fails) >= self.MAX_FAILS:
            # 触发锁定并清空失败记录
            self._locks[key] = now + self.LOCK_SECONDS
            self._fails.pop(key, None)
            raise LoginBlocked(int(self.LOCK_SECONDS) + 1)

        self._fails[key] = fails

    def record_failure(self, key: str) -> None:
        now = time.time()
        fails = [t for t in self._fails.get(key, []) if t > now - self.WINDOW_SECONDS]
        fails.append(now)
        self._fails[key] = fails

    def on_success(self, key: str) -> None:
        """登录成功：清除失败与锁定记录，避免误伤继续累积"""
        self._fails.pop(key, None)
        self._locks.pop(key, None)

    def reset(self) -> None:
        self._fails.clear()
        self._locks.clear()


class LoginBlocked(Exception):
    """登录被限流锁定（HTTP 429）"""

    def __init__(self, retry_after_seconds: int):
        super().__init__(f"失败次数过多，请 {retry_after_seconds // 60} 分钟后再试")
        self.retry_after_seconds = retry_after_seconds


class BytesWindowLimiter:
    """
    滑动窗口字节额度守卫（上传会话累计）。

    同一 key（用户）在窗口内累计上传字节不得超过配额，
    超出即拒绝 —— 防止恶意用户无限上传塞满磁盘。
    """

    def __init__(self, max_bytes: int, window_seconds: float):
        self.max_bytes = max_bytes
        self.window_seconds = window_seconds
        self._records: Dict[str, List[Tuple[float, int]]] = {}

    def remaining(self, key: str) -> int:
        """当前剩余可用字节数（含本请求将消耗的额度判断见 allow）"""
        now = time.time()
        records = [
            (ts, size)
            for ts, size in self._records.get(key, [])
            if ts > now - self.window_seconds
        ]
        used = sum(size for _, size in records)
        return max(0, self.max_bytes - used)

    def allow(self, key: str, add_bytes: int) -> Tuple[bool, int]:
        """
        尝试为 key 增加 add_bytes 的上传量。

        返回 (是否允许, 剩余额度)。允许时记录立即生效；
        拒绝时不写入记录（调用方应终止上传并清理已落盘分块）。
        """
        now = time.time()
        records = [
            (ts, size)
            for ts, size in self._records.get(key, [])
            if ts > now - self.window_seconds
        ]
        used = sum(size for _, size in records)
        if used + add_bytes > self.max_bytes:
            return False, max(0, self.max_bytes - used)
        records.append((now, add_bytes))
        self._records[key] = records
        return True, max(0, self.max_bytes - used - add_bytes)

    def refund(self, key: str, refund_bytes: int) -> None:
        """
        退还已预扣的上传额度（用于入库失败/资源冲突回滚场景），
        避免用户因一次失败被白白扣掉配额。
        """
        if refund_bytes <= 0:
            return
        now = time.time()
        records = [
            (ts, size)
            for ts, size in self._records.get(key, [])
            if ts > now - self.window_seconds
        ]
        if not records:
            return
        to_refund = refund_bytes
        new_records: List[Tuple[float, int]] = []
        for ts, size in records:
            if to_refund > 0:
                take = min(size, to_refund)
                to_refund -= take
                remaining = size - take
                if remaining > 0:
                    new_records.append((ts, remaining))
            else:
                new_records.append((ts, size))
        self._records[key] = new_records

    def reset(self) -> None:
        self._records.clear()


# 全局单例：登录守卫（所有登录通道共享，防爆破）
login_guard = LoginRateGuard()