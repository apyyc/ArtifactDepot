#!/usr/bin/env bash

# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

# ============================================================
# ArtifactDepot 开发环境一键启动脚本（无图形 / SSH 版）
# 后台运行数据仓库服务，日志写到 logs/ 目录
#
#  服务           命令                                                          端口    日志文件
#  ArtifactDepot  <项目本地 venv>/python -m uvicorn artifactdepot.main:app      8004    logs/artifactdepot.log
#
# 用法：
#   ./dev_start_headless.sh            # 后台启动
#   ./dev_start_headless.sh check      # 检查本地 Python 环境（解释器/依赖/是否 venv）
#   ./dev_start_headless.sh status     # 查看是否在跑
#   ./dev_start_headless.sh tail       # 跟踪日志（Ctrl+C 退出）
#   ./dev_start_headless.sh logs       # 查看日志文件列表
#   ./dev_start_headless.sh stop       # 停止
#
# 依赖：bash + 项目本地 venv（推荐用 uv 创建）：
#   cd /home/apyyc/work/ArtifactDepot
#   .tools/uv venv .venv --python /usr/bin/python3.13
#   .tools/uv pip install --python .venv/bin/python -r ArtifactDepot/requirements.txt
# 解释器解析顺序：$AD_PYTHON > ./ .venv（项目内）> ../.venv（工作区根）> python3
# ============================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${ROOT}/logs"
mkdir -p "$LOGDIR"

# ---------- 服务配置 ----------
SERVICE_DIR="${ROOT}"
HOST_PORT="${AD_PORT:-8004}"           # 服务端口（可用 AD_PORT 环境变量覆盖）
CONFIG_FILE="${ARTIFACT_DEPOT_CONFIG:-${ROOT}/src/artifactdepot/config/config.json}"

# ---------- 本地 Python 环境解析（优先项目/工作区 venv，避免误用系统 Python） ----------
_abs_python() {
  local p="$1"
  (cd "$(dirname "$p")" 2>/dev/null && printf '%s/%s' "$PWD" "$(basename "$p")")
}

resolve_python() {
  if [ -n "${AD_PYTHON:-}" ]; then
    PYTHON_BIN="$AD_PYTHON"
    if [ -x "$PYTHON_BIN" ]; then
      PYTHON_BIN="$(_abs_python "$PYTHON_BIN")"
    fi
    return
  fi
  for candidate in \
    "${ROOT}/.venv/bin/python" \
    "${ROOT}/../.venv/bin/python" \
    "${ROOT}/../../.venv/bin/python"; do
    if [ -x "$candidate" ]; then
      PYTHON_BIN="$(_abs_python "$candidate")"
      [ -n "$PYTHON_BIN" ] || PYTHON_BIN="$candidate"
      return
    fi
  done
  PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
}
resolve_python

check_env() {
  echo "项目根目录: $ROOT"
  echo "配置来源:   $CONFIG_FILE"
  if [ -z "${PYTHON_BIN:-}" ] || [ ! -x "$PYTHON_BIN" ]; then
    echo "ERROR: 未找到可执行的 Python 解释器（可用 AD_PYTHON=/path/to/python 指定）"
    return 1
  fi
  echo "Python:     $PYTHON_BIN"
  PYTHON_PREFIX="$("$PYTHON_BIN" -c 'import sys; print(sys.prefix)' 2>/dev/null || true)"
  if [ -n "$PYTHON_PREFIX" ] && [ -f "$PYTHON_PREFIX/pyvenv.cfg" ]; then
    echo "环境:       本地虚拟环境（venv）: $PYTHON_PREFIX"
  else
    echo "环境:       系统 Python（非 venv；建议用 uv 创建本地 .venv）"
  fi
  "$PYTHON_BIN" - <<'PY' || return 1
import sys
try:
    import fastapi, uvicorn, httpx, multipart
except Exception as exc:  # noqa: BLE001
    print(f"ERROR: 缺少运行依赖: {exc}")
    raise SystemExit(1)
print(f"sys.executable: {sys.executable}")
print(f"版本: Python {sys.version.split()[0]}, fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}")
PY
}
# 直接以源码模式启动（不再依赖 scripts/start.sh；PYTHONPATH 指向 src）
START_CMD="env PYTHONPATH=src ARTIFACT_DEPOT_CONFIG=${CONFIG_FILE} ${PYTHON_BIN} -m uvicorn artifactdepot.main:app --host 0.0.0.0 --port ${HOST_PORT}"

# ---------- 当前主机 IP（打印服务地址用） ----------
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$HOST_IP" ] && HOST_IP="127.0.0.1"

# 记录后台进程 PID 的文件
PIDFILE="${LOGDIR}/.dev_pids"

# ---------- PID 记录 ----------
SERVICED_PID=""
write_pid() { SERVICED_PID="$1"; }
save_pid() { echo "$SERVICED_PID" > "$PIDFILE"; }
load_pid() { SERVICED_PID="$(cat "$PIDFILE" 2>/dev/null || echo "")"; }

# ---------- 后台启动服务 ----------
start_one() {
  echo "  - 启动 artifactdepot : $START_CMD"
  # 直接在主 shell 后台启动 + disown 脱离任务表 + stdin 脱离，避免脚本在后台服务上 do_wait 挂住不退出
  cd "$SERVICE_DIR" || exit 1
  nohup bash -c "$START_CMD" > "${LOGDIR}/artifactdepot.log" 2>&1 < /dev/null &
  echo $! > "${LOGDIR}/.pid_dw"
  disown
  write_pid "$(cat "${LOGDIR}/.pid_dw" 2>/dev/null)"
  rm -f "${LOGDIR}/.pid_dw"
}

# ---------- 停止服务 ----------
stop_one() {
  local pid="${SERVICED_PID:-}"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "  ⏹  停止 artifactdepot (PID $pid) ..."
    kill "$pid" 2>/dev/null || true
  else
    echo "  ⏹  artifactdepot 未在运行"
  fi
}

# ---------- 端口预检：被占用则清理 ----------
check_and_free_port() {
  local port="$1"
  if ss -tlnp 2>/dev/null | grep -q ":${port}\b"; then
    echo "  WARN: 端口 ${port} 被占用，清理旧进程 ..."
    fuser -k "${port}/tcp" 2>/dev/null || pkill -f "uvicorn artifactdepot.main" 2>/dev/null || true
    sleep 1
    if ss -tlnp 2>/dev/null | grep -q ":${port}\b"; then
      echo "  ERROR: 端口 ${port} 仍被占用，可能需要 sudo 清理：sudo fuser -k ${port}/tcp"
    fi
  fi
}

# ---------- 启动（供 start / restart 复用） ----------
do_start() {
  echo "项目根目录: $ROOT"
  echo "日志目录:   $LOGDIR"
  echo ""
  echo "后台启动数据仓库 ..."
  check_env || { echo "ERROR: 本地环境检查未通过，启动中止"; exit 1; }

  # 先停旧进程 + 端口预检
  load_pid
  stop_one
  sleep 1
  check_and_free_port "$HOST_PORT"

  start_one
  save_pid

  echo ""
  echo "OK: 已后台启动。"
  echo "   查看日志:  ./dev_start_headless.sh tail"
  echo "   查看状态:  ./dev_start_headless.sh status"
  echo "   停止:      ./dev_start_headless.sh stop"
  echo "   重启:      ./dev_start_headless.sh restart"
  echo ""
  echo "   服务地址:"
  echo "     网页浏览     http://${HOST_IP}:${HOST_PORT}/"
  echo "     Swagger 文档 http://${HOST_IP}:${HOST_PORT}/docs"
  echo "     健康检查     http://${HOST_IP}:${HOST_PORT}/health"
  echo ""
  echo "   提示: 若写入 401，用 --token 或 ARTIFACT_DEPOT_ACCESS_TOKEN 设共享 access_token，"
  echo "         或在成员列表查用户 token 后填写"
}

# ---------- 停止（供 stop / restart 复用） ----------
do_stop() {
  echo "停止数据仓库 ..."
  load_pid
  stop_one
  pkill -f "uvicorn artifactdepot.main" 2>/dev/null && echo "  ⏹  清理残留 uvicorn 进程" || true
  rm -f "$PIDFILE"
  echo "OK: 已停止"
}

# ---------- 主逻辑 ----------
CMD="${1:-start}"
case "$CMD" in
  start)
    do_start
    ;;

  stop)
    do_stop
    ;;

  check)
    check_env
    exit $?
    ;;

  restart)
    echo "重启数据仓库 ..."
    do_stop
    sleep 1
    do_start
    ;;

  status)
    echo "服务状态："
    echo "  Python: $PYTHON_BIN"
    load_pid
    pid="${SERVICED_PID:-}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "  OK: artifactdepot  运行中 (PID $pid)"
    else
      if curl -s -m 2 "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
        echo "  OK: artifactdepot  运行中（端口响应）"
      else
        if tail -n 3 "${LOGDIR}/artifactdepot.log" 2>/dev/null | grep -qiE "error|exception|failed"; then
          echo "  ERROR: artifactdepot  已退出（日志有错误）"
        else
          echo "  WARN: artifactdepot  未检测到 PID（可能已退出，看日志）"
        fi
      fi
    fi
    echo ""
    echo "   查看日志: ./dev_start_headless.sh logs"
    ;;

  logs)
    echo "日志文件（$LOGDIR）："
    ls -lht "${LOGDIR}"/*.log 2>/dev/null | awk '{print "  " $5 "  " $NF}' || echo "  （暂无日志）"
    ;;

  tail)
    echo "跟踪数据仓库日志（Ctrl+C 退出）..."
    tail -f "${LOGDIR}/artifactdepot.log" 2>/dev/null || echo "  （暂无日志）"
    ;;

  *)
    echo "用法: $0 [start|check|stop|restart|status|logs|tail]"
    ;;
esac
