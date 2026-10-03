"""文档入口 / 空 scopes / 读接口资源范围 的回归测试

覆盖 0.7.1 修复与新增：
- `GET /api/docs`、`GET /api/docs/{name}`、`?raw=1`
- `scopes: []` 语义 = 无任何权限（此前被回退成角色预设）
- `require_read_token=true` 时资源范围外 `GET /api/objects/list` 必须 403（此前会泄露白名单外 bucket 内容）
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "test-admin-token"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        '{"depot_dir": "%s", "meta_dir": "", "access_token": "%s", '
        '"datahub_url": "", "ui_enabled": true, "max_upload_mb": 0}' % (depot, ADMIN),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c, cfg, config
    config._CONFIG = None


def _set_read_token(ctx, value=True):
    """就地改配置并清缓存（require_read_token 在请求期读取）"""
    c, cfg, config = ctx
    import json
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["require_read_token"] = value
    cfg.write_text(json.dumps(data), encoding="utf-8")
    config._CONFIG = None
    return c


def test_docs_catalog_and_content(ctx):
    c, _, _ = ctx
    r = c.get("/api/docs")
    assert r.status_code == 200
    names = {d["name"]: d for d in r.json()["data"]}
    assert "api_rules" in names and "architecture" in names
    assert names["api_rules"]["available"] is True

    r = c.get("/api/docs/api_rules")
    assert r.status_code == 200
    assert "ArtifactDepot" in r.json()["data"]["content"]

    raw = c.get("/api/docs/api_rules", params={"raw": "1"})
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/plain")
    assert raw.text.startswith("# ArtifactDepot")

    assert c.get("/api/docs/not-exist").status_code == 404


def test_empty_scopes_means_no_permission(ctx):
    """scopes=[] 必须原样保留：空权限 token 不能上传/列目录（require_read_token=true）"""
    c, _, _ = ctx
    for role in ("user", "custom"):
        r = c.post("/api/tokens", params={"token": ADMIN},
                   json={"user": "none-" + role, "role": role, "scopes": []})
        assert r.status_code == 200, r.text
        tok = r.json()["data"]["token"]
        chk = c.get("/api/auth/check", params={"token": tok}).json()["data"]
        assert chk["valid"] is True
        assert chk["scopes"] == []
        # 上传被拒
        assert c.post("/api/objects", data={"bucket": "b", "key": "k", "token": tok},
                      files={"file": ("k", b"x")}).status_code == 403
        # 收紧读接口后列表也被拒
        _set_read_token(ctx, True)
        assert c.get("/api/objects/list", params={"bucket": "b", "token": tok}).status_code == 403
        _set_read_token(ctx, False)


def test_read_scope_range_enforced_when_token_required(ctx):
    """require_read_token=true：范围外 list 返回 403，不泄露白名单外 bucket 内容"""
    c, _, _ = ctx
    tok = c.post("/api/tokens", params={"token": ADMIN},
                 json={"user": "scoped", "role": "custom",
                       "scopes": ["object:list", "bucket:list"],
                       "allow_buckets": ["demo"]}).json()["data"]["token"]
    c.post("/api/objects", params={"token": ADMIN},
           data={"bucket": "demo", "key": "ok.txt"}, files={"file": ("f", b"x")})
    c.post("/api/objects", params={"token": ADMIN},
           data={"bucket": "secret", "key": "topsecret.txt"}, files={"file": ("f", b"SECRET")})

    # 公开模式：不阻断，但也不额外泄露（匿名语义）
    assert c.get("/api/objects/list", params={"bucket": "secret"}).status_code == 200

    _set_read_token(ctx, True)
    inside = c.get("/api/objects/list", params={"bucket": "demo", "token": tok})
    assert inside.status_code == 200
    assert [i["name"] for i in inside.json()["data"]["items"]] == ["ok.txt"]
    outside = c.get("/api/objects/list", params={"bucket": "secret", "token": tok})
    assert outside.status_code == 403
    assert "topsecret" not in outside.text
    # bucket 列表同样只返回允许的 bucket
    buckets = c.get("/api/buckets", params={"token": tok}).json()["data"]
    assert buckets == ["demo"]
    # 匿名访问被 401
    assert c.get("/api/objects/list", params={"bucket": "demo"}).status_code == 401
