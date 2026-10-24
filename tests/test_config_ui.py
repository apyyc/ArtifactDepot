# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""网页「配置」标签页 + 新增配置项 回归测试

覆盖：
- `GET /api/config` / `POST /api/config` 需管理员共享 token 或 `config:write`（无 token 401、缺权限 403）
- 快照包含全部 schema、config.json 文件值、当前生效值、来源、需重启项；access_token 脱敏
- 在线修改热生效（upload/ui/datahub 等）并写回 config.json
- 需重启项（port 等）写文件但 effective 暂不变化，响应 needs_restart
- 未知配置项 / 越界 / 非法类型 → 400 且文件不变；写盘 0600
- access_token 留空保持原值；提交新值后旧令牌失效、新令牌可用
- `.env` 覆盖：写文件成功但 effective 仍取 .env，响应 overridden 标注
- 新增配置项：upload.session_cleanup_interval_seconds / datahub_sync_interval_seconds /
  ui.file_page_size / ui.audit_page_size
- 配置接口不进入 OpenAPI（操作数仍 35），权限点 23
- 网页静态标记：配置标签页、表单容器、data-action 处理器
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "config-ui-admin-0123456789"
NEW_ADMIN = "config-ui-new-admin-9876543210"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "datahub_url": "",
        "access_token": ADMIN, "ui_enabled": True, "max_upload_mb": 0,
        "upload": {"chunk_size_mb": 8, "max_chunk_mb": 64},
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env"))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield {"client": c, "cfg": cfg, "depot": depot, "tmp": tmp_path, "config": config}
    config._CONFIG = None


def _file(cfg: Path) -> dict:
    return json.loads(cfg.read_text(encoding="utf-8"))


def _get_config(c, token=ADMIN) -> dict:
    r = c.get("/api/config", params={"token": token})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _save(c, values: dict, token=ADMIN):
    return c.post("/api/config", params={"token": token}, json={"values": values})


def test_config_api_requires_config_write(ctx):
    c = ctx["client"]
    assert c.get("/api/config").status_code == 401
    assert c.post("/api/config", json={"values": {}}).status_code == 401
    # 普通 token（无 config:write）→ 403
    user = c.post("/api/tokens", params={"token": ADMIN},
                  json={"user": "cfg-user", "role": "custom",
                        "scopes": ["object:list"]}).json()["data"]["token"]
    assert c.get("/api/config", params={"token": user}).status_code == 403
    assert c.post("/api/config", params={"token": user},
                  json={"values": {"max_upload_mb": 1}}).status_code == 403
    assert c.post("/api/config/reload", params={"token": user}).status_code == 403
    # 具备 config:write 的自定义 token → 读取快照 / 写回热加载 / 重载
    cfg_tok = c.post("/api/tokens", params={"token": ADMIN},
                     json={"user": "cfg-writer", "role": "custom",
                           "scopes": ["config:write"]}).json()["data"]["token"]
    r = c.get("/api/config", params={"token": cfg_tok})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["schema"]
    r = c.post("/api/config", params={"token": cfg_tok},
               json={"values": {"max_upload_mb": 2}})
    assert r.status_code == 200, r.text
    assert ctx["config"].get_config()["max_upload_mb"] == 2
    assert c.post("/api/config/reload", params={"token": cfg_tok}).status_code == 200


def test_snapshot_contains_schema_file_values_and_masked_secret(ctx):
    c = ctx["client"]
    d = _get_config(c)
    assert d["path"].endswith("cfg.json")
    assert d["exists"] is True
    assert d["groups"] == ["基础", "DataHub", "网络与安全", "界面", "上传",
                           "下载", "签名链接", "系统/审计", "令牌"]
    keys = {f["key"] for f in d["schema"]}
    assert {"depot_dir", "access_token", "upload.chunk_size_mb",
            "upload.session_cleanup_interval_seconds",
            "datahub_sync_interval_seconds", "ui.file_page_size",
            "ui.audit_page_size", "download.chunk_size_mb",
            "token.min_length", "token.max_length"} <= keys
    # 文件值 / 生效值都存在，密钥一律脱敏
    assert d["values"]["upload"]["chunk_size_mb"] == 8
    assert d["effective"]["upload"]["chunk_size_mb"] == 8
    assert d["values"]["access_token"] == "********"
    assert d["effective"]["access_token"] == "********"
    assert "access_token" not in d["restart_required"]
    assert "port" in d["restart_required"]
    assert d["sources"]["upload.chunk_size_mb"] in ("config.json", "default")
    assert isinstance(d["warnings"], list)


def test_hot_update_writes_file_and_applies(ctx):
    c, cfg = ctx["client"], ctx["cfg"]
    r = _save(c, {"upload.chunk_size_mb": 16, "ui.file_page_size": 50,
                  "datahub_sync_interval_seconds": 0, "max_upload_mb": 12})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is True
    assert data["needs_restart"] == []
    assert set(data["changed"]) & {"upload", "ui", "max_upload_mb",
                                   "datahub_sync_interval_seconds"}
    file_cfg = _file(cfg)
    assert file_cfg["upload"]["chunk_size_mb"] == 16
    assert file_cfg["ui"]["file_page_size"] == 50
    assert file_cfg["datahub_sync_interval_seconds"] == 0
    assert file_cfg["max_upload_mb"] == 12
    # 热加载后立即生效
    pub = c.get("/api/auth/permissions").json()["data"]
    assert pub["upload"]["chunk_size"] == 16 * 1024 * 1024
    assert pub["ui"]["file_page_size"] == 50
    assert ctx["config"].get_config()["max_upload_mb"] == 12


def test_restart_required_field_written_but_not_effective(ctx):
    c, cfg, config = ctx["client"], ctx["cfg"], ctx["config"]
    r = _save(c, {"port": 9000})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "port" in data["needs_restart"]
    assert _file(cfg)["port"] == 9000          # 已写盘
    assert config.get_config()["port"] == 8004  # 需重启才生效


def test_invalid_or_unknown_values_rejected_without_writing(ctx):
    c, cfg = ctx["client"], ctx["cfg"]
    before = cfg.read_text(encoding="utf-8")
    assert _save(c, {"upload.chunk_size_mb": 999}).status_code == 400
    assert _save(c, {"no_such_key": 1}).status_code == 400
    assert _save(c, {"datahub_sync_interval_seconds": "abc"}).status_code == 400
    assert _save(c, {"signed_links": {"count_min": 1}}).status_code == 400
    assert cfg.read_text(encoding="utf-8") == before
    # 合法写入后配置文件权限为 0600（含密钥）
    assert _save(c, {"max_upload_mb": 5}).status_code == 200
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_access_token_empty_keeps_existing_and_new_value_rotates(ctx):
    c, cfg = ctx["client"], ctx["cfg"]
    assert _save(c, {"access_token": ""}).status_code == 200
    assert _get_config(c)["values"]["access_token"] == "********"
    # 新令牌：旧令牌立即 401，新令牌可继续
    r = _save(c, {"access_token": NEW_ADMIN})
    assert r.status_code == 200, r.text
    assert c.get("/api/config", params={"token": ADMIN}).status_code == 401
    d = _get_config(c, NEW_ADMIN)
    assert d["values"]["access_token"] == "********"
    assert _file(cfg)["access_token"] == NEW_ADMIN


def test_dotenv_override_is_reported_not_effective(ctx):
    c, cfg, tmp, config = ctx["client"], ctx["cfg"], ctx["tmp"], ctx["config"]
    (tmp / ".env").write_text("upload.chunk_size_mb=2\n", encoding="utf-8")
    config._CONFIG = None
    r = _save(c, {"upload.chunk_size_mb": 16})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "upload.chunk_size_mb" in data["overridden"]
    assert _file(cfg)["upload"]["chunk_size_mb"] == 16   # 已写文件
    assert c.get("/api/auth/permissions").json()["data"]["upload"]["chunk_size"] == 2 * 1024 * 1024
    snap = _get_config(c)
    assert snap["sources"]["upload.chunk_size_mb"] == ".env"
    assert "upload.chunk_size_mb" in snap["overridden"]


def test_new_config_items_have_effect(ctx):
    c, config = ctx["client"], ctx["config"]
    assert config.get_upload_settings()["session_cleanup_interval_seconds"] == 600
    r = _save(c, {"upload.session_cleanup_interval_seconds": 120,
                  "ui.audit_page_size": 800,
                  "datahub_sync_interval_seconds": 30})
    assert r.status_code == 200, r.text
    assert config.get_upload_settings()["session_cleanup_interval_seconds"] == 120
    assert c.get("/api/auth/permissions").json()["data"]["ui"]["audit_page_size"] == 800
    from artifactdepot import storage
    assert storage._sync_interval_seconds() == 30


def test_config_routes_hidden_from_openapi_and_counts_stable(ctx):
    c = ctx["client"]
    spec = c.get("/openapi.json").json()
    assert "/api/config" not in spec["paths"]
    ops = {(m.upper(), p) for p, item in spec["paths"].items() for m in item}
    assert len(ops) == 35
    assert len(c.get("/api/auth/permissions").json()["data"]["permissions"]) == 23


def test_frontend_config_tab_markers():
    base = ROOT / "src" / "artifactdepot" / "resources"
    html = (base / "index.html").read_text(encoding="utf-8")
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-t="config"' in html
    assert 'data-tab="config"' in html
    assert 'id="sec-config"' in html and 'id="cfgForm"' in html
    assert 'id="cfgAdminToken"' in html
    for action in ("load-config", "save-config", "reload-config-file"):
        assert 'data-action="%s"' % action in html
        assert "a === '%s'" % action in js
    for fn in ("loadConfigPanel", "renderConfigPanel", "collectConfigValues",
               "saveConfigPanel", "reloadConfigFile"):
        assert "function " + fn + "(" in js or "async function " + fn + "(" in js
    assert "api('/api/config'" in js
    assert "data-cfg-key" in js
    assert "需重启" in js and "被 .env 覆盖" in js
    # 严格 CSP：无内联事件
    import re
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_null_and_wrong_container_types_rejected(ctx):
    """null / 对象 / 列表项对象等错误类型必须 400，不能静默转成字符串。"""
    c, cfg = ctx["client"], ctx["cfg"]
    before = cfg.read_text(encoding="utf-8")
    bad_values = (
        {"host": None},
        {"datahub_url": None},
        {"allowed_hosts": None},
        {"allowed_hosts": {"bad": 1}},
        {"allowed_hosts": ["ok", {"bad": 1}]},
        {"trusted_proxies": {"bad": 1}},
        {"upload.chunk_size_mb": None},
        {"ui.file_page_size": None},
    )
    for values in bad_values:
        r = _save(c, values)
        assert r.status_code == 400, f"{values}: {r.status_code} {r.text}"
    assert cfg.read_text(encoding="utf-8") == before


def test_missing_config_file_uses_effective_baseline(ctx):
    """配置文件缺失时部分更新不得把 access_token 等其它配置重置为默认值。"""
    c, cfg = ctx["client"], ctx["cfg"]
    cfg.unlink()
    r = _save(c, {"max_upload_mb": 7})
    assert r.status_code == 200, r.text
    assert cfg.is_file()
    data = _file(cfg)
    assert data["access_token"] == ADMIN, "partial update lost admin token"
    assert data["max_upload_mb"] == 7
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert _get_config(c)["effective"]["max_upload_mb"] == 7
