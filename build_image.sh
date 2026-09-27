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
# 依赖:
#   - bash 4+
#   - podman 或 docker
# ============================================================

set -euo pipefail

# ---------- 基础路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$SCRIPT_DIR/build_image.conf"

if [[ ! -f "$CONF_FILE" ]]; then
  echo "â æªæ¾å°æå»ºé
ç½®æä»¶: $CONF_FILE" >&2
  exit 1
fi

# ---------- 日志 ----------
LOG_LEVEL="${LOG_LEVEL:-normal}"

log() {
  [[ "$LOG_LEVEL" == "quiet" ]] && return 0
  printf '%s\n' "$*"
}

warn() {
  printf 'â ï¸  %s\n' "$*" >&2
}

die() {
  printf 'â %s\n' "$*" >&2
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
PROXY_MODE="keep"          # keep / strip
PROXY_VARS=(HTTP_PROXY HTTPS_PROXY http_proxy https_proxy)

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
      local t="${CLI_TAG:-$DEFAULT_TAG}"
      [[ -n "$t" ]] || die "éå [$id] æ¯ dynamic tagï¼ä½æªæä¾ --tag ä¸ DEFAULT_TAG ä¸ºç©º"
      printf '%s' "$t"
      ;;
    fixed)
      local v
      v="$(image_field IMAGE_TAG_VALUE "$id")"
      [[ -n "$v" ]] || die "éå [$id] æ¯ fixed tagï¼ä½ IMAGE_TAG_VALUE ä¸ºç©º"
      printf '%s' "$v"
      ;;
    *)
      die "éå [$id] ç IMAGE_TAG_MODE æ æ: $modeï¼åªæ¯æ dynamic / fixedï¼"
      ;;
  esac
}

image_full_ref() {
  local id="$1"
  local name tag
  name="$(image_field IMAGE_NAME "$id")"
  tag="$(image_tag "$id")"

  [[ -n "$name" ]] || die "éå [$id] ç¼ºå° IMAGE_NAME"

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

# ---------- 参数解析 ----------
CLI_TAG=""
DO_SAVE=""
SAVE_DIR_OVERRIDE=""
NO_CACHE="$BUILD_NO_CACHE_DEFAULT"
LIST_IMAGES=false
SELECTED_IMAGES=()

usage() {
  cat <<'EOF'
ç¨æ³:
  ./build_image.sh                        # æå»ºå
¨é¨é»è®¤å¯ç¨éå
  ./build_image.sh --image <id>           # åªæå»ºæå®éå
  ./build_image.sh --image <id> --image <id>
  ./build_image.sh --tag <version>        # è¦ç dynamic tag
  ./build_image.sh --no-cache             # ç¦ç¨æå»ºç¼å­
  ./build_image.sh --no-save              # åªæå»ºï¼ä¸å¯¼åº tar
  ./build_image.sh --save <dir>           # æå®å¯¼åºç®å½
  ./build_image.sh --images               # åªååºé
ç½®éçéå
  ./build_image.sh -h | --help

è¯´æ:
  - --save ä¸å¾è¡¨ç¤ºå¯¼åºç®å½ï¼ä¸æ¯åä¸ª tar æä»¶ã
  - é»è®¤å¯¼åºç®å½ç± build_image.conf ç SAVE_DIR å³å®ã
  - é»è®¤ä¼æå»º IMAGE_ENABLED_BY_DEFAULT=true çéåã
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      [[ $# -ge 2 ]] || die "--image éè¦ä¸ä¸ªéå id"
      SELECTED_IMAGES+=("$2")
      shift 2
      ;;
    --tag)
      [[ $# -ge 2 ]] || die "--tag éè¦ä¸ä¸ªçæ¬å·"
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
      [[ $# -ge 2 ]] || die "--save éè¦ä¸ä¸ªç®å½"
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
      die "æªç¥åæ°: $1ï¼-h æ¥çå¸®å©ï¼"
      ;;
  esac
done

# ---------- 导出开关 ----------
if [[ -z "$DO_SAVE" ]]; then
  DO_SAVE="$SAVE_DEFAULT"
fi

# ---------- 项目根目录 ----------
ROOT_DIR="$(resolve_path "$PROJECT_ROOT")"
[[ -d "$ROOT_DIR" ]] || die "é¡¹ç®ç®å½ä¸å­å¨: $ROOT_DIR"

# ---------- 基本校验 ----------
[[ ${#IMAGE_IDS[@]} -gt 0 ]] || die "build_image.conf æªé
ç½®ä»»ä½ IMAGE_IDS"

if [[ "$LIST_IMAGES" == true ]]; then
  log "éååè¡¨ï¼$PROJECT_IDï¼:"
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

[[ ${#SELECTED_IMAGES[@]} -gt 0 ]] || die "æ²¡æå¯æå»ºçéå"

# 校验选择的镜像是否都存在
for id in "${SELECTED_IMAGES[@]}"; do
  found=false
  for known in "${IMAGE_IDS[@]}"; do
    [[ "$id" == "$known" ]] && { found=true; break; }
  done
  [[ "$found" == true ]] || die "éå [$id] ä¸å¨ IMAGE_IDS é
ç½®é"
done

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

TOOL="$(detect_tool)" || die "æªæ¾å°å¯ç¨çå®¹å¨å·¥å
·ï¼TOOL_PREFERENCE: ${TOOL_PREFERENCE[*]}ï¼"

log "é¡¹ç®: ${PROJECT_TITLE:-$PROJECT_ID}"
log "æ ¹ç®å½: $ROOT_DIR"
log "å®¹å¨å·¥å
·: $TOOL"
log "æå»ºéå:"
for id in "${SELECTED_IMAGES[@]}"; do
  log "  - $id â $(image_full_ref "$id")"
done

# ---------- 构建上下文校验 ----------
validate_context() {
  local id="$1"
  local dockerfile context p path missing=""

  dockerfile="$(image_field IMAGE_DOCKERFILE "$id")"
  context="$(image_field IMAGE_CONTEXT "$id")"
  context="${context:-.}"

  if is_true "$VALIDATE_DOCKERFILE"; then
    [[ -f "$ROOT_DIR/$dockerfile" ]] || die "éå [$id] æ¾ä¸å° Dockerfile: $ROOT_DIR/$dockerfile"
  fi

  if is_true "$VALIDATE_REQUIRED_PATHS"; then
    for p in $(image_field IMAGE_REQUIRED_PATHS "$id"); do
      path="$ROOT_DIR/$p"
      [[ -e "$path" ]] || missing="$missing $p"
    done
    if [[ -n "$missing" ]]; then
      if [[ "$CONTEXT_MISSING_POLICY" == "warn" ]]; then
        warn "éå [$id] ç¼ºå°æå»ºä¸ä¸æ:$missing"
      else
        die "éå [$id] ç¼ºå°æå»ºä¸ä¸æ:$missing"
      fi
    fi
  fi

  for p in $(image_field IMAGE_OPTIONAL_PATHS "$id"); do
    path="$ROOT_DIR/$p"
    [[ -e "$path" ]] || warn "éå [$id] ç¼ºå°å¯éä¸ä¸æ: $p"
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
    *)
      die "éå [$id] ç proxy mode æ æ: $modeï¼åªæ¯æ keep / stripï¼"
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
run_cmd() {
  if is_true "$SHOW_COMMANDS" || [[ "$LOG_LEVEL" == "verbose" ]]; then
    log "â¤ $*"
  fi
  "$@"
}

run_hook() {
  local id="$1" phase="$2" cmd="$3"
  [[ -n "$cmd" ]] || return 0
  log "â¤ [$id] $phase hook"
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
    log "â­ï¸  è·³è¿å¯¼åº [$id]"
    return 0
  fi

  if [[ "$DO_SAVE" == false ]]; then
    log "â­ï¸  è·³è¿å¯¼åºï¼--no-saveï¼"
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
      warn "å·²å­å¨ä¸ä¸è¦çï¼è·³è¿å¯¼åº: $out"
      return 0
    fi
  fi

  log "ð¦ å¯¼åºéå: $full_ref â $out"
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
  log "ââââââââââââââââââââââââââââââââââââââââ"
  log "éå [$id] â $full_ref"
  log "ââââââââââââââââââââââââââââââââââââââââ"

  validate_context "$id"

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
log "â
æå»ºå®æ"
for ref in "${BUILT_IMAGES[@]}"; do
  log "  - $ref"
done

if [[ ${#SAVED_TARS[@]} -gt 0 ]]; then
  log ""
  log "å¯¼åºæä»¶:"
  for tar in "${SAVED_TARS[@]}"; do
    log "  - $tar"
  done
fi
