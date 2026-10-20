# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""第 4 批 + 配置重构 的接口级业务验收测试

严格按业务语义验证：不只 200，还验证返回结构、权限、边界、状态、数据一致性。
用独立临时 depot，不碰真实数据。
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ADMIN = "test-admin-token-0123456789"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    import artifactdepot.config as config
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True, "metrics_enabled": True,
        "max_upload_mb": 0,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    monkeypatch.delenv("ARTIFACT_DEPOT_ACCESS_TOKEN", raising=False)
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c, depot, cfg, config
    config._CONFIG = None


def _upload(c, bucket="b", key="k.txt", data=b"hello", token=ADMIN):
    return c.post("/api/objects", params={"token": token},
                  data={"bucket": bucket, "key": key}, files={"file": (key, data)})


def _new_token(c, **body):
    body.setdefault("user", "tester")
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


# ===================== N1 /health/live =====================
def test_n1_health_live(ctx):
    c, *_ = ctx
    r = c.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "alive"}
    assert c.post("/health/live").status_code == 405


# ===================== N2 /health/ready =====================
def test_n2_health_ready_ok(ctx):
    c, depot, *_ = ctx
    r = c.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready" and r.json()["depot_dir"] == str(depot)


def test_n2_health_ready_not_writable(ctx):
    c, depot, cfg, config = ctx
    depot.chmod(0o500)
    try:
        assert c.get("/health/ready").status_code == 503
    finally:
        depot.chmod(0o755)


def test_n2_health_ready_missing(ctx):
    c, depot, cfg, config = ctx
    depot.rmdir()
    assert c.get("/health/ready").status_code == 503


# ===================== N3 /metrics =====================
def test_n3_metrics_format_and_counters(ctx):
    c, *_ = ctx
    r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    for name in ("artifactdepot_http_requests_total",
                 "artifactdepot_objects_uploaded_total",
                 "artifactdepot_http_request_duration_seconds",
                 "artifactdepot_tokens_total",
                 "artifactdepot_disk_used_bytes"):
        assert name in body, f"缺少指标 {name}"
    assert "# TYPE" in body and "# HELP" in body
    # 上传后计数 +1
    def val(text, name):
        for line in text.splitlines():
            if line.startswith(name + " ") and not line.startswith(name + "_"):
                return float(line.split()[-1])
        return 0.0
    before = val(c.get("/metrics").text, "artifactdepot_objects_uploaded_total")
    _upload(c)
    after = val(c.get("/metrics").text, "artifactdepot_objects_uploaded_total")
    assert after == before + 1


def test_n3_metrics_disabled_404(ctx):
    c, depot, cfg, config = ctx
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["metrics_enabled"] = False
    cfg.write_text(json.dumps(data), encoding="utf-8")
    config._CONFIG = None
    assert c.get("/metrics").status_code == 404


# ===================== N4 /api/config/reload =====================
def test_n4_reload_permissions(ctx):
    c, *_ = ctx
    assert c.post("/api/config/reload").status_code == 401
    assert c.post("/api/config/reload", params={"token": "bad"}).status_code == 401
    user = _new_token(c, role="viewer")
    assert c.post("/api/config/reload", params={"token": user}).status_code == 403
    r = c.post("/api/config/reload", params={"token": ADMIN})
    assert r.status_code == 200 and r.json()["data"]["ok"] is True


def test_n4_reload_changed_and_needs_restart(ctx):
    c, depot, cfg, config = ctx
    # .env 改 max_upload_mb（可热加载）+ config.json 改 port（需重启）
    env = Path(os.environ["ARTIFACT_DEPOT_ENV"])  # conftest 指向不存在文件
    env.write_text("max_upload_mb=5\n", encoding="utf-8")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["port"] = 9999
    cfg.write_text(json.dumps(data), encoding="utf-8")
    r = c.post("/api/config/reload", params={"token": ADMIN}).json()["data"]
    assert "max_upload_mb" in r["changed"]
    assert "port" in r["needs_restart"]
    # 生效：上传 >5MB 被 413
    big = b"x" * (6 * 1024 * 1024)
    assert _upload(c, key="big.bin", data=big).status_code == 413
    # 需重启项保留旧值
    assert config.get_config()["port"] == 8004


def test_n4_reload_bad_json_400_and_keeps_old(ctx):
    c, depot, cfg, config = ctx
    cfg.write_text("{ bad json", encoding="utf-8")
    r = c.post("/api/config/reload", params={"token": ADMIN})
    assert r.status_code == 400 and "解析失败" in r.text
    assert config.get_config()["access_token"] == ADMIN  # 旧配置保留


def test_n4_reload_audit_written(ctx):
    c, depot, *_ = ctx
    c.post("/api/config/reload", params={"token": ADMIN})
    audit = (depot / "audit.log").read_text(encoding="utf-8")
    assert "config_reload" in audit


# ===================== N5 /api/objects/head =====================
def test_n5_head_normal_and_etag(ctx):
    c, depot, *_ = ctx
    _upload(c, key="a.txt", data=b"hello")
    r = c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "a.txt"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["exists"] is True and d["is_dir"] is False
    assert d["size"] == 5 and len(d["sha256"]) == 64 and d["mtime"].endswith("Z")
    etag = r.headers["etag"]
    assert etag == f'"{d["sha256"]}"'
    # If-None-Match 命中 -> 304（含 * / W/ 弱前缀）
    for inm in (etag, "*", f"W/{etag}"):
        r304 = c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "a.txt"},
                     headers={"If-None-Match": inm})
        assert r304.status_code == 304 and r304.content == b"", inm
    # 不命中 -> 200
    assert c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "a.txt"},
                 headers={"If-None-Match": '"other"'}).status_code == 200


def test_n5_head_missing_params_and_notfound(ctx):
    c, *_ = ctx
    assert c.get("/api/objects/head", params={"token": ADMIN}).status_code == 422
    assert c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b"}).status_code == 422
    assert c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "nope"}).status_code == 404


def test_n5_head_permissions_and_scope(ctx):
    c, *_ = ctx
    _upload(c, bucket="b1", key="x.txt")
    _upload(c, bucket="b2", key="y.txt")
    assert c.get("/api/objects/head", params={"bucket": "b1", "key": "x.txt"}).status_code == 401
    viewer = _new_token(c, role="viewer")  # 无 object:head
    assert c.get("/api/objects/head", params={"token": viewer, "bucket": "b1", "key": "x.txt"}).status_code == 403
    downloader = _new_token(c, role="downloader")  # 有 object:head
    assert c.get("/api/objects/head", params={"token": downloader, "bucket": "b1", "key": "x.txt"}).status_code == 200
    # 资源范围：只允许 b1 的 token 访问 b2 -> 403
    scoped = _new_token(c, role="custom", scopes=["object:head"], allow_buckets=["b1"])
    assert c.get("/api/objects/head", params={"token": scoped, "bucket": "b2", "key": "y.txt"}).status_code == 403


def test_n5_head_etag_changes_after_overwrite(ctx):
    c, *_ = ctx
    _upload(c, key="c.txt", data=b"v1")
    e1 = c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "c.txt"}).headers["etag"]
    _upload(c, key="c.txt", data=b"v2-longer")
    e2 = c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "c.txt"}).headers["etag"]
    assert e1 != e2


def test_n5_head_directory(ctx):
    c, *_ = ctx
    c.post("/api/objects/mkdir", params={"token": ADMIN}, json={"bucket": "b", "key": "dir"})
    r = c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "dir"})
    assert r.status_code == 200 and r.json()["data"]["is_dir"] is True


# ===================== N6 /api/objects/download =====================
def test_n6_download_normal_range_etag(ctx):
    c, *_ = ctx
    _upload(c, key="d.txt", data=b"0123456789")
    r = c.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "d.txt"})
    assert r.status_code == 200 and r.content == b"0123456789"
    etag = r.headers["etag"]
    # Range -> 206
    r206 = c.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "d.txt"},
                 headers={"Range": "bytes=0-2"})
    assert r206.status_code == 206 and r206.content == b"012"
    assert r206.headers["content-range"] == "bytes 0-2/10"
    # If-None-Match -> 304
    r304 = c.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "d.txt"},
                 headers={"If-None-Match": etag})
    assert r304.status_code == 304 and r304.content == b""
    # 304 也写审计（not_modified）
    audit = (ctx[1] / "audit.log").read_text(encoding="utf-8")
    assert "not_modified" in audit


def test_n6_download_permissions(ctx):
    c, *_ = ctx
    _upload(c, key="e.txt")
    assert c.get("/api/objects/download", params={"bucket": "b", "key": "e.txt"}).status_code == 401
    viewer = _new_token(c, role="viewer")  # 无 object:download
    assert c.get("/api/objects/download", params={"token": viewer, "bucket": "b", "key": "e.txt"}).status_code == 403
    dl = _new_token(c, role="downloader")
    assert c.get("/api/objects/download", params={"token": dl, "bucket": "b", "key": "e.txt"}).status_code == 200


def test_n6_download_signed_link_count_and_revoke(ctx):
    c, *_ = ctx
    _upload(c, key="f.txt")
    link = c.post("/api/objects/presign", params={"token": ADMIN},
                  json={"bucket": "b", "key": "f.txt", "mode": "count", "count": 2}).json()["data"]
    url = "/api/objects/download" + link["url"].split("download", 1)[1]
    assert c.get(url).status_code == 200
    assert c.get(url).status_code == 200
    assert c.get(url).status_code == 401  # 次数用完
    # 新建 + 作废
    link2 = c.post("/api/objects/presign", params={"token": ADMIN},
                   json={"bucket": "b", "key": "f.txt", "mode": "permanent"}).json()["data"]
    url2 = "/api/objects/download" + link2["url"].split("download", 1)[1]
    assert c.post(f"/api/objects/signed-links/{link2['id']}/revoke",
                  params={"token": ADMIN}).status_code == 200
    assert c.get(url2).status_code == 401


def test_n6_download_notfound_and_bad_token(ctx):
    c, *_ = ctx
    assert c.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "nope"}).status_code == 404
    assert c.get("/api/objects/download", params={"token": "bad", "bucket": "b", "key": "x"}).status_code == 401


# ===================== N7 /api/objects/list =====================
def test_n7_list_pagination(ctx):
    c, *_ = ctx
    for i in range(5):
        _upload(c, key=f"f{i}.txt")
    d = c.get("/api/objects/list", params={"token": ADMIN, "bucket": "b", "limit": 2, "offset": 0}).json()["data"]
    assert d["total"] == 5 and len(d["items"]) == 2 and d["truncated"] is True and d["offset"] == 0
    d2 = c.get("/api/objects/list", params={"token": ADMIN, "bucket": "b", "limit": 2, "offset": 4}).json()["data"]
    assert len(d2["items"]) == 1 and d2["truncated"] is False
    d3 = c.get("/api/objects/list", params={"token": ADMIN, "bucket": "b", "limit": 0}).json()["data"]
    assert len(d3["items"]) == 5


def test_n7_list_param_bounds(ctx):
    c, *_ = ctx
    assert c.get("/api/objects/list", params={"bucket": "b"}).status_code == 200       # 公开
    assert c.get("/api/objects/list", params={"bucket": "b", "limit": 10001}).status_code == 422
    assert c.get("/api/objects/list", params={"bucket": "b", "limit": -1}).status_code == 422
    assert c.get("/api/objects/list", params={"bucket": "b", "offset": -1}).status_code == 422
    assert c.get("/api/objects/list").status_code == 422                                # 缺 bucket


def test_n7_list_read_token_enforcement(ctx):
    c, depot, cfg, config = ctx
    _upload(c, bucket="demo", key="ok.txt")
    _upload(c, bucket="secret", key="top.txt")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["require_read_token"] = True
    cfg.write_text(json.dumps(data), encoding="utf-8")
    config._CONFIG = None
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 401
    scoped = _new_token(c, role="custom", scopes=["object:list"], allow_buckets=["demo"])
    assert c.get("/api/objects/list", params={"token": scoped, "bucket": "demo"}).status_code == 200
    assert c.get("/api/objects/list", params={"token": scoped, "bucket": "secret"}).status_code == 403
    nos = _new_token(c, role="custom", scopes=["object:list"])
    assert c.get("/api/objects/list", params={"token": nos, "bucket": "demo"}).status_code == 200


# ===================== N8/N9/N10 UI + 静态 =====================
def test_n8_ui_external_assets_and_csp(ctx):
    c, *_ = ctx
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert '<script src="/static/app.js" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/app.css" />' in html
    assert "<script>" not in html and "<style>" not in html
    import re
    assert not re.search(r"\son[a-z]+\s*=", html)
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_n8_ui_disabled_403(ctx):
    c, depot, cfg, config = ctx
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["ui_enabled"] = False
    cfg.write_text(json.dumps(data), encoding="utf-8")
    config._CONFIG = None
    assert c.get("/").status_code == 403


def test_n9_n10_static_assets(ctx):
    c, *_ = ctx
    for path, ct in (("/static/app.js", "javascript"), ("/static/app.css", "css")):
        r = c.get(path)
        assert r.status_code == 200 and ct in r.headers["content-type"] and len(r.content) > 100
    assert c.get("/static/nope.js").status_code == 404
    # 不得暴露 .env / 配置
    assert c.get("/static/../config/.env").status_code in (404, 400)
    assert "access_token" not in c.get("/static/app.js").text


# ===================== N11/N12 docs =====================
def test_n11_n12_docs_self_hosted(ctx):
    c, *_ = ctx
    for url, asset in (("/docs", "/static/swagger-ui-bundle.js"), ("/redoc", "/static/redoc.standalone.js")):
        r = c.get(url)
        assert r.status_code == 200 and asset in r.text
        assert "'unsafe-inline'" in r.headers["content-security-policy"]
    assert c.get("/static/swagger-ui-bundle.js").status_code == 200


# ===================== 跨切面：X-Request-ID =====================
def test_request_id_echo_and_generate(ctx):
    c, *_ = ctx
    assert c.get("/health/live", headers={"X-Request-ID": "abc123"}).headers["x-request-id"] == "abc123"
    generated = c.get("/health/live").headers["x-request-id"]
    assert generated and len(generated) >= 8


# ===================== 联动：完整业务链路 =====================
def test_integration_upload_list_head_download_delete(ctx):
    c, depot, *_ = ctx
    assert _upload(c, key="flow.txt", data=b"v1").status_code == 200
    assert [i["name"] for i in c.get("/api/objects/list", params={"bucket": "b"}).json()["data"]["items"]] == ["flow.txt"]
    etag1 = c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "flow.txt"}).headers["etag"]
    assert c.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "flow.txt"}).content == b"v1"
    # 覆盖 -> ETag 变
    _upload(c, key="flow.txt", data=b"v2")
    etag2 = c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "flow.txt"}).headers["etag"]
    assert etag1 != etag2
    # 删除 -> 列表空、head/download 404
    assert c.delete("/api/objects", params={"token": ADMIN, "bucket": "b", "key": "flow.txt"}).status_code == 200
    assert c.get("/api/objects/list", params={"bucket": "b"}).json()["data"]["items"] == []
    assert c.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "flow.txt"}).status_code == 404
    assert c.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "flow.txt"}).status_code == 404


# ===================== 安全：路径穿越 / 恶意输入 =====================
def test_security_path_traversal(ctx):
    c, *_ = ctx
    for bucket, key in [("..", "x"), ("b", "../../etc/passwd"), ("b", "a/../../x"),
                        ("b", "..%2f..%2fetc%2fpasswd")]:
        r = c.get("/api/objects/head", params={"token": ADMIN, "bucket": bucket, "key": key})
        assert r.status_code in (400, 404), (bucket, key, r.status_code)


def test_security_xss_key_is_literal(ctx):
    c, depot, *_ = ctx
    payload = "x');alert(1);('"
    assert _upload(c, key=payload, data=b"x").status_code == 200
    # 文件系统里是字面量，不是可执行；list 原样返回，前端负责转义
    assert (depot / "b" / payload).is_file()
    items = c.get("/api/objects/list", params={"bucket": "b"}).json()["data"]["items"]
    assert items[0]["name"] == payload


def test_security_invalid_content_type(ctx):
    c, *_ = ctx
    assert c.post("/api/objects", params={"token": ADMIN},
                  data={"bucket": "b", "key": "k"}, headers={"Content-Type": "application/json"}).status_code == 422


# ===================== 数据隔离 / IDOR =====================
def test_isolation_and_idor(ctx):
    c, depot, cfg, config = ctx
    _upload(c, bucket="a", key="a.txt")
    _upload(c, bucket="b", key="b.txt")
    # 只允许 a 的 token 不能 head/download b
    tokA = _new_token(c, role="custom", scopes=["object:head", "object:download"],
                      allow_buckets=["a"])
    assert c.get("/api/objects/head", params={"token": tokA, "bucket": "b", "key": "b.txt"}).status_code == 403
    assert c.get("/api/objects/download", params={"token": tokA, "bucket": "b", "key": "b.txt"}).status_code == 403
    assert c.get("/api/objects/head", params={"token": tokA, "bucket": "a", "key": "a.txt"}).status_code == 200
    # 用户 token 不能重载配置
    assert c.post("/api/config/reload", params={"token": tokA}).status_code == 403
