# ============================================================
# ArtifactDepot 单容器镜像：对象存储仓库站点（FastAPI，8004）
#
# 注意：Podman 默认 OCI 格式会忽略 HEALTHCHECK；请用 ./build_image.sh
#       构建（脚本会自动追加 --format docker），或手动 podman build --format docker。
# ============================================================
FROM docker.io/library/python:3.12-alpine

LABEL org.opencontainers.image.title="ArtifactDepot-ObjectStorage"
LABEL org.opencontainers.image.description="Object storage depot (FastAPI :8004)"
LABEL org.opencontainers.image.version="0.7.1"

# 安装 Supervisor + curl（健康检查用）+ tzdata（容器内统一中国时区）
# 先把 Alpine apk 源换成清华镜像：官方源 dl-cdn.alpinelinux.org 的 DNS 返回 IPv6，
# 本机 IPv6 不通时 apk 会死等首连接（假死不超时）；清华源 IPv4 可达且更快
ENV TZ=Asia/Shanghai
RUN sed -i 's#https://dl-cdn.alpinelinux.org#https://mirrors.tuna.tsinghua.edu.cn#g' /etc/apk/repositories \
    && apk add --no-cache supervisor curl tzdata

# Python 依赖（不锁版本，pip 自动适配 Alpine musl 可用 wheel）
# 使用清华 PyPI 镜像避免官方源连接超时；可改回官方源或换其他镜像
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    fastapi \
    uvicorn \
    httpx \
    python-multipart

# 复制源码 + 文档（前端「API 文档」入口读 docs/*.md、README/CHANGELOG）
WORKDIR /app/artifactdepot
COPY src/ /app/artifactdepot/src/
COPY docs/ /app/artifactdepot/docs/
COPY README.md CHANGELOG.md /app/artifactdepot/

# 预留非 root 用户。注意：当前 supervisor 仍以 root 启动；
# 如需真正切换到非 root，必须同时处理 /data/depot 卷权限。
RUN adduser -D -u 10001 artifactdepot

# Supervisor 配置
COPY docker/supervisord.conf /etc/supervisor.d/artifactdepot.ini

# 环境变量（数据目录挂载点）
# 注意：ARTIFACT_DEPOT_DATAHUB_URL 不要在这里预设默认值——config.py 里环境变量优先级
# 高于配置文件，镜像内烤死的 127.0.0.1 会把挂载 config.json 里的 datahub_url 盖掉
# （症状：改了配置文件同步仍报 "All connection attempts failed"）。datahub_url 以
# 容器内配置文件的字段为准；生产用 VOLUME_MAPS 挂载宿主机 config.json 显式填写。
# ARTIFACT_DEPOT_DIR 保留：把数据目录固定到 VOLUME 挂载点 /data/depot。
ENV ARTIFACT_DEPOT_DIR=/data/depot \
    PYTHONPATH=/app/artifactdepot/src

# 数据卷
VOLUME ["/data/depot"]

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD curl -f http://localhost:8004/health || exit 1

CMD ["supervisord", "-c", "/etc/supervisor.d/artifactdepot.ini"]
