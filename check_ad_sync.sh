#!/usr/bin/env bash

# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

# ============================================================
# ArtifactDepot 生产诊断脚本（只依赖宿主 python3）
#
# 用法：
#   bash check_ad_sync.sh [容器名]        # 默认容器名 artifactdepot
#   FORCE=podman bash check_ad_sync.sh
#   FORCE=docker bash check_ad_sync.sh
#
# 检查内容：
#   1. 容器状态
#   2. /data/depot 挂载与全部 bind mount
#   3. ARTIFACT_DEPOT_DIR / ARTIFACT_DEPOT_META_DIR 环境变量
#   4. 容器内 config/config.json 的 depot_dir / meta_dir
#   5. 应用 /health
#   6. 宿主 127.0.0.1:8004 端口连通性
#
# 说明：最终结果会逐项 PASS / WARN / FAIL；某项失败不会中断后续检查。
# ============================================================

set -uo pipefail

CONTAINER="${1:-artifactdepot}"

case "$CONTAINER" in
  -h|--help)
    cat <<'USAGE'
用法: bash check_ad_sync.sh [容器名]

默认容器名: artifactdepot
环境变量: FORCE=podman|docker
USAGE
    exit 0
    ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: 未找到 python3" >&2
  exit 1
fi

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

echo "容器工具: $TOOL"
echo "容器名:   $CONTAINER"
echo ""

exec python3 - "$TOOL" "$CONTAINER" <<'PY'
import json
import socket
import subprocess
import sys
import urllib.request

tool, container = sys.argv[1], sys.argv[2]
fail_count = 0


def say(kind, msg):
    print(f"[{kind}] {msg}")


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def ok(msg):
    say("PASS", msg)


def warn(msg):
    say("WARN", msg)


def fail(msg):
    global fail_count
    fail_count += 1
    say("FAIL", msg)


def info(msg):
    say("INFO", msg)


# --- 1. 容器状态 -----------------------------------------------------------
p = run([tool, "inspect", container])
if p.returncode != 0:
    fail(f"容器不存在或无法 inspect: {container}")
    print(p.stderr.strip())
    sys.exit(1)

try:
    data = json.loads(p.stdout)[0]
except Exception:
    fail("无法解析容器 inspect JSON")
    sys.exit(1)

state = data.get("State") or {}
if state.get("Running"):
    ok(f"容器运行中，PID={state.get('Pid', '?')}")
else:
    fail(f"容器未运行，状态={state.get('Status', 'unknown')}")

# --- 2. 挂载检查 -----------------------------------------------------------
mounts = data.get("Mounts") or []
if not mounts:
    warn("/data/depot 未发现 bind mount，数据可能写在容器内部")
else:
    ok(f"发现 {len(mounts)} 个挂载:")
    for m in mounts:
        src = m.get("Source", "?")
        dst = m.get("Destination", "?")
        mode = "rw" if m.get("RW", True) else "ro"
        print(f"       {src} → {dst} ({mode})")
    main = [m for m in mounts if m.get("Destination") == "/data/depot"]
    if main:
        ok(f"/data/depot 已挂载: {main[0].get('Source')}")
    else:
        fail("/data/depot 没有绑定到宿主机目录")

# --- 3. 环境变量 -----------------------------------------------------------
env_list = (data.get("Config") or {}).get("Env") or []
env = {}
for item in env_list:
    if "=" in item:
        k, v = item.split("=", 1)
        env[k] = v

depot_dir_env = env.get("ARTIFACT_DEPOT_DIR", "")
meta_dir_env = env.get("ARTIFACT_DEPOT_META_DIR", "")
token_env = env.get("ARTIFACT_DEPOT_ACCESS_TOKEN", "")

if depot_dir_env:
    ok(f"ARTIFACT_DEPOT_DIR={depot_dir_env}")
else:
    warn("ARTIFACT_DEPOT_DIR 未注入，depot_dir 将按 config.json 解析")

if meta_dir_env:
    info(f"ARTIFACT_DEPOT_META_DIR={meta_dir_env}（会覆盖 config.json 的 meta_dir）")
else:
    info("ARTIFACT_DEPOT_META_DIR 未注入（meta_dir 以 config.json 为准）")

# --- 4. 容器内 config.json -------------------------------------------------
# 优先用容器内实际生效的 ARTIFACT_DEPOT_CONFIG（部署脚本默认指向 /data/config/config.json），
# 回退到镜像内配置目录，兼容未挂载外部配置的部署方式。
def _env_of(name):
    r = run([tool, "exec", container, "printenv", name])
    return r.stdout.strip() if r.returncode == 0 else ""

config_path = _env_of("ARTIFACT_DEPOT_CONFIG") or "/data/config/config.json"
cp = run([tool, "exec", container, "cat", config_path])
if cp.returncode != 0:
    config_path = "/app/artifactdepot/src/artifactdepot/config/config.json"
    cp = run([tool, "exec", container, "cat", config_path])
config = {}
if cp.returncode == 0:
    try:
        config = json.loads(cp.stdout)
        ok("读取容器内 config/config.json 成功")
    except Exception as exc:
        fail(f"解析容器内 config.json 失败: {exc}")
else:
    fail(f"读取容器内 config.json 失败: {cp.stderr.strip()}")

def cfg(key, default=""):
    return config.get(key, default)

depot_dir = depot_dir_env or cfg("depot_dir", "")
meta_dir = meta_dir_env or cfg("meta_dir", "")
token = token_env or cfg("access_token", "")

if depot_dir:
    info(f"最终 depot_dir: {depot_dir}")
if meta_dir:
    info(f"最终 meta_dir: {meta_dir}")
else:
    info("最终 meta_dir: 空（状态文件放在 depot_dir 根下）")
if token:
    info("access_token/token: 已配置（不打印明文）")
else:
    warn("access_token/token 未配置，写入接口可能 401")

# --- 5. 应用 /health -------------------------------------------------------
try:
    with urllib.request.urlopen("http://127.0.0.1:8004/health", timeout=5) as resp:
        body = resp.read().decode("utf-8", "replace")
        ok(f"/health -> HTTP {resp.status} {body}")
except Exception as exc:
    fail(f"/health 不可达: {exc!r}")

# --- 6. 宿主端口连通性 -----------------------------------------------------
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(3)
try:
    sock.connect(("127.0.0.1", 8004))
    ok("宿主 127.0.0.1:8004 可连接")
except Exception as exc:
    fail(f"宿主 127.0.0.1:8004 不可连接: {exc!r}")
finally:
    sock.close()

print("")
if fail_count:
    print(f"ERROR: 诊断完成，有 {fail_count} 项 FAIL")
    sys.exit(1)
print("OK: 诊断完成，未发现 FAIL")
PY
