"""第 1 批 P0 安全修复的回归测试

覆盖：
- 分片 upload_id 路径穿越（abort/store）被拒绝，合法会话仍可用
- 分片会话绑定创建者，他人不可接管
- access_token 为空时返回 401/403（而非 500）
- 网页 UI 不再把不可信 key/id 拼进内联 onclick（XSS）
- 访问日志 query 脱敏
- Authorization: Bearer 头鉴权（token 不走 URL query）
"""
import logging
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "test-admin-token-0123456789"


def _make_client(tmp_path, monkeypatch, access_token=ADMIN):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        '{"depot_dir": "%s", "meta_dir": "", "access_token": "%s", '
        '"datahub_url": "", "ui_enabled": true, "max_upload_mb": 0}' % (depot, access_token),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    return TestClient(app), config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    c, config = _make_client(tmp_path, monkeypatch)
    with c:
        yield c
    config._CONFIG = None


# --------------------------------------------------------------------------
# 1. upload_id 路径穿越
# --------------------------------------------------------------------------

def test_chunk_upload_id_traversal_blocked(client, tmp_path):
    """abort/store 传入 ../ 或绝对路径必须 400，且不得删除目标目录。"""
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "important.txt"
    marker.write_text("keep", encoding="utf-8")

    from artifactdepot import storage
    rel = os.path.relpath(str(victim), str(storage._chunk_dir()))
    for bad in ["/", "/etc", "../" + victim.name, rel, "short", "a" * 100]:
        r = client.post("/api/objects/abort", data={"upload_id": bad, "token": ADMIN})
        assert r.status_code == 400, f"abort {bad!r} should be 400, got {r.status_code}"
        assert marker.exists(), f"victim deleted by upload_id={bad!r}"

    # store_chunk 同样不得越界写
    with pytest.raises(Exception) as ei:
        storage.store_chunk(rel, 0, marker, actor="系统/工具", is_admin=True)
    assert getattr(ei.value, "status_code", None) == 400


def test_chunk_session_bound_to_owner(client):
    """会话绑定创建者：他人 token 不能 chunk/abort 该会话。"""
    # 管理员创建会话
    uid = client.post("/api/objects/initiate", params={"token": ADMIN}).json()["data"]["upload_id"]
    # 普通用户（有 upload:chunk/abort 权限）尝试接管
    tok = client.post("/api/tokens", params={"token": ADMIN},
                      json={"user": "bob", "role": "uploader"}).json()["data"]["token"]
    r = client.post("/api/objects/chunk", params={"token": tok},
                    data={"upload_id": uid, "index": 0},
                    files={"chunk": ("c", b"abc")})
    assert r.status_code == 403, r.text
    r = client.post("/api/objects/abort", data={"upload_id": uid, "token": tok})
    assert r.status_code == 403, r.text
    # 管理员可正常 abort
    assert client.post("/api/objects/abort", data={"upload_id": uid, "token": ADMIN}).status_code == 200


def test_total_chunks_bounded(client):
    uid = client.post("/api/objects/initiate", params={"token": ADMIN}).json()["data"]["upload_id"]
    r = client.post("/api/objects/complete", params={"token": ADMIN},
                    data={"upload_id": uid, "bucket": "b", "key": "k", "total_chunks": 10**9})
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------
# 2. access_token 为空：401/403 而不是 500
# --------------------------------------------------------------------------

def test_empty_access_token_no_500(tmp_path, monkeypatch):
    c, config = _make_client(tmp_path, monkeypatch, access_token="")
    with c:
        assert c.get("/api/auth/check", params={"token": "x"}).status_code == 200
        assert c.get("/api/tokens", params={"token": "x"}).status_code == 401
        assert c.delete("/api/objects", params={"bucket": "b", "key": "k", "token": "x"}).status_code == 401
    config._CONFIG = None


def test_config_warnings_flag_weak_token(tmp_path, monkeypatch):
    c, config = _make_client(tmp_path, monkeypatch, access_token="y")
    with c:
        from artifactdepot.config import config_warnings
        assert any("过弱" in w for w in config_warnings())
    config._CONFIG = None


# --------------------------------------------------------------------------
# 3. 前端 XSS：不得再有 onclick 内插 esc() 的写法
# --------------------------------------------------------------------------

def test_ui_has_no_inline_onclick_with_untrusted_data():
    base = ROOT / "src" / "artifactdepot" / "resources"
    html = (base / "index.html").read_text(encoding="utf-8")
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    assert not re.search(r'onclick="[^"]*esc\(', html), \
        "index.html 仍存在把 esc() 结果拼进内联 onclick 的 XSS 隐患"
    # CSP 重构后事件委托在 app.js
    assert "data-action" in js and "addEventListener('click'" in js


# --------------------------------------------------------------------------
# 4. 访问日志 query 脱敏
# --------------------------------------------------------------------------

def test_access_log_query_redaction():
    from artifactdepot.logging_utils import RedactAccessLogQuery, redact_query_string
    assert redact_query_string("/d?bucket=b&token=SECRET&key=k") == "/d?bucket=b&token=***&key=k"
    assert redact_query_string("/d?link=a&tk=SECRET") == "/d?link=a&tk=***"
    assert redact_query_string("/health") == "/health"

    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/api/objects/download?token=SECRET", "1.1", 200), None)
    RedactAccessLogQuery().filter(rec)
    assert "SECRET" not in rec.getMessage()
    assert "token=***" in rec.getMessage()


# --------------------------------------------------------------------------
# 5. Authorization: Bearer 头鉴权（token 不走 URL query）
# --------------------------------------------------------------------------

def test_bearer_header_auth(client):
    h = {"Authorization": f"Bearer {ADMIN}"}
    assert client.get("/api/tokens", headers=h).status_code == 200
    r = client.post("/api/objects", headers=h,
                    data={"bucket": "b", "key": "k"}, files={"file": ("k", b"x")})
    assert r.status_code == 200, r.text
