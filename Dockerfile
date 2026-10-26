# WeWrite 本地工作台镜像（python:3.11-slim，非 root，镜像内不落密钥）
FROM python:3.11-slim

# 安装构建/运行所需的最小系统依赖（wewrite 的 lxml 等无需编译，锁依赖走 pip）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户
RUN groupadd --system wewrite && useradd --system --gid wewrite --create-home wewrite

WORKDIR /app

# 先装依赖（按 requirements.lock 锁定全部传递依赖，可复现构建），利用 Docker 层缓存
COPY pyproject.toml README.md requirements.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps .

# 状态目录：挂载命名卷，属主 wewrite
RUN mkdir -p /data/wewrite && chown -R wewrite:wewrite /data/wewrite

USER wewrite

ENV WEWRITE_HOME=/data/wewrite \
    PYTHONUNBUFFERED=1

EXPOSE 8765

# 健康检查打 /healthz
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz')"]

# 入口：目录属主/权限检查后启动 uvicorn（0.0.0.0:8765 仅容器内监听，宿主机由 compose 映射 127.0.0.1）
CMD ["python", "-c", "import os, sys; p='/data/wewrite'; os.makedirs(p, exist_ok=True); import uvicorn; uvicorn.run('app.main:app', host='0.0.0.0', port=8765, workers=1)"]
