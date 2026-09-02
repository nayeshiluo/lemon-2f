import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.config import settings
from backend.database import init_db
from backend.pipeline import pipeline
from backend.bot import create_bot_app
from backend.routes import auth, dedup, submissions, points, wanted, shop, admin

# 日志初始化
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - [%(name)s] - %(message)s"
)
logger = logging.getLogger("lemon_2f.main")

# 后台定时调度任务
async def background_pipeline_worker():
    logger.info("Background Submission Pipeline Worker started")
    while True:
        try:
            await pipeline.process_active_tasks()
        except Exception as e:
            logger.error(f"Error in background pipeline loop: {e}")
        await asyncio.sleep(15) # 每 15 秒轮询一次状态机

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段: 初始化数据库表
    logger.info(f"Starting {settings.SYSTEM_TITLE} v{settings.APP_VERSION}")
    await init_db()
    
    # 启动后台下载质检工作流任务
    worker_task = asyncio.create_task(background_pipeline_worker())
    
    # 启动 Telegram Bot (若已配置 Token)
    bot_app = create_bot_app()
    if bot_app:
        try:
            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling()
            logger.info("Telegram Bot service started successfully")
        except Exception as e:
            logger.error(f"Failed to start Telegram Bot: {e}")

    yield
    
    # 关闭阶段
    if bot_app:
        try:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
        except Exception:
            pass

    worker_task.cancel()
    logger.info("Shutdown completed")

app = FastAPI(
    title=settings.SYSTEM_TITLE,
    version=settings.APP_VERSION,
    description="二楼有请 · 基于 Emby 原生生态的众包入库、质检、二楼币经济与自动化管理系统",
    lifespan=lifespan
)

# 跨域设置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
app.include_router(auth.router)
app.include_router(dedup.router)
app.include_router(submissions.router)
app.include_router(points.router)
app.include_router(wanted.router)
app.include_router(shop.router)
app.include_router(admin.router)

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
        "version": settings.APP_VERSION
    }
