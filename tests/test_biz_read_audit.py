# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""业务验收：读接口 fail-closed / 审计范围过滤 / 公开接口（task-9）

被测接口（编号取自 reports/api_inventory.md，唯一基准）：
- #9  GET  /api/buckets
- #17 GET  /api/objects/list
- #16 GET  /api/audit
- #8  POST /api/config/reload
- 公开接口 #1 GET /、#2 GET /api/auth/permissions、#4 #5 #6 /health*、
  #7 GET /metrics、#26 GET /api/objects/signed-links/config、
  #32 GET /api/docs、#33 GET /api/docs/{name}

判定口径（不看状态码，只看业务结果）：
- R1 读三态：匿名按 require_read_token；无效/过期/禁用 token → 401；
  有效缺 scope → 403；有效越界 → 403；范围内 → 200 且内容正确；
- R2 范围过滤：越界 bucket 的 items / bucket 名绝不出现；前缀受限时根目录 403；
- R3 审计：受限 token 只见范围内记录（bucket/key 双重过滤），bucket="" 全局事件隐藏；
  未配置范围 token 与共享 token 可见全量；limit/since/bucket/key/actor 过滤语义正确；
- #8 重载后立即生效（require_read_token / ui_enabled / metrics_enabled / signed_links），
  语法错误 → 400 + detail 且旧配置保留；
- 公开接口：响应结构与配置开关语义（metrics_enabled=false→404、ui_enabled=false→403、
  docs 白名单与 ?raw=1、未知 name→404）。

夹具写法与 tests/test_scope_enforcement.py 一致：每个用例独立临时 depot + 配置文件；
ARTIFACT_DEPOT_ENV 指向不存在的文件，避免读到仓库里真实的 config/.env。

运行：
    cd ArtifactDepot/ArtifactDepot
    PYTHONPATH=/home/apyyc/work/ArtifactDepot/.audit-deps/libs:src python3 -m pytest tests/test_biz_read_audit.py -q
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from artifactdepot.api.docs import CATALOG as DOC_CATALOG  # noqa: E402
from artifactdepot.permissions import (  # noqa: E402
    PERMISSION_CATALOG, PUBLIC_CATALOG, ROLE_PRESETS,
)

ADMIN = "test-biz-read-audit-admin-0123456789"
# 出现在「范围外」数据里的标记：任何 401/403/越界过滤结果中都不得出现
SECRET_MARKERS = ("secret", "s.txt", "SECRET", "topsecret")


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

def _base_cfg(depot: Path) -> dict:
    return {
        "depot_dir": str(depot),
        "meta_dir": "",
        "access_token": ADMIN,
        "ui_enabled": True,
        "metrics_enabled": True,
        "max_upload_mb": 0,
        "require_read_token": False,
        "signed_links": {
            "count_min": 1,
            "count_max": 10,
            "expire_min_seconds": 60,
            "expire_max_seconds": 604800,
        },
    }


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """独立 depot + 独立 config.json；隔离真实环境变量与真实 config/.env。"""
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    base = _base_cfg(depot)
    cfg.write_text(json.dumps(base), encoding="utf-8")

    # 真实环境变量优先级最高，会覆盖配置文件；逐个清掉保证用例可重复
    for name in (
        "ARTIFACT_DEPOT_ACCESS_TOKEN", "ARTIFACT_DEPOT_DIR", "ARTIFACT_DEPOT_META_DIR",
        "ARTIFACT_DEPOT_MAX_UPLOAD_MB", "ARTIFACT_DEPOT_PORT",
        "WAREHOUSE_DIR", "WAREHOUSE_META_DIR",
        "WAREHOUSE_ACCESS_TOKEN", "WAREHOUSE_PORT", "WAREHOUSE_MAX_UPLOAD_MB",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))

    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield {"client": c, "cfg": cfg, "config": config, "depot": depot,
               "base": base, "tmp": tmp_path}
    config._CONFIG = None


def _detail(resp) -> str:
    try:
        return resp.json().get("detail", "")
    except Exception:
        return resp.text


def _norm_path(path: str) -> str:
    """把 path 模板参数名归一化（目录里写 {id}，OpenAPI 里写 {link_id}）。"""
    return re.sub(r"\{[^}]+\}", "{}", path)


def _write_cfg(ctx, **overrides) -> dict:
    data = dict(ctx["base"])
    data.update(overrides)
    ctx["cfg"].write_text(json.dumps(data), encoding="utf-8")
    return data


def _apply_cfg(ctx, **overrides):
    """外部编辑配置文件后让进程重新读（不经过 reload 接口，保证接口用例互相独立）。"""
    data = _write_cfg(ctx, **overrides)
    ctx["config"]._CONFIG = None
    return data


def _reload(ctx, **overrides):
    """外部编辑配置文件后调用被测的 POST /api/config/reload。"""
    _write_cfg(ctx, **overrides)
    return ctx["client"].post("/api/config/reload", params={"token": ADMIN})


def _new_token(c, **body) -> str:
    body.setdefault("user", "tester")
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _mk_bucket(c, name: str):
    r = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": name})
    assert r.status_code == 200, r.text
    return name


def _upload(c, bucket: str, key: str, data: bytes = b"x", token: str = ADMIN):
    r = c.post("/api/objects", params={"token": token},
               data={"bucket": bucket, "key": key},
               files={"file": (Path(key).name, data)})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _disable_token(c, token: str):
    r = c.put(f"/api/tokens/{token}", params={"token": ADMIN}, json={"enabled": False})
    assert r.status_code == 200, r.text


def _seed_demo(c):
    """共享前置状态：bucket demo（f.txt / dir/g.txt / dir/sub/h.txt / deep/a/b.txt）+ secret/s.txt。"""
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    _upload(c, "demo", "f.txt", b"demo-file")
    _upload(c, "demo", "dir/g.txt", b"g")
    _upload(c, "demo", "dir/sub/h.txt", b"h")
    _upload(c, "demo", "deep/a/b.txt", b"b")
    _upload(c, "secret", "s.txt", b"SECRET")


def _read(c, endpoint: str, token=None, **params):
    """读接口统一调用：endpoint=list → /api/objects/list；endpoint=buckets → /api/buckets。"""
    p = {k: v for k, v in params.items() if v is not None}
    if token is not None:
        p["token"] = token
    if endpoint == "list":
        p.setdefault("bucket", "demo")
        return c.get("/api/objects/list", params=p)
    return c.get("/api/buckets", params=p)


def _audit(c, token=ADMIN, **params):
    params.setdefault("limit", 1000)
    r = c.get("/api/audit", params={"token": token, **params})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _row_key(row: dict):
    return (row.get("time"), row.get("action"), row.get("bucket"),
            row.get("key"), row.get("actor"))


def _demo_viewer(c, **extra):
    body = dict(role="custom", scopes=["object:list", "bucket:list"],
                allow_buckets=["demo"])
    body.update(extra)
    return _new_token(c, **body)


# ---------------------------------------------------------------------------
# #17 GET /api/objects/list
# ---------------------------------------------------------------------------

def test_list_anonymous_content_is_correct(ctx):
    """A 正常：匿名（require_read_token=false）可读，返回内容与磁盘一致。"""
    c = ctx["client"]
    _seed_demo(c)

    r = c.get("/api/objects/list", params={"bucket": "demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0 and body["message"] == "success"
    data = body["data"]
    assert set(data) == {"bucket", "prefix", "items", "total", "offset", "limit", "truncated"}
    assert data["bucket"] == "demo" and data["prefix"] == ""
    by_name = {i["name"]: i for i in data["items"]}
    assert set(by_name) == {"deep", "dir", "f.txt"}          # 目录在前、名称排序
    assert by_name["dir"]["is_dir"] is True and by_name["dir"]["key"] == "dir/"
    assert by_name["deep"]["is_dir"] is True
    assert by_name["f.txt"]["is_dir"] is False and by_name["f.txt"]["key"] == "f.txt"
    assert by_name["f.txt"]["size"] == len(b"demo-file")
    assert by_name["f.txt"]["sha256"]                       # 上传清单里有 sha256
    assert data["total"] == 3 and data["offset"] == 0
    assert data["limit"] == 0 and data["truncated"] is False
    assert "s.txt" not in r.text

    # 公开模式下匿名同样能浏览 secret（这是 require_read_token=false 的既有语义）
    s = c.get("/api/objects/list", params={"bucket": "secret"})
    assert s.status_code == 200
    assert [i["name"] for i in s.json()["data"]["items"]] == ["s.txt"]


def test_list_fail_closed_for_invalid_expired_disabled_tokens(ctx):
    """R1：带无效 / 过期 / 禁用 token 一律 401，且响应不得泄露任何对象名。"""
    c = ctx["client"]
    _seed_demo(c)
    expired = _new_token(c, role="viewer", expires_at="2000-01-01")
    disabled = _new_token(c, role="viewer")
    _disable_token(c, disabled)

    for bad in ("definitely-not-a-token", expired, disabled):
        r = c.get("/api/objects/list", params={"bucket": "demo", "token": bad})
        assert r.status_code == 401, r.text
        assert "无效的访问令牌" in _detail(r)
        assert "f.txt" not in r.text and "dir" not in r.text

        b = c.get("/api/buckets", params={"token": bad})
        assert b.status_code == 401, b.text
        assert "无效的访问令牌" in _detail(b)
        assert "demo" not in b.text and "secret" not in b.text


def test_list_missing_scope_is_403_without_content(ctx):
    """R1：有效但缺 scope → 403（不是匿名放行，也不能带出 items）。"""
    c = ctx["client"]
    _seed_demo(c)
    only_download = _new_token(c, role="custom", scopes=["object:download"],
                               allow_buckets=["demo"])
    r = c.get("/api/objects/list", params={"bucket": "demo", "token": only_download})
    assert r.status_code == 403, r.text
    assert "需要 object:list" in _detail(r)
    assert "f.txt" not in r.text

    only_list = _new_token(c, role="custom", scopes=["object:list"],
                           allow_buckets=["demo"])
    b = c.get("/api/buckets", params={"token": only_list})
    assert b.status_code == 403, b.text
    assert "需要 bucket:list" in _detail(b)
    assert "demo" not in b.text and "secret" not in b.text

    # 无范围的 token 缺 scope 同样是 403，不能因为「不限范围」而全量返回
    no_scope = _new_token(c, role="custom", scopes=["object:download"])
    assert c.get("/api/buckets", params={"token": no_scope}).status_code == 403


def test_list_out_of_range_bucket_is_403_without_leak(ctx):
    """R2：越界 bucket（含大小写/空白/拼接等别名）一律 403，绝不出现 secret 内容。"""
    c = ctx["client"]
    _seed_demo(c)
    tok = _demo_viewer(c)

    for bad in ("secret", "DEMO", "demo ", " demo", "demo/../secret",
                "demo,secret", ".demo", "demo."):
        r = c.get("/api/objects/list", params={"bucket": bad, "token": tok})
        assert r.status_code == 403, (bad, r.status_code, r.text)
        assert "资源范围不允许" in _detail(r), (bad, r.text)
        for marker in SECRET_MARKERS:
            assert marker not in r.text, (bad, marker, r.text)

    # 正向对照：范围内同一 token 仍然可读（证明 403 来自范围而非 token 失效）
    ok = c.get("/api/objects/list", params={"bucket": "demo", "token": tok})
    assert ok.status_code == 200
    assert {i["name"] for i in ok.json()["data"]["items"]} == {"deep", "dir", "f.txt"}


def test_list_in_range_content_and_pagination(ctx):
    """A/D：范围内返回内容精确；limit/offset/truncated 语义正确（limit=0 为不限）。"""
    c = ctx["client"]
    _seed_demo(c)
    tok = _demo_viewer(c)

    r = c.get("/api/objects/list", params={"bucket": "demo", "token": tok})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [i["name"] for i in data["items"]] == ["deep", "dir", "f.txt"]
    assert all(i["key"] for i in data["items"])
    assert "s.txt" not in r.text

    p = c.get("/api/objects/list", params={"bucket": "demo", "token": tok, "limit": 2})
    assert p.status_code == 200
    d = p.json()["data"]
    assert [i["name"] for i in d["items"]] == ["deep", "dir"]
    assert d["total"] == 3 and d["limit"] == 2 and d["truncated"] is True

    p2 = c.get("/api/objects/list",
               params={"bucket": "demo", "token": tok, "offset": 2, "limit": 2})
    d2 = p2.json()["data"]
    assert [i["name"] for i in d2["items"]] == ["f.txt"]
    assert d2["offset"] == 2 and d2["total"] == 3 and d2["truncated"] is False

    d0 = c.get("/api/objects/list",
               params={"bucket": "demo", "token": tok, "limit": 0}).json()["data"]
    assert len(d0["items"]) == 3 and d0["truncated"] is False   # list 的 limit=0 = 不限


def test_list_prefix_scope_root_403_and_no_sibling_leak(ctx):
    """R2：前缀受限 token 列根目录必须 403（不能泄露同级条目），前缀内只返回该目录。"""
    c = ctx["client"]
    _seed_demo(c)
    tok = _new_token(c, role="custom", scopes=["object:list", "bucket:list"],
                     allow_buckets=["demo"], allow_prefixes={"demo": ["dir/"]})

    root = c.get("/api/objects/list", params={"bucket": "demo", "token": tok})
    assert root.status_code == 403, root.text
    assert "资源范围不允许" in _detail(root)
    for marker in ("f.txt", "deep", "g.txt"):
        assert marker not in root.text, (marker, root.text)

    # 显式 prefix="" 的根目录请求同样 403
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "prefix": "", "token": tok}).status_code == 403

    ok = c.get("/api/objects/list", params={"bucket": "demo", "prefix": "dir", "token": tok})
    assert ok.status_code == 200, ok.text
    items = ok.json()["data"]["items"]
    assert [i["name"] for i in items] == ["sub", "g.txt"]
    assert all(i["key"].startswith("dir/") for i in items)
    assert "f.txt" not in ok.text and "deep" not in ok.text

    # 同 bucket 的兄弟目录 → 403（不是 404/200，不能借此枚举同级）
    for outside in ("deep", "dir2"):
        r = c.get("/api/objects/list",
                  params={"bucket": "demo", "prefix": outside, "token": tok})
        assert r.status_code == 403, (outside, r.status_code, r.text)
        assert "f.txt" not in r.text and "g.txt" not in r.text
    # 前缀内写穿越（授权按字面以 dir/ 开头放行）→ 存储层拒绝 400，不泄露任何内容
    trav = c.get("/api/objects/list",
                 params={"bucket": "demo", "prefix": "dir/sub/../..", "token": tok})
    assert trav.status_code == 400, trav.text
    assert "f.txt" not in trav.text and "g.txt" not in trav.text


def test_list_parameter_validation(ctx):
    """B：bucket 缺失 422、bucket 非法 400、prefix 穿越 400、limit/offset 越界 422。"""
    c = ctx["client"]
    _seed_demo(c)

    assert c.get("/api/objects/list").status_code == 422            # 缺 bucket
    for bucket in ("", "a/b", "demo ", " demo", ".hidden", "x" * 64, "demo\\x"):
        r = c.get("/api/objects/list", params={"bucket": bucket})
        assert r.status_code == 400, (bucket, r.status_code, r.text)
    r = c.get("/api/objects/list", params={"bucket": "a/b"})
    assert "非法 bucket" in _detail(r)
    r = c.get("/api/objects/list", params={"bucket": "x" * 64})
    assert "过长" in _detail(r)

    for prefix in ("..", "a/../b", "a\\b", ".hidden"):
        r = c.get("/api/objects/list", params={"bucket": "demo", "prefix": prefix})
        assert r.status_code == 400, (prefix, r.status_code, r.text)

    for bad_limit in (-1, 10001):
        assert c.get("/api/objects/list",
                     params={"bucket": "demo", "limit": bad_limit}).status_code == 422
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "limit": 10000}).status_code == 200
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "offset": -1}).status_code == 422


# ---------------------------------------------------------------------------
# #9 GET /api/buckets
# ---------------------------------------------------------------------------

def test_buckets_anonymous_and_scope_filtering(ctx):
    """A/R2：匿名列全部；受限 token 只列 allow_buckets 内，越界 bucket 名不出现。"""
    c = ctx["client"]
    _seed_demo(c)

    r = c.get("/api/buckets")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0 and body["message"] == "success"
    assert body["data"] == ["demo", "secret"]                  # 排序稳定

    demo_tok = _new_token(c, role="custom", scopes=["bucket:list"],
                          allow_buckets=["demo"])
    r = c.get("/api/buckets", params={"token": demo_tok})
    assert r.status_code == 200 and r.json()["data"] == ["demo"]
    assert "secret" not in r.text

    secret_tok = _new_token(c, role="custom", scopes=["bucket:list"],
                            allow_buckets=["secret"])
    r = c.get("/api/buckets", params={"token": secret_tok})
    assert r.json()["data"] == ["secret"]
    assert "demo" not in r.text

    # 前缀受限 token 的 bucket 级白名单仍是 demo（前缀不放大也不隐藏 bucket 本身）
    prefix_tok = _new_token(c, role="custom", scopes=["bucket:list"],
                            allow_buckets=["demo"],
                            allow_prefixes={"demo": ["dir/"]})
    assert c.get("/api/buckets", params={"token": prefix_tok}).json()["data"] == ["demo"]

    # 未配置范围的普通 token / 共享 token 可见全量
    full_tok = _new_token(c, role="custom", scopes=["bucket:list"])
    assert c.get("/api/buckets", params={"token": full_tok}).json()["data"] == ["demo", "secret"]
    assert c.get("/api/buckets", params={"token": ADMIN}).json()["data"] == ["demo", "secret"]


def test_buckets_fail_closed_states(ctx):
    """R1：无效/过期/禁用 → 401；有效缺 bucket:list → 403；均不得带出 bucket 名。"""
    c = ctx["client"]
    _seed_demo(c)
    expired = _new_token(c, role="viewer", expires_at="2000-01-01")
    disabled = _new_token(c, role="viewer")
    _disable_token(c, disabled)

    for bad in ("bad-token", expired, disabled):
        r = c.get("/api/buckets", params={"token": bad})
        assert r.status_code == 401, r.text
        assert "demo" not in r.text and "secret" not in r.text

    no_scope = _new_token(c, role="custom", scopes=["object:list"],
                          allow_buckets=["demo"])
    r = c.get("/api/buckets", params={"token": no_scope})
    assert r.status_code == 403 and "需要 bucket:list" in _detail(r)
    assert "demo" not in r.text and "secret" not in r.text


# ---------------------------------------------------------------------------
# R1 三态矩阵（list + buckets；require_read_token true/false）
# ---------------------------------------------------------------------------

def test_r1_read_fail_closed_matrix(ctx):
    """R1 矩阵（require_read_token=false）：匿名 200 / 无效或过期或禁用 401 /
    缺 scope 403 / 越界 403 / 范围内 200 且内容正确。"""
    c = ctx["client"]
    _seed_demo(c)
    expired = _new_token(c, role="viewer", expires_at="2000-01-01")
    disabled = _new_token(c, role="viewer")
    _disable_token(c, disabled)
    in_range = _demo_viewer(c)
    out_range = _new_token(c, role="custom", scopes=["object:list", "bucket:list"],
                           allow_buckets=["secret"])
    missing_obj = _new_token(c, role="custom", scopes=["bucket:list"],
                             allow_buckets=["demo"])
    missing_bucket = _new_token(c, role="custom", scopes=["object:list"],
                                allow_buckets=["demo"])

    # --- /api/objects/list ---
    assert _read(c, "list").status_code == 200
    for bad in ("bad-token", expired, disabled):
        r = _read(c, "list", token=bad)
        assert r.status_code == 401
        assert "f.txt" not in r.text
    r = _read(c, "list", token=missing_obj)
    assert r.status_code == 403 and "需要 object:list" in _detail(r)
    r = _read(c, "list", token=out_range, bucket="demo")
    assert r.status_code == 403 and "资源范围不允许" in _detail(r)
    r = _read(c, "list", token=in_range)
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["data"]["items"]] == ["deep", "dir", "f.txt"]

    # --- /api/buckets ---
    assert _read(c, "buckets").json()["data"] == ["demo", "secret"]
    for bad in ("bad-token", expired, disabled):
        assert _read(c, "buckets", token=bad).status_code == 401
    r = _read(c, "buckets", token=missing_bucket)
    assert r.status_code == 403 and "需要 bucket:list" in _detail(r)
    r = _read(c, "buckets", token=out_range)
    assert r.status_code == 200 and r.json()["data"] == ["secret"]
    r = _read(c, "buckets", token=in_range)
    assert r.status_code == 200 and r.json()["data"] == ["demo"]


def test_require_read_token_true_three_states(ctx):
    """R1：require_read_token=true 后立即要求 token；范围语义保持不变。"""
    c = ctx["client"]
    _seed_demo(c)
    in_range = _demo_viewer(c)
    out_range = _new_token(c, role="custom", scopes=["object:list", "bucket:list"],
                           allow_buckets=["secret"])
    expired = _new_token(c, role="viewer", expires_at="2000-01-01")
    _apply_cfg(ctx, require_read_token=True)

    assert ctx["config"].get_config()["require_read_token"] is True
    assert c.get("/api/auth/permissions").json()["data"]["require_read_token"] is True

    for endpoint in ("list", "buckets"):
        r = _read(c, endpoint)
        assert r.status_code == 401 and "缺少访问令牌" in _detail(r)
        r = _read(c, endpoint, token="bad-token")
        assert r.status_code == 401 and "无效的访问令牌" in _detail(r)
        assert _read(c, endpoint, token=expired).status_code == 401
        b = _read(c, "buckets", token=in_range)
        assert b.status_code == 200 and b.json()["data"] == ["demo"]

    r = _read(c, "list", token=in_range)
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["data"]["items"]] == ["deep", "dir", "f.txt"]
    r = _read(c, "list", token=out_range)
    assert r.status_code == 403 and "资源范围不允许" in _detail(r)


# ---------------------------------------------------------------------------
# #16 GET /api/audit
# ---------------------------------------------------------------------------

def test_audit_restricted_bucket_scope_rows(ctx):
    """R3：demo 范围 token 的实际行集合只含 demo 记录；secret 与 bucket="" 全局事件不可见。"""
    c = ctx["client"]
    _seed_demo(c)
    tok = _new_token(c, role="custom", scopes=["audit:read"], allow_buckets=["demo"])

    raw = c.get("/api/audit", params={"token": tok, "limit": 1000})
    assert raw.status_code == 200, raw.text
    rows = raw.json()["data"]
    assert rows
    assert all(r["bucket"] == "demo" for r in rows), rows
    assert not any(r["bucket"] == "" for r in rows)
    assert any(r["action"] == "upload" and r["key"] == "f.txt" for r in rows)
    assert not any("s.txt" in (r.get("key") or "") for r in rows)
    for marker in SECRET_MARKERS:
        assert marker not in raw.text, (marker, raw.text[:400])
    assert not any(r["action"] in ("token_create", "token_update", "config_reload")
                   for r in rows)

    # 共享 token 能看到 secret 与全局事件 → 证明记录存在、只是被范围过滤
    admin_rows = _audit(c, ADMIN)
    assert any(r["bucket"] == "secret" for r in admin_rows)
    assert any(r["bucket"] == "" and r["action"] in ("token_create", "create_bucket")
               for r in admin_rows)
    assert {_row_key(r) for r in rows} <= {_row_key(r) for r in admin_rows}

    # 显式查询范围外 bucket / key：过滤为空（不是 403、不泄露）
    assert _audit(c, tok, bucket="secret") == []
    assert _audit(c, tok, key="s.txt") == []


def test_audit_prefix_scope_rows_and_global_events_hidden(ctx):
    """R3：前缀受限 token 只能看到前缀内记录；bucket 内根级事件（key=""）同样不可见。"""
    c = ctx["client"]
    _seed_demo(c)
    tok = _new_token(c, role="custom", scopes=["audit:read"], allow_buckets=["demo"],
                     allow_prefixes={"demo": ["dir/"]})

    rows = _audit(c, tok)
    assert rows
    assert all(r["bucket"] == "demo" for r in rows)
    assert all((r.get("key") or "").startswith("dir/") for r in rows), rows
    assert {"dir/g.txt", "dir/sub/h.txt"} <= {r["key"] for r in rows}
    # create_bucket demo（key=""）落在前缀之外 → 不应出现
    assert not any((r.get("key") or "") == "" for r in rows)

    assert _audit(c, tok, bucket="secret") == []
    assert _audit(c, tok, key="f.txt") == []
    assert [r["key"] for r in _audit(c, tok, key="dir/g.txt")] == ["dir/g.txt"]


def test_audit_unrestricted_and_shared_see_full_rows(ctx):
    """R3：未配置范围的普通 token 与共享 token 看到完全相同的全量行集合。"""
    c = ctx["client"]
    _seed_demo(c)
    full = _new_token(c, role="custom", scopes=["audit:read"])
    # 产生一条全局事件（bucket=""），供全量对照
    assert c.post("/api/config/reload", params={"token": ADMIN}).status_code == 200

    admin_rows = _audit(c, ADMIN, limit=5000)
    full_rows = _audit(c, full, limit=5000)
    assert [_row_key(r) for r in admin_rows] == [_row_key(r) for r in full_rows]
    assert any(r["bucket"] == "secret" for r in full_rows)
    assert any(r["bucket"] == "" and r["action"] == "token_create" for r in full_rows)
    assert any(r["bucket"] == "" and r["action"] == "config_reload" for r in full_rows)

    # 共享 token 同样能看到全部 bucket
    assert {r["bucket"] for r in admin_rows} >= {"", "demo", "secret"}


def test_audit_filter_semantics(ctx):
    """R3：bucket / key / actor / since 组合过滤语义正确，limit 取最近 N 条命中。"""
    c = ctx["client"]
    _seed_demo(c)
    alice = _new_token(c, user="alice", role="custom", scopes=["object:upload"],
                       allow_buckets=["demo"])
    bob = _new_token(c, user="bob", role="custom", scopes=["object:upload"],
                     allow_buckets=["demo"])
    _upload(c, "demo", "alice.txt", token=alice)
    _upload(c, "demo", "bob.txt", token=bob)
    _upload(c, "demo", "admin2.txt")
    _upload(c, "secret", "s2.txt")

    all_rows = _audit(c, ADMIN, limit=5000)

    demo_rows = _audit(c, ADMIN, bucket="demo", limit=5000)
    assert demo_rows and all(r["bucket"] == "demo" for r in demo_rows)

    key_rows = _audit(c, ADMIN, key="alice.txt")
    assert len(key_rows) == 1
    assert key_rows[0]["key"] == "alice.txt" and key_rows[0]["actor"] == "alice"

    actor_rows = _audit(c, ADMIN, actor="alice")
    assert actor_rows and all(r["actor"] == "alice" for r in actor_rows)
    assert {r["key"] for r in actor_rows} == {"alice.txt"}

    alice_time = key_rows[0]["time"]
    since_rows = _audit(c, ADMIN, since=alice_time, limit=5000)
    assert since_rows and all(r["time"] >= alice_time for r in since_rows)
    assert any(r["key"] == "alice.txt" for r in since_rows)
    assert _audit(c, ADMIN, since="2999-01-01") == []

    combined = _audit(c, ADMIN, bucket="demo", key="alice.txt",
                      actor="alice", since=alice_time)
    assert len(combined) == 1
    assert combined[0]["key"] == "alice.txt" and combined[0]["actor"] == "alice"

    limited = _audit(c, ADMIN, limit=2)
    assert len(limited) == 2
    assert [_row_key(r) for r in limited] == [_row_key(r) for r in all_rows[-2:]]


def test_audit_limit_zero_negative_and_restricted_limit_counts_hits(ctx):
    """R3：limit=0→[]；limit=-1→422；受限 token 的 predicate 在 limit 之前生效。"""
    c = ctx["client"]
    _seed_demo(c)
    tok = _new_token(c, role="custom", scopes=["audit:read"], allow_buckets=["demo"])
    # 制造比 demo 记录更新的范围外噪声：若 predicate 在 limit 之后，结果会不足 2 条
    for i in range(4):
        _upload(c, "secret", f"noise{i}.txt")

    assert _audit(c, ADMIN, limit=0) == []
    assert _audit(c, tok, limit=0) == []

    r = c.get("/api/audit", params={"token": tok, "limit": 2})
    assert r.status_code == 200, r.text
    limited = r.json()["data"]
    assert len(limited) == 2, limited
    assert all(row["bucket"] == "demo" for row in limited)

    in_scope_all = _audit(c, tok, limit=5000)
    assert [_row_key(r) for r in limited] == [_row_key(r) for r in in_scope_all[-2:]]

    for token in (ADMIN, tok):
        assert c.get("/api/audit", params={"token": token, "limit": -1}).status_code == 422
    assert c.get("/api/audit",
                 params={"token": ADMIN, "limit": "abc"}).status_code == 422


def test_audit_prefix_scope_rename_to_key_known_leak(ctx):
    """已登记缺陷（P3）的可复现用例：前缀范围外的 to_key 不应出现在审计响应中。

    rename 审计行归属 `key=from_key`（在范围内），但 extra 的 `to_key` 未按范围裁剪，
    例如前缀 `a/` 的 token 能看到 `a/x.txt -> b/moved.txt` 的目标 key。
    """
    c = ctx["client"]
    _seed_demo(c)
    assert c.post("/api/objects/mkdir", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "a"}).status_code == 200
    assert c.post("/api/objects/mkdir", params={"token": ADMIN},
                  json={"bucket": "demo", "key": "b"}).status_code == 200
    _upload(c, "demo", "a/x.txt")
    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "demo", "key": "a/x.txt", "new_key": "b/moved.txt"})
    assert r.status_code == 200, r.text

    tok = _new_token(c, role="custom", scopes=["audit:read"],
                     allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]})
    rows = _audit(c, tok)
    # 安全属性（不绑实现）：禁止把范围外目标路径带回——隐藏整行或隐藏 to_key 都可接受
    assert "b/moved.txt" not in json.dumps(rows, ensure_ascii=False), rows
    for r0 in rows:
        if r0.get("action") == "rename":
            tk = r0.get("to_key")
            assert not tk or not str(tk).startswith("b/"), f"rename.to_key 泄露范围外路径：{r0}"
    # 记录本身确实存在（共享 token 可见），说明上面「看不到」是范围过滤而非没产生
    admin_rows = _audit(c, ADMIN)
    assert any(r0.get("action") == "rename" and r0.get("key") == "a/x.txt"
               for r0 in admin_rows), "rename 审计记录应存在，受限 token 只是不可见"


def test_audit_rotated_backup_files_are_filtered(ctx):
    """R3：轮转文件（audit.log.1）中的记录同样受范围过滤。"""
    c = ctx["client"]
    depot = ctx["depot"]
    _seed_demo(c)
    tok = _new_token(c, role="custom", scopes=["audit:read"], allow_buckets=["demo"])
    rotated = [
        {"time": "2026-01-01T00:00:00Z", "action": "upload", "bucket": "secret",
         "key": "old-secret.txt", "actor": "old", "ip": "", "public_ip": "",
         "size": 1, "sha256": ""},
        {"time": "2026-01-01T00:00:01Z", "action": "upload", "bucket": "demo",
         "key": "old-demo.txt", "actor": "old", "ip": "", "public_ip": "",
         "size": 1, "sha256": ""},
    ]
    (depot / "audit.log.1").write_text(
        "\n".join(json.dumps(r) for r in rotated) + "\n", encoding="utf-8")

    rows = _audit(c, tok, limit=5000)
    assert any(r["key"] == "old-demo.txt" for r in rows)
    assert not any(r["key"] == "old-secret.txt" for r in rows)

    admin_rows = _audit(c, ADMIN, limit=5000)
    assert any(r["bucket"] == "secret" and r["key"] == "old-secret.txt"
               for r in admin_rows)


# ---------------------------------------------------------------------------
# #8 POST /api/config/reload
# ---------------------------------------------------------------------------

def test_config_reload_requires_config_write(ctx):
    """C：#8 需管理员共享 token 或 config:write；无 token 401、无效 401、普通 token 403。"""
    c = ctx["client"]
    _seed_demo(c)

    assert c.post("/api/config/reload").status_code == 401
    assert c.post("/api/config/reload", params={"token": "bad"}).status_code == 401

    viewer = _new_token(c, role="viewer")
    r = c.post("/api/config/reload", params={"token": viewer})
    assert r.status_code == 403, r.text
    assert "config:write" in _detail(r)

    # role=admin 的非共享 token 拥有全部 scope（含 config:write），因此可重载配置
    admin_role = _new_token(c, role="admin")
    assert c.post("/api/config/reload", params={"token": admin_role}).status_code == 200

    r = c.post("/api/config/reload", params={"token": ADMIN})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is True and data["error"] == ""
    assert isinstance(data["changed"], list) and isinstance(data["needs_restart"], list)
    assert isinstance(data["warnings"], list)

    # 重载动作留审计（bucket="" 的全局事件，仅共享 token 可见）
    rows = _audit(c, ADMIN, limit=5000)
    assert any(row["action"] == "config_reload" and row["bucket"] == "" for row in rows)


def test_config_reload_require_read_token_takes_effect_immediately(ctx):
    """D：#8 require_read_token 重载后立即生效（改完马上请求 list，行为随之变化）。"""
    c = ctx["client"]
    _seed_demo(c)
    tok = _demo_viewer(c)

    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 200
    assert c.get("/api/buckets").status_code == 200

    r = _reload(ctx, require_read_token=True)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is True and "require_read_token" in data["changed"]
    assert c.get("/api/auth/permissions").json()["data"]["require_read_token"] is True

    # 不重启、不刷新缓存，紧接着请求就应 401
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 401
    assert c.get("/api/buckets").status_code == 401
    # 有效范围内 token 不受影响
    inside = c.get("/api/objects/list", params={"bucket": "demo", "token": tok})
    assert inside.status_code == 200
    assert [i["name"] for i in inside.json()["data"]["items"]] == ["deep", "dir", "f.txt"]
    # 公开接口不受 require_read_token 影响
    assert c.get("/api/docs").status_code == 200
    assert c.get("/health").status_code == 200
    assert c.get("/api/objects/signed-links/config").status_code == 200

    # 切回 false 后再次立即生效
    r = _reload(ctx, require_read_token=False)
    assert r.status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 200
    assert c.get("/api/buckets").status_code == 200


def test_config_reload_syntax_error_keeps_old_config(ctx):
    """D：#8 配置文件语法错误 → 400 + detail；旧配置完整保留（行为与值都不变）。"""
    c = ctx["client"]
    _seed_demo(c)

    r = _reload(ctx, require_read_token=True,
                signed_links={"count_min": 1, "count_max": 3,
                              "expire_min_seconds": 60, "expire_max_seconds": 3600})
    assert r.status_code == 200, r.text
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 401
    assert c.get("/api/objects/signed-links/config").json()["data"]["count_max"] == 3

    ctx["cfg"].write_text("{ this is not valid json", encoding="utf-8")
    r = c.post("/api/config/reload", params={"token": ADMIN})
    assert r.status_code == 400, r.text
    detail = _detail(r)
    assert "配置文件解析失败" in detail
    assert str(ctx["cfg"]) in detail
    assert "ok" not in r.json()                        # HTTPException 结构，不带成功 data

    # 旧配置仍生效：require_read_token 仍为 true，行为与配置内容都未回退
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 401
    assert c.get("/api/buckets").status_code == 401
    assert c.get("/api/auth/permissions").json()["data"]["require_read_token"] is True
    assert c.get("/api/objects/signed-links/config").json()["data"]["count_max"] == 3

    # 修复配置文件后可正常重载
    r = _reload(ctx, require_read_token=False)
    assert r.status_code == 200 and r.json()["data"]["ok"] is True
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 200


def test_config_reload_live_public_flags_and_needs_restart(ctx):
    """D：#8 ui_enabled/metrics_enabled/signed_links 即时生效；需重启项只提示不生效。"""
    c = ctx["client"]
    _seed_demo(c)
    assert c.get("/metrics").status_code == 200
    assert c.get("/").status_code == 200
    assert c.get("/api/objects/signed-links/config").json()["data"]["count_max"] == 10

    r = _reload(ctx, metrics_enabled=False, ui_enabled=False,
                signed_links={"count_min": 2, "count_max": 5,
                              "expire_min_seconds": 120, "expire_max_seconds": 3600})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is True
    assert {"metrics_enabled", "ui_enabled", "signed_links"} <= set(data["changed"])

    m = c.get("/metrics")
    assert m.status_code == 404 and "metrics 未启用" in _detail(m)
    assert "artifactdepot_http_requests_total" not in m.text

    ui = c.get("/")
    assert ui.status_code == 403
    assert "Web UI 已禁用" in ui.text
    assert "<!DOCTYPE html>" not in ui.text                 # 不泄露页面内容

    cfg = c.get("/api/objects/signed-links/config").json()["data"]
    assert (cfg["count_min"], cfg["count_max"]) == (2, 5)
    assert (cfg["expire_min_seconds"], cfg["expire_max_seconds"]) == (120, 3600)

    # 需重启项（port）：只出现在 needs_restart，实际值保留旧配置
    r = _reload(ctx, port=9999)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is True
    assert "port" in data["needs_restart"]
    assert "port" not in data["changed"]
    assert ctx["config"].get_config()["port"] != 9999
    assert c.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# 公开接口 #1 / #2 / #4 #5 #6 / #7 / #26 / #32 / #33
# ---------------------------------------------------------------------------

def test_public_index_ui(ctx):
    """#1 GET /：ui_enabled=true → 200 单页；无 token 可访问。"""
    c = ctx["client"]
    r = c.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in r.text
    assert "ArtifactDepot" in r.text
    assert "/static/app.js" in r.text
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert c.get("/", params={"token": "bad"}).status_code == 200   # 公开，不解析 token


def test_public_permissions_catalog(ctx):
    """#2 GET /api/auth/permissions：结构完整，且目录中的每个接口都能在 OpenAPI 找到。"""
    c = ctx["client"]
    r = c.get("/api/auth/permissions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0 and body["message"] == "success"
    data = body["data"]
    assert {"permissions", "public", "roles", "default_role",
            "require_read_token"} <= set(data)
    assert len(data["permissions"]) == len(PERMISSION_CATALOG) == 22
    assert len(data["public"]) == len(PUBLIC_CATALOG) == 6
    assert data["default_role"] == "user"
    assert [r0["key"] for r0 in data["roles"]] == list(ROLE_PRESETS)
    assert data["require_read_token"] is False

    spec = c.get("/openapi.json").json()
    ops = {(method.upper(), _norm_path(path))
           for path, item in spec["paths"].items() for method in item}
    assert len(ops) == 34                                   # 35 条 OpenAPI 操作
    for item in data["permissions"] + data["public"]:
        if item.get("key") == "config:write":
            # 配置接口 include_in_schema=False，仍不出现在 OpenAPI，但确实是受保护权限点
            assert item["path"] == "/api/config"
            assert "/api/config" not in spec["paths"]
            continue
        assert (item["method"].upper(), _norm_path(item["path"])) in ops, item


def test_public_health_endpoints(ctx):
    """#4 #5 #6：/health、/health/live、/health/ready 正常结构；不健康时 503。"""
    c = ctx["client"]
    depot = ctx["depot"]
    _seed_demo(c)

    h = c.get("/health")
    assert h.status_code == 200
    body = h.json()
    assert body["status"] == "ok" and body["exists"] is True
    assert Path(body["depot_dir"]).resolve() == depot.resolve()

    live = c.get("/health/live")
    assert live.status_code == 200 and live.json() == {"status": "alive"}

    ready = c.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert Path(ready.json()["depot_dir"]).resolve() == depot.resolve()

    # depot 不可写 → 503 + 可读原因
    os.chmod(depot, 0o500)
    try:
        bad = c.get("/health/ready")
        assert bad.status_code == 503 and "不可写" in _detail(bad)
        assert c.get("/health").status_code == 200              # liveness 语义仍 200
    finally:
        os.chmod(depot, 0o700)

    # depot 不存在 → 503；/health 用 exists=false 表达
    moved = depot.with_name("depot-moved")
    os.rename(depot, moved)
    try:
        bad = c.get("/health/ready")
        assert bad.status_code == 503 and "不存在" in _detail(bad)
        h = c.get("/health")
        assert h.status_code == 200 and h.json()["exists"] is False
    finally:
        os.rename(moved, depot)


def test_public_metrics(ctx):
    """#7 GET /metrics：Prometheus 文本；metrics_enabled=false → 404 且不输出指标。"""
    c = ctx["client"]
    r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    for name in ("artifactdepot_http_requests_total",
                 "artifactdepot_objects_uploaded_total",
                 "artifactdepot_disk_used_bytes"):
        assert name in r.text, name
    assert "# TYPE artifactdepot_http_requests_total counter" in r.text

    _apply_cfg(ctx, metrics_enabled=False)
    m = c.get("/metrics")
    assert m.status_code == 404 and "metrics 未启用" in _detail(m)
    assert "artifactdepot_http_requests_total" not in m.text

    _apply_cfg(ctx, metrics_enabled=True)
    assert c.get("/metrics").status_code == 200


def test_public_signed_links_config(ctx):
    """#26 GET /api/objects/signed-links/config：公开、字段为整数、随配置即时变化。"""
    c = ctx["client"]
    r = c.get("/api/objects/signed-links/config")
    assert r.status_code == 200
    data = r.json()["data"]
    assert (data["count_min"], data["count_max"]) == (1, 10)
    assert (data["expire_min_seconds"], data["expire_max_seconds"]) == (60, 604800)
    # 目录打包能力随配置下发（嵌套对象；其余四个历史字段仍是整数）
    for key in ("count_min", "count_max", "expire_min_seconds", "expire_max_seconds"):
        assert isinstance(data[key], int)
    assert isinstance(data["package"], dict)
    assert data["package"]["allow_directory"] is True
    assert data["package"]["enabled"] is True

    _apply_cfg(ctx, signed_links={"count_min": 2, "count_max": 4,
                                  "expire_min_seconds": 120,
                                  "expire_max_seconds": 7200})
    data = c.get("/api/objects/signed-links/config").json()["data"]
    assert (data["count_min"], data["count_max"]) == (2, 4)
    assert (data["expire_min_seconds"], data["expire_max_seconds"]) == (120, 7200)


def test_public_docs(ctx):
    """#32 #33：文档清单/内容/raw；白名单之外与未知 name → 404。"""
    c = ctx["client"]
    r = c.get("/api/docs")
    assert r.status_code == 200, r.text
    items = r.json()["data"]
    assert [d["name"] for d in items] == [name for name, _, _ in DOC_CATALOG]
    for d in items:
        assert d["title"] and isinstance(d["available"], bool)
    assert all(d["available"] for d in items)          # 仓库内文档文件齐全

    for d in items:
        j = c.get(f"/api/docs/{d['name']}")
        assert j.status_code == 200, (d["name"], j.text[:200])
        body = j.json()["data"]
        assert body["name"] == d["name"] and body["title"] == d["title"]
        assert body["content"].strip()

        raw = c.get(f"/api/docs/{d['name']}", params={"raw": "1"})
        assert raw.status_code == 200
        assert raw.headers["content-type"].startswith("text/plain")
        assert raw.text == body["content"]              # raw 与 JSON 内容一致

    for unknown in ("not-exist", "API_RULES", "license_header.bak"):
        r = c.get(f"/api/docs/{unknown}")
        assert r.status_code == 404 and "文档不存在" in _detail(r)
    traversal = c.get("/api/docs/../../etc/passwd")
    assert traversal.status_code == 404
    assert "root:" not in traversal.text

    # 公开：带无效 token 不影响文档读取
    assert c.get("/api/docs/api_rules", params={"token": "bad"}).status_code == 200


# ---------------------------------------------------------------------------
# F 数据一致性：只读接口不得改动 depot（含失败请求）
# ---------------------------------------------------------------------------

def test_read_only_interfaces_do_not_mutate_depot(ctx):
    c = ctx["client"]
    depot = ctx["depot"]
    _seed_demo(c)
    tok = _demo_viewer(c)

    def snapshot():
        """只快照 bucket 内的对象数据（tokens.json/audit.log 的 last_used_at 等鉴权
        元数据由认证层正常更新，不属于读接口改数据）。"""
        return {
            p.relative_to(depot).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(depot.rglob("*"))
            if p.is_file()
            and p.relative_to(depot).parts[0] in ("demo", "secret")
            and not p.name.startswith(".tmp-")
        }

    before = snapshot()
    assert any(p.endswith("f.txt") for p in before)          # 对象数据确实在快照里
    calls = [
        c.get("/api/objects/list", params={"bucket": "demo"}),
        c.get("/api/objects/list", params={"bucket": "demo", "token": tok}),
        c.get("/api/objects/list", params={"bucket": "secret", "token": tok}),
        c.get("/api/buckets"),
        c.get("/api/buckets", params={"token": tok}),
        c.get("/api/audit", params={"token": ADMIN}),
        c.get("/api/audit", params={"token": tok}),
        c.get("/api/docs"),
        c.get("/api/docs/api_rules"),
        c.get("/health"),
        c.get("/health/ready"),
        c.get("/metrics"),
        c.get("/api/objects/signed-links/config"),
        c.get("/"),
        c.get("/api/auth/permissions"),
    ]
    # 成功的读 + 失败（401/403）的读都不应写盘
    assert any(r.status_code == 200 for r in calls)
    assert any(r.status_code == 403 for r in calls)
    after = snapshot()
    assert after == before, {k: (before.get(k), after.get(k))
                             for k in set(before) | set(after)
                             if before.get(k) != after.get(k)}
