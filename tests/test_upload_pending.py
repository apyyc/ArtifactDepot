# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""网页「未完成上传」服务端支持回归测试

网页需要在不恢复 File 列表的情况下，按**当前令牌**列出上传了一半的分片会话，
并允许手动取消。接口 `GET /api/objects/uploads` 复用 `upload:abort` 权限，
仅返回当前 actor 名下的会话（同一 token 身份），不新增权限点、不进入 OpenAPI。

覆盖：
- 无 token → 401；缺 upload:abort → 403；同一 token 可见、其它 token/管理员不可见
- 返回字段：upload_id / bucket / key / name / size / received_bytes / percent /
  uploaded_chunks；不泄露服务端绝对路径
- 取消（POST /api/objects/abort）后列表立即消失
- 权限目录仍 22 条、OpenAPI 操作仍 35 条（该接口 include_in_schema=False）
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "pending-admin-0123456789"
CHUNK = 1024 * 1024
SCOPES = ["upload:initiate", "upload:chunk", "upload:complete", "upload:abort"]


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True, "max_upload_mb": 0,
        "upload": {"chunk_size_mb": 1, "max_chunk_mb": 1},
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot import storage
    storage.clear_upload_registry()
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c
    config._CONFIG = None
    storage.clear_upload_registry()


def _token(c, user: str, scopes=None) -> str:
    r = c.post("/api/tokens", params={"token": ADMIN},
               json={"user": user, "role": "custom", "scopes": scopes or SCOPES})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _init(c, token: str, key: str = "dir/half.bin", size: int = 2 * CHUNK) -> str:
    r = c.post("/api/objects/initiate", params={"token": token},
               data={"bucket": "demo", "key": key, "size": str(size)})
    assert r.status_code == 200, r.text
    return r.json()["data"]["upload_id"]


def _chunk(c, token: str, upload_id: str, index: int = 0, data: bytes = b"x" * CHUNK):
    r = c.post("/api/objects/chunk", params={"token": token},
               data={"upload_id": upload_id, "index": str(index)},
               files={"chunk": ("chunk.bin", data)})
    assert r.status_code == 200, r.text


def test_list_requires_valid_token_and_scope(ctx):
    c = ctx
    assert c.get("/api/objects/uploads").status_code == 401
    viewer = _token(c, "viewer-only", ["object:list"])
    assert c.get("/api/objects/uploads", params={"token": viewer}).status_code == 403
    # 无效 token → 401（不降级为匿名）
    assert c.get("/api/objects/uploads", params={"token": "not-a-token"}).status_code == 401


def test_list_is_scoped_to_current_token(ctx):
    c = ctx
    alice = _token(c, "alice")
    bob = _token(c, "bob")
    uid = _init(c, alice)
    _chunk(c, alice, uid)

    # 同一 token 可见（token 只走 Authorization 头，与网页一致）
    r = c.get("/api/objects/uploads", headers={"Authorization": "Bearer " + alice})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["count"] == 1
    item = data["items"][0]
    assert item["upload_id"] == uid
    assert item["bucket"] == "demo"
    assert item["key"] == "dir/half.bin"
    assert item["name"] == "half.bin"
    assert item["size"] == 2 * CHUNK
    assert item["received_bytes"] == CHUNK
    assert item["percent"] == 50
    assert item["uploaded_chunks"] == [0]
    assert item["total_chunks"] == 2
    assert "dir" not in item                      # 不返回服务端绝对路径字段
    assert str(Path(item["upload_id"]).parent) == "."   # upload_id 只是会话目录名

    # 其它 token（含管理员）看不到别人的半成品
    assert c.get("/api/objects/uploads", params={"token": bob}).json()["data"]["count"] == 0
    assert c.get("/api/objects/uploads", params={"token": ADMIN}).json()["data"]["count"] == 0

    # 管理员自己创建的会话只出现在管理员列表
    admin_uid = _init(c, ADMIN, key="admin/half.bin", size=CHUNK)
    _chunk(c, ADMIN, admin_uid, data=b"y" * 1024)
    admin_items = c.get("/api/objects/uploads", params={"token": ADMIN}).json()["data"]["items"]
    assert [it["upload_id"] for it in admin_items] == [admin_uid]
    alice_items = c.get("/api/objects/uploads", params={"token": alice}).json()["data"]["items"]
    assert [it["upload_id"] for it in alice_items] == [uid]


def test_cancel_pending_upload_removes_it(ctx):
    c = ctx
    tok = _token(c, "carol")
    uid = _init(c, tok)
    _chunk(c, tok, uid)
    assert c.get("/api/objects/uploads", params={"token": tok}).json()["data"]["count"] == 1

    r = c.post("/api/objects/abort", data={"upload_id": uid, "token": tok})
    assert r.status_code == 200 and r.json()["data"]["existed"] is True
    assert c.get("/api/objects/uploads", params={"token": tok}).json()["data"]["count"] == 0


def test_empty_list_and_completed_sessions_not_listed(ctx):
    c = ctx
    tok = _token(c, "dave")
    assert c.get("/api/objects/uploads", params={"token": tok}).json()["data"] == {
        "items": [], "count": 0}
    # 完成上传后会话被删除，不应再出现在未完成列表
    uid = _init(c, tok, key="done.bin", size=CHUNK)
    _chunk(c, tok, uid, data=b"z" * 1024)
    r = c.post("/api/objects/complete", params={"token": tok},
               data={"upload_id": uid, "bucket": "demo", "key": "done.bin",
                     "total_chunks": "1"})
    assert r.status_code == 200, r.text
    assert c.get("/api/objects/uploads", params={"token": tok}).json()["data"]["count"] == 0


def test_public_contract_counts_unchanged(ctx):
    """内部接口不进 OpenAPI、不新增权限点：权限目录 23 条、OpenAPI 操作 35 条。"""
    c = ctx
    d = c.get("/api/auth/permissions").json()["data"]
    assert len(d["permissions"]) == 23
    spec = c.get("/openapi.json").json()
    ops = {(m.upper(), p) for p, item in spec["paths"].items() for m in item}
    assert len(ops) == 35
    assert "/api/objects/uploads" not in spec["paths"]
