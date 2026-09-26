#!/usr/bin/env bash
# ============================================================
# dev_common.sh — 开发环境共享函数（被各 per-service start.sh source）
#
# 用法（在 start.sh 顶部）：
#   source "$(cd "$(dirname "$0")/.." && pwd)/dev_common.sh"
# ============================================================

# 找到带 uvicorn/fastapi 的 python3。
# 登录 shell 里 python3 可能是 /usr/bin/python3（无 uvicorn），故按候选列表逐个探测。
# 输出 python 路径到 stdout；找不到则报错并返回非 0。
find_uvicorn_python() {
  local py=""
  for cand in python3 /usr/local/python311/bin/python3 /usr/local/bin/python3.11 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import uvicorn, fastapi" >/dev/null 2>&1; then
      py="$cand"
      break
    fi
  done
  if [ -z "$py" ]; then
    echo "❌ 未找到带 uvicorn/fastapi 的 python3（请检查安装）" >&2
    return 1
  fi
  printf '%s\n' "$py"
}
