#!/usr/bin/env bash
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
#   3. ARTIFACT_DEPOT_DIR / ARTIFACT_DEPOT_META_DIR / ARTIFACT_DEPOT_DATAHUB_URL 环境变量
#   4. 容器内 resources/config.json 的 depot_dir / meta_dir / datahub_url
#   5. 容器内访问 DataHub users.json
#   6. 宿主机访问 DataHub users.json
#   7. 应用 /health
#   8. 应用实际 token 同步 /api/tokens/sync
#   9. 宿主 127.0.0.1:8004 端口连通性
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
  echo "❌ 未找到 python3" >&2
  exit 1
fi

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

echo "🔧 容器工具: $TOOL"
echo "📦 容器名:   $CONTAINER"
echo ""

exec python3 - "$TOOL" "$CONTAINER" <<'PY'
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
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
datahub_env = env.get("ARTIFACT_DEPOT_DATAHUB_URL", "")
token_env = env.get("ARTIFACT_DEPOT_ACCESS_TOKEN", "")

if depot_dir_env:
    ok(f"ARTIFACT_DEPOT_DIR={depot_dir_env}")
else:
    warn("ARTIFACT_DEPOT_DIR 未注入，depot_dir 将按 config.json 解析")

if meta_dir_env:
    info(f"ARTIFACT_DEPOT_META_DIR={meta_dir_env}（会覆盖 config.json 的 meta_dir）")
else:
    info("ARTIFACT_DEPOT_META_DIR 未注入（meta_dir 以 config.json 为准）")

if datahub_env:
    info(f"ARTIFACT_DEPOT_DATAHUB_URL={datahub_env}（会覆盖 config.json 的 datahub_url）")
else:
    info("ARTIFACT_DEPOT_DATAHUB_URL 未注入（datahub_url 以 config.json 为准）")

# --- 4. 容器内 config.json -------------------------------------------------
config_path = "/app/artifactdepot/src/artifactdepot/resources/config.json"
cp = run([tool, "exec", container, "cat", config_path])
config = {}
if cp.returncode == 0:
    try:
        config = json.loads(cp.stdout)
        ok("读取容器内 resources/config.json 成功")
    except Exception as exc:
        fail(f"解析容器内 config.json 失败: {exc}")
else:
    fail(f"读取容器内 config.json 失败: {cp.stderr.strip()}")

def cfg(key, default=""):
    return config.get(key, default)

depot_dir = depot_dir_env or cfg("depot_dir", "")
meta_dir = meta_dir_env or cfg("meta_dir", "")
datahub_url = datahub_env or cfg("datahub_url", "")
token = token_env or cfg("access_token", "")

if depot_dir:
    info(f"最终 depot_dir: {depot_dir}")
if meta_dir:
    info(f"最终 meta_dir: {meta_dir}")
else:
    info("最终 meta_dir: 空（状态文件放在 depot_dir 根下）")
if datahub_url:
    info(f"最终 datahub_url: {datahub_url}")
else:
    fail("datahub_url 为空，token 无法同步")
if token:
    info("access_token/token: 已配置（不打印明文）")
else:
    warn("access_token/token 未配置，写入/同步接口可能 401")

# --- 5. 容器内访问 DataHub -------------------------------------------------
if datahub_url:
    users_url = datahub_url.rstrip("/") + "/users.json"
    code = (
        "import sys, urllib.request\n"
        "try:\n"
        "    with urllib.request.urlopen(sys.argv[1], timeout=5) as r:\n"
        "        print(r.status)\n"
        "except Exception as e:\n"
        "    print('ERROR: ' + repr(e)); sys.exit(1)\n"
    )
    p = run([tool, "exec", container, "python3", "-c", code, users_url], timeout=15)
    if p.returncode == 0 and p.stdout.strip().startswith("2"):
        ok(f"容器内访问 DataHub 成功: {users_url} -> HTTP {p.stdout.strip()}")
    else:
        fail(f"容器内访问 DataHub 失败: {users_url}；{p.stdout.strip()} {p.stderr.strip()}")
else:
    fail("跳过容器内 DataHub 检查（datahub_url 为空）")

# --- 6. 宿主机访问 DataHub -------------------------------------------------
if datahub_url:
    users_url = datahub_url.rstrip("/") + "/users.json"
    try:
        with urllib.request.urlopen(users_url, timeout=5) as resp:
            ok(f"宿主机访问 DataHub 成功: {users_url} -> HTTP {resp.status}")
    except Exception as exc:
        warn(f"宿主机访问 DataHub 失败: {users_url}；{exc!r}（若 DataHub 仅容器内可达可忽略）")
else:
    warn("跳过宿主机 DataHub 检查（datahub_url 为空）")

# --- 7. 应用 /health -------------------------------------------------------
try:
    with urllib.request.urlopen("http://127.0.0.1:8004/health", timeout=5) as resp:
        body = resp.read().decode("utf-8", "replace")
        ok(f"/health -> HTTP {resp.status} {body}")
except Exception as exc:
    fail(f"/health 不可达: {exc!r}")

# --- 8. 应用实际 token 同步 ------------------------------------------------
if datahub_url and token:
    url = "http://127.0.0.1:8004/api/tokens/sync?token=" + urllib.parse.quote(token, safe="")
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body)
                result = payload.get("data") or {}
                if result.get("ok"):
                    ok(f"/api/tokens/sync 成功: {result}")
                else:
                    fail(f"/api/tokens/sync 返回 ok=false: {result}")
            except Exception:
                warn(f"/api/tokens/sync 返回不可解析: HTTP {resp.status} {body[:200]}")
    except urllib.error.HTTPError as exc:
        fail(f"/api/tokens/sync HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except Exception as exc:
        fail(f"/api/tokens/sync 请求失败: {exc!r}")
else:
    warn("跳过实际同步检查（缺少 datahub_url 或 token）")

# --- 9. 宿主端口连通性 -----------------------------------------------------
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
    print(f"❌ 诊断完成，有 {fail_count} 项 FAIL")
    sys.exit(1)
print("✅ 诊断完成，未发现 FAIL")
PY
