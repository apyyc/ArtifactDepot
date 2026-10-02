"""权限点 / 自定义 Token 的端到端测试

运行：
    cd ArtifactDepot/ArtifactDepot
    ARTIFACT_DEPOT_CONFIG=$(mktemp) PYTHONPATH=src python3 -m pytest tests -q
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
def client(tmp_path, monkeypatch):
    """每个用例独立 depot_dir + 内存配置，避免污染真实 depot。"""
    depot = tmp_path / "depot"
    depot.mkdir()
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(
        '{"depot_dir": "%s", "meta_dir": "", "access_token": "%s", '
        '"datahub_url": "", "ui_enabled": true, "max_upload_mb": 0}' % (depot, ADMIN),
        encoding="utf-8",
    )
    # config 是进程级缓存，测试间必须重置
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c
    config._CONFIG = None


def _new_token(c, **body):
    body.setdefault("user", "tester")
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def test_permission_catalog_is_public(client):
    d = client.get("/api/auth/permissions").json()["data"]
    assert len(d["permissions"]) == 21
    assert {p["key"] for p in d["permissions"]} >= {
        "object:upload", "object:download", "object:delete", "link:create", "token:write"}
    assert d["public"] and d["roles"] and d["default_role"] == "user"


def test_create_viewer_token_and_enforce(client):
    tok = _new_token(client, role="viewer")
    chk = client.get("/api/auth/check", params={"token": tok}).json()["data"]
    assert chk["valid"] and chk["role"] == "viewer"
    assert chk["scopes"] == ["bucket:list", "object:list"]
    # viewer 不能上传
    r = client.post("/api/objects", data={"bucket": "b", "key": "k", "token": tok},
                    files={"file": ("k", b"x")})
    assert r.status_code == 403
    # 默认公开的 list 仍可匿名/带 token 访问
    assert client.get("/api/objects/list", params={"bucket": "b"}).status_code == 200


def test_admin_can_do_everything(client):
    assert client.post("/api/objects", params={"token": ADMIN},
                       data={"bucket": "b", "key": "k"}, files={"file": ("k", b"x")}).status_code == 200
    assert client.get("/api/audit", params={"token": ADMIN}).status_code == 200
    assert client.delete("/api/objects", params={"token": ADMIN, "bucket": "b", "key": "k"}).status_code == 200


def test_bucket_and_prefix_scope(client):
    tok = _new_token(client, role="custom",
                     scopes=["object:upload", "object:list"],
                     allow_buckets=["voicevideo"],
                     allow_prefixes={"voicevideo": ["carryvideo/2026/"]})
    inside = client.post("/api/objects", params={"token": tok},
                         data={"bucket": "voicevideo", "key": "carryvideo/2026/a.bin"},
                         files={"file": ("a", b"x")})
    assert inside.status_code == 200
    outside_prefix = client.post("/api/objects", params={"token": tok},
                                 data={"bucket": "voicevideo", "key": "carryvideo/2027/a.bin"},
                                 files={"file": ("a", b"x")})
    assert outside_prefix.status_code == 403
    other_bucket = client.post("/api/objects", params={"token": tok},
                               data={"bucket": "other", "key": "carryvideo/2026/a.bin"},
                               files={"file": ("a", b"x")})
    assert other_bucket.status_code == 403
    # 前缀边界：carryvideo/2026 不应匹配 carryvideo/20260
    boundary = client.post("/api/objects", params={"token": tok},
                           data={"bucket": "voicevideo", "key": "carryvideo/20260/a.bin"},
                           files={"file": ("a", b"x")})
    assert boundary.status_code == 403


def test_chunk_upload_scope(client):
    scopes = ["upload:initiate", "upload:chunk", "upload:complete", "upload:abort"]
    tok = _new_token(client, role="custom", scopes=scopes)
    uid = client.post("/api/objects/initiate", params={"token": tok}).json()["data"]["upload_id"]
    assert client.post("/api/objects/chunk", params={"token": tok},
                       data={"upload_id": uid, "index": 0},
                       files={"chunk": ("c", b"abc")}).status_code == 200
    assert client.post("/api/objects/complete", params={"token": tok},
                       data={"upload_id": uid, "bucket": "b", "key": "big.bin", "total_chunks": 1}
                       ).status_code == 200
    # 表单 token（不带 query）也可用
    uid2 = client.post("/api/objects/initiate", params={"token": tok}).json()["data"]["upload_id"]
    r = client.post("/api/objects/abort", data={"upload_id": uid2, "token": tok})
    assert r.status_code == 200


def test_legacy_token_compatibility(client):
    """旧格式 {"token":"用户名"} 应等价 role=user：可写、不可删除/管理。"""
    from artifactdepot import storage
    storage.add_token("legacy-abc", "laoli")
    chk = client.get("/api/auth/check", params={"token": "legacy-abc"}).json()["data"]
    assert chk["valid"] and chk["actor"] == "laoli" and chk["role"] == "user"
    assert "object:upload" in chk["scopes"] and "object:delete" not in chk["scopes"]
    assert client.post("/api/objects", data={"bucket": "b", "key": "k", "token": "legacy-abc"},
                       files={"file": ("k", b"x")}).status_code == 200
    assert client.delete("/api/objects", params={"bucket": "b", "key": "k",
                                                 "token": "legacy-abc"}).status_code == 403
    assert client.get("/api/tokens", params={"token": "legacy-abc"}).status_code == 403


def test_legacy_registration_keeps_permissions(client):
    """协作平台旧协议登记（只传 token+user）不得重置精细权限。"""
    tok = _new_token(client, role="viewer")
    r = client.post("/api/tokens", params={"token": ADMIN}, json={"token": tok, "user": "new-name"})
    assert r.status_code == 200
    chk = client.get("/api/auth/check", params={"token": tok}).json()["data"]
    assert chk["valid"] and chk["actor"] == "new-name"
    assert chk["scopes"] == ["bucket:list", "object:list"]


def test_expired_and_disabled_token(client):
    expired = _new_token(client, role="viewer", expires_at="2000-01-01")
    d = client.get("/api/auth/check", params={"token": expired}).json()["data"]
    assert d["valid"] is False and d["expired"] is True
    assert client.get("/api/objects/list", params={"bucket": "b", "token": expired}).status_code == 200

    tok = _new_token(client, role="viewer")
    assert client.put(f"/api/tokens/{tok}", params={"token": ADMIN}, json={"enabled": False}).status_code == 200
    assert client.get("/api/auth/check", params={"token": tok}).json()["data"]["valid"] is False


def test_datahub_sync_preserves_permissions(client):
    """DataHub 同步只更新用户名，不覆盖 role/scopes/资源范围。"""
    tok = _new_token(client, role="custom", scopes=["object:download"],
                     allow_buckets=["b1"], description="keep-me")
    from artifactdepot import storage
    storage._merge_datahub_users([{"api_token": tok, "name": "renamed"}])
    rec = storage.load_tokens()[tok]
    assert rec["user"] == "renamed"
    assert rec["scopes"] == ["object:download"]
    assert rec["allow_buckets"] == ["b1"]
    assert rec["description"] == "keep-me"


def test_tokens_list_masked_by_default(client):
    tok = _new_token(client, role="viewer")
    masked = client.get("/api/tokens", params={"token": ADMIN}).json()["data"]
    assert tok not in masked
    plain = client.get("/api/tokens", params={"token": ADMIN, "reveal": "true"}).json()["data"]
    assert tok in plain
