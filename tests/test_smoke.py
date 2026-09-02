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
async def test_readiness_check():
    """验证就绪检查端点"""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
