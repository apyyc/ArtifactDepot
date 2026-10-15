#!/usr/bin/env bash
# ============================================================
# ArtifactDepot 部署脚本：从「已有镜像」创建容器 → 运行 → 配置自启
#
# 前提：镜像已存在（如 localhost/artifactdepot:0.5.5）
#
# 挂载布局：三卷分离，均不随镜像版本变化，升级镜像后数据与配置原地沿用。
#   <部署根>/config/  ->  /data/config   配置目录（config.json + .env）
#   <部署根>/data/    ->  /data/depot    业务数据（bucket 与对象）
#   <部署根>/state/   ->  /data/state    状态文件（tokens.json / signed_links.json / audit.log）
#   默认部署根 ${HOME}/SERVER/artifactdepot；不可写时自动回退
#   ${XDG_DATA_HOME:-$HOME/.local/share}/artifactdepot。
#   - 用 --deploy-root 改基目录，或 --data-dir/--state-dir/--config-dir 单独指定
#   - 首次运行会用 src/artifactdepot/config/config.production.json 播种
#     <部署根>/config/config.json，之后你可以直接编辑该文件
#   - 加 --no-volume 可跳过全部挂载（用镜像内数据，删除容器即丢失，仅调试用）
#   - meta_dir 默认指向状态卷 /data/state；可用 --meta-dir 显式覆盖
#
# 仓库令牌 / DataHub 地址：
#   - token：--token <值> 或环境变量 ARTIFACT_DEPOT_ACCESS_TOKEN
#   - datahub_url：--datahub-url <地址> 或环境变量 ARTIFACT_DEPOT_DATAHUB_URL；
#     不传时**不注入环境变量**，以配置文件里的 datahub_url 为准。
#
# 用法：
#   ./deploy_container.sh                            # 默认 host 网络 + 自启
#   ./deploy_container.sh --deploy-root /srv/artifactdepot   # 改部署根（三卷基目录）
#   ./deploy_container.sh --data-dir /data/ad-data           # 单独指定业务数据目录
#   ./deploy_container.sh --config-dir /etc/artifactdepot    # 单独指定配置目录
#   ./deploy_container.sh --config ~/ad-config.json          # 只挂单个 config.json
#   ./deploy_container.sh --token xxx --datahub-url http://ip:8002/api/data
#   ./deploy_container.sh --meta-dir /data/state             # 显式覆盖 meta_dir
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

# 退出时清理临时 env-file（token），避免残留
ENV_FILE=""
cleanup_env_file() {
  [ -n "${ENV_FILE:-}" ] && [ -f "$ENV_FILE" ] && rm -f "$ENV_FILE"
  return 0
}
trap cleanup_env_file EXIT INT TERM

# ---------- 可配置参数（按需修改） ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 版本唯一源：项目根 VERSION 文件；可用环境变量 IMAGE_TAG 覆盖
_resolve_image_tag() {
  if [ -n "${IMAGE_TAG:-}" ]; then printf '%s' "$IMAGE_TAG"; return; fi
  if [ -f "$SCRIPT_DIR/VERSION" ]; then
    _v="$(grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?' "$SCRIPT_DIR/VERSION" 2>/dev/null | head -1)"
    if [ -n "$_v" ]; then printf '%s' "$_v"; return; fi
  fi
  printf '0.7.4'   # 兜底：VERSION 缺失时使用上次已知版本
}

IMAGE_NAME="artifactdepot"           # 镜像名
IMAGE_TAG="$(_resolve_image_tag)"    # 镜像版本标签（取自 VERSION 文件）
FULL_IMAGE="localhost/${IMAGE_NAME}:${IMAGE_TAG}"
CONTAINER_NAME="artifactdepot"       # 容器名
HOST_PORT="8004"                     # 宿主机映射端口（仅 --port-map 使用）
CONTAINER_PORT="8004"                # 容器内端口
CONTAINER_DATA_PATH="/data/depot"  # 容器内数据目录（由 Dockerfile VOLUME 固定）

# 宿主机部署根目录：不随镜像版本变化，升级后数据与配置原地沿用。
# 布局（三个挂载点，各自可独立备份/迁移/清理）：
#   <DEPLOY_ROOT>/config/   配置目录  -> 容器 /data/config （config.json + .env）
#   <DEPLOY_ROOT>/data/     业务数据  -> 容器 /data/depot
#   <DEPLOY_ROOT>/state/    状态文件  -> 容器 /data/state （tokens/signed_links/audit）
# 改基目录：--deploy-root <路径> 或 DEPLOY_ROOT=<路径>
# 单独覆盖：--data-dir / --state-dir / --config-dir
DATA_BASE="${HOME}/SERVER/artifactdepot"
DEPLOY_ROOT="${DEPLOY_ROOT:-$DATA_BASE}"
# 三个目录在参数解析后统一派生（这样 --deploy-root 才能生效）：
#   显式 --data-dir/--state-dir/--config-dir 优先，否则取 <部署根>/{data,state,config}
DATA_DIR=""
STATE_DIR=""
CONFIG_DIR=""
DATA_DIR_EXPLICIT=false
STATE_DIR_EXPLICIT=false
CONFIG_DIR_EXPLICIT=false
DEPLOY_ROOT_EXPLICIT=false
VOLUME_ENABLED=true

# 容器内固定路径（与镜像内代码约定一致，不随版本变化）
CONTAINER_STATE_PATH="/data/state"
CONTAINER_CONFIG_DIR="/data/config"
CONTAINER_CONFIG_FILE="${CONTAINER_CONFIG_DIR}/config.json"
CONTAINER_ENV_FILE="${CONTAINER_CONFIG_DIR}/.env"

# 容器内原本的配置目录（用于「无外部配置时」沿用镜像内配置）
CONTAINER_CONFIG_IN_IMAGE_FALLBACK="/app/artifactdepot/src/artifactdepot/config/config.json"

# 容器内 meta_dir 覆盖值；留空 = 用上面状态卷的默认值 /data/state
META_DIR_CONTAINER=""

# 允许用环境变量 VOLUME_MAPS 传入额外挂载（空格分隔的 host:container 列表）
USER_VOLUME_MAPS="${VOLUME_MAPS:-}"
VOLUME_MAPS=()

# 仓库令牌与 DataHub 地址（可用 --token / --datahub-url 覆盖，也可用环境变量）
ARTIFACT_DEPOT_TOKEN="${ARTIFACT_DEPOT_ACCESS_TOKEN:-}"
DATAHUB_URL="${ARTIFACT_DEPOT_DATAHUB_URL:-}"

SERVICE_NAME="container-${CONTAINER_NAME}"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"

usage() {
  cat <<'USAGE'
用法: ./deploy_container.sh [选项]

挂载布局（三卷分离，升级镜像原地沿用，不随版本变化）:
  <部署根>/config/  ->  /data/config   配置目录（config.json + .env）
  <部署根>/data/    ->  /data/depot    业务数据（bucket 与对象）
  <部署根>/state/   ->  /data/state    状态文件（tokens.json / signed_links.json / audit.log）
  默认部署根: ~/SERVER/artifactdepot（不可写时自动回退到 ~/.local/share/artifactdepot）

选项:
  --deploy-root <路径>   部署根目录（等价于同时改上面三个子目录的基目录）
  --data-dir <路径>      业务数据目录
  --state-dir <路径>     状态文件目录
  --config-dir <路径>    配置目录（首次运行会用生产模板播种 config.json）
  --config <文件>        只挂载单个 config.json（覆盖 --config-dir）
  --meta-dir <容器内路径> 显式覆盖容器内 meta_dir
  --token <值>           注入 ARTIFACT_DEPOT_ACCESS_TOKEN（经临时 env-file，不出现在命令行）
  --datahub-url <地址>   注入 ARTIFACT_DEPOT_DATAHUB_URL
  --no-volume            不挂载任何卷（数据随容器删除，仅调试用）
  --port-map             使用端口映射（默认 host 网络）
  --network-host         使用 host 网络（默认）
  --no-systemd           不配置自启（Podman 不生成 systemd；Docker 不加 --restart）
  --stop                 停止容器并禁用自启
  --rm                   删除容器（宿主机数据保留；与 --stop 同给时以 --rm 为准）
  -h, --help             显示本帮助

环境变量:
  DEPLOY_ROOT                   部署根目录（等价 --deploy-root）
  IMAGE_TAG                     覆盖镜像版本标签（默认读项目根 VERSION 文件）
  VOLUME_MAPS                   额外挂载，空格分隔的 host:container 列表
  FORCE=podman|docker           强制指定容器工具
  ARTIFACT_DEPOT_ACCESS_TOKEN   默认 token
  ARTIFACT_DEPOT_DATAHUB_URL    默认 DataHub 地址
  XDG_DATA_HOME                 默认目录回退根
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
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --token 需要参数" >&2; exit 1; fi
      ARTIFACT_DEPOT_TOKEN="$1"
      ;;
    --datahub-url)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --datahub-url 需要参数" >&2; exit 1; fi
      DATAHUB_URL="$1"
      ;;
    --data-dir)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --data-dir 需要参数" >&2; exit 1; fi
      DATA_DIR="$1"
      DATA_DIR_EXPLICIT=true
      ;;
    --deploy-root)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --deploy-root 需要参数" >&2; exit 1; fi
      DEPLOY_ROOT="$1"
      DEPLOY_ROOT_EXPLICIT=true
      ;;
    --state-dir)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --state-dir 需要参数" >&2; exit 1; fi
      STATE_DIR="$1"
      STATE_DIR_EXPLICIT=true
      ;;
    --config-dir)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --config-dir 需要参数" >&2; exit 1; fi
      CONFIG_DIR="$1"
      CONFIG_DIR_EXPLICIT=true
      ;;
    --config)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --config 需要参数" >&2; exit 1; fi
      CONFIG_HOST="$1"
      ;;
    --meta-dir)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then echo "ERROR: --meta-dir 需要参数" >&2; exit 1; fi
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

# 部署根转绝对路径，并派生三个挂载目录（显式指定的优先）
DEPLOY_ROOT="$(make_absolute "$DEPLOY_ROOT")"
if [ -z "$DATA_DIR" ]; then
  DATA_DIR="$DEPLOY_ROOT/data"
  DATA_DIR_EXPLICIT="$DEPLOY_ROOT_EXPLICIT"
fi
if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$DEPLOY_ROOT/state"
  STATE_DIR_EXPLICIT="$DEPLOY_ROOT_EXPLICIT"
fi
if [ -z "$CONFIG_DIR" ]; then
  CONFIG_DIR="$DEPLOY_ROOT/config"
  CONFIG_DIR_EXPLICIT="$DEPLOY_ROOT_EXPLICIT"
fi

# --stop 与 --rm 同时给出时以 --rm 为准（下面分支顺序如此），这里显式提示避免误解
if [ "$DO_STOP" = true ] && [ "$DO_RM" = true ]; then
  echo "INFO: --stop 与 --rm 同时指定，按 --rm 处理（删除容器；宿主机数据卷保留）" >&2
fi

# 归一化三个挂载目录为绝对路径（podman/docker 对相对路径处理不一致）
[ "$DATA_DIR_EXPLICIT"   = true ] && DATA_DIR="$(make_absolute "$DATA_DIR")"
[ "$STATE_DIR_EXPLICIT"  = true ] && STATE_DIR="$(make_absolute "$STATE_DIR")"
[ "$CONFIG_DIR_EXPLICIT" = true ] && CONFIG_DIR="$(make_absolute "$CONFIG_DIR")"

# 显式 --config <文件>：只挂这一个文件，覆盖 config 目录整挂
if [ -n "$CONFIG_HOST" ]; then
  if [ ! -f "$CONFIG_HOST" ]; then
    echo "ERROR: 配置文件不存在或不是普通文件: $CONFIG_HOST" >&2
    exit 1
  fi
  CONFIG_HOST="$(make_absolute "$CONFIG_HOST")"
fi

# 目录可写性预检与自动回退（stop/rm 无需碰数据目录，跳过）
#   规则：自动推导的默认路径不可写 -> 回退到 ~/.local/share/artifactdepot/<dir>；
#         显式指定的路径不可写 -> 直接报错（避免偷偷换掉你指定的位置）。
if [ "$VOLUME_ENABLED" = true ] && [ "$DO_STOP" = false ] && [ "$DO_RM" = false ]; then
  _fallback_root="${XDG_DATA_HOME:-${HOME}/.local/share}/artifactdepot"
  _resolve_dir() {
    local var_name="$1" explicit="$2" fallback_sub="$3"
    local cur="${!var_name}"
    if [ "$explicit" = false ] && ! can_write_path "$cur"; then
      local fb="${_fallback_root}/${fallback_sub}"
      fb="$(make_absolute "$fb")"
      echo "WARN: 默认目录不可写: $cur" >&2
      echo "      自动回退到: $fb" >&2
      printf -v "$var_name" '%s' "$fb"
      cur="$fb"
    fi
    if ! can_write_path "$cur"; then
      echo "ERROR: 目录不可写: $cur" >&2
      echo "       请创建可写目录，或用对应 --*-dir 参数指定其他路径。" >&2
      exit 1
    fi
  }
  _resolve_dir DATA_DIR   "$DATA_DIR_EXPLICIT"   "data"
  _resolve_dir STATE_DIR  "$STATE_DIR_EXPLICIT"  "state"
  if [ -z "$CONFIG_HOST" ]; then
    _resolve_dir CONFIG_DIR "$CONFIG_DIR_EXPLICIT" "config"
  fi
  unset -f _resolve_dir
fi

# 旧版本布局迁移提示：早期版本把数据放在 <base>/artifactdepot_<版本>/warehouse，
# 且状态文件混在数据目录里。这里只提示、不搬动数据。
if [ "$VOLUME_ENABLED" = true ] && [ "$DO_STOP" = false ] && [ "$DO_RM" = false ]; then
  _legacy=()
  for _d in "${DATA_BASE}/${IMAGE_NAME}_"*/warehouse \
            "${XDG_DATA_HOME:-${HOME}/.local/share}/artifactdepot/${IMAGE_NAME}_"*/warehouse; do
    [ -d "$_d" ] || continue
    [ "$_d" = "$DATA_DIR" ] && continue
    if [ -n "$(find "$_d" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
      _legacy+=("$_d")
    fi
  done
  if [ ${#_legacy[@]} -gt 0 ]; then
    echo "WARN: 检测到旧版本布局的数据目录（新版默认使用 $DATA_DIR）:" >&2
    for _d in "${_legacy[@]}"; do
      echo "        $_d  ($(du -sh "$_d" 2>/dev/null | cut -f1))" >&2
    done
    echo "      迁移方式（示例，执行前请确认路径）：" >&2
    echo "        mv <旧目录>/* $DATA_DIR/            # 业务数据" >&2
    echo "        mv <旧目录>/tokens.json <旧目录>/signed_links.json <旧目录>/audit.log* $STATE_DIR/   # 状态文件" >&2
    echo "      或直接沿用旧路径：--data-dir <旧目录> --state-dir <旧目录>" >&2
  fi
  unset _d
fi

# ---------- 检测容器工具 ----------
if [ -n "${FORCE:-}" ]; then
  TOOL="$FORCE"
  if ! command -v "$TOOL" >/dev/null 2>&1; then
    echo "ERROR: FORCE 指定的容器工具不存在: $TOOL" >&2
    exit 1
  fi
elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
  TOOL="podman"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  TOOL="docker"
else
  echo "ERROR: 未检测到可用的 podman 或 docker" >&2
  exit 1
fi

IS_PODMAN=false
[ "$TOOL" = "podman" ] && IS_PODMAN=true
AUTO_RESTART=false

# Docker 没有 podman generate systemd；自动改用 Docker 原生 --restart=always。
if [ "$DO_SYSTEMD" = true ] && [ "$IS_PODMAN" = false ] && [ "$DO_STOP" = false ] && [ "$DO_RM" = false ]; then
  AUTO_RESTART=true
  DO_SYSTEMD=false
  echo "INFO: 当前工具为 $TOOL，不使用 systemd；改用 --restart=always 配置自启。" >&2
fi

echo "使用容器工具: $TOOL"
echo "当前用户: $(whoami)   HOME: ${HOME}"
if [ "$VOLUME_ENABLED" = true ] && [ "$DO_STOP" = false ] && [ "$DO_RM" = false ]; then
  echo "挂载数据目录: $DATA_DIR"
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
    echo "OK: 已停止。systemd --new 模式已清理容器（数据卷保留）"
  else
    echo "OK: 已停止。容器仍在（$TOOL ps -a 可见），自启已禁用"
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
  echo "OK: 容器已删除（宿主机数据卷未删除，仍保留在挂载来源路径）"
  exit 0
fi

# ---------- 1. 检查镜像 ----------
echo ""
echo "[1/5] 检查镜像 $FULL_IMAGE ..."
# image inspect 同时适用于 podman 和 docker；docker 没有 image exists 子命令。
if ! "$TOOL" image inspect "$FULL_IMAGE" >/dev/null 2>&1; then
  echo "ERROR: 镜像 $FULL_IMAGE 不存在！请先构建："
  echo "   ./build_image.sh"
  echo "  或 $TOOL build -t $FULL_IMAGE ."
  exit 1
fi
echo "OK: 镜像存在"

# ---------- 2. 清理同名旧容器 ----------
echo ""
echo "[2/5] 清理同名旧容器 $CONTAINER_NAME ..."
if "$TOOL" rm -f "$CONTAINER_NAME" >/dev/null 2>&1; then
  echo "   已删除旧容器"
else
  echo "   无旧容器"
fi

# ---------- 3. 挂载卷 + 创建容器 ----------
echo ""
echo "[3/5] 创建容器 $CONTAINER_NAME ..."
VOLUME_ARGS=()
ENV_ARGS=()

# 三个标准挂载点：数据 / 状态 / 配置，各自独立，升级镜像原地沿用
if [ "$VOLUME_ENABLED" = true ]; then
  mkdir -p "$DATA_DIR" "$STATE_DIR"
  VOLUME_ARGS+=(-v "${DATA_DIR}:${CONTAINER_DATA_PATH}")
  VOLUME_ARGS+=(-v "${STATE_DIR}:${CONTAINER_STATE_PATH}")
  echo "   数据卷: ${DATA_DIR} -> ${CONTAINER_DATA_PATH}"
  echo "   状态卷: ${STATE_DIR} -> ${CONTAINER_STATE_PATH}"
  ENV_ARGS+=(-e "ARTIFACT_DEPOT_DIR=${CONTAINER_DATA_PATH}")
  # meta_dir：未用 --meta-dir 显式覆盖时，默认指向状态卷
  if [ -z "$META_DIR_CONTAINER" ]; then
    ENV_ARGS+=(-e "ARTIFACT_DEPOT_META_DIR=${CONTAINER_STATE_PATH}")
  fi

  # 配置卷：整目录挂载，config.json 与 .env 都在里面
  if [ -n "$CONFIG_HOST" ]; then
    # --config 显式指定单个文件：只挂该文件，不挂整个目录
    VOLUME_ARGS+=(-v "${CONFIG_HOST}:${CONTAINER_CONFIG_FILE}")
    echo "   配置卷: ${CONFIG_HOST} -> ${CONTAINER_CONFIG_FILE}"
    ENV_ARGS+=(-e "ARTIFACT_DEPOT_CONFIG=${CONTAINER_CONFIG_FILE}")
    echo "   注意: 单文件模式不挂载 .env；敏感项请用 --token / --datahub-url 或环境变量注入"
  else
    mkdir -p "$CONFIG_DIR"
    if [ ! -f "${CONFIG_DIR}/config.json" ]; then
      # 首次运行：用仓库内的生产模板播种一份，用户可直接编辑
      _seed="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/src/artifactdepot/config/config.production.json"
      if [ -f "$_seed" ]; then
        cp "$_seed" "${CONFIG_DIR}/config.json"
        echo "   已用生产模板初始化配置: ${CONFIG_DIR}/config.json（请按需修改）"
      else
        : > "${CONFIG_DIR}/config.json"
        echo "   WARN: 未找到生产模板，已创建空配置: ${CONFIG_DIR}/config.json"
      fi
    fi
    VOLUME_ARGS+=(-v "${CONFIG_DIR}:${CONTAINER_CONFIG_DIR}")
    echo "   配置卷: ${CONFIG_DIR} -> ${CONTAINER_CONFIG_DIR}"
    # 始终注入，便于日后往配置目录里补 .env 后重启即生效
    ENV_ARGS+=(-e "ARTIFACT_DEPOT_CONFIG=${CONTAINER_CONFIG_FILE}")
    ENV_ARGS+=(-e "ARTIFACT_DEPOT_ENV=${CONTAINER_ENV_FILE}")
  fi
else
  echo "   未挂载卷，使用镜像内数据（删除容器数据即丢失）"
fi

# meta_dir 显式覆盖（--meta-dir）
if [ -n "$META_DIR_CONTAINER" ]; then
  ENV_ARGS+=(-e "ARTIFACT_DEPOT_META_DIR=${META_DIR_CONTAINER}")
  echo "   ARTIFACT_DEPOT_META_DIR=${META_DIR_CONTAINER}（--meta-dir 显式覆盖状态卷默认值）"
else
  echo "   ARTIFACT_DEPOT_META_DIR=${CONTAINER_STATE_PATH}（状态卷）"
fi

# 附加卷映射（环境变量 VOLUME_MAPS，空格分隔的 host:container）
# 解析规则：按「最后一个冒号」分隔，兼容宿主机路径含冒号的少见情况；
# 容器侧路径必须是绝对路径，否则跳过并说明原因。
add_volume_map() {
  local m="$1" host="" cont="" reason=""
  if [[ "$m" == *:* ]]; then
    host="${m%:*}"
    cont="${m##*:}"
  else
    host="$m"; cont=""
  fi
  if [ -z "$host" ]; then
    reason="格式应为 host:container"
  elif [ -z "$cont" ]; then
    reason="缺少容器侧路径（格式 host:container）"
  elif [[ "$cont" != /* ]]; then
    reason="容器侧路径必须是绝对路径: $cont"
  elif [ ! -e "$host" ]; then
    reason="宿主机路径不存在: $host（请先创建文件/目录）"
  fi
  if [ -n "$reason" ]; then
    echo "   WARN: 跳过附加卷 [$m]：$reason" >&2
    return 0
  fi
  VOLUME_ARGS+=(-v "${host}:${cont}")
  echo "   附加卷: ${host} -> ${cont}"
}

if [ -n "$USER_VOLUME_MAPS" ]; then
  read -r -a _user_maps <<< "$USER_VOLUME_MAPS"
  for m in "${_user_maps[@]}"; do
    [ -n "$m" ] && add_volume_map "$m"
  done
  unset _user_maps
fi
for m in "${VOLUME_MAPS[@]}"; do
  [ -n "$m" ] && add_volume_map "$m"
done
unset m

# 网络模式：host 网络下不映射端口，容器共享宿主网络。
NETWORK_ARGS=()
PORT_ARGS=()
if [ "$DO_HOST_NETWORK" = true ]; then
  NETWORK_ARGS+=(--network=host)
  echo "   网络: host（共享宿主网络，不再映射端口；datahub_url 建议 http://127.0.0.1:8002/api/data）"
else
  PORT_ARGS+=(-p "${HOST_PORT}:${CONTAINER_PORT}")
  echo "   网络: 端口映射 ${HOST_PORT}:${CONTAINER_PORT}"
fi

# Docker 自动重启参数
RESTART_ARGS=()
if [ "$AUTO_RESTART" = true ]; then
  RESTART_ARGS+=(--restart=always)
fi

# 非 root 运行 + 绑定卷权限：
#   镜像内固定 USER=artifactdepot(10001)；绑定宿主机目录时改以宿主当前 UID/GID 运行，
#   保证卷可写。rootless podman 再加 keep-id 把容器 UID 映射回宿主同一 UID。
USER_ARGS=()
if [ "$VOLUME_ENABLED" = true ]; then
  USER_ARGS+=(--user "$(id -u):$(id -g)")
  if [ "$IS_PODMAN" = true ]; then
    USER_ARGS+=(--userns=keep-id)
  fi
  echo "   运行身份: $(id -u):$(id -g)$([ "$IS_PODMAN" = true ] && echo ' (userns=keep-id)')"
else
  echo "   运行身份: 镜像内 artifactdepot(10001)"
fi

CONTAINER_ID="$("$TOOL" run -d \
  --name "$CONTAINER_NAME" \
  "${RESTART_ARGS[@]}" \
  "${NETWORK_ARGS[@]}" \
  "${USER_ARGS[@]}" \
  "${PORT_ARGS[@]}" \
  "${ENV_ARGS[@]}" \
  "${VOLUME_ARGS[@]}" \
  "$FULL_IMAGE")"
cleanup_env_file
ENV_FILE=""
echo "   容器 ID: ${CONTAINER_ID:0:12}"
if [ "$DO_HOST_NETWORK" = true ]; then
  echo "OK: 容器已创建: $CONTAINER_NAME  （host 网络，端口 ${CONTAINER_PORT} 即宿主端口）"
else
  echo "OK: 容器已创建: $CONTAINER_NAME  端口映射: ${PORT_ARGS[*]}"
fi

# ---------- 4. 配置自启 ----------
if [ "$DO_SYSTEMD" = true ]; then
  echo ""
  echo "[4/5] 生成 systemd 开机自启服务 ..."
  mkdir -p "$SYSTEMD_DIR"
  # podman 5.x 的 --files 把 .service 生成到当前目录，需先 cd 到 systemd user 目录
  ( cd "$SYSTEMD_DIR" && "$TOOL" generate systemd \
      --name "$CONTAINER_NAME" \
      --new --files --restart-policy=always )
  if [ ! -f "$SERVICE_FILE" ]; then
    echo "ERROR: 未找到生成的服务文件: $SERVICE_FILE" >&2
    ls -la "$SYSTEMD_DIR" || true
    exit 1
  fi
  chmod 600 "$SERVICE_FILE" 2>/dev/null || true
  echo "OK: 服务文件已生成: $SERVICE_FILE"
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_NAME"
  # --new 模式启动时会重新创建容器，删掉第 3 步手动建的，避免同名冲突
  echo "   删除手动容器，交由 systemd 重新创建 ..."
  "$TOOL" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  systemctl --user start "$SERVICE_NAME"
  echo "OK: 自启服务已启用并启动: $SERVICE_NAME"
elif [ "$AUTO_RESTART" = true ]; then
  echo ""
  echo "[4/5] Docker 已配置 --restart=always（容器随 Docker 服务自动启动）"
else
  echo ""
  echo "[4/5] 未配置开机自启（--no-systemd）"
fi

# ---------- 5. 检查结果 ----------
echo ""
echo "OK: [5/5] 当前状态："
"$TOOL" ps --filter "name=$CONTAINER_NAME"
if [ "$DO_SYSTEMD" = true ]; then
  systemctl --user status "$SERVICE_NAME" --no-pager 2>/dev/null | head -4 || true
elif [ "$AUTO_RESTART" = true ]; then
  echo "   Docker 重启策略: $("$TOOL" inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER_NAME" 2>/dev/null || echo unknown)"
fi

echo ""
echo "完成。"
echo "──────────────────────────────────────────"
if [ "$DO_SYSTEMD" = true ]; then
  echo "若希望『不登录也能后台运行』，请执行："
  echo "   sudo loginctl enable-linger $(whoami)"
  echo "──────────────────────────────────────────"
  echo "查看/取消自启："
  echo "   systemctl --user status $SERVICE_NAME   # 查看"
  echo "   systemctl --user stop $SERVICE_NAME     # 停止"
  echo "   systemctl --user disable $SERVICE_NAME  # 取消自启"
elif [ "$AUTO_RESTART" = true ]; then
  echo "Docker 容器已随 Docker 服务自动启动。"
  echo "──────────────────────────────────────────"
  echo "查看/取消自启："
  echo "   docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' $CONTAINER_NAME"
  echo "   docker update --restart=no $CONTAINER_NAME"
  echo "   docker stop $CONTAINER_NAME"
else
  echo "INFO: 未配置开机自启。"
fi
echo "   ./deploy_container.sh --stop   # 停止+禁用自启"
echo "   ./deploy_container.sh --rm     # 删除容器+服务（数据卷保留）"
