# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""业务验收：Token 管理 5 接口（#11-#15）+ GET /api/auth/check（#3）

基准：reports/api_inventory.md（R4-R9 / R11）与 docs/api_rules.md。

验收原则：**不看状态码就判 PASS**。每条用例都核对业务结果：
- `tokens.json` 实际内容（文件快照，而非只看响应体）；
- 目标 token 的实际可用性（用它能调什么、不能调什么）；
- `audit.log` 实际行（动作、actor、字段、以及失败时零新增）；
- 「不该被改的数据有没有被改」（操作前后逐字段快照对比）。

覆盖维度：A 正常 / B 参数异常 / C 权限 / D 边界 / E 状态流转（enabled/expired/覆写）/
F 数据一致性。每个用例独立（独立临时 depot），不依赖执行顺序。

已知缺陷（R7 覆写守卫）用 strict xfail 标记，便于修复后自动暴露：
- xfail 用例断言的是**基线要求的行为**，当前实现不满足（详见 reports/biz_tokens.md）。

运行：
    cd ArtifactDepot/ArtifactDepot
    PYTHONPATH=/home/apyyc/work/ArtifactDepot/.audit-deps/libs:src python3 -m pytest tests/test_biz_tokens.py -q
"""
import http.server
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "biz-tokens-admin-0123456789"

R7_REASON = (
    "R7 缺陷（已上报 BIZ-TOK-001/002）：POST 带已存在 token 时按「全量重建」处理，"
    "未提供的字段被重置为默认值——enabled 复活、expires_at 清空变永久、"
    "allow_buckets 清空（静默放宽为全部 bucket）、role/scopes 降级。"
)


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

def _write_config(cfg: Path, depot: Path, extra=None) -> None:
    data = {
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True, "max_upload_mb": 0,
        "require_read_token": False,
    }
    data.update(extra or {})
    cfg.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """每个用例独立 depot + 配置；ARTIFACT_DEPOT_ENV 指向不存在文件，隔离真实 .env。"""
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


def _set_datahub(cfg: Path, config, url: str) -> None:
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["datahub_url"] = url
    cfg.write_text(json.dumps(data), encoding="utf-8")
    config._CONFIG = None


def _raw(depot: Path) -> dict:
    """tokens.json 里真实的 token → 记录（原样，不做归一化）。"""
    p = depot / "tokens.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
        return data["tokens"]
    return {k: v for k, v in data.items() if k != "_version"}


def _strip_last_used(mapping: dict) -> dict:
    """去掉 last_used_at（访问 token 时会被 touch，属于预期副作用）。"""
    return {tok: {k: v for k, v in rec.items() if k != "last_used_at"}
            for tok, rec in mapping.items()}


def _audit_rows(depot: Path) -> list:
    p = depot / "audit.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _detail(resp) -> str:
    try:
        return str(resp.json().get("detail", ""))
    except Exception:
        return resp.text


def _post_token(c, **body):
    body.setdefault("user", "tester")
    return c.post("/api/tokens", params={"token": ADMIN}, json=body)


def _create(c, **body) -> str:
    r = _post_token(c, **body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _put(c, value, **body):
    return c.put(f"/api/tokens/{value}", params={"token": ADMIN}, json=body)


def _delete(c, value):
    return c.delete("/api/tokens", params={"token": ADMIN, "value": value})


def _check(c, token):
    return c.get("/api/auth/check", params={"token": token})


def _warm(c, token) -> None:
    """让 token 的 last_used_at 落盘一次，避免后续快照被 touch 干扰。"""
    c.get("/api/tokens", params={"token": token})


def _mk_bucket(c, name: str):
    r = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": name})
    assert r.status_code == 200, r.text
    return name


def _upload(c, bucket: str, key: str, data: bytes = b"x"):
    r = c.post("/api/objects", params={"token": ADMIN},
               data={"bucket": bucket, "key": key}, files={"file": (key, data)})
    assert r.status_code == 200, r.text


@contextmanager
def _mock_datahub(users=None, raw_body=None, status=200):
    """本地假 DataHub：GET /users.json。返回 base_url。"""
    body = raw_body if raw_body is not None else \
        json.dumps({"users": users or []}, ensure_ascii=False).encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


def _dead_datahub_url() -> str:
    srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = srv.server_address[1]
    srv.server_close()
    return f"http://127.0.0.1:{port}"


# ===========================================================================
# #11 GET /api/tokens
# ===========================================================================

def test_get_tokens_requires_token_read_scope(ctx):
    """C 权限：无 token 401 / 无效 401 / 缺 token:read 403；有权限的受限 token 可读。"""
    c, _, depot, _ = ctx
    assert c.get("/api/tokens").status_code == 401
    assert c.get("/api/tokens", params={"token": "not-a-token"}).status_code == 401

    viewer = _create(c, user="viewer", role="viewer")
    r = c.get("/api/tokens", params={"token": viewer})
    assert r.status_code == 403, r.text
    assert "token:read" in _detail(r)

    reader = _create(c, user="reader", role="custom", scopes=["token:read"],
                     allow_buckets=["demo"])
    r = c.get("/api/tokens", params={"token": reader})
    assert r.status_code == 200, r.text
    # 业务结果：注册表条数 == 文件条数（共享 access_token 不写入 tokens.json）
    assert len(r.json()["data"]) == len(_raw(depot))


def test_get_tokens_masks_by_default_and_reveal_is_admin_only(ctx):
    """A/C：默认脱敏；reveal=true 仅共享 token 生效；非共享 token reveal 仍脱敏。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="alice", role="viewer")
    raw = _raw(depot)
    assert tok in raw and raw[tok]["user"] == "alice"

    # 共享 token 默认脱敏：明文 token 绝不出现在响应文本里
    r = c.get("/api/tokens", params={"token": ADMIN})
    assert r.status_code == 200
    assert tok not in r.text
    data = r.json()["data"]
    assert len(data) == 1
    masked_key, rec = next(iter(data.items()))
    assert "****" in masked_key and "****" in rec["token"]
    assert rec["token"] == masked_key
    assert rec["user"] == "alice"

    # 共享 token + reveal=true → 明文 key/明文 token
    r = c.get("/api/tokens", params={"token": ADMIN, "reveal": True})
    data = r.json()["data"]
    assert tok in data
    assert data[tok]["token"] == tok
    assert data[tok]["user"] == "alice"

    # 非共享 token（即使有 token:read）reveal=true 仍然是脱敏
    reader = _create(c, user="reader", role="custom", scopes=["token:read"])
    r = c.get("/api/tokens", params={"token": reader, "reveal": True})
    assert r.status_code == 200
    assert tok not in r.text, "非共享 token 不应拿到其他 token 明文"
    assert reader not in r.text, "非共享 token 也不应拿到自己明文"
    for key, rec in r.json()["data"].items():
        assert "****" in key and key == rec["token"]


def test_get_tokens_records_match_tokens_json(ctx):
    """F 一致性：GET 返回的记录逐字段等于 tokens.json 的实际内容。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="rich", role="custom", scopes=["object:list"],
                  allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]},
                  expires_at="2099-01-01", description="备注")
    raw_rec = _raw(depot)[tok]
    data = c.get("/api/tokens", params={"token": ADMIN, "reveal": True}).json()["data"]
    rec = data[tok]
    for field in ("user", "role", "scopes", "allow_buckets", "allow_prefixes",
                  "enabled", "expires_at", "description", "created_by", "created_at"):
        assert rec[field] == raw_rec[field], field
    assert rec["expired"] is False
    assert rec["allow_prefixes"] == {"demo": ["a"]}


def test_get_tokens_masked_keys_are_unique(ctx):
    """F：脱敏 key 带哈希后缀，前 4 位/后 4 位相同的不同 token 不会互相覆盖。"""
    c, _, depot, _ = ctx
    a = _create(c, user="dup1", role="viewer", token="abcd1111wxyz")
    b = _create(c, user="dup2", role="viewer", token="abcd2222wxyz")
    data = c.get("/api/tokens", params={"token": ADMIN}).json()["data"]
    assert len(data) == 2
    assert len(set(data)) == 2
    assert all(k.startswith("abcd") and "****" in k for k in data)
    assert a not in data and b not in data
    assert {rec["user"] for rec in data.values()} == {"dup1", "dup2"}


def test_get_tokens_expired_flag_and_empty_registry(ctx):
    """D/E：空注册表返回 {}；过期记录 expired=true（并且不写文件）。"""
    c, _, depot, _ = ctx
    r = c.get("/api/tokens", params={"token": ADMIN})
    assert r.status_code == 200 and r.json()["data"] == {}
    assert not (depot / "tokens.json").exists(), "只读接口不应创建 tokens.json"

    exp = _create(c, user="exp", role="viewer", expires_at="2000-01-01")
    data = c.get("/api/tokens", params={"token": ADMIN, "reveal": True}).json()["data"]
    assert data[exp]["expired"] is True
    assert _raw(depot)[exp]["expires_at"] == "2000-01-01"


def test_get_tokens_reads_legacy_v1_without_rewriting(ctx):
    """E/F：v1 格式（token → 用户名）可读，按 role=user 归一化，且文件不被改写。"""
    c, _, depot, _ = ctx
    from artifactdepot.permissions import role_scopes
    v1_text = json.dumps({"legacy-token-1": "olduser"}, ensure_ascii=False)
    (depot / "tokens.json").write_text(v1_text, encoding="utf-8")

    r = c.get("/api/tokens", params={"token": ADMIN, "reveal": True})
    assert r.status_code == 200
    rec = r.json()["data"]["legacy-token-1"]
    assert rec["user"] == "olduser"
    assert rec["role"] == "user"
    assert rec["scopes"] == role_scopes("user")
    assert (depot / "tokens.json").read_text(encoding="utf-8") == v1_text


# ===========================================================================
# #12 POST /api/tokens
# ===========================================================================

def test_post_role_preset_token_is_usable_and_audited(ctx):
    """A/F/R11：按角色预设建 token；文件、可用性、审计行三处一致。"""
    c, _, depot, _ = ctx
    from artifactdepot.permissions import role_scopes
    n_audit = len(_audit_rows(depot))
    raw_before = _raw(depot)

    r = _post_token(c, user="alice", role="viewer")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    tok = data["token"]
    assert tok and tok not in raw_before
    assert data["role"] == "viewer"
    assert data["scopes"] == role_scopes("viewer")
    assert data["enabled"] is True

    raw = _raw(depot)
    assert tok in raw
    for field in ("user", "role", "scopes", "allow_buckets", "allow_prefixes",
                  "enabled", "expires_at", "description"):
        assert raw[tok][field] == data[field], field
    assert raw[tok]["created_by"] == "系统/工具"

    rows = _audit_rows(depot)
    assert len(rows) == n_audit + 1
    row = rows[-1]
    assert row["action"] == "token_create"
    assert row["bucket"] == "" and row["user"] == "alice" and row["role"] == "viewer"
    assert row["scopes"] == role_scopes("viewer")

    # 业务结果：viewer 能用（列 bucket），不能用（读注册表/审计）
    assert c.get("/api/buckets", params={"token": tok}).status_code == 200
    assert c.get("/api/tokens", params={"token": tok}).status_code == 403
    assert c.get("/api/audit", params={"token": tok}).status_code == 403
    chk = _check(c, tok).json()["data"]
    assert chk["valid"] is True and chk["scopes"] == role_scopes("viewer")


def test_post_custom_scope_and_range_are_hard_boundaries(ctx):
    """A/D：自定义 scopes + bucket/前缀范围真实生效（范围内 200、范围外 403）。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    _upload(c, "demo", "a/x.txt")
    _upload(c, "demo", "b/y.txt")
    _upload(c, "secret", "topsecret.txt", b"SECRET")

    r = _post_token(c, user="scoped", role="custom", scopes=["object:list"],
                    allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]})
    assert r.status_code == 200, r.text
    tok = r.json()["data"]["token"]
    assert _raw(depot)[tok]["allow_prefixes"] == {"demo": ["a"]}

    inside = c.get("/api/objects/list", params={"bucket": "demo", "prefix": "a", "token": tok})
    assert inside.status_code == 200
    assert [i["name"] for i in inside.json()["data"]["items"]] == ["x.txt"]
    outside_prefix = c.get("/api/objects/list",
                           params={"bucket": "demo", "prefix": "b", "token": tok})
    assert outside_prefix.status_code == 403
    outside_bucket = c.get("/api/objects/list",
                           params={"bucket": "secret", "token": tok})
    assert outside_bucket.status_code == 403 and "topsecret" not in outside_bucket.text


def test_post_empty_scopes_grants_nothing_but_token_is_valid(ctx):
    """D/E：显式 scopes=[] = 无任何权限（docs 0.7.1 语义），但 token 本身有效。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    tok = _create(c, user="nobody", role="viewer", scopes=[])
    assert _raw(depot)[tok]["scopes"] == []
    assert _raw(depot)[tok]["role"] == "viewer"
    assert _check(c, tok).json()["data"]["valid"] is True
    assert _check(c, tok).json()["data"]["scopes"] == []
    assert c.get("/api/buckets", params={"token": tok}).status_code == 403
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "token": tok}).status_code == 403
    assert c.get("/api/tokens", params={"token": tok}).status_code == 403


def test_post_autogenerated_and_explicit_token(ctx):
    """B/D：token 留空由服务端生成；显式 token 原样登记。"""
    c, _, depot, _ = ctx
    auto = _create(c, user="auto", role="viewer", token="")
    assert auto and len(auto) >= 32
    assert _raw(depot)[auto]["user"] == "auto"

    explicit = "my-fixed-token-0123456789abcdef"
    got = _create(c, user="fixed", role="viewer", token=explicit)
    assert got == explicit
    assert _raw(depot)[explicit]["user"] == "fixed"
    assert _check(c, explicit).json()["data"]["valid"] is True


def test_post_legacy_registration_preserves_permissions(ctx):
    """E/R7 正向：只传 token+user（旧协议）仅更新用户名，权限/范围/过期/禁用全部保留。"""
    c, _, depot, _ = ctx
    # 新 token（旧协议）：默认 role=user
    legacy_new = _create(c, user="legacy-new")
    assert _raw(depot)[legacy_new]["role"] == "user"
    from artifactdepot.permissions import role_scopes
    assert _raw(depot)[legacy_new]["scopes"] == role_scopes("user")

    rich = _create(c, user="rich", role="operator", allow_buckets=["demo"],
                   allow_prefixes={"demo": ["a/"]}, expires_at="2099-01-01",
                   description="orig")
    assert _put(c, rich, enabled=False).status_code == 200
    before = _strip_last_used(_raw(depot))[rich]

    r = c.post("/api/tokens", params={"token": ADMIN}, json={"token": rich, "user": "renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["token"] == rich
    after = _strip_last_used(_raw(depot))[rich]
    for field in ("role", "scopes", "allow_buckets", "allow_prefixes", "expires_at",
                  "description", "enabled", "created_at"):
        assert after[field] == before[field], field
    assert after["user"] == "renamed"
    assert after["enabled"] is False, "旧协议登记不得复活已禁用 token"
    # R11：覆写既有 token 的登记动作应记为 token_update（BIZ-TOK-002 修复）
    assert any(row["action"] == "token_update" and row.get("user") == "renamed"
               for row in _audit_rows(depot))


def test_post_validation_errors_have_zero_side_effects(ctx):
    """B/C/F：参数异常/权限不足时 tokens.json 与 audit.log 均零新增。"""
    c, _, depot, _ = ctx
    cases = [
        ({"json": {"role": "viewer"}}, 422),                                   # 缺 user
        ({"json": {"user": 123}}, 422),                                        # user 类型错
        ({"json": {"user": "   "}}, 400),                                      # user 空白
        ({"json": {"user": "x", "role": "nope"}}, 400),                        # 未知角色
        ({"json": {"user": "x", "role": "custom", "scopes": ["bogus:scope"]}}, 400),
        ({"json": {"user": "x", "allow_buckets": "demo"}}, 422),
        ({"json": {"user": "x", "allow_prefixes": ["a"]}}, 422),
        ({"json": {"user": "x", "allow_buckets": ["demo"],
                   "allow_prefixes": {"demo": 123}}}, 400),
    ]
    for kwargs, expected in cases:
        snap = _strip_last_used(_raw(depot))
        n_audit = len(_audit_rows(depot))
        r = c.post("/api/tokens", params={"token": ADMIN}, **kwargs)
        assert r.status_code == expected, (kwargs, r.text)
        assert _strip_last_used(_raw(depot)) == snap, kwargs
        assert len(_audit_rows(depot)) == n_audit, kwargs

    viewer = _create(c, user="viewer", role="viewer")
    _warm(c, viewer)
    snap = _strip_last_used(_raw(depot))
    n_audit = len(_audit_rows(depot))
    assert c.post("/api/tokens", json={"user": "x"}).status_code == 401
    assert c.post("/api/tokens", params={"token": "bad"}, json={"user": "x"}).status_code == 401
    r = c.post("/api/tokens", params={"token": viewer}, json={"user": "x"})
    assert r.status_code == 403 and "token:write" in _detail(r)
    assert _strip_last_used(_raw(depot)) == snap
    assert len(_audit_rows(depot)) == n_audit


def test_post_r8_prefix_requires_matching_buckets(ctx):
    """R8：只写 allow_prefixes 不写 allow_buckets → 400；前缀 bucket 不在白名单 → 400。"""
    c, _, depot, _ = ctx
    snap = _strip_last_used(_raw(depot))
    n_audit = len(_audit_rows(depot))

    r = _post_token(c, user="p", role="custom", scopes=["object:list"],
                    allow_prefixes={"demo": ["a/"]})
    assert r.status_code == 400, r.text
    assert "allow_buckets" in _detail(r)

    r = _post_token(c, user="p", role="custom", scopes=["object:list"],
                    allow_buckets=["other"], allow_prefixes={"demo": ["a/"]})
    assert r.status_code == 400, r.text
    assert "allow_buckets" in _detail(r)

    assert _strip_last_used(_raw(depot)) == snap
    assert len(_audit_rows(depot)) == n_audit

    # 自洽组合正常
    r = _post_token(c, user="p", role="custom", scopes=["object:list"],
                    allow_buckets=["demo", "other"], allow_prefixes={"demo": ["a/"]})
    assert r.status_code == 200, r.text


def test_post_r9_prefix_normalization(ctx):
    """R9：`..`/`%2e%2e`/反斜杠/空段/`.`/null/`"/"` → 400；`/a/b/` → `a/b`。"""
    c, _, depot, _ = ctx
    bad_prefixes = [
        {"demo": ["a/../secret"]},
        {"demo": ["%2e%2e/secret"]},
        {"demo": ["a\\b"]},
        {"demo": ["a//b"]},
        {"demo": ["a/./b"]},
        {"demo": ["/"]},
        {"demo": ["%2F"]},
        {"demo": [None]},
    ]
    for prefixes in bad_prefixes:
        snap = _strip_last_used(_raw(depot))
        n_audit = len(_audit_rows(depot))
        r = _post_token(c, user="pfx", role="custom", scopes=["object:list"],
                        allow_buckets=["demo"], allow_prefixes=prefixes)
        assert r.status_code == 400, (prefixes, r.text)
        assert _strip_last_used(_raw(depot)) == snap, prefixes
        assert len(_audit_rows(depot)) == n_audit, prefixes

    ok = _post_token(c, user="pfx", role="custom", scopes=["object:list"],
                     allow_buckets=["demo"], allow_prefixes={"demo": ["/a/b/"]})
    assert ok.status_code == 200, ok.text
    tok = ok.json()["data"]["token"]
    assert _raw(depot)[tok]["allow_prefixes"] == {"demo": ["a/b"]}

    encoded = _post_token(c, user="pfx2", role="custom", scopes=["object:list"],
                          allow_buckets=["demo"], allow_prefixes={"demo": ["%61/b"]})
    assert encoded.status_code == 200, encoded.text
    tok2 = encoded.json()["data"]["token"]
    assert _raw(depot)[tok2]["allow_prefixes"] == {"demo": ["a/b"]}


def test_post_r4_restricted_actor_cannot_escalate_scope_or_bucket(ctx):
    """R4/C：受限 actor 不能创建 scope / bucket 范围超出自身（含「清空=放宽」）。"""
    c, _, depot, _ = ctx
    actor = _create(c, user="actor", role="custom",
                    scopes=["token:write", "object:list"], allow_buckets=["demo"])
    _warm(c, actor)

    cases = [
        ("自身没有的 scope", {"user": "e1", "role": "custom",
                              "scopes": ["object:delete"], "allow_buckets": ["demo"]}),
        ("清空 bucket 白名单", {"user": "e2", "role": "custom",
                                "scopes": ["token:write"], "allow_buckets": []}),
        ("省略 bucket 白名单", {"user": "e3", "role": "custom",
                                "scopes": ["token:write"]}),
        ("自身范围外的 bucket", {"user": "e4", "role": "custom",
                                  "scopes": ["token:write"], "allow_buckets": ["secret"]}),
        ("role=admin 预设越权", {"user": "e5", "role": "admin"}),
    ]
    for why, body in cases:
        snap = _strip_last_used(_raw(depot))
        n_audit = len(_audit_rows(depot))
        r = c.post("/api/tokens", params={"token": actor}, json=body)
        assert r.status_code == 403, (why, r.text)
        assert "权限不足" in _detail(r), (why, r.text)
        assert _strip_last_used(_raw(depot)) == snap, why
        assert len(_audit_rows(depot)) == n_audit, why

    # 正向：自身范围内的子集可以创建，且落盘内容与请求一致
    r = c.post("/api/tokens", params={"token": actor},
               json={"user": "ok", "role": "custom", "scopes": ["token:write"],
                     "allow_buckets": ["demo"]})
    assert r.status_code == 200, r.text
    child = r.json()["data"]["token"]
    rec = _raw(depot)[child]
    assert rec["scopes"] == ["token:write"] and rec["allow_buckets"] == ["demo"]
    assert rec["created_by"] == "actor"


def test_post_r4_restricted_actor_cannot_escalate_prefix(ctx):
    """R4/C：受限 actor 不能靠省略/放宽前缀给新 token 更大路径范围。"""
    c, _, depot, _ = ctx
    actor = _create(c, user="pfx-actor", role="custom",
                    scopes=["token:write", "object:list"],
                    allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]})
    _warm(c, actor)

    # 省略前缀 = 放宽整个 bucket → 403
    r = c.post("/api/tokens", params={"token": actor},
               json={"user": "e1", "role": "custom", "scopes": ["object:list"],
                     "allow_buckets": ["demo"]})
    assert r.status_code == 403, r.text
    assert "前缀" in _detail(r)

    # 更宽前缀 → 403
    r = c.post("/api/tokens", params={"token": actor},
               json={"user": "e2", "role": "custom", "scopes": ["object:list"],
                     "allow_buckets": ["demo"], "allow_prefixes": {"demo": ["z/"]}})
    assert r.status_code == 403, r.text
    assert "前缀" in _detail(r)

    # 空前缀列表 = 放宽 → 403
    r = c.post("/api/tokens", params={"token": actor},
               json={"user": "e3", "role": "custom", "scopes": ["object:list"],
                     "allow_buckets": ["demo"], "allow_prefixes": {"demo": []}})
    assert r.status_code == 403, r.text

    # 更窄前缀 → 200，落盘为规范化后的更窄前缀
    r = c.post("/api/tokens", params={"token": actor},
               json={"user": "ok", "role": "custom", "scopes": ["object:list"],
                     "allow_buckets": ["demo"], "allow_prefixes": {"demo": ["a/b/"]}})
    assert r.status_code == 200, r.text
    assert _raw(depot)[r.json()["data"]["token"]]["allow_prefixes"] == {"demo": ["a/b"]}


def test_post_r7_cross_scope_overwrite_is_403_zero_side_effects(ctx):
    """R7 正向/C：受限 actor 不能借 POST 覆写「比自己更大」的既有 token（尤其不能复活它）。"""
    c, _, depot, _ = ctx
    broad = _create(c, user="broad", role="admin")
    assert _put(c, broad, enabled=False).status_code == 200
    actor = _create(c, user="narrow", role="custom",
                    scopes=["token:write", "object:list"], allow_buckets=["demo"])
    _warm(c, actor)

    before = _raw(depot)[broad].copy()
    n_audit = len(_audit_rows(depot))
    r = c.post("/api/tokens", params={"token": actor},
               json={"token": broad, "user": "broad", "role": "custom",
                     "scopes": ["object:list"], "allow_buckets": ["demo"]})
    assert r.status_code == 403, r.text
    assert _raw(depot)[broad] == before, "越权覆写不得改动目标记录"
    assert len(_audit_rows(depot)) == n_audit
    chk = _check(c, broad).json()["data"]
    assert chk["valid"] is False and chk["enabled"] is False, "被禁用的更大 token 不得被复活"


def test_r7_post_overwrite_must_not_revive_disabled_token(ctx):
    """R7：POST 带已存在 token = 更新，不得复活已禁用 token（当前实现会复活）。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="dis", role="custom", scopes=["object:list"],
                  allow_buckets=["demo"])
    assert _put(c, tok, enabled=False).status_code == 200
    assert _raw(depot)[tok]["enabled"] is False

    r = _post_token(c, token=tok, user="dis", role="custom", scopes=["object:list"],
                    allow_buckets=["demo"])
    assert r.status_code == 200, r.text
    assert _raw(depot)[tok]["enabled"] is False, \
        "POST 覆写把已禁用 token 复活（R7：不得复活 disabled）"


def test_r7_post_overwrite_must_not_turn_expired_into_permanent(ctx):
    """R7：POST 覆写不得清空既有 expires_at 把过期 token 变成永久有效。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="exp", role="custom", scopes=["object:list"],
                  allow_buckets=["demo"], expires_at="2000-01-01")
    assert _check(c, tok).json()["data"]["valid"] is False
    assert _raw(depot)[tok]["expires_at"] == "2000-01-01"

    r = _post_token(c, token=tok, user="exp", role="custom", scopes=["object:list"],
                    allow_buckets=["demo"])
    assert r.status_code == 200, r.text
    assert _raw(depot)[tok]["expires_at"] == "2000-01-01", \
        "POST 覆写清空了 expires_at：过期 token 被延长为永久"
    assert _check(c, tok).json()["data"]["valid"] is False, "过期 token 被复活"


def test_r7_post_overwrite_is_update_not_full_replace(ctx):
    """R7：POST 覆写既有 token 等同更新，未提供的字段必须保留（不得降级/放宽范围）。"""
    c, _, depot, _ = ctx
    from artifactdepot.permissions import role_scopes
    tok = _create(c, user="rich", role="operator", allow_buckets=["demo"],
                  allow_prefixes={"demo": ["a/"]}, expires_at="2099-01-01",
                  description="orig")

    r = _post_token(c, token=tok, user="rich", description="only-desc")
    assert r.status_code == 200, r.text
    rec = _raw(depot)[tok]
    assert rec["allow_buckets"] == ["demo"], "POST 覆写把资源范围静默放宽为全部 bucket"
    assert rec["allow_prefixes"] == {"demo": ["a"]}, "POST 覆写丢掉了路径前缀边界"
    assert rec["role"] == "operator", "POST 覆写把角色降级为默认 user"
    assert rec["scopes"] == role_scopes("operator"), "POST 覆写把权限降级为 user 预设"
    assert rec["expires_at"] == "2099-01-01", "POST 覆写清空了过期时间"
    assert rec["description"] == "only-desc"


# ===========================================================================
# #13 PUT /api/tokens/{value}
# ===========================================================================

def test_put_requires_token_write_scope(ctx):
    """C：无 token 401 / 无效 401 / 缺 token:write 403，目标与审计零变化。"""
    c, _, depot, _ = ctx
    target = _create(c, user="target", role="viewer")
    viewer = _create(c, user="viewer", role="viewer")
    before = _raw(depot)[target].copy()
    n_audit = len(_audit_rows(depot))

    assert c.put(f"/api/tokens/{target}", json={"description": "x"}).status_code == 401
    assert c.put(f"/api/tokens/{target}", params={"token": "bad"},
                 json={"description": "x"}).status_code == 401
    r = c.put(f"/api/tokens/{target}", params={"token": viewer}, json={"description": "x"})
    assert r.status_code == 403 and "token:write" in _detail(r)
    assert _raw(depot)[target] == before
    assert len(_audit_rows(depot)) == n_audit


def test_put_404_and_validation_zero_side_effects(ctx):
    """B/F：目标不存在 404；参数异常 400/422；失败时目标与审计均零变化。"""
    c, _, depot, _ = ctx
    target = _create(c, user="target", role="custom", scopes=["object:list"],
                     allow_buckets=["demo"])
    before = _raw(depot)[target].copy()

    n_audit = len(_audit_rows(depot))
    r = _put(c, "definitely-not-a-token", description="x")
    assert r.status_code == 404 and "token 不存在" in _detail(r)
    assert len(_audit_rows(depot)) == n_audit
    assert "definitely-not-a-token" not in _raw(depot)

    cases = [
        ({"role": "nope"}, 400),
        ({"scopes": ["bogus:scope"]}, 400),
        ({"scopes": "object:list"}, 422),
        ({"allow_buckets": ["demo"], "allow_prefixes": {"demo": 123}}, 400),
        ({"allow_buckets": [], "allow_prefixes": {"demo": ["a/"]}}, 400),
        ({"allow_buckets": ["demo"], "allow_prefixes": {"secret": ["a/"]}}, 400),
        ([1, 2], 422),
    ]
    for body, expected in cases:
        n_audit = len(_audit_rows(depot))
        r = c.put(f"/api/tokens/{target}", params={"token": ADMIN}, json=body)
        assert r.status_code == expected, (body, r.text)
        assert _raw(depot)[target] == before, body
        assert len(_audit_rows(depot)) == n_audit, body


def test_put_merges_and_preserves_untouched_fields(ctx):
    """A/F：PUT 只改传入字段；created_at/last_used_at/其他权限字段保持不变。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="rich", role="custom", scopes=["object:list", "token:read"],
                  allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]},
                  expires_at="2099-01-01", description="orig")
    # 用一次（走 require_scope，会 touch_token），验证 PUT 不会把 last_used_at 冲掉
    assert c.get("/api/tokens", params={"token": tok}).status_code == 200
    before = _raw(depot)[tok].copy()
    assert before["last_used_at"], "前置：token 已被使用"

    r = _put(c, tok, description="new-desc")
    assert r.status_code == 200, r.text
    after = _raw(depot)[tok]
    diff = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert diff == {"description"}, f"PUT 改动了未传入的字段：{diff}"
    assert after["description"] == "new-desc"

    data = r.json()["data"]
    assert data["token"] != tok and "****" in data["token"]
    for field in ("user", "role", "scopes", "allow_buckets", "allow_prefixes",
                  "expires_at", "enabled", "description"):
        assert data[field] == after[field], field
    # GET 复读一致
    got = c.get("/api/tokens", params={"token": ADMIN, "reveal": True}).json()["data"][tok]
    assert got["description"] == "new-desc" and got["allow_prefixes"] == {"demo": ["a"]}


def test_put_updates_permissions_with_real_effect(ctx):
    """A/F：PUT 改 scopes/buckets 后，token 实际可调用的接口随之变化。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")
    _upload(c, "demo", "ok.txt")
    _upload(c, "secret", "s.txt", b"SECRET")
    tok = _create(c, user="upd", role="viewer")

    r = _put(c, tok, scopes=["object:list"], allow_buckets=["demo"])
    assert r.status_code == 200, r.text
    assert _raw(depot)[tok]["scopes"] == ["object:list"]
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "token": tok}).status_code == 200
    assert c.get("/api/objects/list",
                 params={"bucket": "secret", "token": tok}).status_code == 403
    assert c.get("/api/buckets", params={"token": tok}).status_code == 403

    r = _put(c, tok, allow_buckets=["demo", "secret"], user="upd2",
             description="扩展", expires_at="2099-12-31")
    assert r.status_code == 200, r.text
    rec = _raw(depot)[tok]
    assert rec["allow_buckets"] == ["demo", "secret"]
    assert rec["user"] == "upd2" and rec["expires_at"] == "2099-12-31"
    assert c.get("/api/objects/list",
                 params={"bucket": "secret", "token": tok}).status_code == 200
    chk = _check(c, tok).json()["data"]
    assert chk["actor"] == "upd2" and chk["expires_at"] == "2099-12-31"
    assert chk["allow_buckets"] == ["demo", "secret"]


def test_put_role_preset_and_empty_scopes(ctx):
    """A/D：PUT 改角色时按预设展开 scopes；scopes=[] 表示无权限；未知角色 400。"""
    c, _, depot, _ = ctx
    from artifactdepot.permissions import role_scopes
    tok = _create(c, user="role", role="viewer")

    assert _put(c, tok, role="downloader").status_code == 200
    assert _raw(depot)[tok]["role"] == "downloader"
    assert _raw(depot)[tok]["scopes"] == role_scopes("downloader")

    before = _raw(depot)[tok].copy()
    r = _put(c, tok, role="nope")
    assert r.status_code == 400 and "未知角色" in _detail(r)
    assert _raw(depot)[tok] == before

    assert _put(c, tok, role="custom", scopes=[]).status_code == 200
    assert _raw(depot)[tok]["scopes"] == []
    assert _check(c, tok).json()["data"]["valid"] is True
    assert c.get("/api/buckets", params={"token": tok}).status_code == 403


def test_put_prefix_normalization_and_r8(ctx):
    """R8/R9：PUT 同样拒绝不自洽范围与非法前缀，合法前缀被规范化。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="pfx", role="custom", scopes=["object:list"],
                  allow_buckets=["demo"])
    before = _raw(depot)[tok].copy()

    # R8：只写前缀、不写 allow_buckets（目标当前有 buckets=demo，但新增前缀 bucket=secret 不在其中）
    r = _put(c, tok, allow_buckets=["demo"], allow_prefixes={"secret": ["x/"]})
    assert r.status_code == 400 and "allow_buckets" in _detail(r)

    for prefixes in ({"demo": ["a/../x"]}, {"demo": ["%2e%2e/x"]},
                     {"demo": ["a\\b"]}, {"demo": ["/"]}, {"demo": [None]},
                     {"demo": ["a//b"]}):
        n_audit = len(_audit_rows(depot))
        r = _put(c, tok, allow_buckets=["demo"], allow_prefixes=prefixes)
        assert r.status_code == 400, (prefixes, r.text)
        assert _raw(depot)[tok] == before, prefixes
        assert len(_audit_rows(depot)) == n_audit, prefixes

    r = _put(c, tok, allow_buckets=["demo"], allow_prefixes={"demo": ["/a/b/"]})
    assert r.status_code == 200, r.text
    assert _raw(depot)[tok]["allow_prefixes"] == {"demo": ["a/b"]}


def test_put_r5_restricted_actor_cannot_touch_broader_or_escalate(ctx):
    """R5/C：目标现有授权越界 → 403（含只改 description）；更新后越界 → 403；均零副作用。"""
    c, _, depot, _ = ctx
    broad = _create(c, user="broad", role="admin")
    actor = _create(c, user="narrow", role="custom",
                    scopes=["token:write", "token:read", "object:list"],
                    allow_buckets=["demo"])
    _warm(c, actor)
    before_broad = _raw(depot)[broad].copy()

    # 只改 description 也不能碰更大的 token
    n_audit = len(_audit_rows(depot))
    r = c.put(f"/api/tokens/{broad}", params={"token": actor},
              json={"description": "hijack"})
    assert r.status_code == 403, r.text
    assert _raw(depot)[broad] == before_broad
    assert len(_audit_rows(depot)) == n_audit

    # 更新后越界：加 bucket / 加 scope / 清空 bucket / role=admin
    for body in ({"allow_buckets": ["demo", "secret"]},
                 {"scopes": ["object:delete"]},
                 {"allow_buckets": []},
                 {"role": "admin"}):
        before = _raw(depot)[actor].copy()
        n_audit = len(_audit_rows(depot))
        r = c.put(f"/api/tokens/{actor}", params={"token": actor}, json=body)
        assert r.status_code == 403, (body, r.text)
        assert _raw(depot)[actor] == before, body
        assert len(_audit_rows(depot)) == n_audit, body

    # 正向：自身范围内更新成功，且真实生效
    r = c.put(f"/api/tokens/{actor}", params={"token": actor},
              json={"description": "ok", "scopes": ["token:write", "object:list"]})
    assert r.status_code == 200, r.text
    assert _raw(depot)[actor]["description"] == "ok"
    assert _raw(depot)[actor]["allow_buckets"] == ["demo"]


def test_put_enable_disable_state_transition(ctx):
    """E：enabled=false → token 立即失效；再启用 → 恢复可用。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    tok = _create(c, user="state", role="viewer")
    assert c.get("/api/buckets", params={"token": tok}).status_code == 200

    assert _put(c, tok, enabled=False).status_code == 200
    assert _raw(depot)[tok]["enabled"] is False
    chk = _check(c, tok).json()["data"]
    assert chk["valid"] is False and chk["enabled"] is False
    assert c.get("/api/buckets", params={"token": tok}).status_code == 401

    assert _put(c, tok, enabled=True).status_code == 200
    assert _raw(depot)[tok]["enabled"] is True
    assert _check(c, tok).json()["data"]["valid"] is True
    assert c.get("/api/buckets", params={"token": tok}).status_code == 200


def test_put_writes_token_update_audit(ctx):
    """R11：PUT 必须产生 token_update 审计行（含更新后的权限/范围/启用状态）。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="audited", role="viewer")
    n_audit = len(_audit_rows(depot))

    r = _put(c, tok, scopes=["object:list"], allow_buckets=["demo"], description="d")
    assert r.status_code == 200, r.text
    rows = _audit_rows(depot)
    assert len(rows) == n_audit + 1
    row = rows[-1]
    assert row["action"] == "token_update"
    assert row["user"] == "audited"
    assert row["scopes"] == ["object:list"]
    assert row["allow_buckets"] == ["demo"]
    assert row["enabled"] is True
    assert not any(x["action"] == "token_create" for x in rows[n_audit:])


def test_put_updates_legacy_v1_record(ctx):
    """E/F：v1 字符串记录可被 PUT 更新并迁移为 v2，权限语义按 role=user 保留。"""
    c, _, depot, _ = ctx
    (depot / "tokens.json").write_text(
        json.dumps({"legacy-token-1": "olduser"}, ensure_ascii=False), encoding="utf-8")
    assert _check(c, "legacy-token-1").json()["data"]["valid"] is True

    r = _put(c, "legacy-token-1", description="updated")
    assert r.status_code == 200, r.text
    raw = _raw(depot)
    assert "legacy-token-1" in raw
    rec = raw["legacy-token-1"]
    assert rec["user"] == "olduser" and rec["description"] == "updated"
    assert rec["role"] == "user"
    assert "object:list" in rec["scopes"]
    assert _check(c, "legacy-token-1").json()["data"]["valid"] is True


# ===========================================================================
# #14 DELETE /api/tokens
# ===========================================================================

def test_delete_requires_token_delete_scope(ctx):
    """C：无 token 401 / 无效 401 / 缺 token:delete 403；目标与审计零变化。"""
    c, _, depot, _ = ctx
    target = _create(c, user="target", role="viewer")
    viewer = _create(c, user="viewer", role="viewer")
    before = _raw(depot)[target].copy()
    n_audit = len(_audit_rows(depot))

    assert c.delete("/api/tokens", params={"value": target}).status_code == 401
    assert c.delete("/api/tokens",
                    params={"value": target, "token": "bad"}).status_code == 401
    r = c.delete("/api/tokens", params={"value": target, "token": viewer})
    assert r.status_code == 403 and "token:delete" in _detail(r)
    assert _raw(depot)[target] == before
    assert len(_audit_rows(depot)) == n_audit


def test_delete_removes_revokes_and_audits(ctx):
    """A/F/R11：删除后文件无记录、token 立即不可用、其他 token 不受影响、有 token_delete 审计。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    tok = _create(c, user="doomed", role="viewer")
    other = _create(c, user="other", role="viewer")
    before_other = _raw(depot)[other].copy()
    n_audit = len(_audit_rows(depot))
    assert c.get("/api/buckets", params={"token": tok}).status_code == 200

    r = _delete(c, tok)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["removed"] == tok
    assert r.json()["data"]["user"] == "doomed"

    raw = _raw(depot)
    assert tok not in raw
    assert raw[other] == before_other, "删除一个 token 不得影响其他 token 记录"
    assert _check(c, tok).json()["data"]["valid"] is False
    assert c.get("/api/buckets", params={"token": tok}).status_code == 401
    assert tok not in c.get("/api/tokens", params={"token": ADMIN, "reveal": True}).json()["data"]

    rows = _audit_rows(depot)
    assert len(rows) == n_audit + 1
    assert rows[-1]["action"] == "token_delete"
    assert rows[-1]["user"] == "doomed"


def test_delete_missing_and_validation_zero_side_effects(ctx):
    """B/F：目标不存在 404；缺 value 422；空 value 404；均不改文件、不写审计。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="keep", role="viewer")
    snap = _raw(depot).copy()
    n_audit = len(_audit_rows(depot))

    r = _delete(c, "no-such-token")
    assert r.status_code == 404 and "token 不存在" in _detail(r)
    assert c.delete("/api/tokens", params={"token": ADMIN}).status_code == 422
    r = _delete(c, "")
    assert r.status_code == 404
    assert _raw(depot) == snap
    assert len(_audit_rows(depot)) == n_audit


def test_delete_r6_restricted_actor_cannot_delete_broader(ctx):
    """R6/C：目标（含已禁用/已过期的原始记录）授权越界 → 403；范围内可删。"""
    c, _, depot, _ = ctx
    actor = _create(c, user="actor", role="custom",
                    scopes=["token:delete", "object:list"], allow_buckets=["demo"])
    _warm(c, actor)

    broad_active = _create(c, user="broad-active", role="admin")
    broad_disabled = _create(c, user="broad-disabled", role="admin")
    assert _put(c, broad_disabled, enabled=False).status_code == 200
    broad_expired = _create(c, user="broad-expired", role="admin",
                            expires_at="2000-01-01")

    for target in (broad_active, broad_disabled, broad_expired):
        before = _raw(depot)[target].copy()
        n_audit = len(_audit_rows(depot))
        r = c.delete("/api/tokens", params={"token": actor, "value": target})
        assert r.status_code == 403, (target, r.text)
        assert "权限不足" in _detail(r)
        assert _raw(depot)[target] == before, f"{target} 记录被改动"
        assert len(_audit_rows(depot)) == n_audit

    # 自身范围内（更小）的目标 → 允许删除
    small = _create(c, user="small", role="custom", scopes=["object:list"],
                    allow_buckets=["demo"])
    n_audit = len(_audit_rows(depot))
    r = c.delete("/api/tokens", params={"token": actor, "value": small})
    assert r.status_code == 200, r.text
    assert small not in _raw(depot)
    assert _audit_rows(depot)[-1]["action"] == "token_delete"
    assert len(_audit_rows(depot)) == n_audit + 1


def test_delete_admin_can_delete_disabled_and_expired_targets(ctx):
    """E：共享 token 可删除已禁用 / 已过期目标（R6 只约束非共享 token）。"""
    c, _, depot, _ = ctx
    disabled = _create(c, user="disabled", role="viewer")
    assert _put(c, disabled, enabled=False).status_code == 200
    expired = _create(c, user="expired", role="viewer", expires_at="2000-01-01")

    assert _delete(c, disabled).status_code == 200
    assert _delete(c, expired).status_code == 200
    raw = _raw(depot)
    assert disabled not in raw and expired not in raw


# ===========================================================================
# #15 POST /api/tokens/sync
# ===========================================================================

def test_sync_requires_token_sync_scope(ctx):
    """C：无 token 401 / 无效 401 / 缺 token:sync 403；文件与审计零变化。"""
    c, _, depot, _ = ctx
    _create(c, user="existing", role="viewer")
    viewer = _create(c, user="viewer", role="viewer")
    _warm(c, viewer)
    snap = _strip_last_used(_raw(depot))
    n_audit = len(_audit_rows(depot))

    assert c.post("/api/tokens/sync").status_code == 401
    assert c.post("/api/tokens/sync", params={"token": "bad"}).status_code == 401
    r = c.post("/api/tokens/sync", params={"token": viewer})
    assert r.status_code == 403 and "token:sync" in _detail(r)
    assert _strip_last_used(_raw(depot)) == snap
    assert len(_audit_rows(depot)) == n_audit


def test_sync_ok_false_without_datahub_no_side_effects(ctx):
    """D/F：未配置 datahub_url → ok=false + error，本地注册表原样返回且不被改写。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="keep", role="viewer")
    snap = _strip_last_used(_raw(depot))
    n_audit = len(_audit_rows(depot))

    r = c.post("/api/tokens/sync", params={"token": ADMIN})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is False
    assert "未配置 datahub_url" in data["error"]
    assert data["datahub_url"] == ""
    assert set(data["mapping"]) == set(_raw(depot))
    assert data["mapping"][tok]["user"] == "keep"

    assert _strip_last_used(_raw(depot)) == snap
    assert len(_audit_rows(depot)) == n_audit


def test_sync_ok_false_when_unreachable(ctx):
    """D：DataHub 不可达 → ok=false + 非空 error，tokens.json 零改动。"""
    c, cfg, depot, config = ctx
    _create(c, user="keep", role="viewer")
    _set_datahub(cfg, config, _dead_datahub_url())
    snap = _strip_last_used(_raw(depot))

    r = c.post("/api/tokens/sync", params={"token": ADMIN})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is False
    assert data["error"]
    assert _strip_last_used(_raw(depot)) == snap


def test_sync_ok_false_on_invalid_json(ctx):
    """B/D：DataHub 返回非 JSON → ok=false + 非空 error，tokens.json 零改动。"""
    c, cfg, depot, config = ctx
    _create(c, user="keep", role="viewer")
    snap = _strip_last_used(_raw(depot))
    with _mock_datahub(raw_body=b"<html>not-json</html>") as url:
        _set_datahub(cfg, config, url)
        r = c.post("/api/tokens/sync", params={"token": ADMIN})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["ok"] is False and data["error"]
    assert _strip_last_used(_raw(depot)) == snap


def test_sync_ok_true_merges_only_add_preserving_permissions(ctx):
    """A/F：ok=true 时只增不删、保留权限字段，仅更新用户名；禁用状态不被复活。"""
    c, cfg, depot, config = ctx
    rich = _create(c, user="rich", role="custom", scopes=["object:list"],
                   allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]},
                   expires_at="2099-01-01", description="keep")
    assert _put(c, rich, enabled=False).status_code == 200
    local_only = _create(c, user="local-only", role="viewer")
    before_rich = _strip_last_used(_raw(depot))[rich]
    before_local = _strip_last_used(_raw(depot))[local_only]

    users = [
        {"api_token": rich, "name": "renamed-by-datahub"},
        {"api_token": "dh-new-token-1", "name": "dh-user"},
    ]
    with _mock_datahub(users=users) as url:
        _set_datahub(cfg, config, url)
        r = c.post("/api/tokens/sync", params={"token": ADMIN})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["ok"] is True and data["error"] == ""
        assert data["datahub_url"] == url
        raw = _strip_last_used(_raw(depot))
        # 既有 token：仅 user 变化，权限/范围/过期/描述/禁用状态保留
        assert raw[rich]["user"] == "renamed-by-datahub"
        for field in ("role", "scopes", "allow_buckets", "allow_prefixes",
                      "expires_at", "description", "enabled", "created_at"):
            assert raw[rich][field] == before_rich[field], field
        assert raw[rich]["enabled"] is False
        # 新 token：默认 role=user、无范围限制
        assert "dh-new-token-1" in raw
        assert raw["dh-new-token-1"]["role"] == "user"
        assert raw["dh-new-token-1"]["allow_buckets"] == []
        # 只增不删：本地独有 token 仍在
        assert local_only in raw
        assert raw[local_only] == before_local
        assert set(data["mapping"]) == set(raw)
    assert _check(c, rich).json()["data"]["valid"] is False, "sync 不得复活被禁用 token"


def test_sync_is_idempotent(ctx):
    """D/F：重复同步不改变注册表内容（幂等，不重复造 token）。"""
    c, cfg, depot, config = ctx
    _create(c, user="keep", role="viewer")
    users = [{"api_token": "dh-token-x", "name": "dh-x"}]
    with _mock_datahub(users=users) as url:
        _set_datahub(cfg, config, url)
        assert c.post("/api/tokens/sync", params={"token": ADMIN}).status_code == 200
        snap = _strip_last_used(_raw(depot))
        assert c.post("/api/tokens/sync", params={"token": ADMIN}).status_code == 200
        assert _strip_last_used(_raw(depot)) == snap


# ===========================================================================
# #3 GET /api/auth/check
# ===========================================================================

def test_auth_check_admin_and_valid_token(ctx):
    """A/F：共享 token 与普通 token 的身份/范围字段与注册表一致；支持 Bearer。"""
    c, _, depot, _ = ctx
    admin = _check(c, ADMIN).json()["data"]
    assert admin["valid"] is True
    assert admin["is_admin"] is True
    assert admin["role"] == "admin"
    assert admin["actor"] == "系统/工具"

    tok = _create(c, user="checked", role="custom", scopes=["object:list"],
                  allow_buckets=["demo"], allow_prefixes={"demo": ["a/"]},
                  expires_at="2099-01-01", description="备注")
    data = _check(c, tok).json()["data"]
    raw_rec = _raw(depot)[tok]
    assert data["valid"] is True
    assert data["actor"] == "checked"
    assert data["role"] == "custom"
    assert data["is_admin"] is False
    assert data["scopes"] == ["object:list"]
    assert data["allow_buckets"] == ["demo"]
    assert data["allow_prefixes"] == {"demo": ["a"]}
    assert data["expires_at"] == "2099-01-01"
    assert data["description"] == "备注"
    assert raw_rec["user"] == "checked"

    bearer = c.get("/api/auth/check",
                   headers={"Authorization": f"Bearer {tok}"}).json()["data"]
    assert bearer["valid"] is True and bearer["actor"] == "checked"


def test_auth_check_invalid_expired_disabled_and_deleted(ctx):
    """C/E：失效分支必须 valid=false，并能区分「过期」「禁用」「不存在」「已删除」。"""
    c, _, depot, _ = ctx
    unknown = _check(c, "no-such-token").json()["data"]
    assert unknown["valid"] is False
    assert unknown["actor"] == "" and unknown["scopes"] == []

    empty = c.get("/api/auth/check").json()["data"]
    assert empty["valid"] is False

    expired = _create(c, user="exp-user", role="viewer", expires_at="2000-01-01")
    data = _check(c, expired).json()["data"]
    assert data["valid"] is False
    assert data["expired"] is True
    assert data["enabled"] is True
    assert data["actor"] == "exp-user"
    assert data["expires_at"] == "2000-01-01"

    disabled = _create(c, user="dis-user", role="viewer")
    assert _put(c, disabled, enabled=False).status_code == 200
    data = _check(c, disabled).json()["data"]
    assert data["valid"] is False
    assert data["enabled"] is False
    assert data["expired"] is False
    assert data["actor"] == "dis-user"

    deleted = _create(c, user="del-user", role="viewer")
    assert _delete(c, deleted).status_code == 200
    data = _check(c, deleted).json()["data"]
    assert data["valid"] is False
    assert data["actor"] == ""


def test_auth_check_does_not_leak_plaintext_or_mutate_registry(ctx):
    """F：auth/check 不返回明文 token，也不修改 tokens.json。"""
    c, _, depot, _ = ctx
    tok = _create(c, user="quiet", role="viewer")
    snap = _strip_last_used(_raw(depot))
    r = _check(c, tok)
    assert r.status_code == 200
    assert tok not in r.text
    assert _strip_last_used(_raw(depot)) == snap


def test_auth_check_valid_token_with_empty_scopes(ctx):
    """D：scopes=[] 的 token 本身 valid=true（无权限 ≠ 无效 token）。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    tok = _create(c, user="no-perm", role="custom", scopes=[])
    data = _check(c, tok).json()["data"]
    assert data["valid"] is True
    assert data["scopes"] == []
    assert c.get("/api/buckets", params={"token": tok}).status_code == 403


# ===========================================================================
# F 数据一致性：五个写/读接口串起来看同一份注册表
# ===========================================================================

def test_registry_consistency_across_all_interfaces(ctx):
    """F：POST → GET → PUT → auth/check → DELETE 全程 tokens.json / 响应 / 可用性一致。"""
    c, _, depot, _ = ctx
    _mk_bucket(c, "demo")
    _mk_bucket(c, "secret")

    a = _create(c, user="alpha", role="viewer")
    b = _create(c, user="beta", role="custom", scopes=["object:list"],
                allow_buckets=["demo"], description="b-orig")

    # POST 后：GET 与文件一致；b 能用 demo 不能 secret
    listed = c.get("/api/tokens", params={"token": ADMIN, "reveal": True}).json()["data"]
    assert set(listed) == set(_raw(depot)) == {a, b}
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "token": b}).status_code == 200
    assert c.get("/api/objects/list",
                 params={"bucket": "secret", "token": b}).status_code == 403

    # PUT a：只改 scopes/buckets
    assert _put(c, a, scopes=["object:list"], allow_buckets=["secret"]).status_code == 200
    raw = _raw(depot)
    assert raw[a]["scopes"] == ["object:list"] and raw[a]["allow_buckets"] == ["secret"]
    assert raw[b]["allow_buckets"] == ["demo"], "PUT 一个 token 不得改动另一个"
    assert c.get("/api/auth/check",
                 params={"token": a}).json()["data"]["allow_buckets"] == ["secret"]
    assert c.get("/api/objects/list",
                 params={"bucket": "secret", "token": a}).status_code == 200

    # DELETE a：文件/GET/可用性三处同步消失；b 不受影响
    assert _delete(c, a).status_code == 200
    raw = _raw(depot)
    assert a not in raw and b in raw
    assert a not in c.get("/api/tokens",
                          params={"token": ADMIN, "reveal": True}).json()["data"]
    assert c.get("/api/auth/check", params={"token": a}).json()["data"]["valid"] is False
    assert c.get("/api/objects/list",
                 params={"bucket": "demo", "token": b}).status_code == 200
