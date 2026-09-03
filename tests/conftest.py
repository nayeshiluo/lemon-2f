"""
测试环境统一夹具。

存在意义：`settings.APP_ENV` 默认值是 "production"，若本地未导出
APP_ENV=testing，测试会跑在生产语义下 —— 例如 RedisLock 在生产是
Fail-Closed 的（无 Redis 则拒绝获取锁），导致流水线相关测试莫名失败。

CI 里通过环境变量设置了 APP_ENV=testing，但本地直接 `pytest` 不会。
把它固定在 conftest 里，让测试结果不依赖外部环境变量，避免出现
"CI 绿但本地红"或反之的假信号。
"""
import pytest

from backend.config import settings


@pytest.fixture(autouse=True)
def _force_testing_env(monkeypatch):
    """所有测试强制运行在 testing 语义下（可被单个测试再次覆盖）"""
    monkeypatch.setattr(settings, "APP_ENV", "testing", raising=False)
    yield
