# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""业务验收：跨接口联动链路 + 数据隔离（IDOR）+ 并发 + 安全输入

与 test_scope_enforcement.py 的区别：这里是**业务链路级**验收，不是单接口校验——
每一步都用后续接口验证前一步产生的**真实数据**，并验证「不该被改的数据没被改」。

运行：
    cd ArtifactDepot/ArtifactDepot
    PYTHONPATH=/home/apyyc/work/ArtifactDepot/.audit-deps/libs:src python3 -m pytest tests/test_biz_integration.py -q
"""
import json
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "biz-integration-admin"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "ui_enabled": True, "max_upload_mb": 0,
        "require_read_token": False,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c, depot
    config._CONFIG = None


A = {"token": ADMIN}
ALL_SCOPES = [
    "bucket:list", "bucket:create", "object:list", "object:head", "object:download",
    "object:upload", "upload:initiate", "upload:chunk", "upload:complete", "upload:abort",
    "object:mkdir", "object:rename", "object:delete", "link:create", "link:list",
    "link:revoke", "token:read", "token:write", "token:delete", "audit:read",
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _tok(c, **body):
    body.setdefault("user", "biz")
    r = c.post("/api/tokens", params=A, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _mk(c, name):
    return c.post("/api/buckets", params=A, json={"bucket": name})


def _mkdir(c, bucket, key, tok=A):
    return c.post("/api/objects/mkdir", params=tok, json={"bucket": bucket, "key": key})


def _put(c, bucket, key, data=b"x", tok=A, **extra):
    form = {"bucket": bucket, "key": key}
    form.update({k: str(v) for k, v in extra.items()})
    return c.post("/api/objects", params=tok, data=form, files={"file": (key.split("/")[-1] or "f", data)})


def _list(c, bucket, tok=A, **q):
    return c.get("/api/objects/list", params={"bucket": bucket, **q, **tok})


def _names(resp):
    return [i["name"] for i in resp.json()["data"]["items"]]


def _head(c, bucket, key, tok=A):
    return c.get("/api/objects/head", params={"bucket": bucket, "key": key, **tok})


def _dl(c, bucket, key, tok=A):
    return c.get("/api/objects/download", params={"bucket": bucket, "key": key, **tok})


def _presign(c, bucket, key, tok=A, **body):
    payload = {"bucket": bucket, "key": key}
    payload.update(body)
    return c.post("/api/objects/presign", params=tok, json=payload)


# --------------------------------------------------------------------------
# 1) 完整业务链路：建桶 → 建目录 → 上传 → 列表 → 探测 → 下载 → 改名
#                → 签名链接 → 用完/作废 → 删除 → 最终状态
# --------------------------------------------------------------------------
def test_full_lifecycle_chain(ctx):
    c, depot = ctx

    # 建桶（幂等）
    assert _mk(c, "demo").json()["data"]["created"] is True
    assert _mk(c, "demo").json()["data"]["created"] is False

    # 建目录
    assert _mkdir(c, "demo", "task1").status_code == 200

    # 上传（真实内容）
    r = _put(c, "demo", "task1/a.txt", b"hello")
    assert r.status_code == 200, r.text
    up = r.json()["data"]
    assert up["size"] == 5 and len(up["sha256"]) == 64
    # 数据确实落盘
    assert (depot / "demo" / "task1" / "a.txt").read_bytes() == b"hello"

    # 列表 / 探测
    names = _names(_list(c, "demo", prefix="task1/"))
    assert "a.txt" in names
    h = _head(c, "demo", "task1/a.txt").json()["data"]
    assert h["exists"] is True and h["size"] == 5 and h["sha256"] == up["sha256"]
    assert h["mtime"].endswith("Z")

    # 下载内容 + ETag
    d = _dl(c, "demo", "task1/a.txt")
    assert d.status_code == 200 and d.content == b"hello"
    etag = d.headers["etag"]

    # 改名（目录整体迁移）
    r = c.post("/api/objects/rename", params=A,
               json={"bucket": "demo", "key": "task1", "new_key": "task1-done"})
    assert r.status_code == 200 and r.json()["data"]["moved_objects"] >= 1
    assert _names(_list(c, "demo", prefix="task1-done/")) == ["a.txt"]
    assert _names(_list(c, "demo", prefix="task1/")) == []
    assert not (depot / "demo" / "task1").exists()

    # 签名链接：按次数 2 次，免 token
    r = _presign(c, "demo", "task1-done/a.txt", mode="count", count=2)
    assert r.status_code == 200, r.text
    link = r.json()["data"]
    assert link["mode"] == "count" and link["max_uses"] == 2 and link["remaining"] == 2

    u1 = c.get(link["url"])
    assert u1.status_code == 200 and u1.content == b"hello"
    u2 = c.get(link["url"])
    assert u2.status_code == 200
    u3 = c.get(link["url"])
    assert u3.status_code == 401, "次数用尽后必须失效"

    # 新建一条链接 → 作废 → 立即失效
    r = _presign(c, "demo", "task1-done/a.txt", mode="permanent")
    link2 = r.json()["data"]
    assert c.get(link2["url"]).status_code == 200
    assert c.post(f"/api/objects/signed-links/{link2['id']}/revoke", params=A).status_code == 200
    assert c.get(link2["url"]).status_code == 401

    # 删除文件 → 探测 404；再删目录
    assert c.delete("/api/objects", params={"bucket": "demo", "key": "task1-done/a.txt", **A}).status_code == 200
    assert _head(c, "demo", "task1-done/a.txt").status_code == 404
    assert c.delete("/api/objects", params={"bucket": "demo", "key": "task1-done", **A}).status_code == 200
    assert _names(_list(c, "demo")) == []

    # 审计里能查到这条链的每个动作
    rows = c.get("/api/audit", params={**A, "limit": 500}).json()["data"]
    actions = {r.get("action") for r in rows}
    for act in ("create_bucket", "mkdir", "upload", "download", "rename", "presign", "revoke", "delete"):
        assert act in actions, f"审计缺少 {act}；实际动作集={sorted(actions)}"


# --------------------------------------------------------------------------
# 2) 数据隔离 / IDOR：A 只能碰 demo，B 的 secret 数据完全不可达且不被改动
# --------------------------------------------------------------------------
def test_idor_cross_bucket_isolation(ctx):
    c, depot = ctx
    _mk(c, "demo")
    _mk(c, "secret")
    _put(c, "demo", "d.txt", b"D")
    _put(c, "secret", "s.txt", b"SECRET")
    before = (depot / "secret" / "s.txt").read_bytes()

    tokA = _tok(c, user="alice", role="custom", scopes=ALL_SCOPES, allow_buckets=["demo"])
    tokB = _tok(c, user="bob", role="custom", scopes=ALL_SCOPES, allow_buckets=["secret"])
    # B 建一条签名链接，供 A 尝试越权作废
    linkB = _presign(c, "secret", "s.txt", tok={"token": tokB}, mode="permanent").json()["data"]

    PA = {"token": tokA}
    # 读：全部 403，且不得泄露内容
    for r in (
        c.get("/api/objects/list", params={"bucket": "secret", **PA}),
        c.get("/api/objects/head", params={"bucket": "secret", "key": "s.txt", **PA}),
        c.get("/api/objects/download", params={"bucket": "secret", "key": "s.txt", **PA}),
    ):
        assert r.status_code == 403, r.text
        assert b"SECRET" not in r.content and "s.txt" not in r.text

    # 写：全部 403
    for r in (
        _put(c, "secret", "x.txt", tok=PA),
        _mkdir(c, "secret", "d", tok=PA),
        c.post("/api/objects/rename", params=PA, json={"bucket": "secret", "key": "s.txt", "new_key": "z.txt"}),
        c.delete("/api/objects", params={"bucket": "secret", "key": "s.txt", **PA}),
        _presign(c, "secret", "s.txt", tok=PA),
        c.post("/api/buckets", params=PA, json={"bucket": "secret2"}),
    ):
        assert r.status_code == 403, r.text

    # 桶列表只含自己的
    assert c.get("/api/buckets", params=PA).json()["data"] == ["demo"]

    # 签名链接：A 看不到 B 的链接，也不能作废
    linksA = c.get("/api/objects/signed-links", params=PA).json()["data"]
    assert all(l["bucket"] != "secret" for l in linksA)
    assert c.post(f"/api/objects/signed-links/{linkB['id']}/revoke", params=PA).status_code == 403
    # 链接仍可用（未被越权作废）
    assert c.get(linkB["url"]).status_code == 200

    # 审计：看不到 secret 记录
    rowsA = c.get("/api/audit", params={**PA, "limit": 500}).json()["data"]
    assert all(r.get("bucket") != "secret" for r in rowsA), rowsA

    # token 管理：不能改 / 删 / 覆写 B 的 token
    assert c.put(f"/api/tokens/{tokB}", params=PA, json={"description": "hijack"}).status_code == 403
    assert c.delete("/api/tokens", params={**PA, "value": tokB}).status_code == 403
    assert c.post("/api/tokens", params=PA,
                  json={"token": tokB, "user": "bob", "role": "custom",
                        "scopes": ["object:list"], "allow_buckets": ["demo"]}).status_code == 403

    # 数据未被任何越权请求改动
    assert (depot / "secret" / "s.txt").read_bytes() == before
    # B 的能力仍然完整
    assert c.get("/api/objects/download",
                 params={"bucket": "secret", "key": "s.txt", "token": tokB}).content == b"SECRET"


# --------------------------------------------------------------------------
# 3) 前缀隔离：同一 bucket 内也只能看/动自己那段前缀
# --------------------------------------------------------------------------
def test_prefix_isolation_within_bucket(ctx):
    c, depot = ctx
    _mk(c, "demo")
    _mkdir(c, "demo", "dir")
    _mkdir(c, "demo", "other")
    _put(c, "demo", "dir/g.txt", b"G")
    _put(c, "demo", "other/o.txt", b"O")
    _put(c, "demo", "root.txt", b"R")

    tok = _tok(c, user="prefixed", role="custom", scopes=ALL_SCOPES,
               allow_buckets=["demo"], allow_prefixes={"demo": ["dir/"]})
    P = {"token": tok}

    # 范围内可见可读
    assert "g.txt" in _names(c.get("/api/objects/list", params={"bucket": "demo", "prefix": "dir/", **P}))
    assert c.get("/api/objects/download",
                 params={"bucket": "demo", "key": "dir/g.txt", **P}).content == b"G"
    # 根列表会泄露前缀外条目 → 必须 403
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 403
    # 前缀外一律 403
    assert c.get("/api/objects/list", params={"bucket": "demo", "prefix": "other/", **P}).status_code == 403
    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "root.txt", **P}).status_code == 403
    assert _head(c, "demo", "other/o.txt", tok=P).status_code == 403
    assert _put(c, "demo", "root2.txt", tok=P).status_code == 403
    assert _mkdir(c, "demo", "other/sub", tok=P).status_code == 403
    # 前缀内可写
    assert _put(c, "demo", "dir/new.txt", b"N", tok=P).status_code == 200
    assert c.get("/api/objects/download",
                 params={"bucket": "demo", "key": "dir/new.txt", **P}).content == b"N"

    # 前缀外文件未受影响
    assert (depot / "demo" / "other" / "o.txt").read_bytes() == b"O"
    assert (depot / "demo" / "root.txt").read_bytes() == b"R"
    # 共享 token 不受前缀限制
    assert c.get("/api/objects/download",
                 params={"bucket": "demo", "key": "root.txt", **A}).content == b"R"


# --------------------------------------------------------------------------
# 4) 分片上传：会话归属 + 范围（越界不能借分片绕过）
# --------------------------------------------------------------------------
def test_chunk_upload_ownership_and_scope(ctx):
    c, depot = ctx
    _mk(c, "demo")
    _mk(c, "secret")
    tokA = _tok(c, user="alice", role="custom", scopes=ALL_SCOPES, allow_buckets=["demo"])
    tokB = _tok(c, user="bob", role="custom", scopes=ALL_SCOPES, allow_buckets=["secret"])

    ini = c.post("/api/objects/initiate", params={"token": tokA})
    assert ini.status_code == 200
    uid = ini.json()["data"]["upload_id"]
    assert ini.json()["data"]["chunk_size"] == 8 * 1024 * 1024

    # 本人上传分片
    r = c.post("/api/objects/chunk", params={"token": tokA},
               data={"upload_id": uid, "index": 0}, files={"chunk": ("0", b"part-1")})
    assert r.status_code == 200, r.text

    # 他人不得写入该会话
    r = c.post("/api/objects/chunk", params={"token": tokB},
               data={"upload_id": uid, "index": 1}, files={"chunk": ("1", b"hijack")})
    assert r.status_code in (400, 403), r.text
    assert c.post("/api/objects/abort", params={"token": tokB},
                  data={"upload_id": uid}).status_code in (400, 403)

    # 合并到范围内 → 成功
    r = c.post("/api/objects/complete", params={"token": tokA},
               data={"upload_id": uid, "bucket": "demo", "key": "big.bin", "total_chunks": 1})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["size"] == len(b"part-1")
    assert (depot / "demo" / "big.bin").read_bytes() == b"part-1"

    # 越界合并 → 403，且 secret 下不得出现文件
    ini2 = c.post("/api/objects/initiate", params={"token": tokA}).json()["data"]["upload_id"]
    c.post("/api/objects/chunk", params={"token": tokA},
           data={"upload_id": ini2, "index": 0}, files={"chunk": ("0", b"evil")})
    r = c.post("/api/objects/complete", params={"token": tokA},
               data={"upload_id": ini2, "bucket": "secret", "key": "evil.bin", "total_chunks": 1})
    assert r.status_code == 403, r.text
    assert not (depot / "secret" / "evil.bin").exists()


# --------------------------------------------------------------------------
# 5) 并发：按次数签名链接不得超用
# --------------------------------------------------------------------------
def test_concurrent_signed_link_single_use(ctx):
    c, _ = ctx
    _mk(c, "demo")
    _put(c, "demo", "once.txt", b"once")
    link = _presign(c, "demo", "once.txt", mode="count", count=1).json()["data"]

    results = []
    barrier = threading.Barrier(8)

    def hit():
        barrier.wait()
        try:
            results.append(c.get(link["url"]).status_code)
        except Exception as e:  # noqa: BLE001 - 并发下客户端异常也计入
            results.append(f"err:{type(e).__name__}")

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = sum(1 for r in results if r == 200)
    assert ok == 1, f"次数=1 的链接并发 8 次应只成功 1 次，实际 {results}"


# --------------------------------------------------------------------------
# 6) 安全输入：注入 / 穿越 / XSS / 超长 / Content-Type / 重复提交
# --------------------------------------------------------------------------
def test_security_inputs(ctx):
    c, depot = ctx
    _mk(c, "demo")

    # 路径穿越：含 .. / . / 空段 / 反斜杠 一律 400
    for bad in ("../etc/passwd", "a/../../b", "%2e%2e/secret",
                "a/./b", "a//b", "a\\b"):
        r = _put(c, "demo", bad)
        assert r.status_code == 400, f"{bad!r} → {r.status_code} {r.text[:120]}"
        assert not (depot.parent / "etc").exists()

    # 前导/尾部斜杠属于**规范化**（文档 1.5：去首尾斜杠），应 200 且落在 bucket 内
    r = _put(c, "demo", "/abs/path")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["key"] == "abs/path", "前导斜杠应被规范化掉"
    assert (depot / "demo" / "abs" / "path").exists()
    assert not (depot.parent / "abs").exists(), "不得写到 bucket 之外"

    # 穿越前缀（list）
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "prefix": "../", **A}).status_code == 400

    # 超长 bucket（>63）→ 400，不得 500
    r = _put(c, "b" * 200, "k.txt")
    assert r.status_code == 400, r.text

    # 超长 key：可以拒绝（400/414/422）或接受，但**不得 500**
    r = _put(c, "demo", "k" * 5000)
    assert r.status_code < 500, f"超长 key 触发服务端错误 {r.status_code}"

    # 注入类 bucket/key 名：无 SQL，但必须不 500、且原样可读回（不产生越权副作用）
    weird = "demo'; DROP TABLE tokens;--"
    assert _mk(c, weird).status_code in (200, 400)
    r = _put(c, "demo", "x' OR '1'='1.txt", b"inj")
    assert r.status_code == 200, r.text
    assert "x' OR '1'='1.txt" in _names(_list(c, "demo"))

    # XSS 载荷：作为 key 可存，但 API 只返回 JSON（不渲染），且 UI 用 esc() 转义
    xss = '<img src=x onerror=alert(1)>.txt'
    assert _put(c, "demo", xss, b"x").status_code == 200
    resp = c.get("/api/objects/list", params={"bucket": "demo", **A})
    assert resp.headers["content-type"].startswith("application/json")
    listed = resp.json()
    assert any(i["name"] == xss for i in listed["data"]["items"]), _names(resp)
    assert listed["code"] == 0

    # 含 '/' 的载荷会被当作目录层级（key 语义），不得报 500，且仍只是普通文件
    assert _put(c, "demo", "<script>alert(1)</script>.txt", b"x").status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **A}).status_code == 200

    # Content-Type 错误 → 422（JSON 接口）
    r = c.post("/api/objects/mkdir", params=A, data="not json",
               headers={"Content-Type": "text/plain"})
    assert r.status_code == 422, r.text
    # 非法 JSON 语法 → 422
    r = c.post("/api/objects/mkdir", params=A, data="{bad json",
               headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text

    # 重复提交：建桶幂等、上传冲突 409
    _mk(c, "demo2")
    assert _mk(c, "demo2").json()["data"]["created"] is False
    _put(c, "demo2", "dup.txt", b"1")
    assert _put(c, "demo2", "dup.txt", b"2", overwrite=False).status_code == 409
    # 覆盖成功且内容被替换
    assert _put(c, "demo2", "dup.txt", b"3", overwrite=True).status_code == 200
    assert c.get("/api/objects/download",
                 params={"bucket": "demo2", "key": "dup.txt", **A}).content == b"3"

    # 未授权调用
    assert c.post("/api/objects/mkdir", json={"bucket": "demo", "key": "z"}).status_code == 401
    assert c.get("/api/objects/download",
                 params={"bucket": "demo", "key": "dup.txt"}, headers={"Authorization": "Bearer bad"}).status_code == 401


# --------------------------------------------------------------------------
# 7) 令牌生命周期对业务访问的**实际**影响（状态流转）
# --------------------------------------------------------------------------
def test_token_lifecycle_changes_real_access(ctx):
    c, _ = ctx
    _mk(c, "demo")
    _put(c, "demo", "f.txt", b"F")
    tok = _tok(c, user="lifecycle", role="custom",
               scopes=["object:list", "object:download"], allow_buckets=["demo"])
    P = {"token": tok}
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 200

    # 禁用 → 立即失效
    assert c.put(f"/api/tokens/{tok}", params=A, json={"enabled": False}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 401
    # 启用 → 恢复
    assert c.put(f"/api/tokens/{tok}", params=A, json={"enabled": True}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 200

    # 过期 → 失效
    assert c.put(f"/api/tokens/{tok}", params=A, json={"expires_at": "2000-01-01"}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 401
    assert c.put(f"/api/tokens/{tok}", params=A, json={"expires_at": ""}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 200

    # 收缩范围 → 立即生效；放宽回来 → 恢复
    assert c.put(f"/api/tokens/{tok}", params=A, json={"allow_buckets": ["other"]}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 403
    assert c.put(f"/api/tokens/{tok}", params=A, json={"allow_buckets": ["demo"]}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 200

    # 删除 token → 立即失效
    assert c.delete("/api/tokens", params={**A, "value": tok}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo", **P}).status_code == 401
