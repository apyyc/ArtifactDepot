# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""第 4 批 CSP 回归测试

- index.html 不再有内联 <style>/<script>/on* 事件处理器/javascript:void
- 所有 data-action 都有 app.js 处理器
- 应用页 CSP 严格（script-src 'self'，无 unsafe-inline）
- /docs、/redoc 单独放行 unsafe-inline
"""
import json
import re
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
        "ui_enabled": True,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c
    config._CONFIG = None


def test_index_has_no_inline_script_style_or_handlers():
    html = (ROOT / "src" / "artifactdepot" / "resources" / "index.html").read_text(encoding="utf-8")
    assert "<style>" not in html
    assert "<script>" not in html                      # 只有 <script src=...>
    assert '<script src="/static/app.js" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/app.css" />' in html
    assert not re.search(r"\son[a-z]+\s*=", html)      # 无内联事件处理器
    assert "javascript:void(0)" not in html


def test_every_data_action_has_js_handler():
    html = (ROOT / "src" / "artifactdepot" / "resources" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "src" / "artifactdepot" / "resources" / "static" / "app.js").read_text(encoding="utf-8")
    html_actions = set(re.findall(r'data-action="([a-z-]+)"', html))
    js_actions = set(re.findall(r"a === '([a-z-]+)'", js))
    assert html_actions, "index.html 应使用 data-action"
    assert html_actions <= js_actions, f"缺少处理器: {sorted(html_actions - js_actions)}"


def test_app_csp_strict_and_assets_served(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_docs_csp_relaxed(client):
    for url in ("/docs", "/redoc"):
        r = client.get(url)
        assert r.status_code == 200
        assert "'unsafe-inline'" in r.headers.get("content-security-policy", "")
