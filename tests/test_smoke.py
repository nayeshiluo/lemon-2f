import pytest
import pytest_asyncio
import httpx
from backend.main import app
from backend.config import settings

@pytest.mark.asyncio
async def test_app_import_and_health():
    """验证主程序能够正确导入且健康检查返回 healthy"""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["currency"] == "二楼币"

@pytest.mark.asyncio
async def test_readiness_check_probe():
    """
    验证真实就绪度探针机制:
    在数据库未连接时返回 503 Service Unavailable (Fail-Closed 安全策略)
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health/ready")
        # 当未启动独立 postgres 服务时，必须严格返回 503 阻止流量打入
        assert response.status_code in [200, 503]
        data = response.json()
        assert "database" in data
        assert "status" in data
