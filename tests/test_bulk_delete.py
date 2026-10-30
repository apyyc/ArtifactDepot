# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""批量删除目录内容（一键清空）后端回归测试 —— 契约 §2.4 的 10 项

覆盖：
  1. 预览 files / all 的计数与大小；dry_run 后磁盘不变
  2. files：直接文件删除；子目录及内部文件、.keep/隐藏项保留；清单项清理
  3. all：递归文件+目录删除；prefix 目录保留；嵌套目录删除；隐藏文件删除；
     bucket 根 .warehouse.json 保留
  4. confirm 缺失/错误 → 400；正确 → 200（服务端二次校验）
  5. 无 token 401 / 缺 scope 403 / 范围内 200 / 范围外 403 / 管理员 200 / operator 预设含新权限点
  6. prefix 不存在 → 404；prefix 是文件 → 404；mode 非法 → 400；项数超限 → 400
  7. 审计写入 delete_bulk；dry_run 不写；DELETES 指标仅在执行时 +1
  8. 符号链接不被跟随（目标文件/目录保留，只删链接本身）
  9. UI 标记：app.js 含 data-action="bulk-delete"，index.html 含 bulkDeleteModal
 10. 全量回归由命令行执行（见任务说明）

运行：
    cd ArtifactDepot/ArtifactDepot
    ../.testenv/bin/python -m pytest -q tests/test_bulk_delete.py
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "bulk-delete-admin-0123456789"
BULK = "/api/objects/bulk-delete"

# 状态文件不属于业务数据，快照对比时排除
_SNAP_EXCLUDE = {"tokens.json", "audit.log", "signed_links.json", ".state.lock"}


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

def _write_config(cfg: Path, depot: Path) -> None:
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "ui_enabled": True, "max_upload_mb": 0,
        "require_read_token": False,
    }), encoding="utf-8")


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    _write_config(cfg, depot)
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, depot, tmp_path
    config._CONFIG = None


def _audit(depot: Path) -> list:
    p = depot / "audit.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _manifest(depot: Path, bucket: str) -> dict:
    p = depot / bucket / ".warehouse.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _snapshot(depot: Path) -> dict:
    """业务数据快照：目录 + 文件内容（排除 tokens/audit/signed_links/锁文件）。"""
    out = {}
    for p in sorted(depot.rglob("*")):
        rel = p.relative_to(depot).as_posix()
        if rel in _SNAP_EXCLUDE:
            continue
        if p.is_dir():
            out[rel + "/"] = None
        elif p.is_file():
            out[rel] = p.read_bytes()
    return out


def _metric(c, name: str) -> float:
    text = c.get("/metrics").text
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _detail(resp) -> str:
    try:
        return str(resp.json().get("detail", ""))
    except Exception:
        return resp.text


def _mkbucket(c, name: str) -> None:
    r = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": name})
    assert r.status_code == 200, r.text


def _up(c, bucket: str, key: str, data: bytes = b"x") -> None:
    r = c.post("/api/objects", params={"token": ADMIN},
               data={"bucket": bucket, "key": key},
               files={"file": (Path(key).name, data)})
    assert r.status_code == 200, r.text


def _mkdir(c, bucket: str, key: str) -> None:
    r = c.post("/api/objects/mkdir", params={"token": ADMIN},
               json={"bucket": bucket, "key": key})
    assert r.status_code == 200, r.text


def _token(c, user: str, scopes=None, buckets=None, prefixes=None, role: str = "custom") -> str:
    body = {"user": user, "role": role}
    if scopes is not None:
        body["scopes"] = scopes
    if buckets is not None:
        body["allow_buckets"] = buckets
    if prefixes is not None:
        body["allow_prefixes"] = prefixes
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _bulk(c, bucket: str, mode: str = "files", prefix: str = "",
          dry_run: bool = True, confirm=None, token: str = ADMIN):
    body = {"bucket": bucket, "prefix": prefix, "mode": mode, "dry_run": dry_run}
    if confirm is not None:
        body["confirm"] = confirm
    if token is None:
        return c.post(BULK, json=body)
    return c.post(BULK, params={"token": token}, json=body)


def _seed_tree(c, depot: Path) -> None:
    """构造 demo bucket：直接文件 2 个、子目录/嵌套文件、隐藏文件/隐藏目录、.keep。"""
    _mkbucket(c, "demo")
    _up(c, "demo", "top1.txt", b"AAA")                       # 3
    _up(c, "demo", "top2.bin", b"BBBBB")                     # 5
    _up(c, "demo", "sub/nested.txt", b"CCCCCCC")             # 7
    _up(c, "demo", "sub/deep/x.txt", b"DDDDDDDDDDD")         # 11
    _mkdir(c, "demo", "sub/empty")                           # sub/empty/.keep（0）
    (depot / "demo" / ".hidden").write_bytes(b"HH")          # 手工隐藏文件（2）
    (depot / "demo" / ".hdir").mkdir()
    (depot / "demo" / ".hdir" / "inner.txt").write_bytes(b"II")   # 2


# ===========================================================================
# 1. 预览：files / all 计数与大小；dry_run 不落盘
# ===========================================================================

def test_preview_files_and_all_counts_dry_run_no_change(ctx):
    """预览返回将删除的文件数/目录数/字节数；dry_run 后磁盘、清单、审计全部不变。"""
    c, depot, _ = ctx
    _seed_tree(c, depot)
    snap = _snapshot(depot)
    manifest_before = _manifest(depot, "demo")
    audit_before = len(_audit(depot))

    # files 模式：只算 bucket 根的可见直接文件 top1.txt + top2.bin
    r = _bulk(c, "demo", mode="files", prefix="", dry_run=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0 and body["message"] == "success"
    data = body["data"]
    assert data["bucket"] == "demo" and data["prefix"] == ""
    assert data["mode"] == "files" and data["dry_run"] is True
    assert data["files"] == 2 and data["dirs"] == 0 and data["bytes"] == 8
    assert data["errors"] == []
    assert isinstance(data["elapsed_ms"], int) and data["elapsed_ms"] >= 0

    # all 模式：递归所有文件（含隐藏 .keep/.hidden/.hdir）+ 全部子目录，排除根清单
    r = _bulk(c, "demo", mode="all", prefix="", dry_run=True)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["files"] == 8 and data["dirs"] == 4 and data["bytes"] == 30
    assert data["dry_run"] is True and data["errors"] == []

    # 子目录前缀预览：sub 下直接文件 1 个；all 下文件 3 个、目录 2 个
    # prefix 省略等价 ""；"/" 也规范化为根目录
    data = c.post(BULK, params={"token": ADMIN},
                  json={"bucket": "demo", "mode": "files", "dry_run": True}).json()["data"]
    assert data["prefix"] == "" and data["files"] == 2
    data = _bulk(c, "demo", mode="files", prefix="/", dry_run=True).json()["data"]
    assert data["prefix"] == "" and data["files"] == 2

    data = _bulk(c, "demo", mode="files", prefix="sub/", dry_run=True).json()["data"]
    assert data["prefix"] == "sub" and data["files"] == 1
    assert data["dirs"] == 0 and data["bytes"] == 7
    data = _bulk(c, "demo", mode="all", prefix="sub", dry_run=True).json()["data"]
    assert data["files"] == 3 and data["dirs"] == 2 and data["bytes"] == 18

    # 预览不落盘：磁盘 / 清单 / 审计都不变
    assert _snapshot(depot) == snap
    assert _manifest(depot, "demo") == manifest_before
    assert len(_audit(depot)) == audit_before


# ===========================================================================
# 2. files 模式：只删直接文件，保留子目录/隐藏项，清理清单
# ===========================================================================

def test_files_mode_deletes_only_direct_files_and_cleans_manifest(ctx):
    """files：删直接文件；子目录及内部文件、.keep、隐藏项保留；清单对应项移除。"""
    c, depot, _ = ctx
    _seed_tree(c, depot)
    base = depot / "demo"

    r = _bulk(c, "demo", mode="files", dry_run=False, confirm="demo")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["dry_run"] is False
    assert data["files"] == 2 and data["dirs"] == 0 and data["bytes"] == 8
    assert data["errors"] == []

    # 直接文件被删除
    assert not (base / "top1.txt").exists()
    assert not (base / "top2.bin").exists()
    # 子目录及其内容保留；prefix（bucket 根）保留
    assert (base / "sub" / "nested.txt").read_bytes() == b"CCCCCCC"
    assert (base / "sub" / "deep" / "x.txt").read_bytes() == b"DDDDDDDDDDD"
    assert (base / "sub" / "empty" / ".keep").is_file()
    # 隐藏项与清单保留
    assert (base / ".keep").is_file()
    assert (base / ".hidden").read_bytes() == b"HH"
    assert (base / ".hdir" / "inner.txt").read_bytes() == b"II"
    assert (base / ".warehouse.json").is_file()

    # 清单：被删文件的 key 移除，子目录条目保留
    manifest = _manifest(depot, "demo")
    assert "top1.txt" not in manifest and "top2.bin" not in manifest
    assert "sub/nested.txt" in manifest and "sub/deep/x.txt" in manifest

    # 审计留痕：delete_bulk + mode/files 统计
    row = _audit(depot)[-1]
    assert row["action"] == "delete_bulk" and row["bucket"] == "demo" and row["key"] == ""
    assert row["size"] == 8 and row["mode"] == "files"
    assert row["files"] == 2 and row["dirs"] == 0


# ===========================================================================
# 3. all 模式：递归删文件+子目录，保留 prefix 目录与根清单
# ===========================================================================

def test_all_mode_recursive_keeps_prefix_dir_and_root_manifest(ctx):
    """all：递归删除文件与子目录（含隐藏项），prefix 目录本身与根清单保留，清单清理。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "task/交付物/a.txt", b"AAA")                 # 3
    _up(c, "demo", "task/交付物/b/c.txt", b"CC")                # 2
    _up(c, "demo", "task/交付物/b/deep/d.txt", b"DDDD")         # 4
    _mkdir(c, "demo", "task/交付物/empty")                      # .keep（0）
    (depot / "demo" / "task" / "交付物" / ".secret").write_bytes(b"SS")  # 2
    _up(c, "demo", "task/keepme.txt", b"KEEP")                  # 保留（prefix 外）
    target = depot / "demo" / "task" / "交付物"

    r = _bulk(c, "demo", mode="all", prefix="task/交付物", dry_run=False, confirm="交付物")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["prefix"] == "task/交付物"
    assert data["files"] == 5 and data["dirs"] == 3 and data["bytes"] == 11
    assert data["errors"] == []

    # prefix 目录本身保留，内容清空
    assert target.is_dir()
    assert list(target.iterdir()) == []
    # 嵌套目录、隐藏文件均已删除
    assert not (depot / "demo" / "task" / "交付物" / "b").exists()
    assert not (depot / "demo" / "task" / "交付物" / ".secret").exists()
    # prefix 外的文件保留
    assert (depot / "demo" / "task" / "keepme.txt").read_bytes() == b"KEEP"
    # bucket 根清单保留且被清理（只剩 prefix 外条目）
    assert (depot / "demo" / ".warehouse.json").is_file()
    assert _manifest(depot, "demo") == {
        "task/keepme.txt": _manifest(depot, "demo")["task/keepme.txt"]}
    # 审计 key 用规范化前缀，extra 统计正确
    row = _audit(depot)[-1]
    assert row["action"] == "delete_bulk" and row["key"] == "task/交付物"
    assert row["size"] == 11 and row["mode"] == "all"
    assert row["files"] == 5 and row["dirs"] == 3


def test_all_mode_bucket_root_keeps_bucket_and_manifest(ctx):
    """all 作用在 bucket 根：删除全部内容，但 bucket 目录与 .warehouse.json 保留。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "f1.txt", b"AA")              # 2
    _up(c, "demo", "sub/f2.txt", b"BBB")         # 3
    _mkdir(c, "demo", "emptydir")                # emptydir/.keep（0）
    base = depot / "demo"

    r = _bulk(c, "demo", mode="all", dry_run=False, confirm="demo")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["files"] == 4 and data["dirs"] == 2 and data["bytes"] == 5
    assert data["errors"] == []

    assert base.is_dir(), "bucket 根目录本身必须保留"
    manifest_file = base / ".warehouse.json"
    assert manifest_file.is_file(), "bucket 根 .warehouse.json 永远不能删除"
    assert _manifest(depot, "demo") == {}
    remaining = [p.name for p in base.iterdir()]
    assert remaining == [".warehouse.json"], remaining


# ===========================================================================
# 4. confirm 服务端二次校验
# ===========================================================================

def test_confirm_required_and_must_match(ctx):
    """dry_run 不需要 confirm；执行时缺失/错误 → 400 且不删；正确 → 200。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "f.txt", b"F")
    _up(c, "demo", "sub/g.txt", b"G")
    f = depot / "demo" / "f.txt"

    # 预览无需 confirm
    assert _bulk(c, "demo", mode="files", dry_run=True).status_code == 200

    # 缺失 confirm → 400，零改动、零审计
    before = _snapshot(depot)
    n = len(_audit(depot))
    r = _bulk(c, "demo", mode="files", dry_run=False)
    assert r.status_code == 400 and "确认" in _detail(r)
    assert _snapshot(depot) == before and len(_audit(depot)) == n

    # 错误 confirm → 400
    r = _bulk(c, "demo", mode="files", dry_run=False, confirm="wrong")
    assert r.status_code == 400 and "确认" in _detail(r)
    assert f.is_file() and len(_audit(depot)) == n

    # prefix 最后一段才是目录名：prefix=sub 时 confirm 应为 sub（bucket 名无效）
    assert _bulk(c, "demo", mode="files", prefix="sub", dry_run=False,
                 confirm="demo").status_code == 400
    # 前后空白按 strip 后比较，可接受
    r = _bulk(c, "demo", mode="files", prefix="sub", dry_run=False, confirm="  sub  ")
    assert r.status_code == 200, r.text
    assert not (depot / "demo" / "sub" / "g.txt").exists()

    # 正确 confirm 才真正执行；dry_run 省略时默认 false（真删）
    r = c.post(BULK, params={"token": ADMIN},
               json={"bucket": "demo", "prefix": "", "mode": "files", "confirm": "demo"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["dry_run"] is False
    assert not f.exists()
    assert _audit(depot)[-1]["action"] == "delete_bulk"


# ===========================================================================
# 5. 鉴权 / 权限点 / 资源范围 / 角色预设
# ===========================================================================

def test_auth_scope_and_role_presets(ctx):
    """401/403/200 矩阵；operator 预设含新权限点；admin 自动全量。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _mkbucket(c, "secret")
    _up(c, "demo", "a/1.txt", b"1")
    _up(c, "demo", "b/2.txt", b"2")
    _up(c, "secret", "s.txt", b"SECRET")

    # 无 token / 无效 token → 401
    assert _bulk(c, "demo", mode="files", dry_run=True, token=None).status_code == 401
    assert _bulk(c, "demo", mode="files", dry_run=True, token="bad-token").status_code == 401

    # 有效 token 但缺 object:delete_bulk → 403，数据不变
    nobody = _token(c, "nobody", scopes=["object:list", "object:delete"])
    snap = _snapshot(depot)
    r = _bulk(c, "demo", mode="all", dry_run=False, confirm="demo", token=nobody)
    assert r.status_code == 403 and "object:delete_bulk" in _detail(r)
    assert _snapshot(depot) == snap

    # 带 scope 且范围内 → 200
    actor = _token(c, "actor", scopes=["object:delete_bulk"],
                   buckets=["demo"], prefixes={"demo": ["a/"]})
    assert _bulk(c, "demo", mode="files", prefix="a", dry_run=True,
                 token=actor).status_code == 200
    r = _bulk(c, "demo", mode="files", prefix="a", dry_run=False, confirm="a", token=actor)
    assert r.status_code == 200, r.text
    assert not (depot / "demo" / "a" / "1.txt").exists()

    # token 资源范围外：越界前缀 / 越界 bucket / 根目录（不在 a/ 前缀内）→ 403
    snap = _snapshot(depot)
    for body in ({"mode": "files", "prefix": "b", "confirm": "b"},
                 {"mode": "files", "prefix": "", "confirm": "demo"},
                 {"mode": "files", "prefix": "", "confirm": "secret"}):
        r = c.post(BULK, params={"token": actor},
                   json={"bucket": "secret" if body["confirm"] == "secret" else "demo",
                         "dry_run": False, **body})
        assert r.status_code == 403, (body, r.text)
        assert "资源范围" in _detail(r)
    assert _snapshot(depot) == snap

    # operator 预设包含新权限点，可直接执行
    op = _token(c, "operator-user", role="operator")
    r = _bulk(c, "demo", mode="files", dry_run=False, confirm="demo", token=op)
    assert r.status_code == 200, r.text
    assert _audit(depot)[-1]["actor"] == "operator-user"

    # 管理员共享 token → 200
    assert _bulk(c, "secret", mode="files", dry_run=False,
                 confirm="secret", token=ADMIN).status_code == 200

    # 权限目录与角色预设
    catalog = c.get("/api/auth/permissions").json()["data"]
    entry = [p for p in catalog["permissions"] if p["key"] == "object:delete_bulk"]
    assert len(entry) == 1
    entry = entry[0]
    assert entry["group"] == "对象删除" and entry["method"] == "POST"
    assert entry["path"] == "/api/objects/bulk-delete" and entry["write"] is True
    roles = {r0["key"]: r0["scopes"] for r0 in catalog["roles"]}
    assert "object:delete_bulk" in roles["operator"]
    assert "object:delete_bulk" in roles["admin"]
    assert "object:delete_bulk" not in roles["user"]
    assert "object:delete_bulk" not in roles["uploader"]


# ===========================================================================
# 6. 错误码与上限
# ===========================================================================

def test_error_codes_prefix_mode_and_limit(ctx, monkeypatch):
    """404（bucket/prefix 不存在、prefix 是文件）、400（mode/prefix/项数超限）。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "f.txt", b"F")
    _up(c, "demo", "sub/g.txt", b"G")

    # bucket / prefix 不存在 → 404
    r = _bulk(c, "ghost", mode="files", dry_run=True)
    assert r.status_code == 404
    assert _bulk(c, "demo", mode="files", prefix="nope", dry_run=True).status_code == 404
    assert _bulk(c, "demo", mode="all", prefix="nope/deep", dry_run=True).status_code == 404
    # prefix 是文件而非目录 → 404
    assert _bulk(c, "demo", mode="files", prefix="f.txt", dry_run=True).status_code == 404

    # mode 非法 / prefix 非法 → 400
    snap = _snapshot(depot)
    assert _bulk(c, "demo", mode="partial", dry_run=True).status_code == 400
    assert _bulk(c, "demo", mode="", dry_run=True).status_code == 400
    assert _bulk(c, "demo", mode="files", prefix="../", dry_run=True).status_code == 400
    assert _bulk(c, "demo", mode="files", prefix="a/../b", dry_run=True).status_code == 400
    assert _bulk(c, "demo", mode="files", prefix=".hidden", dry_run=True).status_code == 400
    assert _snapshot(depot) == snap

    # 项数超限（monkeypatch 常量）：预览与执行都 400，且不落盘
    from artifactdepot import storage
    monkeypatch.setattr(storage, "MAX_BULK_DELETE_ITEMS", 1)
    _up(c, "demo", "g1.txt", b"1")
    _up(c, "demo", "g2.txt", b"2")
    assert _bulk(c, "demo", mode="files", dry_run=True).status_code == 400
    snap = _snapshot(depot)
    r = _bulk(c, "demo", mode="files", dry_run=False, confirm="demo")
    assert r.status_code == 400 and "上限" in _detail(r)
    assert _snapshot(depot) == snap
    assert (depot / "demo" / "g1.txt").exists() and (depot / "demo" / "g2.txt").exists()

    # 空目录（无可删项）：200 且计数全 0，prefix 目录保留
    (depot / "demo" / "rawempty").mkdir()
    data = _bulk(c, "demo", mode="all", prefix="rawempty", dry_run=True).json()["data"]
    assert data["files"] == 0 and data["dirs"] == 0 and data["bytes"] == 0
    r = _bulk(c, "demo", mode="all", prefix="rawempty", dry_run=False, confirm="rawempty")
    assert r.status_code == 200 and r.json()["data"]["files"] == 0
    assert (depot / "demo" / "rawempty").is_dir()


# ===========================================================================
# 7. 审计 + 指标（dry_run 不计）
# ===========================================================================

def test_audit_and_delete_metric_only_on_execute(ctx):
    """dry_run 不写审计、不加 DELETE 指标；执行写 delete_bulk 并 +1。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "x.txt", b"XXX")
    _up(c, "demo", "sub/y.txt", b"YY")
    audit_before = len(_audit(depot))
    metric_before = _metric(c, "artifactdepot_objects_deleted_total")

    # 预览：无审计、无指标
    assert _bulk(c, "demo", mode="all", dry_run=True).status_code == 200
    assert len(_audit(depot)) == audit_before
    assert _metric(c, "artifactdepot_objects_deleted_total") == metric_before

    # 执行：审计一条 + 指标 +1
    r = _bulk(c, "demo", mode="files", dry_run=False, confirm="demo")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    rows = _audit(depot)
    assert len(rows) == audit_before + 1
    row = rows[-1]
    assert row["action"] == "delete_bulk"
    assert row["bucket"] == "demo" and row["key"] == ""
    assert row["size"] == data["bytes"] == 3
    assert row["mode"] == "files" and row["files"] == 1 and row["dirs"] == 0
    assert _metric(c, "artifactdepot_objects_deleted_total") == metric_before + 1

    # dry_run=false 但无任何可删项：仍算一次执行（审计 + 指标均 +1）
    audit2 = len(_audit(depot))
    metric2 = _metric(c, "artifactdepot_objects_deleted_total")
    r = _bulk(c, "demo", mode="files", dry_run=False, confirm="demo")
    assert r.status_code == 200 and r.json()["data"]["files"] == 0
    assert len(_audit(depot)) == audit2 + 1
    assert _metric(c, "artifactdepot_objects_deleted_total") == metric2 + 1


# ===========================================================================
# 8. 符号链接安全：绝不跟随，只删链接本身
# ===========================================================================

def test_symlinks_not_followed_targets_preserved(ctx):
    """all 模式删除符号链接本身；链接指向的文件/目录（含仓库外）原样保留。"""
    c, depot, tmp_path = ctx
    _mkbucket(c, "demo")
    base = depot / "demo"
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"OUTSIDE")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_bytes(b"KEEP")

    os.symlink(outside_file, base / "link.txt")
    os.symlink(outside_dir, base / "dirlink", target_is_directory=True)
    (base / "sub").mkdir()
    os.symlink(outside_file, base / "sub" / "link2")

    # 预览：3 个符号链接按文件计数，sub 是唯一子目录；不跟随（不会把目标算进来）
    data = _bulk(c, "demo", mode="all", dry_run=True).json()["data"]
    assert data["files"] == 4 and data["dirs"] == 1, data   # .keep + 3 个链接
    link_stats = [os.lstat(p).st_size for p in
                  (base / ".keep", base / "link.txt", base / "dirlink", base / "sub" / "link2")]
    assert data["bytes"] == sum(link_stats), "bytes 只能统计链接自身大小，绝不能读目标文件"

    r = _bulk(c, "demo", mode="all", dry_run=False, confirm="demo")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["errors"] == []

    # 链接本身被删除
    assert not os.path.lexists(base / "link.txt")
    assert not os.path.lexists(base / "dirlink")
    assert not os.path.lexists(base / "sub")
    # 链接目标原样保留（绝不跟随）
    assert outside_file.read_bytes() == b"OUTSIDE"
    assert (outside_dir / "keep.txt").read_bytes() == b"KEEP"


def test_prefix_symlink_cannot_escape_bucket(ctx):
    """安全加固：bucket 内指向别的 bucket 的符号链接目录，不能作为批量删除 prefix（400）。

    否则 prefix 解析到别的 bucket 后，递归删除会清空他人数据。预览与执行都必须拒绝，
    且目标 bucket 与全仓库快照保持不变。
    """
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _mkbucket(c, "secret")
    _up(c, "secret", "s.txt", b"SECRET")
    os.symlink(depot / "secret", depot / "demo" / "escape", target_is_directory=True)
    snap = _snapshot(depot)

    for dry in (True, False):
        r = _bulk(c, "demo", mode="all", prefix="escape",
                  dry_run=dry, confirm=None if dry else "escape")
        assert r.status_code == 400, r.text
        assert "越界" in _detail(r)
    assert (depot / "secret" / "s.txt").read_bytes() == b"SECRET"
    assert _snapshot(depot) == snap


# ===========================================================================
# 9. UI 标记（前端契约 §3.1/§3.2）
# ===========================================================================

def test_ui_markers():
    """前端已实现入口按钮与确认弹窗（防前后端遗漏回归）。"""
    base = ROOT / "src" / "artifactdepot" / "resources"
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    html = (base / "index.html").read_text(encoding="utf-8")
    assert 'data-action="bulk-delete"' in js
    assert "bulkDeleteModal" in html


# ===========================================================================
# 附加：storage 层默认值 / 部分失败明细上限 / 并发消失文件
# ===========================================================================

def test_storage_default_is_dry_run(ctx):
    """storage.bulk_delete 默认 dry_run=True：直接调用也只统计不落盘（契约 §2.1）。"""
    c, depot, _ = ctx
    from artifactdepot import storage
    _mkbucket(c, "demo")
    _up(c, "demo", "keep.txt", b"K")
    data = storage.bulk_delete("demo", "", "files")
    assert data["dry_run"] is True and data["files"] == 1
    assert (depot / "demo" / "keep.txt").is_file()
    assert _manifest(depot, "demo")["keep.txt"]["size"] == 1


def test_partial_failure_errors_capped_at_20(ctx, monkeypatch):
    """逐项删除失败继续执行且 errors 最多 20 条；FileNotFoundError 视为已不存在、不计错误。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    for i in range(25):
        _up(c, "demo", f"f{i:02d}.txt", b"x")
    real_unlink = Path.unlink

    def fake_unlink(self, *args, **kwargs):
        if self.name in ("f00.txt", "f01.txt"):
            raise FileNotFoundError(self.name)       # 并发已被删除：跳过且不记错误
        if self.name.startswith("f"):
            raise OSError("模拟删除失败")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    r = _bulk(c, "demo", mode="files", dry_run=False, confirm="demo")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["files"] == 0 and data["bytes"] == 0
    assert len(data["errors"]) == 20, "errors 明细必须封顶 20 条"
    assert all(e["error"] == "模拟删除失败" for e in data["errors"])
    assert all(e["key"].startswith("f") for e in data["errors"])
    # 失败的文件仍在磁盘上，清单未被误删
    assert (depot / "demo" / "f02.txt").is_file()
    assert "f02.txt" in _manifest(depot, "demo")
