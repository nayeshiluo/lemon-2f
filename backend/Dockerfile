FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖 (含 ffprobe 用于视频元数据深度质检)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY backend /app/backend
COPY assets /app/assets
COPY frontend /app/frontend
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
