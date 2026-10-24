# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""上传参数配置化回归测试

覆盖：
- config.json `upload.*` 默认值（8MB/4 并发/24h 超时）
- /api/auth/permissions 与 /api/objects/initiate 下发配置
- 自定义 chunk_size_mb / chunk_concurrency / file_concurrency / request_timeout_seconds
- max_chunk_mb / max_chunks 生效（413 / 400）
- config reload 后即时生效（无需重启）
- .env 点号写法覆盖
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "upload-config-admin-0123456789"
INITIATE = "/api/objects/initiate"
CHUNK = "/api/objects/chunk"
COMPLETE = "/api/objects/complete"


def _write_config(path: Path, depot: Path, upload=None) -> None:
    cfg = {
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True, "max_upload_mb": 0,
    }
    if upload is not None:
        cfg["upload"] = upload
    path.write_text(json.dumps(cfg), encoding="utf-8")


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
    with TestClient(app) as c:
        yield c, cfg, depot, config
    config._CONFIG = None


def _init(c) -> dict:
    r = c.post(INITIATE, params={"token": ADMIN})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _chunk(c, uid: str, index: int, data: bytes):
    return c.post(CHUNK, params={"token": ADMIN},
                  data={"upload_id": uid, "index": str(index)},
                  files={"chunk": (f"chunk_{index}.bin", data)})


def _complete(c, uid: str, total: int):
    return c.post(COMPLETE, params={"token": ADMIN},
                  data={"upload_id": uid, "bucket": "b", "key": "k",
                        "total_chunks": str(total)})


def test_default_upload_settings_exposed(ctx):
    """默认：8MB/片、浏览器 4 并发、3 个文件、请求超时 24h。"""
    c, _, _, _ = ctx
    data = c.get("/api/auth/permissions").json()["data"]["upload"]
    assert data["chunk_size"] == 8 * 1024 * 1024
    assert data["chunk_concurrency"] == 4
    assert data["file_concurrency"] == 3
    assert data["request_timeout_seconds"] == 86400
    assert data["max_upload_mb"] == 0

    ini = _init(c)
    assert ini["chunk_size"] == 8 * 1024 * 1024
    assert ini["chunk_concurrency"] == 4
    assert ini["file_concurrency"] == 3
    assert ini["request_timeout_seconds"] == 86400
    assert ini["upload_id"]


def test_custom_upload_settings_from_config_json(ctx):
    """config.json 的 upload.* 覆盖默认值，并下发到 initiate。"""
    c, cfg, depot, config = ctx
    _write_config(cfg, depot, {
        "chunk_size_mb": 16,
        "chunk_concurrency": 2,
        "file_concurrency": 1,
        "request_timeout_seconds": 1234,
        "max_chunk_mb": 32,
        "max_chunks": 50,
        "max_session_gb": 10,
        "session_ttl_seconds": 7200,
        "session_idle_ttl_seconds": 600,
    })
    config._CONFIG = None
    public = c.get("/api/auth/permissions").json()["data"]["upload"]
    assert public["chunk_size"] == 16 * 1024 * 1024
    assert public["chunk_concurrency"] == 2
    assert public["file_concurrency"] == 1
    assert public["request_timeout_seconds"] == 1234
    assert public["max_chunk_size"] == 32 * 1024 * 1024

    ini = _init(c)
    assert ini["chunk_size"] == 16 * 1024 * 1024
    assert ini["request_timeout_seconds"] == 1234


def test_max_chunk_mb_enforced(ctx):
    """config.upload.max_chunk_mb 调小后，超限分片返回 413。"""
    c, cfg, depot, config = ctx
    _write_config(cfg, depot, {"chunk_size_mb": 1, "max_chunk_mb": 1,
                               "max_chunks": 100, "request_timeout_seconds": 60})
    config._CONFIG = None
    ini = _init(c)
    assert ini["chunk_size"] == 1024 * 1024
    r = _chunk(c, ini["upload_id"], 0, b"x" * (2 * 1024 * 1024))
    assert r.status_code == 413, r.text
    assert "分片" in r.json()["detail"]


def test_max_chunks_enforced(ctx):
    """config.upload.max_chunks 调小后，complete 的 total_chunks 超限返回 400。"""
    c, cfg, depot, config = ctx
    _write_config(cfg, depot, {"max_chunks": 2, "request_timeout_seconds": 60})
    config._CONFIG = None
    ini = _init(c)
    _chunk(c, ini["upload_id"], 0, b"a")
    r = _complete(c, ini["upload_id"], 3)
    assert r.status_code == 400, r.text
    assert "total_chunks" in r.json()["detail"]


def test_reload_applies_upload_settings(ctx):
    """POST /api/config/reload 后新的 upload.* 立即生效（initiate 返回新值）。"""
    c, cfg, depot, config = ctx
    _write_config(cfg, depot, {"chunk_size_mb": 4, "request_timeout_seconds": 300})
    config._CONFIG = None
    r = c.post("/api/config/reload", params={"token": ADMIN})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["ok"] is True
    ini = _init(c)
    assert ini["chunk_size"] == 4 * 1024 * 1024
    assert ini["request_timeout_seconds"] == 300


def test_env_dotted_override(tmp_path, monkeypatch):
    """.env 支持 upload.chunk_size_mb=2 的点号写法。"""
    import artifactdepot.config as config
    cfg = tmp_path / "cfg.json"
    env = tmp_path / ".env"
    cfg.write_text(json.dumps({"access_token": "x", "depot_dir": str(tmp_path)}), encoding="utf-8")
    env.write_text("upload.chunk_size_mb=2\nupload.chunk_concurrency=1\n"
                   "upload.request_timeout_seconds=600\n", encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(env))
    config._CONFIG = None
    s = config.get_upload_settings()
    assert s["chunk_size_mb"] == 2
    assert s["chunk_concurrency"] == 1
    assert s["request_timeout_seconds"] == 600
    config._CONFIG = None


def test_upload_limits_are_clamped(tmp_path, monkeypatch):
    """非法/越界配置被夹紧：分片 ≤64MB、并发 ≤16、超时 ≥60s。"""
    import artifactdepot.config as config
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "access_token": "x", "depot_dir": str(tmp_path),
        "upload": {"chunk_size_mb": 999, "chunk_concurrency": 999,
                   "file_concurrency": 0, "request_timeout_seconds": 1},
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    config._CONFIG = None
    s = config.get_upload_settings()
    assert s["chunk_size_mb"] == 64
    assert s["chunk_concurrency"] == 16
    assert s["file_concurrency"] == 1
    assert s["request_timeout_seconds"] == 60
    config._CONFIG = None


def test_frontend_reads_upload_config():
    """网页必须从 /api/auth/permissions 读取 upload 配置（防回归）。"""
    js = (ROOT / "src" / "artifactdepot" / "resources" / "static" / "app.js").read_text(encoding="utf-8")
    assert "loadUploadConfig" in js
    assert "/api/auth/permissions" in js
    assert "uploadCfg.chunk_concurrency" in js
    assert "uploadCfg.file_concurrency" in js
    assert "uploadTimeoutMs" in js
