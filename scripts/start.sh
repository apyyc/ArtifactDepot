#!/usr/bin/env bash
# DataWarehouse 本地启动脚本（源码模式，端口 8004）
# 用法: ./scripts/start.sh [--port 8004] [--config /path/to/config.json] [--reload]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$DIR/dev_common.sh"

PORT="8004"
CONFIG=""
RELOAD=""                # DW 默认不热重载（稳定）；--reload 可选开启
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)   PORT="${2:?--port 需要参数}"; shift 2 ;;
    --config) CONFIG="${2:?--config 需要参数}"; shift 2 ;;
    --reload) RELOAD="--reload"; shift ;;
    *) echo "❌ 未知参数: $1"; exit 1 ;;
  esac
done

export WAREHOUSE_CONFIG="${CONFIG:-$DIR/src/datawarehouse/resources/config.json}"
cd "$DIR/src"
PY="$(find_uvicorn_python)" || exit 1
echo "[DataWarehouse] 启动于 0.0.0.0:${PORT}  reload=${RELOAD:-off}  python=${PY}"
echo "[DataWarehouse] 配置: ${WAREHOUSE_CONFIG}"
exec env PYTHONPATH=. "$PY" -m uvicorn datawarehouse.main:app --host 0.0.0.0 --port "$PORT" $RELOAD
