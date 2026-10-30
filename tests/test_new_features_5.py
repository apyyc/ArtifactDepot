# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""2026-10-25 五项需求回归测试

覆盖：
1. 配置页输入令牌后自动加载（前端静态契约）
2. 新增权限点 config:write（配置读写/热加载，管理员或授权 token）
3. 文档与网页 UI 禁止 emoji（见 tests/test_no_emoji.py）
4. Token 未「显示明文」时脱敏为前 2 位 + **** + 后 4 位
5. 配置项 token.min_length / token.max_length（默认 5 / 30）与 token 创建长度校验
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "new5-admin-0123456789"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "",
        "access_token": ADMIN, "ui_enabled": True, "max_upload_mb": 0,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / "none.env"))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield {"client": c, "cfg": cfg, "config": config}
    config._CONFIG = None


def _token(c, **body):
    body.setdefault("user", "new5")
    body.setdefault("role", "custom")
    body.setdefault("scopes", ["object:list"])
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


# ---------------------------------------------------------------------------
# 需求 4：Token 脱敏（前 2 + **** + 后 4）
# ---------------------------------------------------------------------------

def test_mask_token_rule():
    from artifactdepot.api.system import _mask_token
    assert _mask_token("abcdefghijkl") == "ab****ijkl"
    assert _mask_token("abcde") == "ab****"
    assert _mask_token("abcd") == "ab****"
    assert _mask_token("") == ""
    # 不得再带旧的 6 位哈希后缀
    assert re.fullmatch(r"ab\*{4}ijkl", _mask_token("abcdefghijkl"))


def test_tokens_list_masked_prefix2_suffix4(ctx):
    c = ctx["client"]
    tok = _token(c, token="zz12345678yw")
    data = c.get("/api/tokens", params={"token": ADMIN}).json()["data"]
    assert tok not in json.dumps(data, ensure_ascii=False)
    key, rec = next((k, v) for k, v in data.items() if v["user"] == "new5")
    assert key == "zz****78yw" == rec["token"]
    # 显式「显示明文」仍仅管理员可用
    plain = c.get("/api/tokens", params={"token": ADMIN, "reveal": True}).json()["data"]
    assert tok in plain and plain[tok]["token"] == tok


def test_tokens_masked_collision_does_not_overwrite(ctx):
    c = ctx["client"]
    a = _token(c, user="dup1", token="ab1111xxxxyz")
    b = _token(c, user="dup2", token="ab2222xxxxyz")
    data = c.get("/api/tokens", params={"token": ADMIN}).json()["data"]
    assert len(data) == 2 and len(set(data)) == 2
    assert a not in data and b not in data
    assert all(k.startswith("ab****xxyz") for k in data)
    assert len({k.split("-")[0] for k in data}) == 1


# ---------------------------------------------------------------------------
# 需求 5：token 长度配置 + 创建校验
# ---------------------------------------------------------------------------

def test_token_length_validation_default_5_30(ctx):
    c = ctx["client"]
    for bad in ("abcd", "a" * 31, "a" * 40):
        r = c.post("/api/tokens", params={"token": ADMIN},
                   json={"user": "len", "role": "viewer", "token": bad})
        assert r.status_code == 400, f"{len(bad)}: {r.status_code} {r.text}"
        assert "config.token.min_length" in r.text
    for ok in ("abcde", "a" * 30):
        r = c.post("/api/tokens", params={"token": ADMIN},
                   json={"user": "len-ok", "role": "viewer", "token": ok})
        assert r.status_code == 200, r.text


def test_generated_token_uses_max_length_and_is_configurable(ctx):
    c, config = ctx["client"], ctx["config"]
    tok = c.post("/api/tokens", params={"token": ADMIN},
                 json={"user": "gen", "role": "viewer"}).json()["data"]["token"]
    assert len(tok) == 30
    pub = c.get("/api/auth/permissions").json()["data"]
    assert pub["token"] == {"min_length": 5, "max_length": 30}
    # 在线把 max_length 改成 20 → 生成长度随之变化
    r = c.post("/api/config", params={"token": ADMIN},
               json={"values": {"token.max_length": 20}})
    assert r.status_code == 200, r.text
    tok2 = c.post("/api/tokens", params={"token": ADMIN},
                  json={"user": "gen2", "role": "viewer"}).json()["data"]["token"]
    assert len(tok2) == 20
    assert c.get("/api/auth/permissions").json()["data"]["token"]["max_length"] == 20
    assert config.get_token_settings() == {"min_length": 5, "max_length": 20}


def test_token_min_greater_than_max_rejected(ctx):
    c = ctx["client"]
    r = c.post("/api/config", params={"token": ADMIN},
               json={"values": {"token.min_length": 31}})
    assert r.status_code == 400, r.text
    assert "Token 最小长度不能大于最大长度" in r.text
    # 文件未被写入非法组合
    assert json.loads(ctx["cfg"].read_text(encoding="utf-8")).get("token", {}).get(
        "min_length", 5) == 5


def test_token_config_schema_and_group(ctx):
    c = ctx["client"]
    d = c.get("/api/config", params={"token": ADMIN}).json()["data"]
    fields = {f["key"]: f for f in d["schema"]}
    for key, default in (("token.min_length", 5), ("token.max_length", 30)):
        assert key in fields
        f = fields[key]
        assert f["group"] == "令牌"
        assert f["type"] == "int"
        assert f["default"] == default
        assert f["min"] == 1 and f["max"] == 256
    assert "令牌" in d["groups"]


def test_old_token_shorter_than_min_still_updatable(ctx):
    """覆写既有短 token（历史数据）不做长度校验，更新权限仍可用。"""
    c = ctx["client"]
    legacy = "old1"  # 4 位，低于默认 min_length=5
    with (Path(ctx["cfg"]).parent / "depot" / "tokens.json").open("w", encoding="utf-8") as f:
        json.dump({"_version": 2, "tokens": {
            legacy: {"user": "legacy", "role": "viewer", "scopes": ["object:list"]}}}, f)
    r = c.put(f"/api/tokens/{legacy}", params={"token": ADMIN},
              json={"description": "updated"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 需求 1 / 2：配置权限与前端自动加载
# ---------------------------------------------------------------------------

def test_config_write_scope_controls_config_endpoints(ctx):
    c, config = ctx["client"], ctx["config"]
    plain = _token(c, user="plain", scopes=["object:list"])
    assert c.get("/api/config", params={"token": plain}).status_code == 403
    cfg_tok = _token(c, user="cfg-writer", scopes=["config:write"])
    assert c.get("/api/config", params={"token": cfg_tok}).status_code == 200
    assert c.post("/api/config", params={"token": cfg_tok},
                  json={"values": {"token.max_length": 31}}).status_code == 200
    assert config.get_token_settings()["max_length"] == 31
    assert c.post("/api/config/reload", params={"token": cfg_tok}).status_code == 200


def test_frontend_autoload_markers():
    base = ROOT / "src" / "artifactdepot" / "resources"
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    html = (base / "index.html").read_text(encoding="utf-8")
    # 配置页令牌输入触发防抖自动加载（切换标签页的既有调用仍在）
    assert "cfgAutoTimer" in js
    assert re.search(r"_cfgTokenInput\.addEventListener\('input'", js)
    assert "loadConfigPanel(false), 400" in js or "loadConfigPanel(false), 400)" in js
    assert "loadConfigPanel(false)" in js
    # 提示文案与权限说明
    assert "自动加载" in html
    assert "config:write" in html
    # 配置页不再写死「仅管理员共享令牌」；access_token 仅管理员可改的提示保留
    assert "（仅管理员共享令牌）" not in html
    assert "仅管理员共享令牌可修改" in html


def test_config_endpoints_still_hidden_and_openapi_count(ctx):
    c = ctx["client"]
    spec = c.get("/openapi.json").json()
    assert "/api/config" not in spec["paths"]
    ops = {(m.upper(), p) for p, item in spec["paths"].items() for m in item}
    assert len(ops) == 34
    assert len(c.get("/api/auth/permissions").json()["data"]["permissions"]) == 22


def test_config_write_cannot_rotate_access_token(ctx):
    """config:write 用户 token 不能轮换管理员共享令牌（403），其他配置项仍可改。"""
    c, cfg = ctx["client"], ctx["cfg"]
    old = json.loads(cfg.read_text(encoding="utf-8"))["access_token"]
    cfgw = _token(c, user="cfg-guard", scopes=["config:write"])
    r = c.post("/api/config", params={"token": cfgw},
               json={"values": {"access_token": "hijack-token-0123456789"}})
    assert r.status_code == 403, r.text
    assert "仅管理员共享令牌" in r.text
    assert json.loads(cfg.read_text(encoding="utf-8"))["access_token"] == old
    # 管理员仍可轮换
    assert c.get("/api/config", params={"token": ADMIN}).status_code == 200
    # 其他配置项可改；占位符/空值保持原令牌
    assert c.post("/api/config", params={"token": cfgw},
                  json={"values": {"max_upload_mb": 3}}).status_code == 200
    assert c.post("/api/config", params={"token": cfgw},
                  json={"values": {"access_token": "********"}}).status_code == 200
    assert c.post("/api/config", params={"token": cfgw},
                  json={"values": {"access_token": ""}}).status_code == 200
    assert json.loads(cfg.read_text(encoding="utf-8"))["access_token"] == old


def test_config_update_is_serialized(tmp_path, monkeypatch):
    """并发写入不同配置项不得丢失更新（进程内 _CONFIG_LOCK 串行化）。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(tmp_path / "depot"), "meta_dir": "",
        "access_token": ADMIN, "ui": {"file_page_size": 200, "audit_page_size": 500},
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / "none.env"))
    import artifactdepot.config as config
    config._CONFIG = None
    try:
        for i in range(5):
            a, b = 100 + i, 900 + i
            with ThreadPoolExecutor(max_workers=2) as ex:
                list(ex.map(lambda args: config.update_config_file({args[0]: args[1]}),
                            [("ui.file_page_size", a), ("ui.audit_page_size", b)]))
            data = json.loads(cfg.read_text(encoding="utf-8"))
            assert data["ui"]["file_page_size"] == a, data["ui"]
            assert data["ui"]["audit_page_size"] == b, data["ui"]
    finally:
        config._CONFIG = None
