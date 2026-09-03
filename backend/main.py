import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
import redis.asyncio as aioredis

from backend.config import settings, get_cors_origins
from backend.database import AsyncSessionLocal
from backend.services.pipeline_service import SubmissionPipelineService
from backend.redis_client import redis_manager
from backend.bot import create_bot_app
from backend.routes.v1 import auth, tasks, submissions, points, wanted, shop, admin, webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - [%(name)s] - %(message)s"
)
logger = logging.getLogger("lemon_2f.main")


async def background_pipeline_worker():
    """
    后台状态机调度循环：事件驱动为主，轮询兜底为辅。

    原实现是固定 `await asyncio.sleep(15)`，一条投稿从下载完成到被发现
    平均要等半个周期。现在改为 Redis BRPOP 阻塞等待唤醒信号：
      - qB 完成 Webhook / 新投稿受理会 LPUSH 一个 token，Worker 立即醒来；
      - 无事件时 BRPOP 自然超时，等价于一次常规轮询节拍（兜底安全网）；
      - 有活跃任务时用短间隔，全空闲时用长间隔，降低数据库空转查询。

    Redis 不可用时自动退化为 asyncio.sleep 轮询，绝不因缺 Redis 停摆。
    """
    logger.info("Submission Pipeline Worker started (event-driven with polling fallback)")
    idle_rounds = 0

    while True:
        try:
            async with AsyncSessionLocal() as session:
                pipeline_service = SubmissionPipelineService(session)
                advanced = await pipeline_service.run_state_machine_cycle()

            # 有推进说明系统繁忙，用短间隔快速跟进后续阶段；
            # 连续空转则放宽到空闲间隔，避免无谓的全表扫描。
            if advanced:
                idle_rounds = 0
            else:
                idle_rounds = min(idle_rounds + 1, 10)

            wait_seconds = (
                settings.PIPELINE_POLL_INTERVAL_SECONDS
                if idle_rounds < 3
                else settings.PIPELINE_IDLE_INTERVAL_SECONDS
            )

            # 阻塞等待事件唤醒；超时即为一次常规轮询节拍
            try:
                reason = await redis_manager.wait_for_wake(wait_seconds)
                if reason:
                    logger.info(f"Pipeline woken by event: {reason}")
                    idle_rounds = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis 异常：退化为纯 sleep 轮询
                await asyncio.sleep(wait_seconds)

        except asyncio.CancelledError:
            logger.info("Submission Pipeline Worker cancelled")
            break
        except Exception as e:
            logger.error(f"Error in pipeline worker cycle: {e}", exc_info=True)
            # 出错后短暂退避，避免异常状态下疯狂空转刷日志
            try:
                await asyncio.sleep(settings.PIPELINE_POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

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
    description="二楼有请 · 基于 Emby 原生生态的众包入库、质检、软妹币经济与自动化管理系统",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
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
app.include_router(webhooks.router, prefix="/api/v1")

# 注册兼容前缀路由 (/api/...) 供前端直接无缝调用
app.include_router(auth.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(submissions.router, prefix="/api")
app.include_router(points.router, prefix="/api")
app.include_router(wanted.router, prefix="/api")
app.include_router(shop.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")

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
    真实生产就绪度探针: 探测数据库 SELECT 1、Redis 实时 PING 探测及核心外部依赖
    生产环境下若必需核心依赖缺失，严格返回 503 Service Unavailable (Fail-Closed)
    """
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception as e:
        logger.error(f"Database readiness probe failed: {e}")
        db_ok = False

    # 实时执行 Redis PING 探测 (修复启动后中途掉线误判 Bug)
    redis_ok = await redis_manager.ping()

    emby_configured = bool(settings.EMBY_SERVER_URL and settings.EMBY_API_KEY)
    tmdb_configured = bool(settings.TMDB_API_KEY)

    # 生产模式严格判定 (Fail-Closed)
    if settings.APP_ENV == "production":
        is_ready = db_ok and (not settings.REQUIRE_REDIS_IN_PROD or redis_ok) and emby_configured and tmdb_configured
        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "unready",
                "database": "connected" if db_ok else "disconnected",
                "redis": "connected" if redis_ok else "unavailable",
                "emby": "configured" if emby_configured else "unconfigured",
                "tmdb": "configured" if tmdb_configured else "unconfigured"
            }

    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unready",
            "database": "disconnected"
        }

    return {
        "status": "ready",
        "database": "connected",
        "redis": "connected" if redis_ok else "degraded_fallback",
        "emby": "configured" if emby_configured else "unconfigured",
        "tmdb": "configured" if tmdb_configured else "unconfigured"
    }
