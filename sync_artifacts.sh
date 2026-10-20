#!/usr/bin/env bash

# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

# ============================================================
# sync_artifacts.sh
#
# 通用产物同步脚本：通过 rsync 把本地目录同步到一个或多个远端目录。
# 不包含 IP、账号、项目名或密钥。
#
# 用法:
#   ./sync_artifacts.sh [--config <file>] [--source <dir>]
#                       [--dest <user@host:port:dir>]...
#                       [--bwlimit <KB/s>] [--delete] [--dry-run]
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CONFIG="$SCRIPT_DIR/sync_artifacts.conf"
CONFIG_FILE=""
SOURCE_OVERRIDE=""
DEST_OVERRIDE=()
BWLIMIT_OVERRIDE=""
DELETE_OVERRIDE=""
DRY_RUN=false

usage() {
  cat <<'USAGE'
用法:
  ./sync_artifacts.sh [--config <file>] [--source <dir>]
                      [--dest <user@host:port:dir>]...
                      [--bwlimit <KB/s>] [--delete] [--dry-run]

说明:
  --dest 可重复。
  不传 --config 时依次查找当前目录和脚本目录的 sync_artifacts.conf。
USAGE
}

args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo "[ERROR] --config 需要文件路径" >&2; exit 1; }
      CONFIG_FILE="$2"; shift 2 ;;
    --source)
      [[ $# -ge 2 ]] || { echo "[ERROR] --source 需要目录" >&2; exit 1; }
      SOURCE_OVERRIDE="$2"; shift 2 ;;
    --dest)
      [[ $# -ge 2 ]] || { echo "[ERROR] --dest 需要目标" >&2; exit 1; }
      DEST_OVERRIDE+=("$2"); shift 2 ;;
    --bwlimit)
      [[ $# -ge 2 ]] || { echo "[ERROR] --bwlimit 需要数字" >&2; exit 1; }
      BWLIMIT_OVERRIDE="$2"; shift 2 ;;
    --delete) DELETE_OVERRIDE=true; shift ;;
    --dry-run|-n) DRY_RUN=true; shift ;;
    -h|--help|help) usage; exit 0 ;;
    *) echo "[ERROR] 未知参数: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "$CONFIG_FILE" ]]; then
  if [[ -f "$PWD/sync_artifacts.conf" ]]; then
    CONFIG_FILE="$PWD/sync_artifacts.conf"
  elif [[ -f "$DEFAULT_CONFIG" ]]; then
    CONFIG_FILE="$DEFAULT_CONFIG"
  else
    echo "[ERROR] 未找到 sync_artifacts.conf（可用 --config 指定）" >&2
    exit 1
  fi
fi
[[ -f "$CONFIG_FILE" ]] || { echo "[ERROR] 配置文件不存在: $CONFIG_FILE" >&2; exit 1; }
CONFIG_DIR="$(cd "$(dirname "$CONFIG_FILE")" && pwd)"
CONFIG_FILE="$CONFIG_DIR/$(basename "$CONFIG_FILE")"

SOURCE_DIR="podman"
REMOTE_DESTINATIONS=()
BWLIMIT_KBPS=0
LOG_DIR=""
SSH_KEY_FILE=""
SSH_EXTRA_OPTS=()
RSYNC_EXTRA_ARGS=()
DELETE_REMOTE=false

# shellcheck disable=SC1090
source "$CONFIG_FILE"

[[ -n "$SOURCE_OVERRIDE" ]] && SOURCE_DIR="$SOURCE_OVERRIDE"
[[ ${#DEST_OVERRIDE[@]} -gt 0 ]] && REMOTE_DESTINATIONS=("${DEST_OVERRIDE[@]}")
[[ -n "$BWLIMIT_OVERRIDE" ]] && BWLIMIT_KBPS="$BWLIMIT_OVERRIDE"
[[ -n "$DELETE_OVERRIDE" ]] && DELETE_REMOTE="$DELETE_OVERRIDE"

case "$SOURCE_DIR" in
  /*) ;;
  *) SOURCE_DIR="$CONFIG_DIR/$SOURCE_DIR" ;;
esac
LOG_DIR="${LOG_DIR:-$CONFIG_DIR/logs}"
case "$LOG_DIR" in
  /*) ;;
  *) LOG_DIR="$CONFIG_DIR/$LOG_DIR" ;;
esac

[[ -d "$SOURCE_DIR" ]] || { echo "[ERROR] 源目录不存在: $SOURCE_DIR" >&2; exit 1; }
[[ ${#REMOTE_DESTINATIONS[@]} -gt 0 ]] || { echo "[ERROR] 未配置 REMOTE_DESTINATIONS" >&2; exit 1; }
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/sync-$(date +%Y%m%d_%H%M%S).log"

log()  { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
is_true() {
  case "${1,,}" in
    1|true|yes|y|on) return 0 ;;
    *) return 1 ;;
  esac
}

SSH_CMD="ssh -o ConnectTimeout=15"
if [[ -n "$SSH_KEY_FILE" ]]; then
  printf -v _key_q '%q' "$SSH_KEY_FILE"
  SSH_CMD+=" -i ${_key_q}"
fi
if [[ ${#SSH_EXTRA_OPTS[@]} -gt 0 ]]; then
  for opt in "${SSH_EXTRA_OPTS[@]}"; do
    printf -v _opt_q '%q' "$opt"
    SSH_CMD+=" ${_opt_q}"
  done
fi

BWARG=""
if [[ "$BWLIMIT_KBPS" -gt 0 ]] 2>/dev/null; then
  BWARG="--bwlimit=${BWLIMIT_KBPS}"
fi
DELETE_ARG=""
is_true "$DELETE_REMOTE" && DELETE_ARG="--delete"

log "源目录: $SOURCE_DIR"
log "目标数量: ${#REMOTE_DESTINATIONS[@]}"
log "日志文件: $LOG_FILE"

TOTAL_OK=0
TOTAL_FAIL=0
for DEST in "${REMOTE_DESTINATIONS[@]}"; do
  [[ -n "$DEST" ]] || continue
  if [[ "$DEST" != *@*:*:* ]]; then
    warn "目标格式无效，应为 user@host:port:dir: $DEST"
    TOTAL_FAIL=$((TOTAL_FAIL+1))
    continue
  fi
  USER_HOST="${DEST%%:*}"
  REST="${DEST#*:}"
  PORT="${REST%%:*}"
  REMOTE_DIR="${REST#*:}"
  HOST="${USER_HOST#*@}"
  USER_NAME="${USER_HOST%%@*}"
  log "同步 -> ${USER_NAME}@${HOST}:${PORT}:${REMOTE_DIR}"

  set +e
  if [[ "$DRY_RUN" == true ]]; then
    printf '[DRY-RUN] rsync %s %s %s -e "%s" -- "%s/" "%s@%s:%s/"\n' \
      "$DELETE_ARG" "$BWARG" "${RSYNC_EXTRA_ARGS[*]}" "$SSH_CMD" "$SOURCE_DIR" "$USER_NAME" "$HOST" "$REMOTE_DIR"
    EXIT_CODE=0
  else
    rsync -avzP ${BWARG:-} ${DELETE_ARG:-} "${RSYNC_EXTRA_ARGS[@]}" -e "$SSH_CMD" -- \
      "${SOURCE_DIR}/" "${USER_NAME}@${HOST}:${REMOTE_DIR}/" 2>&1 | tee -a "$LOG_FILE"
    EXIT_CODE=${PIPESTATUS[0]}
  fi
  set -e

  if [[ "${EXIT_CODE:-1}" -eq 0 ]]; then
    log "同步成功: ${HOST}:${REMOTE_DIR}"
    TOTAL_OK=$((TOTAL_OK+1))
  else
    warn "同步失败(${EXIT_CODE}): ${HOST}:${REMOTE_DIR}"
    TOTAL_FAIL=$((TOTAL_FAIL+1))
  fi
done

log "完成: 成功 ${TOTAL_OK} 个，失败 ${TOTAL_FAIL} 个"
[[ "$TOTAL_FAIL" -eq 0 ]]
