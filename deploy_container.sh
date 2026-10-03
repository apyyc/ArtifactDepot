#!/usr/bin/env bash
# ============================================================
# ArtifactDepot 部署脚本：从「已有镜像」创建容器 → 运行 → 配置自启
#
# 前提：镜像已存在（如 localhost/artifactdepot:0.5.5）
#
# 数据卷：默认挂载 ${HOME}/SERVER/artifactdepot/artifactdepot_0.6.1/warehouse
#   → /data/depot；若该默认路径不可写，会自动回退到
#   ${XDG_DATA_HOME:-$HOME/.local/share}/artifactdepot/artifactdepot_0.6.1/warehouse。
#   - 用 --data-dir <路径> 显式指定挂载位置（显式路径不可写会直接报错）
#   - 加 --no-volume 可跳过挂载（用镜像内数据，删除容器即丢失）
#   - 主数据卷始终注入 ARTIFACT_DEPOT_DIR=/data/depot，固定数据落点
#   - meta_dir 默认不注入环境变量，以容器内 config.json 为准；
#     需要覆盖时用 --meta-dir <容器内目录>
#
# 仓库令牌 / DataHub 地址：
#   - token：--token <值> 或环境变量 ARTIFACT_DEPOT_ACCESS_TOKEN
#   - datahub_url：--datahub-url <地址> 或环境变量 ARTIFACT_DEPOT_DATAHUB_URL；
#     不传时**不注入环境变量**，以容器内配置文件的 datahub_url 为准。
#
# 配置文件挂载：
#   - 默认自动探测 ${HOME}/SERVER/artifactdepot/config/config.json
#     或 ${HOME}/SERVER/artifactdepot/<image>_<tag>/config/config.json
#   - 也可用 --config <宿主机配置路径> 显式挂载到容器内 config.json
#   - 未找到配置时使用镜像内 resources/config.json
#
# 用法：
#   ./deploy_container.sh                            # 默认 host 网络 + 自启
#   ./deploy_container.sh --data-dir /path/to/data   # 指定挂载位置
#   ./deploy_container.sh --config ~/dw/config.json  # 挂载外部配置
#   ./deploy_container.sh --token xxx --datahub-url http://ip:8002/api/data
#   ./deploy_container.sh --meta-dir /data/depot/state
#   ./deploy_container.sh --port-map                 # 改用端口映射
#   ./deploy_container.sh --no-systemd               # 只建容器，不配自启
#   ./deploy_container.sh --stop                     # 停止并禁用自启
#   ./deploy_container.sh --rm                       # 删除容器
#
# 自启差异：
#   - podman：生成 systemd --user 服务（默认）
#   - docker：使用 --restart=always（Docker 没有 generate systemd）
#
# 依赖：podman 或 docker（自动检测，可用 FORCE=... 强制指定）
# ============================================================

set -euo pipefail
: "${HOME:?HOME 未设置}"

# ---------- 可配置参数（按需修改） ----------
IMAGE_NAME="artifactdepot"           # 镜像名
IMAGE_TAG="0.7.1"                    # 镜像版本标签
FULL_IMAGE="localhost/${IMAGE_NAME}:${IMAGE_TAG}"
CONTAINER_NAME="artifactdepot"       # 容器名
HOST_PORT="8004"                     # 宿主机映射端口（仅 --port-map 使用）
CONTAINER_PORT="8004"                # 容器内端口
CONTAINER_DATA_PATH="/data/depot"  # 容器内数据目录（由 Dockerfile VOLUME 固定）
CONTAINER_CONFIG_PATH="/app/artifactdepot/src/artifactdepot/resources/config.json"

# 宿主机默认数据目录（按版本隔离）。默认不可写时自动回退到用户数据目录。
DATA_BASE="${HOME}/SERVER/artifactdepot"
DEFAULT_DATA_DIR="${DATA_BASE}/${IMAGE_NAME}_${IMAGE_TAG}/warehouse"
DATA_DIR="$DEFAULT_DATA_DIR"
DATA_DIR_EXPLICIT=false
VOLUME_ENABLED=true

# 容器内状态文件目录（tokens.json / signed_links.json / audit.log）。
# 留空 = 不注入 ARTIFACT_DEPOT_META_DIR，完全以容器内 config.json 的 meta_dir 为准。
META_DIR_CONTAINER=""

# 默认配置文件候选路径：
#   1) 多个版本共用的 ~/SERVER/artifactdepot/config/config.json
#   2) 与数据目录同级的 ~/SERVER/artifactdepot/<image>_<tag>/config/config.json
# 找到第一个存在的文件就自动挂载；也可用 --config 显式覆盖。
DEFAULT_CONFIG_CANDIDATES=(
  "${DATA_BASE}/config/config.json"
  "${DATA_BASE}/${IMAGE_NAME}_${IMAGE_TAG}/config/config.json"
)
VOLUME_MAPS=()
for _cfg in "${DEFAULT_CONFIG_CANDIDATES[@]}"; do
  if [ -f "$_cfg" ]; then
    VOLUME_MAPS=("${_cfg}:${CONTAINER_CONFIG_PATH}")
    break
  fi
done
unset _cfg

# 仓库令牌与 DataHub 地址（可用 --token / --datahub-url 覆盖，也可用环境变量）
ARTIFACT_DEPOT_TOKEN="${ARTIFACT_DEPOT_ACCESS_TOKEN:-}"
DATAHUB_URL="${ARTIFACT_DEPOT_DATAHUB_URL:-}"

SERVICE_NAME="container-${CONTAINER_NAME}"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"

usage() {
  cat <<'USAGE'
用法: ./deploy_container.sh [选项]

选项:
  --data-dir <路径>      宿主机数据目录（挂载到容器 /data/depot）
  --config <路径>        宿主机 config.json（挂载到容器 resources/config.json）
  --meta-dir <路径>      容器内 meta_dir；不传则不注入 ARTIFACT_DEPOT_META_DIR
  --token <值>           注入 ARTIFACT_DEPOT_ACCESS_TOKEN
  --datahub-url <地址>   注入 ARTIFACT_DEPOT_DATAHUB_URL
  --no-volume            不挂载数据卷（不推荐，数据随容器删除）
  --port-map             使用端口映射（默认 host 网络）
  --network-host         使用 host 网络（默认）
  --no-systemd           不配置自启（Podman 不生成 systemd；Docker 不加 --restart）
  --stop                 停止容器并禁用自启
  --rm                   删除容器（数据卷保留）
  -h, --help             显示本帮助

环境变量:
  FORCE=podman|docker           强制指定容器工具
  ARTIFACT_DEPOT_ACCESS_TOKEN        默认 token
  ARTIFACT_DEPOT_DATAHUB_URL         默认 DataHub 地址
  XDG_DATA_HOME                 默认数据目录回退根
USAGE
}

# 检查某个路径（或其最近已存在父目录）是否可写。
can_write_path() {
  local p="$1"
  [ -n "$p" ] || return 1
  while [ ! -e "$p" ]; do
    local parent
    parent="$(dirname "$p")"
    [ "$parent" = "$p" ] && break
    p="$parent"
  done
  [ -w "$p" ]
}

# 取绝对路径；路径不存在也返回可用的绝对路径。
make_absolute() {
  local p="$1"
  local abs=""
  if command -v realpath >/dev/null 2>&1; then
    abs="$(realpath -m -- "$p" 2>/dev/null || true)"
  fi
  [ -n "$abs" ] && printf '%s\n' "$abs" || printf '%s\n' "$p"
}

# ---------- 参数解析 ----------
DO_SYSTEMD=true
DO_STOP=false
DO_RM=false
DO_HOST_NETWORK=true
CONFIG_HOST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-systemd) DO_SYSTEMD=false ;;
    --no-volume)  VOLUME_ENABLED=false ;;
    --token)
      shift
      if [ $# -eq 0 ]; then echo "❌ --token 需要参数" >&2; exit 1; fi
      ARTIFACT_DEPOT_TOKEN="$1"
      ;;
    --datahub-url)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "❌ --datahub-url 需要参数" >&2; exit 1; fi
      DATAHUB_URL="$1"
      ;;
    --data-dir)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "❌ --data-dir 需要参数" >&2; exit 1; fi
      DATA_DIR="$1"
      DATA_DIR_EXPLICIT=true
      ;;
    --config)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "❌ --config 需要参数" >&2; exit 1; fi
      CONFIG_HOST="$1"
      ;;
    --meta-dir)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "❌ --meta-dir 需要参数" >&2; exit 1; fi
      META_DIR_CONTAINER="$1"
      ;;
    --network-host) DO_HOST_NETWORK=true ;;
    --port-map)     DO_HOST_NETWORK=false ;;
    --stop)         DO_STOP=true ;;
    --rm)           DO_RM=true ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

# 显式配置优先；默认自动探测的配置文件映射会被替换。
if [ -n "$CONFIG_HOST" ]; then
  if [ ! -f "$CONFIG_HOST" ]; then
    echo "❌ 配置文件不存在或不是普通文件: $CONFIG_HOST" >&2
    exit 1
  fi
  CONFIG_HOST="$(make_absolute "$CONFIG_HOST")"
  VOLUME_MAPS=("${CONFIG_HOST}:${CONTAINER_CONFIG_PATH}")
fi

# 显式数据目录转换为绝对路径，避免 podman/docker -v 对相对路径处理不一致。
if [ "$DATA_DIR_EXPLICIT" = true ]; then
  DATA_DIR="$(make_absolute "$DATA_DIR")"
fi

# 默认数据目录不可写时回退到用户数据目录，保证开箱即用。
# stop/rm 不需要创建数据目录，跳过预检。
if [ "$VOLUME_ENABLED" = true ] && [ "$DO_STOP" = false ] && [ "$DO_RM" = false ]; then
  if [ "$DATA_DIR_EXPLICIT" = false ] && ! can_write_path "$DATA_DIR"; then
    FALLBACK_DATA_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/artifactdepot/${IMAGE_NAME}_${IMAGE_TAG}/warehouse"
    FALLBACK_DATA_DIR="$(make_absolute "$FALLBACK_DATA_DIR")"
    echo "⚠️  默认数据目录不可写: $DATA_DIR" >&2
    echo "   自动回退到: $FALLBACK_DATA_DIR" >&2
    echo "   如仍想使用原路径，请先创建并赋权，或用 --data-dir 显式指定。" >&2
    DATA_DIR="$FALLBACK_DATA_DIR"
  fi
  if ! can_write_path "$DATA_DIR"; then
    echo "❌ 数据目录不可写: $DATA_DIR" >&2
    echo "   请创建可写目录，或用 --data-dir 指定其他路径。" >&2
    exit 1
  fi
fi

# ---------- 检测容器工具 ----------
if [ -n "${FORCE:-}" ]; then
  TOOL="$FORCE"
  if ! command -v "$TOOL" >/dev/null 2>&1; then
    echo "❌ FORCE 指定的容器工具不存在: $TOOL" >&2
    exit 1
  fi
elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
  TOOL="podman"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  TOOL="docker"
else
  echo "❌ 未检测到可用的 podman 或 docker" >&2
  exit 1
fi

IS_PODMAN=false
[ "$TOOL" = "podman" ] && IS_PODMAN=true
AUTO_RESTART=false

# Docker 没有 podman generate systemd；自动改用 Docker 原生 --restart=always。
if [ "$DO_SYSTEMD" = true ] && [ "$IS_PODMAN" = false ] && [ "$DO_STOP" = false ] && [ "$DO_RM" = false ]; then
  AUTO_RESTART=true
  DO_SYSTEMD=false
  echo "ℹ️  当前工具为 $TOOL，不使用 systemd；改用 --restart=always 配置自启。" >&2
fi

echo "🔧 使用容器工具: $TOOL"
echo "👤 当前用户: $(whoami)   HOME: ${HOME}"
if [ "$VOLUME_ENABLED" = true ] && [ "$DO_STOP" = false ] && [ "$DO_RM" = false ]; then
  echo "📁 挂载数据目录: $DATA_DIR"
fi

# ---------- 停止/删除模式 ----------
if [ "$DO_STOP" = true ] && [ "$DO_RM" = false ]; then
  if [ "$IS_PODMAN" = true ]; then
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
  else
    "$TOOL" update --restart=no "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  "$TOOL" stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if [ "$IS_PODMAN" = true ] && ! "$TOOL" container exists "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "✅ 已停止。systemd --new 模式已清理容器（数据卷保留）"
  else
    echo "✅ 已停止。容器仍在（$TOOL ps -a 可见），自启已禁用"
  fi
  exit 0
fi
if [ "$DO_RM" = true ]; then
  if [ "$IS_PODMAN" = true ]; then
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
  else
    "$TOOL" update --restart=no "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  "$TOOL" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if [ "$IS_PODMAN" = true ]; then
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  echo "✅ 容器已删除（宿主机数据卷未删除，仍保留在挂载来源路径）"
  exit 0
fi

# ---------- 1. 检查镜像 ----------
echo ""
echo "🔍 [1/5] 检查镜像 $FULL_IMAGE ..."
# image inspect 同时适用于 podman 和 docker；docker 没有 image exists 子命令。
if ! "$TOOL" image inspect "$FULL_IMAGE" >/dev/null 2>&1; then
  echo "❌ 镜像 $FULL_IMAGE 不存在！请先构建："
  echo "   ./build_image.sh"
  echo "   或 $TOOL build -t $FULL_IMAGE ."
  exit 1
fi
echo "✅ 镜像存在"

# ---------- 2. 清理同名旧容器 ----------
echo ""
echo "🧹 [2/5] 清理同名旧容器 $CONTAINER_NAME ..."
if "$TOOL" rm -f "$CONTAINER_NAME" >/dev/null 2>&1; then
  echo "   已删除旧容器"
else
  echo "   无旧容器"
fi

# ---------- 3. 数据卷 + 创建容器 ----------
echo ""
echo "🚀 [3/5] 创建容器 $CONTAINER_NAME ..."
VOLUME_ARGS=()
ENV_ARGS=()
if [ "$VOLUME_ENABLED" = true ]; then
  mkdir -p "$DATA_DIR"
  VOLUME_ARGS=(-v "${DATA_DIR}:${CONTAINER_DATA_PATH}")
  echo "   挂载: ${DATA_DIR} → ${CONTAINER_DATA_PATH}"
  # 固定应用数据根到卷挂载点。
  ENV_ARGS+=(-e "ARTIFACT_DEPOT_DIR=${CONTAINER_DATA_PATH}")
  echo "   ARTIFACT_DEPOT_DIR=${CONTAINER_DATA_PATH}"
else
  echo "   未挂载卷，使用镜像内数据（删除容器数据即丢失）"
fi
# meta_dir 不强制覆盖，保留 config.json 语义；只有 --meta-dir 显式指定才注入。
if [ -n "$META_DIR_CONTAINER" ]; then
  ENV_ARGS+=(-e "ARTIFACT_DEPOT_META_DIR=${META_DIR_CONTAINER}")
  echo "   ARTIFACT_DEPOT_META_DIR=${META_DIR_CONTAINER}（显式注入，覆盖 config.json）"
else
  echo "   ARTIFACT_DEPOT_META_DIR 未注入 → meta_dir 以容器内 config.json 为准"
fi

# 附加卷映射（配置文件等）
for m in "${VOLUME_MAPS[@]}"; do
  [ -n "$m" ] || continue
  host="${m%%:*}"
  if [ ! -e "$host" ]; then
    echo "   ⚠️  跳过附加卷：宿主机路径不存在: $host（请先创建文件/目录）"
    continue
  fi
  VOLUME_ARGS+=(-v "$m")
  echo "   📄 附加卷: $m"
done
if [ ${#VOLUME_MAPS[@]} -eq 0 ]; then
  echo "   ℹ️  未配置外部 config.json，使用镜像内 resources/config.json"
fi

if [ -n "$ARTIFACT_DEPOT_TOKEN" ]; then
  ENV_ARGS+=(-e "ARTIFACT_DEPOT_ACCESS_TOKEN=$ARTIFACT_DEPOT_TOKEN")
  echo "   token: 已设置"
else
  echo "   ⚠️  未通过 --token / ARTIFACT_DEPOT_ACCESS_TOKEN 注入 token；若 config.json 也无 access_token，写操作会 401"
fi
if [ -n "$DATAHUB_URL" ]; then
  ENV_ARGS+=(-e "ARTIFACT_DEPOT_DATAHUB_URL=$DATAHUB_URL")
  echo "   datahub_url: $DATAHUB_URL（显式注入环境变量）"
else
  echo "   datahub_url: 未显式指定 → 以容器内配置文件（config.json）的 datahub_url 为准"
fi

# 网络模式：host 网络下不映射端口，容器共享宿主网络。
NETWORK_ARGS=()
PORT_ARGS=()
if [ "$DO_HOST_NETWORK" = true ]; then
  NETWORK_ARGS+=(--network=host)
  echo "   🌐 网络: host（共享宿主网络，不再映射端口；datahub_url 建议 http://127.0.0.1:8002/api/data）"
else
  PORT_ARGS+=(-p "${HOST_PORT}:${CONTAINER_PORT}")
  echo "   🌐 网络: 端口映射 ${HOST_PORT}:${CONTAINER_PORT}"
fi

# Docker 自动重启参数
RESTART_ARGS=()
if [ "$AUTO_RESTART" = true ]; then
  RESTART_ARGS+=(--restart=always)
fi

CONTAINER_ID="$("$TOOL" run -d \
  --name "$CONTAINER_NAME" \
  "${RESTART_ARGS[@]}" \
  "${NETWORK_ARGS[@]}" \
  "${PORT_ARGS[@]}" \
  "${ENV_ARGS[@]}" \
  "${VOLUME_ARGS[@]}" \
  "$FULL_IMAGE")"
echo "   容器 ID: ${CONTAINER_ID:0:12}"
if [ "$DO_HOST_NETWORK" = true ]; then
  echo "✅ 容器已创建: $CONTAINER_NAME  （host 网络，端口 ${CONTAINER_PORT} 即宿主端口）"
else
  echo "✅ 容器已创建: $CONTAINER_NAME  端口映射: ${PORT_ARGS[*]}"
fi

# ---------- 4. 配置自启 ----------
if [ "$DO_SYSTEMD" = true ]; then
  echo ""
  echo "⚙️   [4/5] 生成 systemd 开机自启服务 ..."
  mkdir -p "$SYSTEMD_DIR"
  # podman 5.x 的 --files 把 .service 生成到当前目录，需先 cd 到 systemd user 目录
  ( cd "$SYSTEMD_DIR" && "$TOOL" generate systemd \
      --name "$CONTAINER_NAME" \
      --new --files --restart-policy=always )
  if [ ! -f "$SERVICE_FILE" ]; then
    echo "❌ 未找到生成的服务文件: $SERVICE_FILE" >&2
    ls -la "$SYSTEMD_DIR" || true
    exit 1
  fi
  chmod 600 "$SERVICE_FILE" 2>/dev/null || true
  echo "✅ 服务文件已生成: $SERVICE_FILE"
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_NAME"
  # --new 模式启动时会重新创建容器，删掉第 3 步手动建的，避免同名冲突
  echo "   删除手动容器，交由 systemd 重新创建 ..."
  "$TOOL" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  systemctl --user start "$SERVICE_NAME"
  echo "✅ 自启服务已启用并启动: $SERVICE_NAME"
elif [ "$AUTO_RESTART" = true ]; then
  echo ""
  echo "⚙️   [4/5] Docker 已配置 --restart=always（容器随 Docker 服务自动启动）"
else
  echo ""
  echo "⏭️   [4/5] 未配置开机自启（--no-systemd）"
fi

# ---------- 5. 检查结果 ----------
echo ""
echo "✅ [5/5] 当前状态："
"$TOOL" ps --filter "name=$CONTAINER_NAME"
if [ "$DO_SYSTEMD" = true ]; then
  systemctl --user status "$SERVICE_NAME" --no-pager 2>/dev/null | head -4 || true
elif [ "$AUTO_RESTART" = true ]; then
  echo "   Docker 重启策略: $("$TOOL" inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER_NAME" 2>/dev/null || echo unknown)"
fi

echo ""
echo "🎉 完成。"
echo "──────────────────────────────────────────"
if [ "$DO_SYSTEMD" = true ]; then
  echo "🔒 若希望『不登录也能后台运行』，请执行："
  echo "   sudo loginctl enable-linger $(whoami)"
  echo "──────────────────────────────────────────"
  echo "查看/取消自启："
  echo "   systemctl --user status $SERVICE_NAME   # 查看"
  echo "   systemctl --user stop $SERVICE_NAME     # 停止"
  echo "   systemctl --user disable $SERVICE_NAME  # 取消自启"
elif [ "$AUTO_RESTART" = true ]; then
  echo "🔒 Docker 容器已随 Docker 服务自动启动。"
  echo "──────────────────────────────────────────"
  echo "查看/取消自启："
  echo "   docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' $CONTAINER_NAME"
  echo "   docker update --restart=no $CONTAINER_NAME"
  echo "   docker stop $CONTAINER_NAME"
else
  echo "ℹ️  未配置开机自启。"
fi
echo "   ./deploy_container.sh --stop   # 停止+禁用自启"
echo "   ./deploy_container.sh --rm     # 删除容器+服务（数据卷保留）"
