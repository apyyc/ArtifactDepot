#!/usr/bin/env bash
# ============================================================
# DataWarehouse 镜像构建脚本：构建 → 校验 →（可选）导出 tar
#
# 与 README 的构建约定一致：
#   - env -u HTTP_PROXY ... 绕过本机代理对 docker.io 的拦截
#   - --network=host         绕过容器网桥 DNS 失效
#   - Podman 时附加 --format docker，避免 HEALTHCHECK 被 OCI 格式忽略
#   - 镜像标签 = 版本 0.5.5（localhost/datawarehouse:0.5.5）
#
# 用法：
#   ./build_image.sh                              # 构建镜像 + 自动导出 datawarehouse-<tag>.tar
#   ./build_image.sh --no-save                    # 只构建，不导出 tar
#   ./build_image.sh --save /path/to/out.tar      # 指定导出路径（默认同目录 datawarehouse-<tag>.tar）
#   ./build_image.sh --no-cache                   # 不使用构建缓存（依赖层有改动时用）
#   ./build_image.sh --tag 0.5.5                  # 自定义版本标签（默认 0.5.5）
#   ./build_image.sh --load /path/to/in.tar       # 不构建，直接导入已有 tar
#   ./build_image.sh --list                       # 只列出本地镜像
#
# 依赖：podman 或 docker（自动检测，可用 FORCE=... 强制指定）
# ============================================================

set -euo pipefail

# ---------- 可配置参数（按需修改） ----------
IMAGE_NAME="datawarehouse"           # 镜像名
IMAGE_TAG="0.5.5"                    # 版本标签（可用 --tag 覆盖）
REGISTRY="localhost"                 # 本地镜像仓库前缀
FULL_IMAGE=""                        # 参数解析后生成

# 构建目录（脚本所在目录，需含 Dockerfile）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 需要绕过的代理环境变量（本机代理会拦截 docker.io 拉取）
PROXY_ENVS=(HTTP_PROXY HTTPS_PROXY http_proxy https_proxy)

usage() {
  cat <<'USAGE'
用法: ./build_image.sh [选项]

选项:
  --no-cache          不使用构建缓存
  --tag <版本>        自定义镜像版本标签（默认 0.5.5）
  --no-save           只构建，不导出 tar
  --save [路径]       导出 tar；不传路径则导出到脚本同目录
  --load <tar>        不构建，直接导入已有 tar
  --list              只列出本地镜像
  -h, --help          显示本帮助

环境变量:
  FORCE=podman|docker 强制指定容器工具
USAGE
}

# ---------- 参数解析 ----------
DO_CACHE=true
DO_SAVE=true
SAVE_PATH=""
DO_LOAD=false
LOAD_PATH=""
DO_LIST=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-cache) DO_CACHE=false ;;
    --tag)
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then
        echo "❌ --tag 需要一个非选项参数" >&2
        exit 1
      fi
      IMAGE_TAG="$1"
      ;;
    --no-save) DO_SAVE=false ;;
    --save)
      DO_SAVE=true
      if [[ $# -gt 1 && ! "$2" == -* ]]; then
        shift
        SAVE_PATH="$1"
      fi
      ;;
    --load)
      DO_LOAD=true
      shift
      if [ $# -eq 0 ] || [[ "$1" == -* ]]; then
        echo "❌ --load 需要一个 tar 路径" >&2
        exit 1
      fi
      LOAD_PATH="$1"
      ;;
    --list) DO_LIST=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "❌ 未知参数: $1（-h 查看帮助）" >&2; exit 1 ;;
  esac
  shift
done
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

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
echo "🔧 使用容器工具: $TOOL"
echo "📦 镜像: $FULL_IMAGE"
echo "📂 构建目录: $SCRIPT_DIR"

# ---------- 导入模式：只导入 tar，不构建 ----------
if [ "$DO_LOAD" = true ]; then
  echo ""
  echo "📥 [导入] $LOAD_PATH → $FULL_IMAGE"
  if [ ! -f "$LOAD_PATH" ]; then
    echo "❌ 找不到导入文件: $LOAD_PATH" >&2
    exit 1
  fi
  "$TOOL" load -i "$LOAD_PATH"
  echo "✅ 导入完成"
  echo ""
  "$TOOL" images --filter "reference=${REGISTRY}/${IMAGE_NAME}"
  exit 0
fi

# ---------- 列表模式 ----------
if [ "$DO_LIST" = true ]; then
  echo ""
  "$TOOL" images --filter "reference=${REGISTRY}/${IMAGE_NAME}"
  exit 0
fi

# ---------- 1. 检查 Dockerfile ----------
echo ""
echo "🔍 [1/4] 检查 Dockerfile ..."
if [ ! -f "$SCRIPT_DIR/Dockerfile" ]; then
  echo "❌ 未找到 $SCRIPT_DIR/Dockerfile" >&2
  exit 1
fi
echo "✅ Dockerfile 存在"

# ---------- 2. 检查构建上下文 ----------
echo ""
echo "📁 [2/4] 检查构建上下文 ..."
for d in src docker; do
  if [ ! -d "$SCRIPT_DIR/$d" ]; then
    echo "❌ 缺少构建上下文目录: $d/（Dockerfile COPY 会失败）" >&2
    exit 1
  fi
  echo "   ✅ $d/ 存在"
done

# ---------- 3. 构建 ----------
echo ""
echo "🏗️   [3/4] 构建镜像 $FULL_IMAGE ..."
CACHE_ARG=""
if [ "$DO_CACHE" = false ]; then
  CACHE_ARG="--no-cache"
  echo "   ⚠️  不使用构建缓存"
fi

# Podman 默认输出 OCI 格式，会忽略 Dockerfile 的 HEALTHCHECK。
# 加 --format docker 后既保留健康检查，也仍然可以 save/load tar。
BUILD_FORMAT_ARGS=()
if [ "$TOOL" = "podman" ]; then
  BUILD_FORMAT_ARGS=(--format docker)
  echo "   ℹ️  Podman 构建格式: docker（保留 HEALTHCHECK）"
fi

# 生成绕代理命令：env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy
ENV_U_ARGS=()
for p in "${PROXY_ENVS[@]}"; do
  if [ -n "${!p:-}" ]; then
    ENV_U_ARGS+=(-u "$p")
  fi
done

# 用 env 包裹，绕开可能存在的代理变量；--network=host 避免容器网桥 DNS 失效
# shellcheck disable=SC2086  # CACHE_ARG 需要按空/非空做单词拆分
env "${ENV_U_ARGS[@]}" "$TOOL" build \
  "${BUILD_FORMAT_ARGS[@]}" \
  --network=host \
  $CACHE_ARG \
  -t "$FULL_IMAGE" \
  "$SCRIPT_DIR"

echo "✅ 镜像构建成功: $FULL_IMAGE"
echo ""
"$TOOL" images "$FULL_IMAGE"

# ---------- 4. 导出 tar（默认自动导出；--no-save 跳过） ----------
if [ "$DO_SAVE" = true ]; then
  echo ""
  echo "📦 [4/4] 导出镜像 ..."
  if [ -z "$SAVE_PATH" ]; then
    SAVE_PATH="$SCRIPT_DIR/${IMAGE_NAME}-${IMAGE_TAG}.tar"
  elif [ -d "$SAVE_PATH" ]; then
    SAVE_PATH="${SAVE_PATH%/}/${IMAGE_NAME}-${IMAGE_TAG}.tar"
  fi
  SAVE_DIR="$(dirname "$SAVE_PATH")"
  if [ -n "$SAVE_DIR" ]; then mkdir -p "$SAVE_DIR"; fi
  "$TOOL" save -o "$SAVE_PATH" "$FULL_IMAGE"
  echo "✅ 已导出: $SAVE_PATH"
else
  echo ""
  echo "⏭️   [4/4] 跳过导出（--no-save）"
fi

echo ""
echo "🎉 完成。下一步（把 tar 带到目标机）："
echo "   $TOOL load -i ${SAVE_PATH:-$SCRIPT_DIR/${IMAGE_NAME}-${IMAGE_TAG}.tar}"
echo "   ./deploy_container.sh --token y   # 默认 host 网络，datahub_url 用挂载的 config.json"
