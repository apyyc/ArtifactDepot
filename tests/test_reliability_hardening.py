# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""第 2 批 P1 加固的回归测试

覆盖：
- 状态文件原子写 + 并发锁（并发 add_token / consume_signed_link 不丢更新、不超用）
- 状态文件权限 0600
- 分片会话配额（单分片上限、会话总大小上限）
- 审计公网 IP 只信可信代理的 X-Forwarded-For
"""
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "test-admin-token-0123456789"


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
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
    yield config
    config._CONFIG = None


# --------------------------------------------------------------------------
# 原子写 + 并发锁
# --------------------------------------------------------------------------

def test_concurrent_add_token_no_lost_update(cfg_env):
    from artifactdepot import storage
    n = 25
    errors = []

    def worker(i):
        try:
            storage.add_token(f"tok-{i:04d}-abcdef", f"user{i}", role="viewer")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    mapping = storage.load_tokens()
    # 25 个新 token 一个都不能丢
    assert len([t for t in mapping if t.startswith("tok-")]) == n


def test_concurrent_signed_link_consume_exact(cfg_env):
    """count=10 的链接被 40 个线程并发消费，必须恰好成功 10 次。"""
    from artifactdepot import storage
    link = storage.create_signed_link("b", "k", mode="count", count=10, created_by="u")
    ok_count = []
    lock = threading.Lock()

    def worker():
        ok, _actor, _err = storage.consume_signed_link(link["id"], link["token"], "b", "k")
        if ok:
            with lock:
                ok_count.append(1)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(ok_count) == 10
    assert storage.get_signed_link(link["id"])["remaining"] == 0


def test_state_files_are_0600_and_valid_json(cfg_env):
    from artifactdepot import storage
    storage.add_token("tok-format-test-0001", "u1", role="viewer")
    storage.create_signed_link("b", "k", mode="permanent", created_by="u1")
    for path in (storage._tokens_path(), storage._signed_links_path()):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"{path} mode={oct(mode)}"
        json.loads(path.read_text(encoding="utf-8"))  # 必须是完整 JSON


# --------------------------------------------------------------------------
# 分片配额
# --------------------------------------------------------------------------

def test_chunk_single_size_limit(cfg_env, tmp_path, monkeypatch):
    from artifactdepot import storage
    monkeypatch.setattr(storage, "MAX_CHUNK_BYTES", 10)
    uid = storage.create_chunk_session(owner="u")
    big = tmp_path / "big"; big.write_bytes(b"x" * 11)
    with pytest.raises(Exception) as ei:
        storage.store_chunk(uid, 0, big, actor="u")
    assert getattr(ei.value, "status_code", None) == 413
    storage.abort_chunk_session(uid, actor="u")


def test_chunk_session_total_limit(cfg_env, tmp_path, monkeypatch):
    from artifactdepot import storage
    monkeypatch.setattr(storage, "MAX_CHUNK_BYTES", 100)
    monkeypatch.setattr(storage, "MAX_SESSION_BYTES", 5)
    uid = storage.create_chunk_session(owner="u")
    p1 = tmp_path / "p1"; p1.write_bytes(b"abc")
    p2 = tmp_path / "p2"; p2.write_bytes(b"def")
    storage.store_chunk(uid, 0, p1, actor="u")
    with pytest.raises(Exception) as ei:
        storage.store_chunk(uid, 1, p2, actor="u")
    assert getattr(ei.value, "status_code", None) == 413
    storage.abort_chunk_session(uid, actor="u")


def test_chunk_overwrite_not_double_counted(cfg_env, tmp_path, monkeypatch):
    from artifactdepot import storage
    monkeypatch.setattr(storage, "MAX_CHUNK_BYTES", 100)
    monkeypatch.setattr(storage, "MAX_SESSION_BYTES", 5)
    uid = storage.create_chunk_session(owner="u")
    p = tmp_path / "p"; p.write_bytes(b"abc")
    storage.store_chunk(uid, 0, p, actor="u")
    # 覆盖同一 index，不应把 3 字节算两次（否则会误报超限）
    storage.store_chunk(uid, 0, p, actor="u")
    assert storage.get_chunk_session(uid)["received_bytes"] == 3
    storage.abort_chunk_session(uid, actor="u")


# --------------------------------------------------------------------------
# 审计公网 IP：只信可信代理
# --------------------------------------------------------------------------

def _fake_request(client_ip, xff=""):
    from starlette.requests import Request
    headers = []
    if xff:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {"type": "http", "client": (client_ip, 1234), "headers": headers,
             "method": "GET", "path": "/", "query_string": b""}
    return Request(scope)


def test_public_ip_only_from_trusted_proxy(cfg_env):
    from artifactdepot import netutil
    cfg_env.get_config()["trusted_proxies"] = ["10.0.0.0/8"]
    # 可信代理 + XFF -> 取最接近客户端的非代理地址
    assert netutil.resolved_public_ip(_fake_request("10.0.0.5", "203.0.113.9, 10.0.0.5")) == "203.0.113.9"
    # 直连方不可信 -> 忽略 XFF
    assert netutil.resolved_public_ip(_fake_request("198.51.100.7", "1.2.3.4")) == ""
    # 未配置 trusted_proxies -> 一律空
    cfg_env.get_config()["trusted_proxies"] = []
    assert netutil.resolved_public_ip(_fake_request("10.0.0.5", "203.0.113.9")) == ""


def test_client_supplied_public_ip_is_ignored(cfg_env, tmp_path):
    """query/form 里的 public_ip 不再进审计（服务端只按可信代理解析）。"""
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        c.post("/api/objects", params={"token": ADMIN, "public_ip": "9.9.9.9"},
               data={"bucket": "b", "key": "k", "public_ip": "9.9.9.9"},
               files={"file": ("k", b"x")})
        rows = c.get("/api/audit", params={"token": ADMIN}).json()["data"]
        up = [r for r in rows if r["action"] == "upload"][0]
        assert up["public_ip"] == ""
    config._CONFIG = None
