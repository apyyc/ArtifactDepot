"""配置包（config/）与手动重载的回归测试

覆盖：
- 默认路径改为 src/artifactdepot/config/config.json / .env
- .env 覆盖 config.json；真实环境变量最高；点号嵌套
- reload_config()：changed / needs_restart / 坏 JSON 保留旧配置
- POST /api/config/reload：仅管理员
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ADMIN = "test-admin-token-0123456789"


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    import artifactdepot.config as config
    cfg = tmp_path / "config.json"
    env = tmp_path / ".env"
    cfg.write_text(json.dumps({"access_token": "from-json", "port": 9000}), encoding="utf-8")
    env.write_text("access_token=from-env\nmax_upload_mb=7\nsigned_links.count_max=3\n", encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(env))
    monkeypatch.delenv("ARTIFACT_DEPOT_ACCESS_TOKEN", raising=False)
    config._CONFIG = None
    yield config, cfg, env
    config._CONFIG = None


def test_default_path_is_package(cfg_env):
    config, _, _ = cfg_env
    assert config._default_config_path() == ROOT / "src" / "artifactdepot" / "config" / "config.json"
    assert config._env_path().name == ".env"


def test_env_overrides_json_and_nested(cfg_env):
    config, _, _ = cfg_env
    cfg = config.load_config()
    assert cfg["access_token"] == "from-env"       # .env 覆盖 config.json
    assert cfg["max_upload_mb"] == 7
    assert cfg["signed_links"]["count_max"] == 3   # 点号嵌套
    assert cfg["port"] == 9000                     # 只有 config.json 有


def test_real_env_beats_env_file(cfg_env, monkeypatch):
    config, _, _ = cfg_env
    monkeypatch.setenv("ARTIFACT_DEPOT_ACCESS_TOKEN", "from-real-env")
    assert config.load_config()["access_token"] == "from-real-env"


def test_reload_changed_and_needs_restart(cfg_env):
    config, cfg_file, env_file = cfg_env
    config._CONFIG = None
    config.get_config()
    env_file.write_text("access_token=changed\nmax_upload_mb=9\n", encoding="utf-8")
    cfg_file.write_text(json.dumps({"access_token": "from-json", "port": 9001}), encoding="utf-8")
    res = config.reload_config()
    assert res["ok"] is True
    assert "access_token" in res["changed"]
    assert "port" in res["needs_restart"]
    assert config.get_config()["access_token"] == "changed"
    assert config.get_config()["port"] == 9000      # 需重启项保留旧值


def test_reload_bad_json_keeps_old(cfg_env):
    config, cfg_file, _ = cfg_env
    config._CONFIG = None
    config.get_config()
    cfg_file.write_text("{ bad json", encoding="utf-8")
    res = config.reload_config()
    assert res["ok"] is False and res["error"]
    assert config.get_config()["access_token"] == "from-env"   # 旧配置保留


def test_config_reload_endpoint_admin_only(tmp_path, monkeypatch):
    import artifactdepot.config as config
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
                               "datahub_url": "", "ui_enabled": True}), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    monkeypatch.delenv("ARTIFACT_DEPOT_ACCESS_TOKEN", raising=False)
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        assert c.post("/api/config/reload").status_code == 401
        assert c.post("/api/config/reload", params={"token": "bad"}).status_code == 401
        r = c.post("/api/config/reload", params={"token": ADMIN})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["ok"] is True
        tok = c.post("/api/tokens", params={"token": ADMIN},
                     json={"user": "bob", "role": "viewer"}).json()["data"]["token"]
        assert c.post("/api/config/reload", params={"token": tok}).status_code == 403
    config._CONFIG = None
