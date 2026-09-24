#!/bin/sh
set -e

echo "=== [二楼有请 / Lemon 2F] 数据库迁移由 Compose migrate 阶段负责 ==="
echo "=== 正在启动后端 API 网关服务 ==="

exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
