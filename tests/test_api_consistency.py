"""第 3 批 P2 回归测试

- head：不存在 404，存在 200 且 mtime 为 UTC ISO8601
- list：offset/limit 分页
- 目录递归统计跳过隐藏目录内容
- audit 日志轮转 + query_audit 读取轮转文件
- _mask_token 唯一性
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "test-admin-token-0123456789"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True, "max_upload_mb": 0,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c
    config._CONFIG = None


def _put(c, key, data=b"x"):
    return c.post("/api/objects", params={"token": ADMIN},
                  data={"bucket": "b", "key": key}, files={"file": (key, data)})


def test_head_404_and_iso_mtime(client):
    _put(client, "a.txt", b"hello")
    ok = client.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "a.txt"})
    assert ok.status_code == 200
    d = ok.json()["data"]
    assert d["exists"] is True and d["mtime"].endswith("Z")
    # 不存在 -> S3 HeadObject 语义 404
    miss = client.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "nope.txt"})
    assert miss.status_code == 404


def test_list_pagination(client):
    for i in range(5):
        _put(client, f"f{i}.txt")
    r = client.get("/api/objects/list", params={"token": ADMIN, "bucket": "b", "limit": 2, "offset": 0})
    d = r.json()["data"]
    assert d["total"] == 5 and len(d["items"]) == 2 and d["truncated"] is True
    r2 = client.get("/api/objects/list", params={"token": ADMIN, "bucket": "b", "limit": 2, "offset": 4})
    d2 = r2.json()["data"]
    assert len(d2["items"]) == 1 and d2["truncated"] is False


def test_dir_recursive_skips_hidden_dirs(client):
    from artifactdepot import storage
    depot = Path(storage.get_root())
    d = depot / "b" / "dir"
    d.mkdir(parents=True, exist_ok=True)
    (d / "visible.bin").write_bytes(b"x" * 10)
    hidden = d / ".hidden"
    hidden.mkdir()
    (hidden / "big.bin").write_bytes(b"x" * 100000)
    info = storage._dir_recursive_info(d)
    assert info["size"] == 10, info


def test_audit_rotation_and_query(tmp_path, monkeypatch):
    depot = tmp_path / "depot"; depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "audit_max_mb": 1, "audit_backups": 3,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot import storage

    # 预置一个 >1MB 的 audit.log，再写一条触发轮转
    p = storage._audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x" * (1_200_000) + "\n", encoding="utf-8")
    storage.audit("upload", "b", "k", actor="tester")
    assert p.with_name("audit.log.1").exists(), "audit.log 未轮转"
    rows = storage.query_audit(actor="tester")
    assert any(r["action"] == "upload" for r in rows)
    config._CONFIG = None


def test_mask_token_unique_and_masked():
    from artifactdepot.api.system import _mask_token
    a = _mask_token("aaaa1111bbbb2222")
    b = _mask_token("aaaa3333bbbb4444")
    assert a != b and "****" in a and "aaaa1111bbbb2222" not in a


def test_ui_pagination_markers():
    base = ROOT / "src" / "artifactdepot" / "resources"
    html = (base / "index.html").read_text(encoding="utf-8")
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    # CSP 重构后 JS 在 app.js
    assert 'data-action="load-more"' in js
    assert "async function loadPage()" in js
    assert 'onclick="goUp()"' not in html


def test_etag_and_conditional_requests(client):
    _put(client, "e.txt", b"hello")
    h = client.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "e.txt"})
    etag = h.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    # If-None-Match 命中 -> 304
    h304 = client.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "e.txt"},
                      headers={"If-None-Match": etag})
    assert h304.status_code == 304
    d = client.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "e.txt"})
    assert d.status_code == 200 and d.headers["etag"] == etag
    d304 = client.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "e.txt"},
                      headers={"If-None-Match": etag})
    assert d304.status_code == 304
    # 内容变化 -> ETag 变化
    _put(client, "e.txt", b"world")
    h2 = client.get("/api/objects/head", params={"token": ADMIN, "bucket": "b", "key": "e.txt"})
    assert h2.headers["etag"] != etag
