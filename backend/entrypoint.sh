#!/bin/sh
set -e

echo "=== [二楼有请 / Lemon 2F] 正在执行生产数据库版本升级 (Alembic Upgrade) ==="
alembic upgrade head
echo "=== 数据库迁移检查完成，正在启动后端 API 网关服务 ==="

exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
