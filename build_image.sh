#!/usr/bin/env bash
# ============================================================
# 通用镜像构建脚本
#
# 设计约定:
#   - 本脚本和 build_image.conf 一起放在项目根目录。
#   - build_image.conf 描述项目、镜像、构建上下文、导出规则等。
#   - 本脚本不写死任何项目，只负责通用构建流程。
#
# 用法:
#   ./build_image.sh                         # 构建全部默认启用的镜像
#   ./build_image.sh --image <id>            # 只构建指定镜像
#   ./build_image.sh --image <id> --image <id>
#   ./build_image.sh --tag <version>         # 覆盖 dynamic tag
#   ./build_image.sh --no-cache              # 禁用构建缓存
#   ./build_image.sh --no-save               # 只构建，不导出 tar
#   ./build_image.sh --save <dir>            # 指定导出目录
#   ./build_image.sh --images                # 只列出配置里的镜像
#   ./build_image.sh -h | --help
#
# 代理:
#   - build_image.conf 的 PROXY_MODE 决定构建时是否使用代理:
#       keep  = 继承当前 shell 的 HTTP(S)_PROXY 环境变量
#       strip = 摘掉环境里的代理，构建全程直连
#       set   = 用 PROXY_URL 显式注入（可含账号密码）
#   - 代理地址与账号密码直接写在 build_image.conf 的 PROXY_URL
#
# 依赖:
#   - bash 4+
#   - podman 或 docker
# ============================================================

set -euo pipefail

# ---------- 基础路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$SCRIPT_DIR/build_image.conf"

if [[ ! -f "$CONF_FILE" ]]; then
  echo "❌ 未找到构建配置文件: $CONF_FILE" >&2
  exit 1
fi

# ---------- 日志 ----------
LOG_LEVEL="${LOG_LEVEL:-normal}"

log() {
  [[ "$LOG_LEVEL" == "quiet" ]] && return 0
  printf '%s\n' "$*"
}

warn() {
  printf '⚠️  %s\n' "$*" >&2
}

die() {
  printf '❌ %s\n' "$*" >&2
  exit 1
}

# ---------- 默认全局配置 ----------
PROJECT_ID=""
PROJECT_TITLE=""
PROJECT_ROOT="."

REGISTRY=""
DEFAULT_TAG=""

TOOL_PREFERENCE=(podman docker)

BUILD_NETWORK="host"
PROXY_MODE="keep"          # keep / strip / set
PROXY_VARS=(HTTP_PROXY HTTPS_PROXY http_proxy https_proxy)
PROXY_URL=""               # PROXY_MODE=set 时注入的代理地址（可含账号密码）
PROXY_NO_PROXY=""          # 可选：不走代理的地址（逗号分隔，仅 set 模式使用）

PODMAN_HTTP_PROXY_FALSE=false
PODMAN_FORMAT_DOCKER=false

BUILD_NO_CACHE_DEFAULT=false
BUILD_PULL_POLICY=""

SAVE_DEFAULT=true
SAVE_DIR="../podman"
SAVE_NAME_PATTERN='${IMAGE_NAME}-${TAG}.tar'
SAVE_OVERWRITE=true

VALIDATE_DOCKERFILE=true
VALIDATE_REQUIRED_PATHS=true
CONTEXT_MISSING_POLICY="error"   # error / warn

SHOW_COMMANDS=false

# ---------- 镜像配置容器 ----------
IMAGE_IDS=()

declare -A IMAGE_LABEL=()
declare -A IMAGE_DOCKERFILE=()
declare -A IMAGE_CONTEXT=()
declare -A IMAGE_NAME=()

declare -A IMAGE_TAG_MODE=()      # dynamic / fixed
declare -A IMAGE_TAG_VALUE=()

declare -A IMAGE_REQUIRED_PATHS=()
declare -A IMAGE_OPTIONAL_PATHS=()

declare -A IMAGE_BUILD_ARGS=()
declare -A IMAGE_BUILD_TARGET=()
declare -A IMAGE_PLATFORM=()

declare -A IMAGE_ENABLED_BY_DEFAULT=()
declare -A IMAGE_SAVE_ENABLED=()
declare -A IMAGE_SAVE_DIR=()
declare -A IMAGE_SAVE_NAME=()

declare -A IMAGE_PRE_BUILD_CMD=()
declare -A IMAGE_POST_BUILD_CMD=()

# 允许项目覆盖字段
declare -A IMAGE_PROXY_MODE=()
declare -A IMAGE_PODMAN_HTTP_PROXY_FALSE=()
declare -A IMAGE_PODMAN_FORMAT_DOCKER=()

# ---------- 读取项目配置 ----------
# shellcheck disable=SC1090
source "$CONF_FILE"

# ---------- 工具函数 ----------
ROOT_DIR="$SCRIPT_DIR"

resolve_path() {
  local p="${1:-}"
  case "$p" in
    "")       printf '%s' "$ROOT_DIR" ;;
    /*)       printf '%s' "$p" ;;
    ".")      printf '%s' "$ROOT_DIR" ;;
    "./*")    printf '%s/%s' "$ROOT_DIR" "${p#./}" ;;
    *)        printf '%s/%s' "$ROOT_DIR" "$p" ;;
  esac
}

is_true() {
  case "${1,,}" in
    1|true|yes|y|on) return 0 ;;
    *) return 1 ;;
  esac
}

image_field() {
  local arr="$1" id="$2"
  local -n ref="$arr"
  printf '%s' "${ref[$id]:-}"
}

image_proxy_mode() {
  local id="$1"
  local v
  v="$(image_field IMAGE_PROXY_MODE "$id")"
  printf '%s' "${v:-$PROXY_MODE}"
}

image_proxy_url() {
  printf '%s' "$PROXY_URL"
}

# 代理配置校验：必须放在主 shell 里执行（build_proxy_env_args 跑在进程替换的子 shell 中，
# 在里面 die 只会终止子 shell，主流程会带着空参数继续构建）
validate_proxy_config() {
  local id="$1"
  local mode url
  mode="$(image_proxy_mode "$id")"

  case "$mode" in
    keep|strip)
      ;;
    set)
      url="$(image_proxy_url "$id")"
      if [[ -z "$url" ]]; then
        die "镜像 [$id] 的 PROXY_MODE=set，但代理地址为空：请在 build_image.conf 里填写 PROXY_URL=\"http://用户名:密码@主机:端口/\"（或把 PROXY_MODE 改成 strip/keep）"
      fi
      if [[ "$url" != *"://"* ]]; then
        die "镜像 [$id] 的 PROXY_URL 格式不对（应形如 http://user:pass@host:port/）: $url"
      fi
      ;;
    *)
      die "镜像 [$id] 的 proxy mode 无效: $mode（只支持 keep / strip / set）"
      ;;
  esac
}

image_podman_http_proxy_false() {
  local id="$1"
  local v
  v="$(image_field IMAGE_PODMAN_HTTP_PROXY_FALSE "$id")"
  printf '%s' "${v:-$PODMAN_HTTP_PROXY_FALSE}"
}

image_podman_format_docker() {
  local id="$1"
  local v
  v="$(image_field IMAGE_PODMAN_FORMAT_DOCKER "$id")"
  printf '%s' "${v:-$PODMAN_FORMAT_DOCKER}"
}

image_tag() {
  local id="$1"
  local mode
  mode="$(image_field IMAGE_TAG_MODE "$id")"
  mode="${mode:-dynamic}"

  case "$mode" in
    dynamic)
      # 实际取值由 resolve_tag_early() 在主 shell 内解析好，避免 die 落在子 shell 里失效
      printf '%s' "$RESOLVED_TAG"
      ;;
    fixed)
      local v
      v="$(image_field IMAGE_TAG_VALUE "$id")"
      [[ -n "$v" ]] || die "镜像 [$id] 是 fixed tag，但 IMAGE_TAG_VALUE 为空"
      printf '%s' "$v"
      ;;
    *)
      die "镜像 [$id] 的 IMAGE_TAG_MODE 无效: $mode（只支持 dynamic / fixed）"
      ;;
  esac
}

image_full_ref() {
  local id="$1"
  local name tag
  name="$(image_field IMAGE_NAME "$id")"
  tag="$(image_tag "$id")"

  [[ -n "$name" ]] || die "镜像 [$id] 缺少 IMAGE_NAME"

  if [[ -n "$REGISTRY" ]]; then
    printf '%s/%s:%s' "$REGISTRY" "$name" "$tag"
  else
    printf '%s:%s' "$name" "$tag"
  fi
}

expand_save_name() {
  local pattern="$1"
  local image_name="$2"
  local tag="$3"
  local s="$pattern"
  s="${s//\$\{IMAGE_NAME\}/${image_name}}"
  s="${s//\$\{TAG\}/${tag}}"
  s="${s//\$\{REGISTRY\}/${REGISTRY}}"
  s="${s//\$\{PROJECT_ID\}/${PROJECT_ID}}"
  printf '%s' "$s"
}

# ---------- 版本唯一源 ----------
# 版本号只写在项目根 VERSION 文件里（build_image.conf 的 DEFAULT_TAG 留空即自动读取）。
# 优先级：--tag 命令行 > VERSION 文件 > build_image.conf 的 DEFAULT_TAG
read_version_file() {
  local f="$SCRIPT_DIR/VERSION"
  [[ -f "$f" ]] || return 0
  grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?' "$f" 2>/dev/null | head -1
}

resolve_build_version() {
  if [[ -n "${CLI_TAG:-}" ]]; then
    printf '%s' "$CLI_TAG"
    return
  fi
  local v=""
  v="$(read_version_file)"
  if [[ -z "$v" ]]; then
    v="$DEFAULT_TAG"
  fi
  [[ -n "$v" ]] || die "未能确定版本：VERSION 文件与 build_image.conf 的 DEFAULT_TAG 都为空"
  printf '%s' "$v"
}

# 在主 shell 内解析版本（必须在构建前调用：resolve_build_version 里的 die 只有
# 在 main shell 执行才能真正终止脚本）
RESOLVED_TAG=""
resolve_tag_early() {
  local id mode
  for id in "$@"; do
    mode="$(image_field IMAGE_TAG_MODE "$id")"
    mode="${mode:-dynamic}"
    if [[ "$mode" == "dynamic" ]]; then
      RESOLVED_TAG="$(resolve_build_version)"
      return 0
    fi
  done
  RESOLVED_TAG=""
  return 0
}

# ---------- 参数解析 ----------
CLI_TAG=""
DO_SAVE=""
SAVE_DIR_OVERRIDE=""
NO_CACHE="$BUILD_NO_CACHE_DEFAULT"
LIST_IMAGES=false
SELECTED_IMAGES=()

usage() {
  cat <<'EOF'
用法:
  ./build_image.sh                        # 构建全部默认启用的镜像
  ./build_image.sh --image <id>           # 只构建指定镜像
  ./build_image.sh --image <id> --image <id>
  ./build_image.sh --tag <version>        # 覆盖 dynamic tag
  ./build_image.sh --no-cache             # 禁用构建缓存
  ./build_image.sh --no-save              # 只构建，不导出 tar
  ./build_image.sh --save <dir>           # 指定导出目录
  ./build_image.sh --images               # 只列出配置里的镜像
  ./build_image.sh -h | --help

说明:
  - --save 一律表示导出目录，不是单个 tar 文件。
  - 默认导出目录由 build_image.conf 的 SAVE_DIR 决定。
  - 默认会构建 IMAGE_ENABLED_BY_DEFAULT=true 的镜像。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      [[ $# -ge 2 ]] || die "--image 需要一个镜像 id"
      SELECTED_IMAGES+=("$2")
      shift 2
      ;;
    --tag)
      [[ $# -ge 2 ]] || die "--tag 需要一个版本号"
      CLI_TAG="$2"
      shift 2
      ;;
    --no-cache)
      NO_CACHE=true
      shift
      ;;
    --no-save)
      DO_SAVE=false
      shift
      ;;
    --save)
      [[ $# -ge 2 ]] || die "--save 需要一个目录"
      SAVE_DIR_OVERRIDE="$2"
      DO_SAVE=true
      shift 2
      ;;
    --images)
      LIST_IMAGES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数: $1（-h 查看帮助）"
      ;;
  esac
done

# ---------- 导出开关 ----------
if [[ -z "$DO_SAVE" ]]; then
  DO_SAVE="$SAVE_DEFAULT"
fi

# ---------- 项目根目录 ----------
ROOT_DIR="$(resolve_path "$PROJECT_ROOT")"
[[ -d "$ROOT_DIR" ]] || die "项目目录不存在: $ROOT_DIR"

# ---------- 基本校验 ----------
[[ ${#IMAGE_IDS[@]} -gt 0 ]] || die "build_image.conf 未配置任何 IMAGE_IDS"

if [[ "$LIST_IMAGES" == true ]]; then
  log "镜像列表（$PROJECT_ID）:"
  for id in "${IMAGE_IDS[@]}"; do
    label="$(image_field IMAGE_LABEL "$id")"
    dockerfile="$(image_field IMAGE_DOCKERFILE "$id")"
    name="$(image_field IMAGE_NAME "$id")"
    mode="$(image_field IMAGE_TAG_MODE "$id")"
    mode="${mode:-dynamic}"
    enabled="$(image_field IMAGE_ENABLED_BY_DEFAULT "$id")"
    save_enabled="$(image_field IMAGE_SAVE_ENABLED "$id")"
    tag_value="$(image_field IMAGE_TAG_VALUE "$id")"

    default="yes"
    if [[ -n "$enabled" ]] && ! is_true "$enabled"; then default="no"; fi

    save="yes"
    if [[ -n "$save_enabled" ]] && ! is_true "$save_enabled"; then save="no"; fi

    tag_show="$mode"
    [[ "$mode" == "fixed" ]] && tag_show="$tag_value"

    printf '  %-16s %-16s %-28s %-28s default=%-3s save=%s\n' \
      "$id" "$label" "$dockerfile" "$tag_show" "$default" "$save"
  done
  exit 0
fi

# ---------- 选择镜像 ----------
if [[ ${#SELECTED_IMAGES[@]} -eq 0 ]]; then
  for id in "${IMAGE_IDS[@]}"; do
    if [[ -z "$(image_field IMAGE_ENABLED_BY_DEFAULT "$id")" ]] || is_true "$(image_field IMAGE_ENABLED_BY_DEFAULT "$id")"; then
      SELECTED_IMAGES+=("$id")
    fi
  done
fi

[[ ${#SELECTED_IMAGES[@]} -gt 0 ]] || die "没有可构建的镜像"

# 校验选择的镜像是否都存在
for id in "${SELECTED_IMAGES[@]}"; do
  found=false
  for known in "${IMAGE_IDS[@]}"; do
    [[ "$id" == "$known" ]] && { found=true; break; }
  done
  [[ "$found" == true ]] || die "镜像 [$id] 不在 IMAGE_IDS 配置里"
done

# 版本早绑定：在主 shell 内解析（VERSION 文件优先），非法配置在此直接退出
resolve_tag_early "${SELECTED_IMAGES[@]}"

# ---------- 检测容器工具 ----------
detect_tool() {
  local tool
  for tool in "${TOOL_PREFERENCE[@]}"; do
    if command -v "$tool" >/dev/null 2>&1; then
      printf '%s' "$tool"
      return 0
    fi
  done
  return 1
}

TOOL="$(detect_tool)" || die "未找到可用的容器工具（TOOL_PREFERENCE: ${TOOL_PREFERENCE[*]}）"

log "项目: ${PROJECT_TITLE:-$PROJECT_ID}"
log "根目录: $ROOT_DIR"
log "容器工具: $TOOL"
log "构建镜像:"
for id in "${SELECTED_IMAGES[@]}"; do
  log "  - $id → $(image_full_ref "$id")"
done

# ---------- 构建上下文校验 ----------
validate_context() {
  local id="$1"
  local dockerfile context p path missing=""

  dockerfile="$(image_field IMAGE_DOCKERFILE "$id")"
  context="$(image_field IMAGE_CONTEXT "$id")"
  context="${context:-.}"

  if is_true "$VALIDATE_DOCKERFILE"; then
    [[ -f "$ROOT_DIR/$dockerfile" ]] || die "镜像 [$id] 找不到 Dockerfile: $ROOT_DIR/$dockerfile"
  fi

  if is_true "$VALIDATE_REQUIRED_PATHS"; then
    for p in $(image_field IMAGE_REQUIRED_PATHS "$id"); do
      path="$ROOT_DIR/$p"
      [[ -e "$path" ]] || missing="$missing $p"
    done
    if [[ -n "$missing" ]]; then
      if [[ "$CONTEXT_MISSING_POLICY" == "warn" ]]; then
        warn "镜像 [$id] 缺少构建上下文:$missing"
      else
        die "镜像 [$id] 缺少构建上下文:$missing"
      fi
    fi
  fi

  for p in $(image_field IMAGE_OPTIONAL_PATHS "$id"); do
    path="$ROOT_DIR/$p"
    [[ -e "$path" ]] || warn "镜像 [$id] 缺少可选上下文: $p"
  done
}

# ---------- 构建代理参数 ----------
build_proxy_env_args() {
  local id="$1"
  local mode
  mode="$(image_proxy_mode "$id")"

  case "$mode" in
    strip)
      local p
      for p in "${PROXY_VARS[@]}"; do
        printf '%s\0' "-u"
        printf '%s\0' "$p"
      done
      ;;
    keep)
      ;;
    set)
      local url
      url="$(image_proxy_url "$id")"
      if [[ -z "$url" ]]; then
        die "镜像 [$id] 的 PROXY_MODE=set，但代理地址为空：请在 build_image.conf 里填写 PROXY_URL"
      fi
      local p
      for p in "${PROXY_VARS[@]}"; do
        printf '%s\0' "$p=$url"
      done
      if [[ -n "$PROXY_NO_PROXY" ]]; then
        printf '%s\0' "NO_PROXY=$PROXY_NO_PROXY"
        printf '%s\0' "no_proxy=$PROXY_NO_PROXY"
      fi
      ;;
    *)
      die "镜像 [$id] 的 proxy mode 无效: $mode（只支持 keep / strip / set）"
      ;;
  esac
}

build_extra_args() {
  local id="$1"
  local args=()
  local raw
  local -a parsed=()

  raw="$(image_field IMAGE_BUILD_ARGS "$id")"
  if [[ -n "$raw" ]]; then
    read -r -a parsed <<< "$raw"
    args+=("${parsed[@]}")
  fi

  local target platform
  target="$(image_field IMAGE_BUILD_TARGET "$id")"
  [[ -n "$target" ]] && args+=(--target "$target")

  platform="$(image_field IMAGE_PLATFORM "$id")"
  [[ -n "$platform" ]] && args+=(--platform "$platform")

  # 把版本号传给 Dockerfile（LABEL org.opencontainers.image.version）
  if [[ -n "$RESOLVED_TAG" ]]; then
    args+=(--build-arg "APP_VERSION=$RESOLVED_TAG")
  fi

  if [[ "$TOOL" == "podman" ]] && is_true "$(image_podman_format_docker "$id")"; then
    args+=(--format docker)
  fi

  if [[ "$TOOL" == "podman" ]] && is_true "$(image_podman_http_proxy_false "$id")"; then
    args+=(--http-proxy=false)
  fi

  if is_true "$NO_CACHE"; then
    args+=(--no-cache)
  fi

  if [[ -n "$BUILD_PULL_POLICY" ]]; then
    args+=(--pull="$BUILD_PULL_POLICY")
  fi

  if [[ -n "$BUILD_NETWORK" ]]; then
    args+=(--network="$BUILD_NETWORK")
  fi

  printf '%s\0' "${args[@]}"
}

# ---------- 执行命令 ----------
mask_secrets() {
  # 日志展示用：把 http://user:pass@host 里的账号密码遮掉
  printf '%s' "$1" | sed -E 's#(//)[^/@[:space:]]+@#\1***:***@#g'
}

run_cmd() {
  if is_true "$SHOW_COMMANDS" || [[ "$LOG_LEVEL" == "verbose" ]]; then
    log "➤ $(mask_secrets "$*")"
  fi
  "$@"
}

run_hook() {
  local id="$1" phase="$2" cmd="$3"
  [[ -n "$cmd" ]] || return 0
  log "➤ [$id] $phase hook"
  (
    cd "$ROOT_DIR"
    bash -c "$cmd"
  )
}

# ---------- 保存镜像 ----------
save_image() {
  local id="$1" full_ref="$2" tag="$3"

  local enabled
  enabled="$(image_field IMAGE_SAVE_ENABLED "$id")"
  if [[ -n "$enabled" ]] && ! is_true "$enabled"; then
    log "⏭️  跳过导出 [$id]"
    return 0
  fi

  if [[ "$DO_SAVE" == false ]]; then
    log "⏭️  跳过导出（--no-save）"
    return 0
  fi

  local save_dir
  if [[ -n "$SAVE_DIR_OVERRIDE" ]]; then
    save_dir="$SAVE_DIR_OVERRIDE"
  else
    save_dir="$(image_field IMAGE_SAVE_DIR "$id")"
    if [[ -z "$save_dir" ]]; then
      save_dir="$SAVE_DIR"
    fi
  fi

  local dir_abs
  dir_abs="$(resolve_path "$save_dir")"
  mkdir -p "$dir_abs"

  local name
  name="$(image_field IMAGE_SAVE_NAME "$id")"
  if [[ -z "$name" ]]; then
    name="$SAVE_NAME_PATTERN"
  fi
  if [[ -z "$name" ]]; then
    name="${IMAGE_NAME:-image}-${tag}.tar"
  fi
  name="$(expand_save_name "$name" "$(image_field IMAGE_NAME "$id")" "$tag")"

  local out="$dir_abs/$name"

  if [[ -e "$out" ]]; then
    if is_true "$SAVE_OVERWRITE"; then
      rm -f "$out"
    else
      warn "已存在且不覆盖，跳过导出: $out"
      return 0
    fi
  fi

  log "📦 导出镜像: $full_ref → $out"
  run_cmd "$TOOL" save -o "$out" "$full_ref"
  SAVED_TARS+=("$out")
}

# ---------- 构建单个镜像 ----------
build_one() {
  local id="$1"
  local full_ref tag dockerfile context

  full_ref="$(image_full_ref "$id")"
  tag="$(image_tag "$id")"
  dockerfile="$(image_field IMAGE_DOCKERFILE "$id")"
  context="$(image_field IMAGE_CONTEXT "$id")"
  context="${context:-.}"

  log ""
  log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  log "镜像 [$id] → $full_ref"
  log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  validate_context "$id"
  validate_proxy_config "$id"

  run_hook "$id" "pre-build" "$(image_field IMAGE_PRE_BUILD_CMD "$id")"

  local -a build_cmd=()
  local -a proxy_args=()

  mapfile -d '' proxy_args < <(build_proxy_env_args "$id")

  if [[ ${#proxy_args[@]} -gt 0 ]]; then
    build_cmd+=(env "${proxy_args[@]}")
  fi

  build_cmd+=("$TOOL" build)

  local -a extra_args=()
  mapfile -d '' extra_args < <(build_extra_args "$id")
  build_cmd+=("${extra_args[@]}")

  build_cmd+=(
    -f "$ROOT_DIR/$dockerfile"
    -t "$full_ref"
    "$ROOT_DIR/$context"
  )

  run_cmd "${build_cmd[@]}"

  run_hook "$id" "post-build" "$(image_field IMAGE_POST_BUILD_CMD "$id")"

  save_image "$id" "$full_ref" "$tag"
}

# ---------- 主流程 ----------
BUILT_IMAGES=()
SAVED_TARS=()

for id in "${SELECTED_IMAGES[@]}"; do
  build_one "$id"
  BUILT_IMAGES+=("$(image_full_ref "$id")")
done

log ""
log "✅ 构建完成"
for ref in "${BUILT_IMAGES[@]}"; do
  log "  - $ref"
done

if [[ ${#SAVED_TARS[@]} -gt 0 ]]; then
  log ""
  log "导出文件:"
  for tar in "${SAVED_TARS[@]}"; do
    log "  - $tar"
  done
fi
