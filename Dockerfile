# ============================================================
# ClipMind 后端 Docker 镜像
# ============================================================
# 构建：docker build -t clipmind-backend .
# 运行：docker run -p 8000:8000 -v $(pwd)/data:/app/data clipmind-backend
# ============================================================

FROM python:3.11-slim AS base

# 系统依赖：ffmpeg（ASR 音频处理）、Playwright Chromium 运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    # Playwright Chromium 运行时依赖
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxss1 \
    libasound2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    # 中文字体（Playwright 截图抖音登录二维码需要）
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium 浏览器
RUN playwright install chromium

# 复制应用代码
COPY . .

# 数据目录（运行时通过 volume 挂载持久化）
RUN mkdir -p data
VOLUME ["/app/data"]

# 环境变量默认值
ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    DEBUG=false \
    LOG_LEVEL=INFO \
    CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000 \
    CLIPMIND_DATA_DIR=/app/data

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" || exit 1

CMD ["python", "run.py"]