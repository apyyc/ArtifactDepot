# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""令牌资源范围（bucket / 前缀）硬边界回归测试

覆盖 0.7.x「令牌资源范围硬化」后的语义：
- 读接口 fail-closed：带 token 时无效/过期 → 401，缺 scope → 403，越界 → 403；
  不带 token 时仍由 require_read_token 决定（false=匿名 200 / true=401）
- GET /api/buckets 只返回 allow_buckets 内的 bucket
- download/head/upload/mkdir/delete/presign/complete 越界 → 403
- GET /api/audit 对非共享 token 只返回其 bucket/前缀范围内的记录
- 非共享 token 不能创建/更新/删除「超出自身范围」的 token（防提权 403）
- 共享 access_token（is_admin）完全不受限

运行：
    cd ArtifactDepot/ArtifactDepot
    PYTHONPATH=/home/apyyc/work/ArtifactDepot/.audit-deps/libs:src python3 -m pytest tests -q
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "test-scope-enforcement-admin"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """每个用例独立 depot + 配置；ARTIFACT_DEPOT_ENV 指向不存在的文件，避免读到真实 access_token。"""
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True, "max_upload_mb": 0,
        "require_read_token": False,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    # tests/conftest.py 的 autouse fixture 已设置；这里显式再设一次，保证本文件独立可读
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c, cfg, config
    config._CONFIG = None


def _detail(resp):
    try:
        return resp.json().get("detail", "")
    except Exception:
        return resp.text


def _new_token(c, **body):
    body.setdefault("user", "tester")
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _mk_bucket(c, name):
    r = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": name})
    assert r.status_code == 200, r.text
    return name


def _upload(c, bucket, key, data=b"x"):
    r = c.post("/api/objects", params={"token": ADMIN},
               data={"bucket": bucket, "key": key}, files={"file": (key, data)})
    assert r.status_code == 200, r.text


def _set_read_token(ctx, value):
    """就地修改配置文件的 require_read_token 并清配置缓存（请求期读取）。"""
    c, cfg, config = ctx
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["require_read_token"] = value
    cfg.write_text(json.dumps(data), encoding="utf-8")
    config._CONFIG = None
    return c


# ---------------------------------------------------------------------------
# 1) 匿名读：require_read_token=false 时保持公开
# ---------------------------------------------------------------------------

def test_anonymous_read_allowed_when_not_required(ctx):
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    _upload(c, "demo", "ok.txt")

    r = c.get("/api/objects/list", params={"bucket": "demo"})
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["data"]["items"]] == ["ok.txt"]

    r = c.get("/api/buckets")
    assert r.status_code == 200
    assert r.json()["data"] == ["demo"]


# ---------------------------------------------------------------------------
# 2) 带无效 / 过期 token：fail-closed 401（不再降级匿名 200）
# ---------------------------------------------------------------------------

def test_invalid_and_expired_token_are_rejected(ctx):
    c, _, _ = ctx
    expired = _new_token(c, role="viewer", expires_at="2000-01-01")
    for bad, why in (("definitely-not-a-token", "无效"),
                     (expired, "过期")):
        r = c.get("/api/objects/list", params={"bucket": "b", "token": bad})
        assert r.status_code == 401, (why, r.text)
        assert "无效的访问令牌" in _detail(r)

        r = c.get("/api/buckets", params={"token": bad})
        assert r.status_code == 401, (why, r.text)
        assert "无效的访问令牌" in _detail(r)


# ---------------------------------------------------------------------------
# 3) 有效 token 缺 scope：403
# ---------------------------------------------------------------------------

def test_valid_token_missing_scope_is_403(ctx):
    c, _, _ = ctx
    tok = _new_token(c, role="custom", scopes=["object:download"])
    r = c.get("/api/objects/list", params={"bucket": "b", "token": tok})
    assert r.status_code == 403
    assert "需要 object:list" in _detail(r)

    tok2 = _new_token(c, role="custom", scopes=["object:list"])
    r = c.get("/api/buckets", params={"token": tok2})
    assert r.status_code == 403
    assert "需要 bucket:list" in _detail(r)


# ---------------------------------------------------------------------------
# 4) 范围内 200 / 越界 403（公开模式下同样如此）
# ---------------------------------------------------------------------------

def test_list_bucket_range_inside_200_outside_403(ctx):
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    _upload(c, "demo", "ok.txt")
    _upload(c, "secret", "topsecret.txt", b"SECRET")

    tok = _new_token(c, role="custom", scopes=["object:list"], allow_buckets=["demo"])

    inside = c.get("/api/objects/list", params={"bucket": "demo", "token": tok})
    assert inside.status_code == 200
    assert [i["name"] for i in inside.json()["data"]["items"]] == ["ok.txt"]

    outside = c.get("/api/objects/list", params={"bucket": "secret", "token": tok})
    assert outside.status_code == 403
    assert "资源范围不允许" in _detail(outside)
    assert "topsecret" not in outside.text


# ---------------------------------------------------------------------------
# 5) GET /api/buckets 只返回 allow_buckets 内的 bucket
# ---------------------------------------------------------------------------

def test_bucket_list_filtered_by_allow_buckets(ctx):
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")

    scoped = _new_token(c, role="custom", scopes=["bucket:list", "object:list"],
                        allow_buckets=["demo"])
    r = c.get("/api/buckets", params={"token": scoped})
    assert r.status_code == 200
    assert r.json()["data"] == ["demo"]

    # 无范围限制的 token 看到全部
    full = _new_token(c, role="custom", scopes=["bucket:list"])
    assert set(c.get("/api/buckets", params={"token": full}).json()["data"]) == {"demo", "secret"}

    # 共享 access_token 看到全部
    assert set(c.get("/api/buckets", params={"token": ADMIN}).json()["data"]) == {"demo", "secret"}


# ---------------------------------------------------------------------------
# 6) require_read_token=true 的三态
# ---------------------------------------------------------------------------

def test_require_read_token_three_states(ctx):
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    _upload(c, "demo", "ok.txt")
    tok = _new_token(c, role="custom", scopes=["object:list", "bucket:list"],
                     allow_buckets=["demo"])
    _set_read_token(ctx, True)

    # 无 token → 401
    r = c.get("/api/objects/list", params={"bucket": "demo"})
    assert r.status_code == 401
    assert "缺少访问令牌" in _detail(r)
    assert c.get("/api/buckets").status_code == 401

    # 范围内 → 200
    inside = c.get("/api/objects/list", params={"bucket": "demo", "token": tok})
    assert inside.status_code == 200
    assert [i["name"] for i in inside.json()["data"]["items"]] == ["ok.txt"]

    # 范围外 → 403
    outside = c.get("/api/objects/list", params={"bucket": "secret", "token": tok})
    assert outside.status_code == 403
    assert "资源范围不允许" in _detail(outside)

    # 带 token 时仍只列范围内的 bucket
    assert c.get("/api/buckets", params={"token": tok}).json()["data"] == ["demo"]


# ---------------------------------------------------------------------------
# 7) 写接口 / 下载接口越界 → 403（回归）
# ---------------------------------------------------------------------------

def test_write_and_download_endpoints_outside_range_403(ctx):
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    _upload(c, "demo", "ok.txt")
    tok = _new_token(
        c, role="custom",
        scopes=["object:download", "object:head", "object:upload", "object:mkdir",
                "object:delete", "link:create", "upload:complete"],
        allow_buckets=["demo"],
    )

    cases = {}

    r = c.get("/api/objects/download", params={"bucket": "secret", "key": "x.txt", "token": tok})
    cases["download"] = r
    r = c.get("/api/objects/head", params={"bucket": "secret", "key": "x.txt", "token": tok})
    cases["head"] = r
    r = c.post("/api/objects", data={"bucket": "secret", "key": "x.txt", "token": tok},
               files={"file": ("x.txt", b"x")})
    cases["upload"] = r
    r = c.post("/api/objects/mkdir", params={"token": tok},
               json={"bucket": "secret", "key": "dir"})
    cases["mkdir"] = r
    r = c.delete("/api/objects", params={"token": tok, "bucket": "secret", "key": "x.txt"})
    cases["delete"] = r
    r = c.post("/api/objects/presign", params={"token": tok},
               json={"bucket": "secret", "key": "x.txt"})
    cases["presign"] = r
    r = c.post("/api/objects/complete",
               data={"upload_id": "dummy", "bucket": "secret", "key": "x.txt",
                     "total_chunks": 1, "token": tok})
    cases["complete"] = r

    for name, resp in cases.items():
        assert resp.status_code == 403, (name, resp.text)
        assert "资源范围不允许" in _detail(resp), (name, resp.text)

    # 正向对照：范围内接口仍可用（证明 403 来自资源范围而非 token 失效）
    assert c.post("/api/objects", data={"bucket": "demo", "key": "new.txt", "token": tok},
                  files={"file": ("new.txt", b"x")}).status_code == 200
    assert c.post("/api/objects/mkdir", params={"token": tok},
                  json={"bucket": "demo", "key": "dir"}).status_code == 200
    # 范围内但对象不存在 → 404（鉴权通过）
    assert c.get("/api/objects/head",
                 params={"bucket": "demo", "key": "nope.txt", "token": tok}).status_code == 404


# ---------------------------------------------------------------------------
# 8) 审计过滤：非共享 token 只看得到自身 bucket / 前缀范围内的记录
# ---------------------------------------------------------------------------

def test_audit_only_returns_records_in_bucket_scope(ctx):
    c, _, _ = ctx
    _upload(c, "demo", "visible.txt")
    _upload(c, "secret", "topsecret.txt", b"SECRET")
    tok = _new_token(c, role="custom", scopes=["audit:read"], allow_buckets=["demo"])

    r = c.get("/api/audit", params={"token": tok})
    assert r.status_code == 200
    rows = r.json()["data"]
    assert any(row.get("bucket") == "demo" and row.get("key") == "visible.txt" for row in rows)
    # 受限 token 只能看到「归属到范围内 bucket」的记录；secret 与空 bucket 的全局事件都被过滤
    assert all(row.get("bucket") == "demo" for row in rows), rows
    assert all(row.get("key", "") != "topsecret.txt" for row in rows), rows
    # 全局事件（token_create / token_update 等，bucket 为空）对受限 token 不可见：
    # 它们无法归属到任何范围，且会泄露其他 token 的 user/role/scopes
    assert not any(row.get("action") in ("token_create", "token_update", "token_delete")
                   and not row.get("bucket") for row in rows), rows
    # 但共享 token 能看到这些全局事件（证明记录存在、只是对受限 token 隐藏）
    assert any(row.get("action") == "token_create" and not row.get("bucket")
               for row in c.get("/api/audit", params={"token": ADMIN}).json()["data"])

    # 共享 token 能看到 secret 记录，说明记录确实存在、只是被范围过滤
    all_rows = c.get("/api/audit", params={"token": ADMIN}).json()["data"]
    assert any(row.get("bucket") == "secret" and row.get("key") == "topsecret.txt"
               for row in all_rows)

    # 显式查询范围外 bucket：返回空，而不是 403（过滤语义）
    r = c.get("/api/audit", params={"token": tok, "bucket": "secret"})
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_audit_respects_prefix_scope(ctx):
    c, _, _ = ctx
    _upload(c, "demo", "public/a.txt")
    _upload(c, "demo", "private/b.txt")
    tok = _new_token(c, role="custom", scopes=["audit:read"],
                     allow_buckets=["demo"],
                     allow_prefixes={"demo": ["public/"]})

    rows = c.get("/api/audit", params={"token": tok}).json()["data"]
    assert any(row.get("key") == "public/a.txt" for row in rows), rows
    assert all(row.get("key") != "private/b.txt" for row in rows), rows


# ---------------------------------------------------------------------------
# 9) 防提权：非共享 token 不能创建 / 更新 / 删除超出自身范围的 token
# ---------------------------------------------------------------------------

def test_non_shared_token_cannot_escalate(ctx):
    c, _, _ = ctx
    actor = _new_token(c, user="delegated", role="custom",
                       scopes=["token:write", "token:delete"], allow_buckets=["demo"])
    me = {"token": actor}

    # (a) 把资源范围放宽为「全部 bucket」→ 403
    r = c.post("/api/tokens", params=me,
               json={"user": "esc1", "role": "custom",
                     "scopes": ["token:write"], "allow_buckets": []})
    assert r.status_code == 403
    assert "全部 bucket" in _detail(r)

    # (b) 更新自己，把 allow_buckets 清空 → 403
    r = c.put(f"/api/tokens/{actor}", params=me, json={"allow_buckets": []})
    assert r.status_code == 403
    assert "全部 bucket" in _detail(r)

    # (c) 授予自己没有的 scope → 403
    r = c.post("/api/tokens", params=me,
               json={"user": "esc2", "role": "custom",
                     "scopes": ["object:delete"], "allow_buckets": ["demo"]})
    assert r.status_code == 403
    assert "自身没有的权限点" in _detail(r)
    assert "object:delete" in _detail(r)

    # (d) 授予自身范围外的 bucket → 403
    r = c.post("/api/tokens", params=me,
               json={"user": "esc3", "role": "custom",
                     "scopes": ["token:write"], "allow_buckets": ["secret"]})
    assert r.status_code == 403
    assert "自身范围外的 bucket" in _detail(r)
    assert "secret" in _detail(r)

    # 正向对照：自身范围内的子集可以正常创建 / 更新
    r = c.post("/api/tokens", params=me,
               json={"user": "ok", "role": "custom",
                     "scopes": ["token:write"], "allow_buckets": ["demo"]})
    assert r.status_code == 200, r.text
    ok_child = r.json()["data"]["token"]

    r = c.put(f"/api/tokens/{actor}", params=me,
              json={"scopes": ["token:write", "token:delete"], "allow_buckets": ["demo"]})
    assert r.status_code == 200, r.text

    # (e) 更新「目标 token 现有授权超出自己」→ 403
    big_target = _new_token(c, user="big", role="custom",
                            scopes=["object:delete"], allow_buckets=["demo"])
    r = c.put(f"/api/tokens/{big_target}", params=me, json={"description": "nope"})
    assert r.status_code == 403
    assert "自身没有的权限点" in _detail(r)

    # (f) 删除「目标 token 现有授权超出自己」→ 403；删除范围内的 → 200
    r = c.delete("/api/tokens", params={"token": actor, "value": big_target})
    assert r.status_code == 403
    assert "自身没有的权限点" in _detail(r)

    small_target = _new_token(c, user="small", role="custom",
                              scopes=["token:write"], allow_buckets=["demo"])
    assert c.delete("/api/tokens", params={"token": actor,
                                           "value": small_target}).status_code == 200
    # 子 token 仍在（未被误删）
    assert c.get("/api/auth/check",
                 params={"token": ok_child}).json()["data"]["valid"] is True


# ---------------------------------------------------------------------------
# 10) 共享 access_token 不受资源范围限制
# ---------------------------------------------------------------------------

def test_shared_token_remains_unrestricted(ctx):
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    _upload(c, "secret", "topsecret.txt", b"SECRET")

    # 读任意 bucket
    assert c.get("/api/objects/list", params={"bucket": "secret", "token": ADMIN}).status_code == 200
    assert set(c.get("/api/buckets", params={"token": ADMIN}).json()["data"]) == {"demo", "secret"}

    # 可创建任意范围 / 任意 scope 的 token（包括「全部 bucket」与越界 bucket）
    r = c.post("/api/tokens", params={"token": ADMIN},
               json={"user": "any", "role": "custom",
                     "scopes": ["object:delete"], "allow_buckets": []})
    assert r.status_code == 200, r.text
    arbitrary = r.json()["data"]["token"]

    # 可把 token 更新为任意范围
    r = c.put(f"/api/tokens/{arbitrary}", params={"token": ADMIN},
              json={"allow_buckets": ["anything", "secret"]})
    assert r.status_code == 200, r.text
    assert c.put(f"/api/tokens/{arbitrary}", params={"token": ADMIN},
                 json={"enabled": True}).status_code == 200

    # 可删除任意 token
    assert c.delete("/api/tokens", params={"token": ADMIN, "value": arbitrary}).status_code == 200

    # 审计不受范围限制
    rows = c.get("/api/audit", params={"token": ADMIN}).json()["data"]
    assert any(row.get("bucket") == "secret" and row.get("key") == "topsecret.txt"
               for row in rows)


def test_bucket_name_normalization_cannot_bypass_scope(ctx):
    """bucket 名首尾空白/大小写/装饰字符不能把「范围外」变成「范围内」。

    回归：授权层曾对 bucket 做 strip()，而落盘按原样取目录，导致
    `bucket="demo "` 被按 `demo` 授权、却去读 `demo ` 的数据（跨 bucket 越权）。
    """
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    tok = _new_token(c, user="normalizer", role="custom",
                     scopes=["object:list"], allow_buckets=["demo"])

    # 范围内正常
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "token": tok}).status_code == 200

    # 各种「像 demo 但不是 demo」的名字一律不得放行
    for bad in ("demo ", " demo", "DEMO", "demo/", "demo/../secret",
                "demo,secret", "demo.", ".demo"):
        r = c.get("/api/objects/list", params={"bucket": bad, "token": tok})
        assert r.status_code != 200, f"{bad!r} 不应放行，实际 {r.status_code}"
        assert r.status_code in (400, 403), f"{bad!r} 期望 400/403，实际 {r.status_code}"

    # 落盘侧同样拒绝首尾空白：即使共享 token 也不能创建带首尾空白的 bucket
    for bad in ("demo ", " demo"):
        r = c.post("/api/objects", params={"token": ADMIN},
                   data={"bucket": bad, "key": "f.txt"},
                   files={"file": ("f.txt", b"x")})
        assert r.status_code == 400, f"{bad!r} 应被 validate_bucket 拒绝"
        assert "空白" in _detail(r)


def test_non_array_allow_prefixes_returns_400_not_500(ctx):
    """受限 actor 传非数组 allow_prefixes 时应 400，而不是未捕获 TypeError 的 500。"""
    c, _, _ = ctx
    actor = _new_token(c, user="pfx-actor", role="custom",
                       scopes=["token:write", "object:list"],
                       allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]})
    r = c.post("/api/tokens", params={"token": actor},
               json={"user": "new", "role": "custom", "scopes": ["object:list"],
                     "allow_buckets": ["demo"], "allow_prefixes": {"demo": 123}})
    assert r.status_code == 400, r.text
    assert "allow_prefixes" in _detail(r)

    # 非对象整体由 pydantic 拦下（422），同样不会 500
    r = c.post("/api/tokens", params={"token": actor},
               json={"user": "new", "role": "custom", "scopes": ["object:list"],
                     "allow_buckets": ["demo"], "allow_prefixes": [1, 2]})
    assert r.status_code in (400, 422), r.text


def test_put_cannot_touch_token_broader_than_actor(ctx):
    """受限 actor 不能改动「比自身更大」的既有 token（含把它降级成自己范围）。"""
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    # 目标 token：不限 bucket + 全部 scope
    broad = c.post("/api/tokens", params={"token": ADMIN},
                   json={"user": "broad", "role": "admin"}).json()["data"]["token"]
    actor = _new_token(c, user="narrow", role="custom",
                       scopes=["token:write"], allow_buckets=["demo"])

    r = c.put(f"/api/tokens/{broad}", params={"token": actor},
              json={"allow_buckets": ["demo"]})
    assert r.status_code == 403, r.text
    # 即使只想改描述，也不能碰更大的 token
    r = c.put(f"/api/tokens/{broad}", params={"token": actor},
              json={"description": "hijack"})
    assert r.status_code == 403, r.text
    # 共享 token 仍然可以
    assert c.put(f"/api/tokens/{broad}", params={"token": ADMIN},
                 json={"description": "ok"}).status_code == 200


def test_put_token_writes_audit(ctx):
    """PUT /api/tokens 必须留审计（此前完全无记录）。"""
    c, _, _ = ctx
    tok = _new_token(c, user="audited", role="viewer")
    before = len(c.get("/api/audit",
                       params={"token": ADMIN, "limit": 5000}).json()["data"])
    assert c.put(f"/api/tokens/{tok}", params={"token": ADMIN},
                 json={"description": "changed"}).status_code == 200
    rows = c.get("/api/audit", params={"token": ADMIN, "limit": 5000}).json()["data"]
    assert any(row.get("action") == "token_update" for row in rows), rows
    assert len(rows) > before


def test_post_cannot_overwrite_token_broader_than_actor(ctx):
    """POST /api/tokens 显式指定「已存在」的 token（覆写）时，同样不能动更大的 token；
    尤其不能借覆写把已禁用的 token 复活。"""
    c, _, _ = ctx
    _mk_bucket(c, "demo")
    broad = c.post("/api/tokens", params={"token": ADMIN},
                   json={"user": "broad", "role": "admin"}).json()["data"]["token"]
    assert c.put(f"/api/tokens/{broad}", params={"token": ADMIN},
                 json={"enabled": False}).status_code == 200
    actor = _new_token(c, user="narrow", role="custom",
                       scopes=["token:write", "object:list"], allow_buckets=["demo"])

    # 覆写（降级 + 复活）→ 403，且目标仍处于禁用状态
    r = c.post("/api/tokens", params={"token": actor},
               json={"token": broad, "user": "broad", "role": "custom",
                     "scopes": ["object:list"], "allow_buckets": ["demo"]})
    assert r.status_code == 403, r.text
    chk = c.get("/api/auth/check", params={"token": broad}).json()["data"]
    assert chk["valid"] is False and chk["enabled"] is False

    # 共享 token 覆写：merge 语义 → 未传的 enabled 保留（仍禁用），不会被被动复活
    assert c.post("/api/tokens", params={"token": ADMIN},
                  json={"token": broad, "user": "broad", "role": "viewer"}).status_code == 200
    assert c.get("/api/auth/check",
                 params={"token": broad}).json()["data"]["valid"] is False, \
        "覆写既有 token 不得复活已禁用 token"
    # 只有显式启用才恢复
    assert c.put(f"/api/tokens/{broad}", params={"token": ADMIN},
                 json={"enabled": True}).status_code == 200
    assert c.get("/api/auth/check",
                 params={"token": broad}).json()["data"]["valid"] is True


def test_prefix_normalization_rejects_dead_and_unlimited_writes(ctx):
    """路径前缀必须规范化：拒绝 .. / %2e%2e / 反斜杠 / 「/」这类会被静默当成「不限」的写法。"""
    c, _, _ = ctx

    def _mk(prefixes):
        return c.post("/api/tokens", params={"token": ADMIN},
                      json={"user": "pfx", "role": "custom", "scopes": ["object:list"],
                            "allow_buckets": ["demo"], "allow_prefixes": prefixes})

    assert _mk({"demo": ["a/../secret"]}).status_code == 400
    assert _mk({"demo": ["%2e%2e/secret"]}).status_code == 400
    assert _mk({"demo": ["a\\b"]}).status_code == 400
    assert _mk({"demo": ["/"]}).status_code == 400      # 否则会变成「不限前缀」
    assert _mk({"demo": [None]}).status_code == 400

    ok = _mk({"demo": ["/a/b/"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["allow_prefixes"] == {"demo": ["a/b"]}


def test_prefix_requires_matching_allow_buckets(ctx):
    """只写 allow_prefixes 会把「其余 bucket」静默放开（allow_buckets 为空 = 不限），必须 400。"""
    c, _, _ = ctx

    def mk(**kw):
        return c.post("/api/tokens", params={"token": ADMIN},
                      json={"user": "p", "role": "custom", "scopes": ["object:list"], **kw})

    # 缺 allow_buckets → 400
    r = mk(allow_prefixes={"demo": ["a/"]})
    assert r.status_code == 400, r.text
    assert "allow_buckets" in _detail(r)

    # 前缀所在 bucket 不在白名单 → 400
    r = mk(allow_buckets=["other"], allow_prefixes={"demo": ["a/"]})
    assert r.status_code == 400, r.text

    # 一致 → 200；只写 bucket 不写前缀也 200
    assert mk(allow_buckets=["demo", "other"],
              allow_prefixes={"demo": ["a/"]}).status_code == 200
    assert mk(allow_buckets=["demo"]).status_code == 200

    # PUT 同样受约束
    tok = _new_token(c, user="upd", role="custom", scopes=["object:list"],
                     allow_buckets=["demo"])
    r = c.put(f"/api/tokens/{tok}", params={"token": ADMIN},
              json={"allow_prefixes": {"secret": ["x/"]}})
    assert r.status_code == 400, r.text
