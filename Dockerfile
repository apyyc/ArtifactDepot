# ============================================================
# ArtifactDepot 单容器镜像：对象存储仓库站点（FastAPI，8004）
#
# 注意：
#   - Podman 默认 OCI 格式会忽略 HEALTHCHECK；请用 ./build_image.sh 构建
#     （脚本会自动追加 --format docker），或手动 podman build --format docker。
#   - Python 依赖锁定在 requirements.txt；基础镜像钉到 python:3.12-alpine。
#     生产建议进一步钉 digest（FROM ...@sha256:...），避免上游 tag 漂移。
#   - 容器以非 root 用户 artifactdepot(10001) 运行；绑定宿主机目录时由
#     deploy_container.sh 传 --user/--userns=keep-id 保证卷可写。
# ============================================================
FROM docker.io/library/python:3.12-alpine

LABEL org.opencontainers.image.title="ArtifactDepot-ObjectStorage"
LABEL org.opencontainers.image.description="Object storage depot (FastAPI :8004)"
# 版本号唯一源：项目根 VERSION 文件（由 build_image.sh 用 --build-arg 传入，勿在此写死）
ARG APP_VERSION=unknown
LABEL org.opencontainers.image.version="${APP_VERSION}"

# 安装 Supervisor + curl（健康检查用）+ tzdata（容器内统一中国时区）
# 先把 Alpine apk 源换成清华镜像：官方源 dl-cdn.alpinelinux.org 的 DNS 返回 IPv6，
# 本机 IPv6 不通时 apk 会死等首连接（假死不超时）；清华源 IPv4 可达且更快
ENV TZ=Asia/Shanghai
RUN sed -i 's#https://dl-cdn.alpinelinux.org#https://mirrors.tuna.tsinghua.edu.cn#g' /etc/apk/repositories \
    && apk add --no-cache supervisor curl tzdata

# Python 依赖：版本锁定在 requirements.txt，构建可复现
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# 复制源码 + 文档（前端「API 文档」入口读 docs/*.md、README/CHANGELOG）
# 许可证文件必须随镜像分发（Apache-2.0 第 4(a) 条）
WORKDIR /app/artifactdepot
COPY src/ /app/artifactdepot/src/
COPY docs/ /app/artifactdepot/docs/
COPY README.md CHANGELOG.md VERSION /app/artifactdepot/
COPY LICENSE LICENSE.zh-CN.md LICENSE-HEADER /app/artifactdepot/
COPY docker/supervisord.conf /etc/supervisor.d/artifactdepot.ini

# 非 root 运行：创建 artifactdepot(10001)，把代码与数据目录归属它
RUN addgroup -g 10001 artifactdepot \
    && adduser -D -u 10001 -G artifactdepot artifactdepot \
    && mkdir -p /data/depot \
    && chown -R artifactdepot:artifactdepot /app /data/depot
USER artifactdepot

# 环境变量（数据目录挂载点）
# 注意：ARTIFACT_DEPOT_DATAHUB_URL 不要在这里预设默认值——config/__init__.py 里环境变量优先级
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
