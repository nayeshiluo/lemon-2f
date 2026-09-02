import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.services.pipeline_service import SubmissionPipelineService
from backend.redis_client import redis_manager
from backend.bot import create_bot_app
from backend.routes.v1 import auth, tasks, submissions, points, wanted, shop, admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - [%(name)s] - %(message)s"
)
logger = logging.getLogger("lemon_2f.main")

async def background_pipeline_worker():
    """后台任务状态机调度循环"""
    logger.info("Submission Pipeline Worker started")
    while True:
        try:
            async with AsyncSessionLocal() as session:
                pipeline_service = SubmissionPipelineService(session)
                await pipeline_service.run_state_machine_cycle()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in pipeline worker cycle: {e}", exc_info=True)
        await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.SYSTEM_TITLE} v{settings.APP_VERSION} [{settings.APP_ENV}]")
    
    # 1. 建立 Redis 连接
    await redis_manager.connect()
    
    # 2. 启动后台流水线状态机 Worker
    worker_task = asyncio.create_task(background_pipeline_worker())
    
    # 3. 启动 Telegram Bot 服务 (若已配置 Token)
    bot_app = create_bot_app()
    if bot_app:
        try:
            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling()
            logger.info("Telegram Bot service started")
        except Exception as e:
            logger.error(f"Failed to start Telegram Bot: {e}")

    yield

    # 关闭与清理
    if bot_app:
        try:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
        except Exception:
            pass

    worker_task.cancel()
    await redis_manager.close()
    logger.info("Shutdown completed")

app = FastAPI(
    title=settings.SYSTEM_TITLE,
    version=settings.APP_VERSION,
    description="二楼有请 · 基于 Emby 原生生态的众包入库、质检、二楼币经济与自动化管理系统",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 v1 标准 API 路由
app.include_router(auth.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(submissions.router, prefix="/api/v1")
app.include_router(points.router, prefix="/api/v1")
app.include_router(wanted.router, prefix="/api/v1")
app.include_router(shop.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")

# 注册兼容前缀路由 (/api/...) 供前端直接无缝调用
app.include_router(auth.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(submissions.router, prefix="/api")
app.include_router(points.router, prefix="/api")
app.include_router(wanted.router, prefix="/api")
app.include_router(shop.router, prefix="/api")
app.include_router(admin.router, prefix="/api")

# 静态前端资源挂载
frontend_dist = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
assets_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

if os.path.exists(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

if os.path.exists(frontend_dist):
    app.mount("/static", StaticFiles(directory=frontend_dist), name="static")

    @app.get("/")
    async def serve_index():
        index_file = os.path.join(frontend_dist, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return {"message": "Frontend index.html not found"}

@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "currency": settings.CURRENCY_NAME,
        "version": settings.APP_VERSION,
        "env": settings.APP_ENV
    }

@app.get("/api/health/ready")
async def readiness_check(response: Response):
    """
    真实生产就绪度探针: 执行真实数据库 SELECT 1 与 Redis ping
    若数据库不可用，HTTP 响应 503 Service Unavailable
    """
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception as e:
        logger.error(f"Database readiness probe failed: {e}")
        db_ok = False

    redis_ok = redis_manager.is_available
    emby_configured = bool(settings.EMBY_SERVER_URL and settings.EMBY_API_KEY)
    tmdb_configured = bool(settings.TMDB_API_KEY)

    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unready",
            "database": "disconnected",
            "redis": "connected" if redis_ok else "unavailable"
        }

    return {
        "status": "ready",
        "database": "connected",
        "redis": "connected" if redis_ok else "degraded_fallback",
        "emby": "configured" if emby_configured else "unconfigured",
        "tmdb": "configured" if tmdb_configured else "unconfigured"
    }
