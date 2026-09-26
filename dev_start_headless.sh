#!/usr/bin/env bash
# ============================================================
# DataWarehouse 开发环境一键启动脚本（无图形 / SSH 版）
# 后台运行数据仓库服务，日志写到 logs/ 目录
#
#  服务           命令                      端口    日志文件
#  DataWarehouse  bash scripts/start.sh     8004    logs/datawarehouse.log
#
# 用法：
#   ./dev_start_headless.sh            # 后台启动
#   ./dev_start_headless.sh status     # 查看是否在跑
#   ./dev_start_headless.sh tail       # 跟踪日志（Ctrl+C 退出）
#   ./dev_start_headless.sh logs       # 查看日志文件列表
#   ./dev_start_headless.sh stop       # 停止
#
# 依赖：bash、python3（含 uvicorn/fastapi/httpx/python-multipart）
# ============================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${ROOT}/logs"
mkdir -p "$LOGDIR"

# ---------- 服务配置 ----------
SERVICE_DIR="${ROOT}"
HOST_PORT="${DW_PORT:-8004}"           # 服务端口（可用 DW_PORT 环境变量覆盖）
START_CMD="bash scripts/start.sh --port ${HOST_PORT}"   # 复用项目的启动脚本（自动找带 uvicorn 的 python，并传端口）

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
  echo "  ▶ 启动 datawarehouse : $START_CMD"
  # 直接在主 shell 后台启动 + disown 脱离任务表 + stdin 脱离，避免脚本在后台服务上 do_wait 挂住不退出
  cd "$SERVICE_DIR" || exit 1
  nohup bash -c "$START_CMD" > "${LOGDIR}/datawarehouse.log" 2>&1 < /dev/null &
  echo $! > "${LOGDIR}/.pid_dw"
  disown
  write_pid "$(cat "${LOGDIR}/.pid_dw" 2>/dev/null)"
  rm -f "${LOGDIR}/.pid_dw"
}

# ---------- 停止服务 ----------
stop_one() {
  local pid="${SERVICED_PID:-}"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "  ⏹  停止 datawarehouse (PID $pid) ..."
    kill "$pid" 2>/dev/null || true
  else
    echo "  ⏹  datawarehouse 未在运行"
  fi
}

# ---------- 端口预检：被占用则清理 ----------
check_and_free_port() {
  local port="$1"
  if ss -tlnp 2>/dev/null | grep -q ":${port}\b"; then
    echo "  ⚠️  端口 ${port} 被占用，清理旧进程 ..."
    fuser -k "${port}/tcp" 2>/dev/null || pkill -f "uvicorn datawarehouse.main" 2>/dev/null || true
    sleep 1
    if ss -tlnp 2>/dev/null | grep -q ":${port}\b"; then
      echo "  ❌ 端口 ${port} 仍被占用，可能需要 sudo 清理：sudo fuser -k ${port}/tcp"
    fi
  fi
}

# ---------- 启动（供 start / restart 复用） ----------
do_start() {
  echo "📁 项目根目录: $ROOT"
  echo "📄 日志目录:   $LOGDIR"
  echo ""
  echo "🚀 后台启动数据仓库 ..."

  # 先停旧进程 + 端口预检
  load_pid
  stop_one
  sleep 1
  check_and_free_port "$HOST_PORT"

  start_one
  save_pid

  echo ""
  echo "✅ 已后台启动。"
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
  echo "   提示: 若写入 401，用 --token 或 WAREHOUSE_ACCESS_TOKEN 设共享 access_token，"
  echo "         或在成员列表查用户 token 后填写"
}

# ---------- 停止（供 stop / restart 复用） ----------
do_stop() {
  echo "🛑 停止数据仓库 ..."
  load_pid
  stop_one
  pkill -f "uvicorn datawarehouse.main" 2>/dev/null && echo "  ⏹  清理残留 uvicorn 进程" || true
  rm -f "$PIDFILE"
  echo "✅ 已停止"
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

  restart)
    echo "🔄 重启数据仓库 ..."
    do_stop
    sleep 1
    do_start
    ;;

  status)
    echo "📊 服务状态："
    load_pid
    pid="${SERVICED_PID:-}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "  ✅ datawarehouse  运行中 (PID $pid)"
    else
      if curl -s -m 2 "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
        echo "  ✅ datawarehouse  运行中（端口响应）"
      else
        if tail -n 3 "${LOGDIR}/datawarehouse.log" 2>/dev/null | grep -qiE "error|exception|failed"; then
          echo "  ❌ datawarehouse  已退出（日志有错误）"
        else
          echo "  ⚠️  datawarehouse  未检测到 PID（可能已退出，看日志）"
        fi
      fi
    fi
    echo ""
    echo "   查看日志: ./dev_start_headless.sh logs"
    ;;

  logs)
    echo "📄 日志文件（$LOGDIR）："
    ls -lht "${LOGDIR}"/*.log 2>/dev/null | awk '{print "  " $5 "  " $NF}' || echo "  （暂无日志）"
    ;;

  tail)
    echo "🖥  跟踪数据仓库日志（Ctrl+C 退出）..."
    tail -f "${LOGDIR}/datawarehouse.log" 2>/dev/null || echo "  （暂无日志）"
    ;;

  *)
    echo "用法: $0 [start|stop|restart|status|logs|tail]"
    ;;
esac
