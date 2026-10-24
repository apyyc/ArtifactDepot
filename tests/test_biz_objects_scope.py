# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""业务验收：17 个对象接口的资源范围语义矩阵（task-10）

覆盖（按 task-10 描述清单，共 15 条路由）：
  #10 POST /api/buckets
  #17 GET  /api/objects/list        （只验范围与只读性，读语义由 task-9 覆盖）
  #18 GET  /api/objects/head
  #19 GET  /api/objects/download
  #20 POST /api/objects             （upload）
  #21 DELETE /api/objects
  #22 POST /api/objects/mkdir
  #23 POST /api/objects/rename
  #24 POST /api/objects/presign
  #25 GET  /api/objects/signed-links
  #27 POST /api/objects/signed-links/{id}/revoke
  #28 POST /api/objects/initiate
  #29 POST /api/objects/chunk
  #30 POST /api/objects/complete
  #31 POST /api/objects/abort

验收原则（业务结果，不看状态码）：
- A 正常：受限 token 在范围内操作 → 成功，且用共享 token 复查**实际落盘 / 清单 / 审计**；
- 越界：范围外 bucket / 前缀 → 403，且用「磁盘 + .warehouse.json + signed_links.json +
  audit.log 条数 + tokens.json」快照证明数据未被改动；
- B 参数异常：缺必填 422、非法 bucket/key 400、空串、超长（bucket>63）、不存在 404；
- D 边界：overwrite=false 409、删除非空目录 400、rename 自身子目录 400、rename 源不存在 404、
  presign count/expires 越界 400、分片单块超限 413、complete 分片不全 400；
- E 状态/归属：分片会话绑定创建者、他人 chunk/abort/complete 403、abort 后 complete 400；
- 签名链接：link+tk 免 token 下载、按次扣减、作废后失效、他人不能作废；
- R12：共享 access_token 不受任何范围限制。

已知缺陷（见 reports/biz_objects_scope.md）用 strict xfail 标记，断言的是**基线要求的行为**：
修复后 xfail 会转 xpass，需移除标记并转正为回归用例。

运行：
    cd ArtifactDepot/ArtifactDepot
    PYTHONPATH=/home/apyyc/work/ArtifactDepot/.audit-deps/libs:src python3 -m pytest tests/test_biz_objects_scope.py -q
"""
import hashlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "biz-objects-admin-0123456789"

ALL_OBJ_SCOPES = [
    "bucket:create", "object:list", "object:head", "object:download",
    "object:upload", "object:delete", "object:mkdir", "object:rename",
    "link:create", "link:list", "link:revoke",
    "upload:initiate", "upload:chunk", "upload:complete", "upload:abort",
]

# 缺陷说明（strict xfail reason）
RENAME_500_REASON = (
    "已上报缺陷 OBJ-001（P1）：rename 源/目标类型冲突时抛未捕获异常 → 500；"
    "目录移动中途失败不回滚，会留下部分移动的数据（违反文档「失败即整体回滚」）"
)
RENAME_MANIFEST_REASON = (
    "已上报缺陷 OBJ-002（P2）：rename overwrite=true 覆盖既有文件/目录后未更新 "
    ".warehouse.json，sha256/size/mtime 与实际文件不符；head/list/ETag 返回陈旧值"
)
MKDIR_500_REASON = (
    "已上报缺陷 OBJ-004（P2）：mkdir 的 key 或父路径已是文件时抛未捕获异常 → 500"
)
PRESIGN_URL_REASON = (
    "已上报缺陷 OBJ-006（P2）：presign / signed-links 拼装的 url 未做 URL 编码，"
    "key（或 bucket）含 & # + 等合法字符时生成的共享链接不可用（404/401）"
)


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

def _write_config(cfg: Path, depot: Path, extra=None) -> None:
    data = {
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True, "max_upload_mb": 0,
        "require_read_token": False,
    }
    data.update(extra or {})
    cfg.write_text(json.dumps(data), encoding="utf-8")


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
    # 不抛 500：让未处理异常以真实 HTTP 状态出现，便于断言/定位（缺陷用例会断到 500）
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, cfg, depot, config
    config._CONFIG = None


def _set_config(cfg: Path, config, **overrides) -> None:
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data.update(overrides)
    cfg.write_text(json.dumps(data), encoding="utf-8")
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


def _links(depot: Path) -> list:
    p = depot / "signed_links.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_SNAP_EXCLUDE = {"tokens.json", "audit.log", "signed_links.json", ".state.lock"}


def _disk(depot: Path) -> dict:
    out = {}
    for p in sorted(depot.rglob("*")):
        rel = p.relative_to(depot).as_posix()
        if rel in _SNAP_EXCLUDE:
            continue
        if p.is_dir():
            out[rel + "/"] = None
        elif p.is_file():
            out[rel] = _sha(p.read_bytes())
    return out


def _links_text(depot: Path) -> str:
    p = depot / "signed_links.json"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _manifests_text(depot: Path) -> dict:
    out = {}
    for p in sorted(depot.glob("*/.warehouse.json")):
        out[p.parent.name] = p.read_text(encoding="utf-8")
    return out


def _state(depot: Path) -> dict:
    """与业务数据相关的完整快照（token 的 last_used_at 属预期副作用，单独排除）。"""
    return {
        "disk": _disk(depot),
        "manifests": _manifests_text(depot),
        "links": _links_text(depot),
        "audit_count": len(_audit(depot)),
    }


def _detail(resp) -> str:
    try:
        return str(resp.json().get("detail", ""))
    except Exception:
        return resp.text


def _mk_bucket(c, name: str):
    r = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": name})
    assert r.status_code == 200, r.text
    return r


def _up(c, bucket: str, key: str, data: bytes = b"x", token: str = ADMIN,
        overwrite: bool = True, source_url: str = "", filename: str = "f.bin"):
    return c.post("/api/objects", params={"token": token},
                  data={"bucket": bucket, "key": key,
                        "overwrite": str(overwrite).lower(), "source_url": source_url},
                  files={"file": (filename, data)})


def _token(c, user: str, scopes=None, buckets=None, prefixes=None) -> str:
    body = {"user": user, "role": "custom",
            "scopes": ALL_OBJ_SCOPES if scopes is None else scopes}
    if buckets is not None:
        body["allow_buckets"] = buckets
    if prefixes is not None:
        body["allow_prefixes"] = prefixes
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _link(c, token: str, bucket: str, key: str, **kw) -> dict:
    r = c.post("/api/objects/presign", params={"token": token},
               json={"bucket": bucket, "key": key, **kw})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _link_query(url: str) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def _session_path(uid: str) -> Path:
    from artifactdepot import storage
    return storage._chunk_session_path(uid)


def _chunk(c, token: str, uid: str, index: int, data: bytes):
    return c.post("/api/objects/chunk", params={"token": token},
                  data={"upload_id": uid, "index": index},
                  files={"chunk": ("part.bin", data)})


def _complete(c, token: str, uid: str, bucket: str, key: str, total: int, **kw):
    return c.post("/api/objects/complete", params={"token": token},
                  data={"upload_id": uid, "bucket": bucket, "key": key,
                        "total_chunks": total, **kw})


def _seed(c):
    """demo: a/x.txt、b/y.txt；secret: s.txt。"""
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    assert _up(c, "demo", "a/x.txt", b"AAA").status_code == 200
    assert _up(c, "demo", "b/y.txt", b"BBB").status_code == 200
    assert _up(c, "secret", "s.txt", b"SECRET").status_code == 200


# ===========================================================================
# #10 POST /api/buckets
# ===========================================================================

def test_create_bucket_normal_idempotent_and_audit(ctx):
    """A/F：创建 bucket 真实落盘 + 审计；重复创建幂等且不破坏内容。"""
    c, _, depot, _ = ctx
    n = len(_audit(depot))
    r = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": "newb"})
    assert r.status_code == 200
    assert r.json()["data"] == {"bucket": "newb", "created": True}
    assert (depot / "newb").is_dir() and (depot / "newb" / ".keep").is_file()
    rows = _audit(depot)
    assert len(rows) == n + 1 and rows[-1]["action"] == "create_bucket"
    assert rows[-1]["bucket"] == "newb" and rows[-1]["actor"] == "系统/工具"

    snap = _state(depot)
    r2 = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": "newb"})
    assert r2.status_code == 200 and r2.json()["data"]["created"] is False
    assert _state(depot)["disk"] == snap["disk"], "重复创建不得改动已有目录内容"


def test_create_bucket_validation_400_422(ctx):
    """B：缺 body/字段 422；空/超长/路径分隔符/隐藏/首尾空白 400；全部零落盘零审计。"""
    c, _, depot, _ = ctx
    n = len(_audit(depot))
    assert c.post("/api/buckets", params={"token": ADMIN}).status_code == 422
    for body, code in (({}, 422), ({"bucket": ""}, 400), ({"bucket": "a" * 64}, 400),
                       ({"bucket": "a/b"}, 400), ({"bucket": "a\\b"}, 400),
                       ({"bucket": ".hidden"}, 400), ({"bucket": " demo"}, 400),
                       ({"bucket": "demo "}, 400)):
        r = c.post("/api/buckets", params={"token": ADMIN}, json=body)
        assert r.status_code == code, (body, r.text)
    assert _disk(depot) == {}
    assert len(_audit(depot)) == n


def test_create_bucket_scope_and_auth(ctx):
    """C：受限 token 只能创建 allow_buckets 内的 bucket；缺 scope 403、无 token 401。"""
    c, _, depot, _ = ctx
    actor = _token(c, "actor", scopes=["bucket:create"], buckets=["demo"])
    r = c.post("/api/buckets", params={"token": actor}, json={"bucket": "demo"})
    assert r.status_code == 200 and r.json()["data"]["created"] is True
    assert (depot / "demo" / ".keep").is_file()

    n = len(_audit(depot))
    snap = _state(depot)
    r = c.post("/api/buckets", params={"token": actor}, json={"bucket": "secret"})
    assert r.status_code == 403 and "资源范围" in _detail(r)
    assert _state(depot) == snap and len(_audit(depot)) == n
    assert not (depot / "secret").exists()

    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.post("/api/buckets", params={"token": nobody},
                  json={"bucket": "demo"}).status_code == 403
    assert c.post("/api/buckets", json={"bucket": "demo"}).status_code == 401
    assert c.post("/api/buckets", params={"token": "bad"},
                  json={"bucket": "demo"}).status_code == 401


# ===========================================================================
# #17 GET /api/objects/list（范围 + 只读性）
# ===========================================================================

def test_list_range_and_readonly(ctx):
    """A/C/D/F：范围内可见且内容正确；越界 403；列表不写盘、不写审计。"""
    c, _, depot, _ = ctx
    _seed(c)
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    snap = _state(depot)

    r = c.get("/api/objects/list", params={"bucket": "demo", "prefix": "a", "token": actor})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["prefix"] == "a" and data["total"] == 1
    item = data["items"][0]
    assert item["name"] == "x.txt" and item["key"] == "a/x.txt" and item["is_dir"] is False
    assert item["size"] == 3 and item["sha256"] == _sha(b"AAA")

    for params in ({"bucket": "demo", "prefix": "b"},
                   {"bucket": "demo"},              # 根目录也在前缀范围外
                   {"bucket": "secret"}):
        r = c.get("/api/objects/list", params={**params, "token": actor})
        assert r.status_code == 403, params
        assert "资源范围" in _detail(r)
        assert "AAA" not in r.text and "BBB" not in r.text and "SECRET" not in r.text

    # 读接口不该改动任何数据/审计
    assert _state(depot) == snap
    # 不存在的 bucket 对共享 token 返回空列表（不是 404）
    r = c.get("/api/objects/list", params={"bucket": "ghost", "token": ADMIN})
    assert r.status_code == 200 and r.json()["data"]["items"] == []


def test_list_param_boundaries(ctx):
    """B：缺 bucket 422、非法 bucket/prefix 400、limit/offset 越界 422。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "a.txt", b"A")
    assert c.get("/api/objects/list").status_code == 422
    assert c.get("/api/objects/list", params={"bucket": "a/b"}).status_code == 400
    assert c.get("/api/objects/list", params={"bucket": "demo "}).status_code == 400
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "prefix": "../"}).status_code == 400
    for params in ({"limit": -1}, {"limit": 10001}, {"offset": -1}):
        r = c.get("/api/objects/list", params={"bucket": "demo", **params})
        assert r.status_code == 422, params
    # 匿名仍公开（require_read_token=false）
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 200


# ===========================================================================
# #18 GET /api/objects/head
# ===========================================================================

def test_head_scope_metadata_and_etag(ctx):
    """A/C/D/F：范围内元信息真实（size/sha/ETag/is_dir）；越界 403；不存在 404；只读。"""
    c, _, depot, _ = ctx
    _seed(c)
    assert c.post("/api/objects/mkdir", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/dir"}).status_code == 200
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    snap = _state(depot)

    r = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/x.txt", "token": actor})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["exists"] is True and data["is_dir"] is False
    assert data["size"] == 3 and data["sha256"] == _sha(b"AAA")
    etag = r.headers.get("etag")
    assert etag == f'"{_sha(b"AAA")}"'
    # 目录也能探测
    r = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/dir", "token": actor})
    assert r.status_code == 200 and r.json()["data"]["is_dir"] is True
    # 越界 / 不存在
    assert c.get("/api/objects/head", params={"bucket": "demo", "key": "b/y.txt",
                                              "token": actor}).status_code == 403
    assert c.get("/api/objects/head", params={"bucket": "secret", "key": "s.txt",
                                              "token": actor}).status_code == 403
    r = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/nope.txt",
                                           "token": actor})
    assert r.status_code == 404 and "对象不存在" in _detail(r)
    # If-None-Match → 304，且不产生审计
    r = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/x.txt", "token": actor},
              headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert _state(depot) == snap


def test_head_auth_and_validation(ctx):
    """B/C：无/无效 token 401、缺 scope 403、缺 key 422、非法 key 400。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "x.txt", b"X")
    assert c.get("/api/objects/head", params={"bucket": "demo", "key": "x.txt"}).status_code == 401
    assert c.get("/api/objects/head", params={"bucket": "demo", "key": "x.txt",
                                              "token": "bad"}).status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.get("/api/objects/head", params={"bucket": "demo", "key": "x.txt",
                                              "token": nobody}).status_code == 403
    assert c.get("/api/objects/head", params={"bucket": "demo", "token": ADMIN}).status_code == 422
    assert c.get("/api/objects/head", params={"bucket": "demo", "key": "a/../b",
                                              "token": ADMIN}).status_code == 400


# ===========================================================================
# #19 GET /api/objects/download
# ===========================================================================

def test_download_scope_content_and_range(ctx):
    """A/C/D/F：范围内下载内容正确、Range 206；越界 403 且不消费任何东西。"""
    c, _, depot, _ = ctx
    _seed(c)
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    snap = _state(depot)
    n = len(_audit(depot))

    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "token": actor})
    assert r.status_code == 200 and r.content == b"AAA"
    assert r.headers.get("etag") == f'"{_sha(b"AAA")}"'
    row = _audit(depot)[-1]
    assert row["action"] == "download" and row["actor"] == "actor"
    assert row["bucket"] == "demo" and row["key"] == "a/x.txt" and row["size"] == 3

    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt"},
              headers={"Authorization": f"Bearer {actor}"})
    assert r.status_code == 200 and r.content == b"AAA"
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "token": actor}, headers={"Range": "bytes=0-0"})
    assert r.status_code == 206 and r.content == b"A"
    assert r.headers.get("content-range") == "bytes 0-0/3"

    for params in ({"bucket": "demo", "key": "b/y.txt"}, {"bucket": "secret", "key": "s.txt"}):
        before = len(_audit(depot))
        r = c.get("/api/objects/download", params={**params, "token": actor})
        assert r.status_code == 403, params
        assert _state(depot)["disk"] == snap["disk"]
        assert len(_audit(depot)) == before, "越界下载不应写审计"

    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "a/nope.txt",
                                                  "token": actor}).status_code == 404
    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt"}).status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                                  "token": nobody}).status_code == 403
    assert n < len(_audit(depot))


def test_download_signed_link_count_revoke(ctx):
    """A/E/R：link+tk 免 token 下载；按次扣减；用尽 401；作废后 401；审计 actor=创建者。"""
    c, _, depot, _ = ctx
    _seed(c)
    creator = _token(c, "creator", buckets=["demo"], prefixes={"demo": ["a/"]})
    data = _link(c, creator, "demo", "a/x.txt", mode="count", count=2)
    qs = _link_query(data["url"])
    assert data["mode"] == "count" and data["max_uses"] == 2 and data["remaining"] == 2
    assert qs["link"] == data["id"] and qs["tk"]

    def remaining():
        return [l["remaining"] for l in _links(depot) if l["id"] == data["id"]][0]

    assert remaining() == 2
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "link": data["id"], "tk": qs["tk"]})
    assert r.status_code == 200 and r.content == b"AAA"
    assert remaining() == 1
    # 审计 actor 是链接创建者
    row = _audit(depot)[-1]
    assert row["action"] == "download" and row["actor"] == "creator" and row["size"] == 3

    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "link": data["id"], "tk": qs["tk"]})
    assert r.status_code == 200 and remaining() == 0
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "link": data["id"], "tk": qs["tk"]})
    assert r.status_code == 401 and "次数已用完" in _detail(r)

    # 创建者作废自己的链接；作废后立即失效
    n = len(_audit(depot))
    r = c.post(f"/api/objects/signed-links/{data['id']}/revoke", params={"token": creator})
    assert r.status_code == 200
    assert [l for l in _links(depot) if l["id"] == data["id"]][0]["revoked"] is True
    assert len(_audit(depot)) == n + 1 and _audit(depot)[-1]["action"] == "revoke"
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "link": data["id"], "tk": qs["tk"]})
    assert r.status_code == 401 and "已作废" in _detail(r)


def test_download_signed_link_edge_cases(ctx):
    """E/D：错误 tk/key 不扣次数；对象不存在 404 不扣次数；time 链接过期 401。"""
    c, _, depot, _ = ctx
    _seed(c)
    data = _link(c, ADMIN, "demo", "a/x.txt", mode="count", count=2)
    qs = _link_query(data["url"])

    def remaining(link_id):
        return [l["remaining"] for l in _links(depot) if l["id"] == link_id][0]

    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "link": data["id"], "tk": "wrong"})
    assert r.status_code == 401 and "链接无效" in _detail(r)
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "b/y.txt",
                                               "link": data["id"], "tk": qs["tk"]})
    assert r.status_code == 401 and "链接与文件不符" in _detail(r)
    assert remaining(data["id"]) == 2, "无效消费不得扣减次数"

    # 对象不存在：先查存在再扣次数 → 404 且 remaining 不变（文档 2.3）
    data2 = _link(c, ADMIN, "demo", "a/nope.txt", mode="count", count=2)
    qs2 = _link_query(data2["url"])
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/nope.txt",
                                               "link": data2["id"], "tk": qs2["tk"]})
    assert r.status_code == 404 and remaining(data2["id"]) == 2

    # time 链接过期：直接改 signed_links.json 模拟时间流逝
    data3 = _link(c, ADMIN, "demo", "a/x.txt", mode="time", expires=60)
    links = _links(depot)
    for entry in links:
        if entry["id"] == data3["id"]:
            entry["expires"] = 1
    (depot / "signed_links.json").write_text(json.dumps(links), encoding="utf-8")
    qs3 = _link_query(data3["url"])
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "link": data3["id"], "tk": qs3["tk"]})
    assert r.status_code == 401 and "已过期" in _detail(r)


def test_download_conditional_request_records_audit(ctx):
    """F：If-None-Match 命中返回 304 并记录 not_modified 审计（不重复传输）。"""
    c, _, depot, _ = ctx
    _seed(c)
    etag = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/x.txt",
                                              "token": ADMIN}).headers.get("etag")
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "token": ADMIN},
              headers={"If-None-Match": etag})
    assert r.status_code == 304 and r.content == b""
    row = _audit(depot)[-1]
    assert row["action"] == "download" and row["not_modified"] is True and row["size"] == 0


# ===========================================================================
# #20 POST /api/objects（upload）
# ===========================================================================

def test_upload_normal_disk_manifest_audit(ctx):
    """A/F：落盘字节、清单（sha/size/uploader/source_url）、审计三处一致，共享 token 复查可读。"""
    c, _, depot, _ = ctx
    _seed(c)
    actor = _token(c, "uploader", buckets=["demo"], prefixes={"demo": ["a/"]})
    payload = b"DATA-1234"
    n = len(_audit(depot))
    r = _up(c, "demo", "a/new.bin", payload, token=actor,
            source_url="http://example.com/src", filename="new.bin")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["bucket"] == "demo" and data["key"] == "a/new.bin"
    assert data["size"] == len(payload) and data["sha256"] == _sha(payload)

    assert (depot / "demo" / "a" / "new.bin").read_bytes() == payload
    meta = _manifest(depot, "demo")["a/new.bin"]
    assert meta["size"] == len(payload) and meta["sha256"] == _sha(payload)
    assert meta["uploader"] == "uploader" and meta["source_url"] == "http://example.com/src"

    rows = _audit(depot)
    assert len(rows) == n + 1
    row = rows[-1]
    assert row["action"] == "upload" and row["actor"] == "uploader"
    assert row["bucket"] == "demo" and row["key"] == "a/new.bin"
    assert row["size"] == len(payload) and row["sha256"] == _sha(payload)

    # 共享 token 复查：list/head 都能看到正确元信息
    item = [i for i in c.get("/api/objects/list", params={"bucket": "demo", "prefix": "a",
                                                          "token": ADMIN}).json()["data"]["items"]
            if i["key"] == "a/new.bin"][0]
    assert item["sha256"] == _sha(payload) and item["uploader"] == "uploader"
    head = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/new.bin",
                                              "token": ADMIN}).json()["data"]
    assert head["sha256"] == _sha(payload) and head["size"] == len(payload)
    # 空文件也可上传
    assert _up(c, "demo", "a/empty.bin", b"").status_code == 200
    assert (depot / "demo" / "a" / "empty.bin").read_bytes() == b""
    # 文档 1.5：bucket 无需预创建，首次上传自动建目录（允许范围内的新 bucket）
    fresh = _token(c, "fresh-user", scopes=["object:upload"], buckets=["v-new"])
    assert _up(c, "v-new", "k.txt", b"NEW", token=fresh).status_code == 200
    assert (depot / "v-new" / "k.txt").read_bytes() == b"NEW"
    # token 传递方式：multipart 表单字段 / Authorization: Bearer 均可
    r = c.post("/api/objects", data={"bucket": "demo", "key": "a/form-token.txt",
                                     "token": ADMIN},
               files={"file": ("f", b"FORM")})
    assert r.status_code == 200
    r = c.post("/api/objects", headers={"Authorization": f"Bearer {ADMIN}"},
               data={"bucket": "demo", "key": "a/bearer-token.txt"},
               files={"file": ("f", b"BEAR")})
    assert r.status_code == 200
    assert (depot / "demo" / "a" / "bearer-token.txt").read_bytes() == b"BEAR"


def test_upload_out_of_range_403_zero_side_effects(ctx):
    """C/F：越界 bucket / 越界前缀上传 403，磁盘/清单/审计零变化。"""
    c, _, depot, _ = ctx
    _seed(c)
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    snap = _state(depot)

    for bucket, key in (("demo", "b/evil.txt"), ("secret", "a/evil.txt")):
        r = _up(c, bucket, key, b"EVIL", token=actor)
        assert r.status_code == 403, (bucket, key, r.text)
        assert "资源范围" in _detail(r)
        assert _state(depot) == snap
        assert not (depot / bucket / key).exists()


def test_upload_overwrite_conflict_and_success(ctx):
    """D/F：overwrite=false 冲突 409 且原文件/清单/审计不变；overwrite=true 真正覆盖并更新清单。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    assert _up(c, "demo", "f.txt", b"OLD").status_code == 200
    old_sha = _sha(b"OLD")
    assert _manifest(depot, "demo")["f.txt"]["sha256"] == old_sha
    snap = _state(depot)

    r = _up(c, "demo", "f.txt", b"NEW", overwrite=False)
    assert r.status_code == 409 and "已存在" in _detail(r)
    assert (depot / "demo" / "f.txt").read_bytes() == b"OLD"
    assert _manifest(depot, "demo")["f.txt"]["sha256"] == old_sha
    assert _state(depot) == snap

    r = _up(c, "demo", "f.txt", b"NEW", overwrite=True)
    assert r.status_code == 200
    assert (depot / "demo" / "f.txt").read_bytes() == b"NEW"
    assert _manifest(depot, "demo")["f.txt"]["sha256"] == _sha(b"NEW")
    assert _audit(depot)[-1]["sha256"] == _sha(b"NEW")


def test_upload_validation_and_limits(ctx):
    """B/D：缺字段 422、非法 bucket/key 400、超限 413；失败零落盘零审计。"""
    c, cfg, depot, config = ctx
    _mk_bucket(c, "demo")
    snap = _state(depot)
    n = len(_audit(depot))
    cases = [
        (c.post("/api/objects", params={"token": ADMIN},
                data={"bucket": "demo", "key": "nofile.txt"}), 422),
        (c.post("/api/objects", params={"token": ADMIN}, data={"key": "k.txt"},
                files={"file": ("f", b"x")}), 422),
        (c.post("/api/objects", params={"token": ADMIN}, data={"bucket": "demo"},
                files={"file": ("f", b"x")}), 422),
        (_up(c, "a" * 64, "k.txt"), 400),
        (_up(c, "demo", "a/../x.txt"), 400),
        (_up(c, "demo", "a/./b.txt"), 400),
        (_up(c, "demo", "%2e%2e/x.txt"), 400),
        (_up(c, "demo", "a\\b.txt"), 400),
        (_up(c, "demo ", "k.txt"), 400),
    ]
    for resp, code in cases:
        assert resp.status_code == code, (resp.text, code)
    assert _state(depot) == snap
    assert len(_audit(depot)) == n

    # 受限 token：bucket 首尾空白先被资源范围拦成 403（文档 1.5 已注明）
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    r = _up(c, "demo ", "a/k.txt", b"x", token=actor)
    assert r.status_code == 403

    # max_upload_mb 上限 → 413，无文件、无清单、无审计
    _set_config(cfg, config, max_upload_mb=1)
    big = b"z" * (1024 * 1024 + 10)
    before = _state(depot)
    r = _up(c, "demo", "a/big.bin", big)
    assert r.status_code == 413 and "上限" in _detail(r)
    assert _state(depot) == before
    assert not (depot / "demo" / "a" / "big.bin").exists()

    # 权限
    assert c.post("/api/objects", data={"bucket": "demo", "key": "x"},
                  files={"file": ("f", b"x")}).status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert _up(c, "demo", "a/k.txt", b"x", token=nobody).status_code == 403


def test_upload_type_conflict_should_not_500(ctx):
    """B/D：upload 目标 key 已是目录、或父路径是文件 → 应 400/409，而不是 500。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    assert c.post("/api/objects/mkdir", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "dir"}).status_code == 200

    r = _up(c, "demo", "dir", b"FILE")  # 目标 key 是目录（overwrite 默认 true）
    assert r.status_code in (400, 409), f"upload 目标为目录时返回 {r.status_code}（未处理异常 → 500）"

    assert _up(c, "demo", "f.txt", b"F").status_code == 200
    r = _up(c, "demo", "f.txt/sub.txt", b"S")  # 父路径是文件
    assert r.status_code in (400, 409), f"upload 父路径为文件时返回 {r.status_code}（未处理异常 → 500）"


# ===========================================================================
# #21 DELETE /api/objects
# ===========================================================================

def test_delete_file_and_empty_dir_consistency(ctx):
    """A/F：删除文件/空目录真实生效，清单同步移除，审计留痕，其他数据不变。"""
    c, _, depot, _ = ctx
    _seed(c)
    assert c.post("/api/objects/mkdir", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "emptydir"}).status_code == 200
    secret_before = _disk(depot)["secret/s.txt"]
    n = len(_audit(depot))

    r = c.delete("/api/objects", params={"token": ADMIN, "bucket": "demo", "key": "a/x.txt"})
    assert r.status_code == 200 and r.json()["data"]["deleted"] == "demo/a/x.txt"
    assert not (depot / "demo" / "a" / "x.txt").exists()
    assert "a/x.txt" not in _manifest(depot, "demo")
    row = _audit(depot)[-1]
    assert row["action"] == "delete" and row["bucket"] == "demo" and row["key"] == "a/x.txt"

    r = c.delete("/api/objects", params={"token": ADMIN, "bucket": "demo", "key": "emptydir"})
    assert r.status_code == 200
    assert not (depot / "demo" / "emptydir").exists()
    assert _audit(depot)[-1]["action"] == "delete"
    assert len(_audit(depot)) == n + 2
    assert _disk(depot)["secret/s.txt"] == secret_before, "删除一个对象不得影响其他 bucket"


def test_delete_nonempty_dir_400_zero_side_effects(ctx):
    """D/F：删除非空目录 400，目录/文件/清单/审计全部不变；清空后可删。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    assert c.post("/api/objects/mkdir", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "d"}).status_code == 200
    assert _up(c, "demo", "d/f.txt", b"F").status_code == 200
    snap = _state(depot)

    r = c.delete("/api/objects", params={"token": ADMIN, "bucket": "demo", "key": "d"})
    assert r.status_code == 400 and "目录非空" in _detail(r)
    assert _state(depot) == snap

    assert c.delete("/api/objects", params={"token": ADMIN,
                                            "bucket": "demo", "key": "d/f.txt"}).status_code == 200
    assert c.delete("/api/objects", params={"token": ADMIN, "bucket": "demo",
                                            "key": "d"}).status_code == 200
    assert not (depot / "demo" / "d").exists()


def test_delete_scope_auth_and_boundaries(ctx):
    """C/B/D：越界 403 且零改动；范围内可删；404/422/400 参数边界。"""
    c, _, depot, _ = ctx
    _seed(c)
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    snap = _state(depot)
    for params in ({"bucket": "demo", "key": "b/y.txt"},
                   {"bucket": "secret", "key": "s.txt"}):
        r = c.delete("/api/objects", params={**params, "token": actor})
        assert r.status_code == 403
        assert _state(depot) == snap

    assert c.delete("/api/objects", params={"token": actor,
                                            "bucket": "demo", "key": "a/x.txt"}).status_code == 200
    assert not (depot / "demo" / "a" / "x.txt").exists()

    assert c.delete("/api/objects", params={"token": ADMIN,
                                            "bucket": "demo", "key": "nope"}).status_code == 404
    assert c.delete("/api/objects", params={"token": ADMIN,
                                            "bucket": "demo"}).status_code == 422
    assert c.delete("/api/objects", params={"token": ADMIN,
                                            "bucket": "demo", "key": "../x"}).status_code == 400
    assert c.delete("/api/objects", params={"bucket": "demo", "key": "x"}).status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.delete("/api/objects", params={"token": nobody,
                                            "bucket": "demo", "key": "b/y.txt"}).status_code == 403


# ===========================================================================
# #22 POST /api/objects/mkdir
# ===========================================================================

def test_mkdir_normal_scope_audit(ctx):
    """A/F：范围内建多级目录 + .keep；幂等；越界 403 零改动；审计留痕。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    n = len(_audit(depot))

    r = c.post("/api/objects/mkdir", params={"token": actor},
               json={"bucket": "demo", "key": "a/d1/d2"})
    assert r.status_code == 200 and r.json()["data"]["key"] == "a/d1/d2/"
    assert (depot / "demo" / "a" / "d1" / "d2" / ".keep").is_file()
    assert len(_audit(depot)) == n + 1
    row = _audit(depot)[-1]
    assert row["action"] == "mkdir" and row["actor"] == "actor"
    assert row["bucket"] == "demo" and row["key"] == "a/d1/d2"

    snap = _state(depot)
    assert c.post("/api/objects/mkdir", params={"token": actor},
                  json={"bucket": "demo", "key": "a/d1/d2"}).status_code == 200
    assert _state(depot)["disk"] == snap["disk"]

    snap = _state(depot)
    for bucket, key in (("demo", "b/x"), ("secret", "a/x")):
        r = c.post("/api/objects/mkdir", params={"token": actor},
                   json={"bucket": bucket, "key": key})
        assert r.status_code == 403
        assert _state(depot) == snap
        assert not (depot / bucket / key).exists()

    # 文档 2.5：bucket 不存在时随目录一并创建
    mk = _token(c, "mk", scopes=["object:mkdir"], buckets=["m-new"])
    r = c.post("/api/objects/mkdir", params={"token": mk},
               json={"bucket": "m-new", "key": "d"})
    assert r.status_code == 200
    assert (depot / "m-new" / "d" / ".keep").is_file()


def test_mkdir_validation_and_auth(ctx):
    """B/C：缺字段 422、空/非法 key 400、越界前缀 traversal 400、无 token 401、缺 scope 403。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    snap = _state(depot)
    for body, code in (("missing", 422), ({"bucket": "demo"}, 422),
                       ({"bucket": "demo", "key": ""}, 400),
                       ({"bucket": "demo", "key": "."}, 400),
                       ({"bucket": "demo", "key": "../x"}, 400),
                       ({"bucket": "a" * 64, "key": "x"}, 400)):
        if body == "missing":
            r = c.post("/api/objects/mkdir", params={"token": ADMIN})
        else:
            r = c.post("/api/objects/mkdir", params={"token": ADMIN}, json=body)
        assert r.status_code == code, (body, r.text)
    assert _state(depot) == snap
    assert c.post("/api/objects/mkdir", json={"bucket": "demo", "key": "x"}).status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.post("/api/objects/mkdir", params={"token": nobody},
                  json={"bucket": "demo", "key": "x"}).status_code == 403

    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    snap = _state(depot)
    r = c.post("/api/objects/mkdir", params={"token": actor},
               json={"bucket": "demo", "key": "a/../evil"})
    assert r.status_code == 400
    assert _state(depot) == snap


@pytest.mark.xfail(strict=True, reason=MKDIR_500_REASON)
def test_mkdir_type_conflict_should_not_500(ctx):
    """B/D：mkdir 的 key 或父路径已是文件 → 应 400，而不是 500。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    assert _up(c, "demo", "f.txt", b"F").status_code == 200

    r = c.post("/api/objects/mkdir", params={"token": ADMIN},
               json={"bucket": "demo", "key": "f.txt"})
    assert r.status_code == 400, f"mkdir key 为文件时返回 {r.status_code}（未处理异常 → 500）"

    r = c.post("/api/objects/mkdir", params={"token": ADMIN},
               json={"bucket": "demo", "key": "f.txt/sub"})
    assert r.status_code == 400, f"mkdir 父路径为文件时返回 {r.status_code}（未处理异常 → 500）"


# ===========================================================================
# #23 POST /api/objects/rename
# ===========================================================================

def test_rename_normal_file_dir_audit_and_scope(ctx):
    """A/F：文件/目录改名真实迁移（磁盘+清单+审计）；越界目标 403 零改动；边界 400/404/422。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "a/src.txt", b"SRC")
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    n = len(_audit(depot))

    r = c.post("/api/objects/rename", params={"token": actor},
               json={"bucket": "demo", "key": "a/src.txt", "new_key": "a/dst.txt"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["from_key"] == "a/src.txt" and data["to_key"] == "a/dst.txt"
    assert not (depot / "demo" / "a" / "src.txt").exists()
    assert (depot / "demo" / "a" / "dst.txt").read_bytes() == b"SRC"
    manifest = _manifest(depot, "demo")
    assert "a/src.txt" not in manifest and manifest["a/dst.txt"]["sha256"] == _sha(b"SRC")
    row = _audit(depot)[-1]
    assert row["action"] == "rename" and row["actor"] == "actor"
    assert row["from_key"] == "a/src.txt" and row["to_key"] == "a/dst.txt"

    # 目录整体迁移
    c.post("/api/objects/mkdir", params={"token": actor}, json={"bucket": "demo", "key": "a/d1"})
    _up(c, "demo", "a/d1/x.txt", b"X")
    r = c.post("/api/objects/rename", params={"token": actor},
               json={"bucket": "demo", "key": "a/d1", "new_key": "a/d2"})
    assert r.status_code == 200 and r.json()["data"]["moved_objects"] == 2
    assert (depot / "demo" / "a" / "d2" / "x.txt").read_bytes() == b"X"
    assert "a/d2/x.txt" in _manifest(depot, "demo")

    # 目标越界 → 403，且数据不变
    snap = _state(depot)
    r = c.post("/api/objects/rename", params={"token": actor},
               json={"bucket": "demo", "key": "a/dst.txt", "new_key": "b/dst.txt"})
    assert r.status_code == 403 and "资源范围" in _detail(r)
    assert _state(depot) == snap
    assert (depot / "demo" / "a" / "dst.txt").exists()

    # 参数/状态边界
    assert c.post("/api/objects/rename", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/dst.txt"}).status_code == 422
    assert c.post("/api/objects/rename", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "nope", "new_key": "x"}).status_code == 404
    assert c.post("/api/objects/rename", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/dst.txt",
                        "new_key": "a/dst.txt"}).status_code == 400
    assert c.post("/api/objects/rename", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/d2",
                        "new_key": "a/d2/sub"}).status_code == 400
    assert c.post("/api/objects/rename", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/dst.txt", "new_key": "../../x"}).status_code == 400


def test_rename_target_conflict_409_zero_side_effects(ctx):
    """D/F：overwrite=false 目标冲突 409，文件/目录/清单/审计全部不变。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "src.txt", b"SRC")
    _up(c, "demo", "dst.txt", b"DST")
    c.post("/api/objects/mkdir", params={"token": ADMIN}, json={"bucket": "demo", "key": "d1"})
    _up(c, "demo", "d1/1.txt", b"ONE")
    c.post("/api/objects/mkdir", params={"token": ADMIN}, json={"bucket": "demo", "key": "d2"})
    _up(c, "demo", "d2/1.txt", b"OTHER")
    snap = _state(depot)

    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "src.txt", "new_key": "dst.txt"})
    assert r.status_code == 409 and "目标已存在" in _detail(r)
    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "d1", "new_key": "d2", "overwrite": False})
    assert r.status_code == 409
    assert _state(depot) == snap
    assert (depot / "demo" / "src.txt").read_bytes() == b"SRC"
    assert (depot / "demo" / "dst.txt").read_bytes() == b"DST"


def test_rename_empty_dir(ctx):
    """A/E：空目录（只有 .keep）可改名，占位随之迁移。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    c.post("/api/objects/mkdir", params={"token": ADMIN}, json={"bucket": "demo", "key": "emptyA"})
    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "emptyA", "new_key": "emptyB"})
    assert r.status_code == 200
    assert (depot / "demo" / "emptyB" / ".keep").is_file()
    assert not (depot / "demo" / "emptyA").exists()


@pytest.mark.xfail(strict=True, reason=RENAME_500_REASON)
def test_rename_type_conflict_should_be_4xx_not_500(ctx):
    """B/D：文件↔目录类型冲突且 overwrite=true → 应 400/409，而不是 500。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "b.txt", b"B")
    c.post("/api/objects/mkdir", params={"token": ADMIN}, json={"bucket": "demo", "key": "edir"})
    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "b.txt", "new_key": "edir", "overwrite": True})
    assert r.status_code in (400, 409), f"文件覆盖目录时返回 {r.status_code}（未处理异常 → 500）"

    c.post("/api/objects/mkdir", params={"token": ADMIN}, json={"bucket": "demo", "key": "sdir"})
    _up(c, "demo", "sdir/x.txt", b"X")
    _up(c, "demo", "a.txt", b"A")
    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "sdir", "new_key": "a.txt", "overwrite": True})
    assert r.status_code in (400, 409), f"目录覆盖文件时返回 {r.status_code}（未处理异常 → 500）"


@pytest.mark.xfail(strict=True, reason=RENAME_500_REASON)
def test_rename_failure_must_rollback_partial_moves(ctx):
    """F：rename 中途失败必须整体回滚（文档 2.6）——当前会留下部分移动的数据并 500。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "td/sub", b"TARGETFILE")   # 目标前缀下 sub 是同名文件（类型冲突）
    _up(c, "demo", "sd/1.txt", b"ONE")
    _up(c, "demo", "sd/sub/3.txt", b"THREE")
    snap = _state(depot)

    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "sd", "new_key": "td", "overwrite": True})
    assert r.status_code in (400, 409, 500)  # 允许显式失败码，但不允许数据被拆散
    assert _state(depot) == snap, "rename 失败后数据必须原样保留（不得部分移动）"


@pytest.mark.xfail(strict=True, reason=RENAME_MANIFEST_REASON)
def test_rename_overwrite_must_update_manifest_and_etag(ctx):
    """F：rename overwrite=true 覆盖既有文件后，清单 sha/head/ETag 必须反映新内容。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "a/old.txt", b"OLD")
    _up(c, "demo", "a/exists.txt", b"ORIGINAL")
    old_etag = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/exists.txt",
                                                  "token": ADMIN}).headers.get("etag")
    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "a/old.txt", "new_key": "a/exists.txt",
                     "overwrite": True})
    assert r.status_code == 200, r.text
    actual = _sha((depot / "demo" / "a" / "exists.txt").read_bytes())
    manifest = _manifest(depot, "demo")
    assert manifest["a/exists.txt"]["sha256"] == actual, "清单 sha256 仍是旧目标文件的（陈旧）"
    head = c.get("/api/objects/head", params={"bucket": "demo", "key": "a/exists.txt",
                                              "token": ADMIN})
    assert head.json()["data"]["sha256"] == actual, "head 返回陈旧 sha256"
    assert head.headers.get("etag") == f'"{actual}"', "ETag 未随覆盖更新"
    # 用旧 ETag 条件请求不能命中 304（否则客户端会持续使用旧缓存内容）
    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/exists.txt",
                                               "token": ADMIN},
              headers={"If-None-Match": old_etag})
    assert r.status_code == 200 and r.content == b"OLD", "旧 ETag 仍返回 304，客户端将拿到过期内容"


# ===========================================================================
# #24 POST /api/objects/presign
# ===========================================================================

def test_presign_modes_bounds_range_audit(ctx):
    """A/B/C/F：三种模式与 count/expires 上下限；越界 400；范围外 403 零写入；审计留痕。"""
    c, _, depot, _ = ctx
    _seed(c)
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})

    r = c.post("/api/objects/presign", params={"token": actor},
               json={"bucket": "demo", "key": "a/x.txt", "mode": "count", "count": 1})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["mode"] == "count" and data["max_uses"] == 1 and data["remaining"] == 1
    assert data["id"] in data["url"] and "tk=" in data["url"]
    assert len(_links(depot)) == 1
    assert _audit(depot)[-1]["action"] == "presign" and _audit(depot)[-1]["actor"] == "actor"

    assert c.post("/api/objects/presign", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/x.txt", "mode": "count",
                        "count": 10}).status_code == 200
    assert c.post("/api/objects/presign", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/x.txt", "mode": "time",
                        "expires": 60}).status_code == 200
    assert c.post("/api/objects/presign", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/x.txt", "mode": "time",
                        "expires": 604800}).status_code == 200
    # 默认 mode=time（1 小时），permanent 无过期且免 token 可下载
    default = c.post("/api/objects/presign", params={"token": ADMIN},
                     json={"bucket": "demo", "key": "a/x.txt"}).json()["data"]
    assert default["mode"] == "time" and default["max_uses"] is None
    assert isinstance(default["expires"], int) and default["expires"] > 0
    perm = c.post("/api/objects/presign", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/x.txt",
                        "mode": "permanent"}).json()["data"]
    assert perm["mode"] == "permanent" and perm["expires"] is None
    qs = _link_query(perm["url"])
    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                                  "link": perm["id"],
                                                  "tk": qs["tk"]}).content == b"AAA"
    # 越界/非法值
    for body in ({"bucket": "demo", "key": "a/x.txt", "mode": "bad"},
                 {"bucket": "demo", "key": "a/x.txt", "mode": "count", "count": 0},
                 {"bucket": "demo", "key": "a/x.txt", "mode": "count", "count": 11},
                 {"bucket": "demo", "key": "a/x.txt", "mode": "time", "expires": 59},
                 {"bucket": "demo", "key": "a/x.txt", "mode": "time", "expires": 604801}):
        before = _links_text(depot)
        r = c.post("/api/objects/presign", params={"token": ADMIN}, json=body)
        assert r.status_code == 400, (body, r.text)
        assert _links_text(depot) == before
    assert c.post("/api/objects/presign", params={"token": ADMIN}).status_code == 422
    assert c.post("/api/objects/presign", params={"token": ADMIN},
                  json={"bucket": "demo"}).status_code == 422
    assert c.post("/api/objects/presign", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a/x.txt",
                        "mode": "count", "count": "x"}).status_code == 422

    # 资源范围
    before = _links_text(depot)
    for bucket, key in (("demo", "b/y.txt"), ("secret", "s.txt")):
        r = c.post("/api/objects/presign", params={"token": actor},
                   json={"bucket": bucket, "key": key})
        assert r.status_code == 403
        assert _links_text(depot) == before
    # 文档 2.8：生成时只校验格式、不强制对象已存在
    r = c.post("/api/objects/presign", params={"token": actor},
               json={"bucket": "demo", "key": "a/not-created.txt"})
    assert r.status_code == 200
    assert c.post("/api/objects/presign", json={"bucket": "demo",
                                                "key": "a/x.txt"}).status_code == 401


@pytest.mark.xfail(strict=True, reason=PRESIGN_URL_REASON)
def test_presign_url_must_work_for_legal_special_keys(ctx):
    """A/F：presign 返回的 url 必须可直接访问（key 含 & # + 等合法字符时同样可用）。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    keys = ("a/weird&name.txt", "a/hash#name.txt", "a/plus+name.txt")
    for key in keys:
        assert _up(c, "demo", key, b"DATA").status_code == 200
        data = _link(c, ADMIN, "demo", key)
        r = c.get(data["url"])
        assert r.status_code == 200 and r.content == b"DATA", (
            f"key={key!r} 的共享链接不可用：{r.status_code} {r.text[:80]}")


# ===========================================================================
# #25 GET /api/objects/signed-links
# ===========================================================================

def test_signed_links_scope_and_url_visibility(ctx):
    """C/F：受限 token 只看到范围内链接；URL 仅管理员/创建者可见；不泄露 tk。"""
    c, _, depot, _ = ctx
    _seed(c)
    a = _token(c, "alice", buckets=["demo"], prefixes={"demo": ["a/"]})
    b = _token(c, "bob", buckets=["demo"])           # 整个 demo，能看 a/ 与 b/
    admin_link = _link(c, ADMIN, "secret", "s.txt")
    a_link = _link(c, a, "demo", "a/x.txt")
    b_link = _link(c, b, "demo", "b/y.txt")

    def ids(token):
        r = c.get("/api/objects/signed-links", params={"token": token})
        assert r.status_code == 200, r.text
        return r.json()["data"]

    alice_rows = ids(a)
    assert {x["id"] for x in alice_rows} == {a_link["id"]}
    assert alice_rows[0]["url"], "创建者应能看到完整链接"
    assert "token" not in alice_rows[0], "响应不得回传链接密钥"

    bob_rows = {x["id"]: x for x in ids(b)}
    assert set(bob_rows) == {a_link["id"], b_link["id"]}
    assert bob_rows[a_link["id"]]["url"] == "", "非创建者不应看到他人链接 URL"
    assert bob_rows[b_link["id"]]["url"]

    admin_rows = {x["id"]: x for x in ids(ADMIN)}
    assert set(admin_rows) == {admin_link["id"], a_link["id"], b_link["id"]}
    assert all(x["url"] for x in admin_rows.values())
    assert all("token" not in x for x in admin_rows.values())


def test_signed_links_auth_and_readonly(ctx):
    """C/F：无 token 401、缺 scope 403；列表调用不改链接/审计。"""
    c, _, depot, _ = ctx
    _seed(c)
    _link(c, ADMIN, "demo", "a/x.txt")
    assert c.get("/api/objects/signed-links").status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.get("/api/objects/signed-links", params={"token": nobody}).status_code == 403
    before_links = _links_text(depot)
    n = len(_audit(depot))
    assert c.get("/api/objects/signed-links", params={"token": ADMIN}).status_code == 200
    assert _links_text(depot) == before_links
    assert len(_audit(depot)) == n


# ===========================================================================
# #27 POST /api/objects/signed-links/{link_id}/revoke
# ===========================================================================

def test_revoke_idempotent_audit_and_effect(ctx):
    """A/E/F：创建者作废自己的链接 → 下载立即 401；重复作废幂等；404 零改动。"""
    c, _, depot, _ = ctx
    _seed(c)
    creator = _token(c, "creator", buckets=["demo"])
    data = _link(c, creator, "demo", "a/x.txt")
    qs = _link_query(data["url"])
    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                                  "link": data["id"],
                                                  "tk": qs["tk"]}).status_code == 200

    n = len(_audit(depot))
    r = c.post(f"/api/objects/signed-links/{data['id']}/revoke", params={"token": creator})
    assert r.status_code == 200
    assert [l for l in _links(depot) if l["id"] == data["id"]][0]["revoked"] is True
    assert len(_audit(depot)) == n + 1 and _audit(depot)[-1]["action"] == "revoke"
    assert _audit(depot)[-1]["bucket"] == "demo" and _audit(depot)[-1]["key"] == "a/x.txt"

    r = c.get("/api/objects/download", params={"bucket": "demo", "key": "a/x.txt",
                                               "link": data["id"], "tk": qs["tk"]})
    assert r.status_code == 401 and "已作废" in _detail(r)

    # 幂等：再次作废仍 200
    assert c.post(f"/api/objects/signed-links/{data['id']}/revoke",
                  params={"token": creator}).status_code == 200
    # 不存在 → 404，链接表不变
    before = _links_text(depot)
    r = c.post("/api/objects/signed-links/no-such-link/revoke", params={"token": ADMIN})
    assert r.status_code == 404 and "链接不存在" in _detail(r)
    assert _links_text(depot) == before


def test_revoke_scope_and_ownership(ctx):
    """C：范围外 403（且不作废）；他人链接 403（只能作废自己的）；管理员可作废任意。"""
    c, _, depot, _ = ctx
    _seed(c)
    alice = _token(c, "alice", buckets=["demo"])
    bob = _token(c, "bob", buckets=["demo"])
    secret_link = _link(c, ADMIN, "secret", "s.txt")
    alice_link = _link(c, alice, "demo", "a/x.txt")

    before = _links_text(depot)
    r = c.post(f"/api/objects/signed-links/{secret_link['id']}/revoke",
               params={"token": alice})
    assert r.status_code == 403 and "资源范围" in _detail(r)
    assert _links_text(depot) == before

    r = c.post(f"/api/objects/signed-links/{alice_link['id']}/revoke",
               params={"token": bob})
    assert r.status_code == 403 and "只能作废自己创建的链接" in _detail(r)
    assert [l for l in _links(depot) if l["id"] == alice_link["id"]][0]["revoked"] is False

    assert c.post(f"/api/objects/signed-links/{secret_link['id']}/revoke",
                  params={"token": ADMIN}).status_code == 200
    assert [l for l in _links(depot) if l["id"] == secret_link["id"]][0]["revoked"] is True
    assert c.post(f"/api/objects/signed-links/{alice_link['id']}/revoke",
                  params={"token": ADMIN}).status_code == 200
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.post(f"/api/objects/signed-links/{alice_link['id']}/revoke",
                  params={"token": nobody}).status_code == 403
    assert c.post(f"/api/objects/signed-links/{alice_link['id']}/revoke").status_code == 401


# ===========================================================================
# #28 POST /api/objects/initiate
# ===========================================================================

def test_initiate_scope_and_session_owner(ctx):
    """A/C/E：返回 upload_id/chunk_size 并建会话；缺 scope 403 不建会话；无 token 401。"""
    c, _, depot, _ = ctx
    actor = _token(c, "alice", buckets=["demo"], prefixes={"demo": ["a/"]})
    r = c.post("/api/objects/initiate", params={"token": actor})
    assert r.status_code == 200
    data = r.json()["data"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", data["upload_id"])
    assert data["chunk_size"] == 8 * 1024 * 1024
    assert _session_path(data["upload_id"]).is_dir()

    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.post("/api/objects/initiate", params={"token": nobody}).status_code == 403
    assert c.post("/api/objects/initiate").status_code == 401
    assert c.post("/api/objects/initiate", params={"token": "bad"}).status_code == 401

    admin = c.post("/api/objects/initiate", params={"token": ADMIN}).json()["data"]["upload_id"]
    meta = json.loads((_session_path(admin) / ".session.json").read_text())
    assert meta["owner"] == "系统/工具"
    c.post("/api/objects/abort", params={"token": ADMIN}, data={"upload_id": admin})


# ===========================================================================
# #29 POST /api/objects/chunk
# ===========================================================================

def test_chunk_ownership_bounds_and_single_chunk_limit(ctx, monkeypatch):
    """A/C/E/D：会话归属校验、index/upload_id 边界、单分片上限 413。"""
    from artifactdepot import storage
    c, _, depot, _ = ctx
    _seed(c)
    alice = _token(c, "alice", buckets=["demo"], prefixes={"demo": ["a/"]})
    bob = _token(c, "bob", buckets=["demo"], prefixes={"demo": ["a/"]})
    uid = c.post("/api/objects/initiate", params={"token": alice}).json()["data"]["upload_id"]

    r = _chunk(c, alice, uid, 0, b"part0")
    assert r.status_code == 200 and r.json()["data"]["size"] == 5
    r = _chunk(c, alice, uid, 1, b"part1")
    assert r.status_code == 200

    # 他人（不同用户）不能往会话里写分片
    r = _chunk(c, bob, uid, 2, b"evil")
    assert r.status_code == 403 and "无权访问该分片会话" in _detail(r)
    assert not (depot / "demo" / "a" / "evil").exists()

    # index / upload_id 边界
    assert _chunk(c, alice, uid, -1, b"x").status_code == 400
    assert _chunk(c, alice, uid, 100001, b"x").status_code == 400
    assert _chunk(c, alice, "bad", 0, b"x").status_code == 400
    r = c.post("/api/objects/chunk", params={"token": alice},
               data={"index": 0}, files={"chunk": ("p", b"x")})
    assert r.status_code == 422
    assert _chunk(c, "bad", uid, 0, b"x").status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert _chunk(c, nobody, uid, 0, b"x").status_code == 403

    # 单分片上限（生产为 64MB；测试用 monkeypatch 缩小常量，避免写 64MB 数据）
    monkeypatch.setattr(storage, "MAX_CHUNK_BYTES", 1024)
    r = _chunk(c, alice, uid, 2, b"z" * 2048)
    assert r.status_code == 413 and "单个分片超过上限" in _detail(r)
    assert _chunk(c, alice, uid, 2, b"z" * 512).status_code == 200
    c.post("/api/objects/abort", params={"token": alice}, data={"upload_id": uid})


# ===========================================================================
# #30 POST /api/objects/complete
# ===========================================================================

def test_complete_normal_flow_disk_manifest_audit(ctx):
    """A/F：分片合并落盘、清单/审计正确、会话清理；缺分片 400 后仍可补齐重试。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    alice = _token(c, "alice", buckets=["demo"], prefixes={"demo": ["a/"]})
    uid = c.post("/api/objects/initiate", params={"token": alice}).json()["data"]["upload_id"]
    _chunk(c, alice, uid, 0, b"part0")
    _chunk(c, alice, uid, 1, b"part1")

    # 缺分片 → 400（会话保留、不落盘、不写审计）
    n = len(_audit(depot))
    r = _complete(c, alice, uid, "demo", "a/merged.txt", 3)
    assert r.status_code == 400 and "分片 2 缺失" in _detail(r)
    assert not (depot / "demo" / "a" / "merged.txt").exists()
    assert len(_audit(depot)) == n
    assert _session_path(uid).is_dir()

    r = _complete(c, alice, uid, "demo", "a/merged.txt", 2)
    assert r.status_code == 200
    payload = b"part0part1"
    assert r.json()["data"]["sha256"] == _sha(payload)
    assert (depot / "demo" / "a" / "merged.txt").read_bytes() == payload
    assert _manifest(depot, "demo")["a/merged.txt"]["sha256"] == _sha(payload)
    row = _audit(depot)[-1]
    assert row["action"] == "upload" and row["actor"] == "alice"
    assert row["key"] == "a/merged.txt" and row["size"] == len(payload)
    assert not _session_path(uid).is_dir(), "完成后应清理分片会话"
    # 再次 complete 同一会话 → 400
    assert _complete(c, alice, uid, "demo", "a/merged.txt", 2).status_code == 400


def test_complete_scope_ownership_and_boundaries(ctx):
    """C/D/E：他人 complete 403；越界 403 数据未动；total_chunks 越界 400；冲突 409。"""
    c, _, depot, _ = ctx
    _seed(c)
    alice = _token(c, "alice", buckets=["demo"], prefixes={"demo": ["a/"]})
    bob = _token(c, "bob", buckets=["demo"], prefixes={"demo": ["a/"]})
    uid = c.post("/api/objects/initiate", params={"token": alice}).json()["data"]["upload_id"]
    _chunk(c, alice, uid, 0, b"DATA")

    r = _complete(c, bob, uid, "demo", "a/x.txt", 1)
    assert r.status_code == 403 and "无权访问该分片会话" in _detail(r)
    r = _complete(c, alice, uid, "secret", "a/x.txt", 1)
    assert r.status_code == 403
    assert _session_path(uid).is_dir(), "越界被拒后会话应保留（可重试）"

    assert _complete(c, alice, uid, "demo", "a/x.txt", 0).status_code == 400
    assert _complete(c, alice, uid, "demo", "a/x.txt", 100001).status_code == 400
    assert c.post("/api/objects/complete", params={"token": alice},
                  data={"upload_id": uid}).status_code == 422

    # 目标已存在 + overwrite=false → 409，原文件与会话都不变
    r = _complete(c, alice, uid, "demo", "a/x.txt", 1, overwrite="false")
    assert r.status_code == 409
    assert (depot / "demo" / "a" / "x.txt").read_bytes() == b"AAA"
    assert _session_path(uid).is_dir()
    # overwrite=true → 成功替换
    r = _complete(c, alice, uid, "demo", "a/x.txt", 1, overwrite="true")
    assert r.status_code == 200
    assert (depot / "demo" / "a" / "x.txt").read_bytes() == b"DATA"

    # 共享 token 可以接管他人会话（R12）并写任意 bucket
    uid2 = c.post("/api/objects/initiate", params={"token": alice}).json()["data"]["upload_id"]
    _chunk(c, alice, uid2, 0, b"ADMIN")
    r = _complete(c, ADMIN, uid2, "secret", "admin.txt", 1)
    assert r.status_code == 200
    assert (depot / "secret" / "admin.txt").read_bytes() == b"ADMIN"


def test_complete_total_size_limit(ctx):
    """D：分片总大小超过 max_upload_mb → 413，不落盘、会话保留。"""
    c, cfg, depot, config = ctx
    _mk_bucket(c, "demo")
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    uid = c.post("/api/objects/initiate", params={"token": actor}).json()["data"]["upload_id"]
    # 先按不限大小收下分片，再收紧上限，触发 complete 的「总大小」校验（413）
    _chunk(c, actor, uid, 0, b"a" * 600_000)
    _chunk(c, actor, uid, 1, b"b" * 600_000)
    _set_config(cfg, config, max_upload_mb=1)
    n = len(_audit(depot))
    r = _complete(c, actor, uid, "demo", "a/big.txt", 2)
    assert r.status_code == 413 and "上限" in _detail(r)
    assert not (depot / "demo" / "a" / "big.txt").exists()
    assert len(_audit(depot)) == n
    assert _session_path(uid).is_dir()
    c.post("/api/objects/abort", params={"token": actor}, data={"upload_id": uid})


def test_complete_type_conflict_should_not_500(ctx):
    """B/D：complete 目标 key 已是目录 → 应 400/409，而不是 500。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    c.post("/api/objects/mkdir", params={"token": ADMIN}, json={"bucket": "demo", "key": "dir"})
    # 不加前缀限制，确保 403 不会先于存储层类型冲突发生（否则测不到 500）
    actor = _token(c, "actor", buckets=["demo"])
    uid = c.post("/api/objects/initiate", params={"token": actor}).json()["data"]["upload_id"]
    _chunk(c, actor, uid, 0, b"DATA")
    r = _complete(c, actor, uid, "demo", "dir", 1)
    assert r.status_code in (400, 409), f"complete 目标为目录时返回 {r.status_code}（未处理异常 → 500）"
    # 目录冲突属于不可恢复失败：本次会话应清理，避免 /tmp 残留
    assert not _session_path(uid).is_dir()


# ===========================================================================
# #31 POST /api/objects/abort
# ===========================================================================

def test_abort_scope_ownership_idempotent_and_after_abort_complete(ctx):
    """A/C/E：他人 abort 403；本人 abort 200（幂等）；abort 后 complete 400。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    alice = _token(c, "alice", buckets=["demo"], prefixes={"demo": ["a/"]})
    bob = _token(c, "bob", buckets=["demo"], prefixes={"demo": ["a/"]})
    uid = c.post("/api/objects/initiate", params={"token": alice}).json()["data"]["upload_id"]
    _chunk(c, alice, uid, 0, b"x")

    r = c.post("/api/objects/abort", params={"token": bob}, data={"upload_id": uid})
    assert r.status_code == 403 and "无权访问该分片会话" in _detail(r)
    assert _session_path(uid).is_dir()

    r = c.post("/api/objects/abort", params={"token": alice}, data={"upload_id": uid})
    assert r.status_code == 200
    assert r.json()["data"] == {"aborted": uid, "existed": True}
    assert not _session_path(uid).is_dir()

    r = c.post("/api/objects/abort", params={"token": alice}, data={"upload_id": uid})
    assert r.status_code == 200 and r.json()["data"]["existed"] is False
    r = _complete(c, alice, uid, "demo", "a/x.txt", 1)
    assert r.status_code == 400 and "无效或已失效的分片会话" in _detail(r)

    assert c.post("/api/objects/abort", params={"token": alice},
                  data={"upload_id": "bad"}).status_code == 400
    r = c.post("/api/objects/abort", params={"token": alice},
               data={"upload_id": "abcdefghijklmnop"})
    assert r.status_code == 200 and r.json()["data"]["existed"] is False
    assert c.post("/api/objects/abort", data={"upload_id": uid}).status_code == 401
    nobody = _token(c, "nobody", scopes=["object:list"])
    assert c.post("/api/objects/abort", params={"token": nobody},
                  data={"upload_id": uid}).status_code == 403

    # R12：共享 token 可以取消他人会话
    uid_admin = c.post("/api/objects/initiate", params={"token": alice}).json()["data"]["upload_id"]
    r = c.post("/api/objects/abort", params={"token": ADMIN}, data={"upload_id": uid_admin})
    assert r.status_code == 200 and r.json()["data"]["existed"] is True
    assert not _session_path(uid_admin).is_dir()


# ===========================================================================
# B 参数异常矩阵：非法/超长 bucket、非法/空 key 在各接口的表现（共享 token 视角）
# ===========================================================================

def test_invalid_bucket_and_key_matrix_across_endpoints(ctx):
    """B：head/download/delete/rename/presign 对非法 bucket（含 >63）与非法 key 返回 400。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _up(c, "demo", "x.txt", b"X")
    long_bucket = "a" * 64
    urls = {
        "head_bucket": lambda: c.get("/api/objects/head", params={
            "bucket": long_bucket, "key": "x.txt", "token": ADMIN}),
        "head_key": lambda: c.get("/api/objects/head", params={
            "bucket": "demo", "key": "a/../x", "token": ADMIN}),
        "download_bucket": lambda: c.get("/api/objects/download", params={
            "bucket": long_bucket, "key": "x.txt", "token": ADMIN}),
        "download_key": lambda: c.get("/api/objects/download", params={
            "bucket": "demo", "key": "a/../x", "token": ADMIN}),
        "download_empty_key": lambda: c.get("/api/objects/download", params={
            "bucket": "demo", "key": "", "token": ADMIN}),
        "delete_bucket": lambda: c.delete("/api/objects", params={
            "bucket": long_bucket, "key": "x.txt", "token": ADMIN}),
        "delete_key": lambda: c.delete("/api/objects", params={
            "bucket": "demo", "key": "a/../x", "token": ADMIN}),
        "mkdir_bucket": lambda: c.post("/api/objects/mkdir", params={"token": ADMIN},
                                       json={"bucket": long_bucket, "key": "d"}),
        "rename_bucket": lambda: c.post("/api/objects/rename", params={"token": ADMIN},
                                        json={"bucket": long_bucket, "key": "x.txt",
                                              "new_key": "y.txt"}),
        "rename_invalid_key": lambda: c.post("/api/objects/rename", params={"token": ADMIN},
                                             json={"bucket": "demo", "key": "../x",
                                                   "new_key": "y.txt"}),
        "rename_invalid_new_key": lambda: c.post("/api/objects/rename", params={"token": ADMIN},
                                                 json={"bucket": "demo", "key": "x.txt",
                                                       "new_key": "../y"}),
        "presign_bucket": lambda: c.post("/api/objects/presign", params={"token": ADMIN},
                                         json={"bucket": long_bucket, "key": "x.txt"}),
        "presign_invalid_key": lambda: c.post("/api/objects/presign", params={"token": ADMIN},
                                              json={"bucket": "demo", "key": "a/../x"}),
        "presign_empty_key": lambda: c.post("/api/objects/presign", params={"token": ADMIN},
                                            json={"bucket": "demo", "key": ""}),
    }
    snap = _state(depot)
    for name, call in urls.items():
        r = call()
        assert r.status_code == 400, (name, r.status_code, r.text[:120])
    assert _state(depot) == snap, "非法参数不应产生任何数据变更"


# ===========================================================================
# R12：共享 access_token 完全不受资源范围限制
# ===========================================================================

def test_shared_token_unrestricted_across_all_object_apis(ctx):
    """R12：共享 token 可跨 bucket 上传/探测/下载/删除/建目录/改名/签名/作废/分片。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")

    # upload / head / download / list 任意 bucket
    assert _up(c, "secret", "deep/a.txt", b"S1").status_code == 200
    assert _up(c, "brandnew", "x.txt", b"NEWBUCKET").status_code == 200
    assert (depot / "brandnew" / "x.txt").read_bytes() == b"NEWBUCKET"
    head = c.get("/api/objects/head", params={"bucket": "secret", "key": "deep/a.txt",
                                              "token": ADMIN})
    assert head.status_code == 200 and head.json()["data"]["sha256"] == _sha(b"S1")
    r = c.get("/api/objects/download", params={"bucket": "secret", "key": "deep/a.txt",
                                               "token": ADMIN})
    assert r.status_code == 200 and r.content == b"S1"
    assert {b["key"] for b in c.get("/api/objects/list",
                                    params={"bucket": "secret", "token": ADMIN}).json()["data"]["items"]} >= {"deep/"}

    # mkdir / rename / delete 任意 bucket
    assert c.post("/api/objects/mkdir", params={"token": ADMIN},
                  json={"bucket": "secret", "key": "d"}).status_code == 200
    assert c.post("/api/objects/rename", params={"token": ADMIN},
                  json={"bucket": "secret", "key": "d", "new_key": "d2"}).status_code == 200
    assert c.delete("/api/objects", params={"token": ADMIN,
                                            "bucket": "secret", "key": "deep/a.txt"}).status_code == 200

    # presign / 作废他人链接 / 列出全部链接
    link = _link(c, ADMIN, "secret", "s.txt")
    rows = c.get("/api/objects/signed-links", params={"token": ADMIN}).json()["data"]
    assert link["id"] in {x["id"] for x in rows}
    assert c.post(f"/api/objects/signed-links/{link['id']}/revoke",
                  params={"token": ADMIN}).status_code == 200

    # 分片：任意 bucket / 接管他人会话
    uid = c.post("/api/objects/initiate", params={"token": ADMIN}).json()["data"]["upload_id"]
    assert _chunk(c, ADMIN, uid, 0, b"AA").status_code == 200
    r = _complete(c, ADMIN, uid, "other-bucket", "k.bin", 1)
    assert r.status_code == 200
    assert (depot / "other-bucket" / "k.bin").read_bytes() == b"AA"

    uid2 = c.post("/api/objects/initiate", params={"token": ADMIN}).json()["data"]["upload_id"]
    assert c.post("/api/objects/abort", params={"token": ADMIN},
                  data={"upload_id": uid2}).status_code == 200


def test_restricted_token_still_limited_while_shared_token_sees_all(ctx):
    """R12 对照：同一数据上受限 token 403、共享 token 200（证明 403 来自范围而非数据不存在）。"""
    c, _, depot, _ = ctx
    _seed(c)
    actor = _token(c, "actor", buckets=["demo"], prefixes={"demo": ["a/"]})
    assert c.get("/api/objects/head", params={"bucket": "secret", "key": "s.txt",
                                              "token": actor}).status_code == 403
    assert c.get("/api/objects/head", params={"bucket": "secret", "key": "s.txt",
                                              "token": ADMIN}).status_code == 200
    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "b/y.txt",
                                                  "token": actor}).status_code == 403
    assert c.get("/api/objects/download", params={"bucket": "demo", "key": "b/y.txt",
                                                  "token": ADMIN}).content == b"BBB"
