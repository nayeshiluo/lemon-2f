FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖 (含 ffprobe 用于视频元数据深度质检)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 使用经过 CI 验证的完整锁定依赖，避免未来重建时解析到不同版本
COPY backend/requirements.lock.txt /app/requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && pip check

# 复制代码
COPY backend /app/backend
COPY assets /app/assets
COPY frontend /app/frontend
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations

RUN chmod +x /app/backend/entrypoint.sh

ENV PYTHONPATH=/app

EXPOSE 8000

ENTRYPOINT ["/app/backend/entrypoint.sh"]
