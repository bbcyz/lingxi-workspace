# ==========================================
# 完整版 Dockerfile（无侵入热注入架构）
# ==========================================
FROM python:3.12-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    LINGXI_HOST=0.0.0.0 \
    LINGXI_PORT=5500 \
    LINGXI_DATA_DIR=/data \
    CMCC_BROWSER_EXECUTABLE=/usr/bin/chromium

# 安装系统依赖、时区、Node.js 与 Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
       tzdata \
       ca-certificates \
       curl \
       nodejs \
       npm \
       chromium \
       fonts-wqy-zenhei \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖定义，充分利用 Docker 缓存
COPY requirements.txt package.json ./
COPY recharge/package.json ./recharge/

# 安装依赖：在此强制追加安装 requests 和 pillow，绝不受上游影响
RUN pip install --no-cache-dir -r requirements.txt requests pillow \
    && npm install --omit=dev \
    && cd recharge && npm install --omit=dev && cd ..

# 拷贝原作者的全部源代码
COPY . .

# 🌟【核心外挂步骤】：在打包镜像时，自动用 Linux 适配版替换掉原版
COPY login_storage_linux.py ./login_storage.py

# 创建持久化数据目录
RUN mkdir -p /data

EXPOSE 5500

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://127.0.0.1:5500/api/access/status || exit 1

CMD ["python", "-X", "utf8", "app.py"]
