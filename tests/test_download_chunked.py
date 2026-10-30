# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""分块并发下载（服务端）回归测试

覆盖：
- access_token 默认值 admin（无配置 / config.json / .env / 环境变量优先级）
- config.download.* 默认值下发（8MiB/4/1h）、自定义覆盖、越界夹紧、.env 点号写法
- POST /api/config/reload 后下载配置即时生效
- HEAD /api/objects/download：Content-Length / Accept-Ranges / ETag / Content-Disposition；
  审计 head=true 且 size=0，下载指标不增；404
- Range：206 + Content-Range、首/中/尾/后缀区间内容正确；416；多区间不 500；
  每个 Range 审计 size=实际字节、partial=true；完整 GET size=整文件
- 并发多 Range 请求内容正确（httpx ASGITransport + asyncio.gather）
- 权限目录不新增 scope（仍 22 条）
"""
import asyncio
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "download-test-admin-0123456789"
DOWNLOAD = "/api/objects/download"


def _write_config(path: Path, depot: Path, **extra) -> None:
    cfg = {
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "ui_enabled": True, "max_upload_mb": 0,
    }
    cfg.update(extra)
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _make_client(cfg: Path, tmp_path: Path):
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    return TestClient(app), config


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    _write_config(cfg, depot)
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    monkeypatch.delenv("ARTIFACT_DEPOT_ACCESS_TOKEN", raising=False)
    c, config = _make_client(cfg, tmp_path)
    with c:
        yield c, cfg, depot, config
    config._CONFIG = None


@pytest.fixture()
def files(ctx):
    """上传两个文件：10240 字节定长内容 + 单字节文件，返回 (name, data, etag_sha)。"""
    c, _, _, _ = ctx
    data = bytes(range(256)) * 40
    r = c.post("/api/objects", data={"bucket": "b", "key": "big.bin", "token": ADMIN},
               files={"file": ("big.bin", data)})
    assert r.status_code == 200, r.text
    r = c.post("/api/objects", data={"bucket": "b", "key": "one.bin", "token": ADMIN},
               files={"file": ("one.bin", b"Z")})
    assert r.status_code == 200, r.text
    return data, hashlib.sha256(data).hexdigest()


def _audit_rows(depot: Path) -> list:
    p = depot / "audit.log"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _download_metric() -> float:
    from artifactdepot.observability import render_metrics
    for line in render_metrics().splitlines():
        if line.startswith("artifactdepot_objects_downloaded_total"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


# --------------------------------------------------------------------------
# 1. access_token 默认 admin
# --------------------------------------------------------------------------

def test_default_access_token_is_admin(tmp_path, monkeypatch):
    """不写 access_token 时，内置默认（以及 config.json 默认）都是 admin。"""
    from artifactdepot.config import DEFAULT
    assert DEFAULT["access_token"] == "admin"

    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"depot_dir": str(depot), "meta_dir": ""}),
                   encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    monkeypatch.delenv("ARTIFACT_DEPOT_ACCESS_TOKEN", raising=False)
    c, config = _make_client(cfg, tmp_path)
    with c:
        assert config.load_config()["access_token"] == "admin"
        # admin 可直接作为管理员共享令牌使用
        assert c.get("/api/tokens", params={"token": "admin"}).status_code == 200
        # 错误令牌仍被拒绝
        assert c.get("/api/tokens", params={"token": "not-admin"}).status_code == 401
    config._CONFIG = None


def test_env_token_overrides_default(ctx, monkeypatch):
    """真实环境变量优先级最高，仍可覆盖默认 admin。"""
    c, _, _, config = ctx
    monkeypatch.setenv("ARTIFACT_DEPOT_ACCESS_TOKEN", "strong-token-0123456789")
    config._CONFIG = None
    assert c.get("/api/tokens", params={"token": "strong-token-0123456789"}).status_code == 200
    assert c.get("/api/tokens", params={"token": ADMIN}).status_code == 401
    monkeypatch.delenv("ARTIFACT_DEPOT_ACCESS_TOKEN", raising=False)
    config._CONFIG = None


# --------------------------------------------------------------------------
# 2. download 配置下发 / 覆盖 / 夹紧 / 热重载 / .env
# --------------------------------------------------------------------------

def test_default_download_settings_exposed(ctx):
    c, _, _, _ = ctx
    data = c.get("/api/auth/permissions").json()["data"]["download"]
    assert data == {"chunk_size": 8 * 1024 * 1024,
                    "chunk_concurrency": 4,
                    "request_timeout_seconds": 3600}


def test_custom_download_settings_and_clamp(ctx):
    c, cfg, depot, config = ctx
    _write_config(cfg, depot, download={"chunk_size_mb": 16, "chunk_concurrency": 2,
                                        "request_timeout_seconds": 120})
    config._CONFIG = None
    data = c.get("/api/auth/permissions").json()["data"]["download"]
    assert data == {"chunk_size": 16 * 1024 * 1024, "chunk_concurrency": 2,
                    "request_timeout_seconds": 120}

    # 越界值夹紧到 1–64MB / 1–16 / 60s–7d
    _write_config(cfg, depot, download={"chunk_size_mb": 999, "chunk_concurrency": 0,
                                        "request_timeout_seconds": 1})
    config._CONFIG = None
    data = c.get("/api/auth/permissions").json()["data"]["download"]
    assert data == {"chunk_size": 64 * 1024 * 1024, "chunk_concurrency": 1,
                    "request_timeout_seconds": 60}


def test_download_settings_env_dotted(tmp_path, monkeypatch):
    """`.env` 点号写法 download.chunk_size_mb 生效。"""
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    env = tmp_path / ".env"
    _write_config(cfg, depot)
    env.write_text("download.chunk_size_mb=32\ndownload.chunk_concurrency=8\n"
                   "download.request_timeout_seconds=90\n", encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(env))
    import artifactdepot.config as config
    config._CONFIG = None
    assert config.public_download_settings() == {
        "chunk_size": 32 * 1024 * 1024, "chunk_concurrency": 8,
        "request_timeout_seconds": 90}
    config._CONFIG = None


def test_download_settings_reload(ctx):
    """POST /api/config/reload 后下载配置立即生效（无需重启）。"""
    c, cfg, depot, config = ctx
    _write_config(cfg, depot, download={"chunk_size_mb": 4, "chunk_concurrency": 6,
                                        "request_timeout_seconds": 300})
    r = c.post("/api/config/reload", params={"token": ADMIN})
    assert r.status_code == 200, r.text
    data = c.get("/api/auth/permissions").json()["data"]["download"]
    assert data == {"chunk_size": 4 * 1024 * 1024, "chunk_concurrency": 6,
                    "request_timeout_seconds": 300}


# --------------------------------------------------------------------------
# 3. HEAD 探测
# --------------------------------------------------------------------------

def test_head_download_headers_audit_and_metric(ctx, files):
    c, _, depot, _ = ctx
    data, sha = files
    before = _download_metric()
    r = c.head(DOWNLOAD, params={"bucket": "b", "key": "big.bin", "token": ADMIN})
    assert r.status_code == 200, r.text
    assert r.content == b""
    assert r.headers["content-length"] == str(len(data))
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"] == f'"{sha}"'
    assert 'filename="big.bin"' in r.headers["content-disposition"]
    assert _download_metric() == before, "HEAD 不应计为一次下载"

    rows = [x for x in _audit_rows(depot) if x.get("action") == "download"]
    assert rows and rows[-1].get("head") is True
    assert rows[-1].get("size") == 0


def test_head_missing_and_auth(ctx):
    c, _, _, _ = ctx
    assert c.head(DOWNLOAD, params={"bucket": "b", "key": "nope", "token": ADMIN}).status_code == 404
    assert c.head(DOWNLOAD, params={"bucket": "b", "key": "nope"}).status_code == 401


def test_head_with_bearer(ctx, files):
    """桌面客户端/网页用 Authorization: Bearer 探测，不把 token 放 URL。"""
    c, _, _, _ = ctx
    data, _ = files
    r = c.head(DOWNLOAD, params={"bucket": "b", "key": "big.bin"},
               headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(data))
    assert r.headers["accept-ranges"] == "bytes"


# --------------------------------------------------------------------------
# 4. Range 语义与审计
# --------------------------------------------------------------------------

def test_single_range_206_content_and_audit(ctx, files):
    c, _, depot, _ = ctx
    data, _ = files
    r = c.get(DOWNLOAD, params={"bucket": "b", "key": "big.bin", "token": ADMIN},
              headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{len(data)}"
    assert r.headers["content-length"] == "100"
    assert r.content == data[100:200]

    rows = [x for x in _audit_rows(depot) if x.get("action") == "download"]
    last = rows[-1]
    assert last.get("partial") is True
    assert last.get("range") == "bytes=100-199"
    assert last.get("size") == 100


def test_range_variants(ctx, files):
    c, _, depot, _ = ctx
    data, _ = files
    q = {"bucket": "b", "key": "big.bin", "token": ADMIN}
    # 首段 / 尾段 / 单字节 / 后缀
    for header, expected in [
        ("bytes=0-9", data[0:10]),
        (f"bytes={len(data) - 5}-", data[-5:]),
        ("bytes=0-0", data[0:1]),
        ("bytes=-10", data[-10:]),
    ]:
        r = c.get(DOWNLOAD, params=q, headers={"Range": header})
        assert r.status_code == 206, header
        assert r.content == expected, header
        assert r.headers["content-range"].endswith(f"/{len(data)}")
    rows = [x for x in _audit_rows(depot) if x.get("action") == "download" and x.get("partial")]
    assert [x["size"] for x in rows[-4:]] == [10, 5, 1, 10]


def test_range_unsatisfiable_and_multiple(ctx, files):
    c, _, depot, _ = ctx
    data, _ = files
    q = {"bucket": "b", "key": "big.bin", "token": ADMIN}
    r = c.get(DOWNLOAD, params=q, headers={"Range": f"bytes={len(data) + 10}-"})
    assert r.status_code == 416
    assert "content-range" in {k.lower() for k in r.headers}
    # 多区间：Starlette 返回 multipart/byteranges；不得 500
    r = c.get(DOWNLOAD, params=q, headers={"Range": "bytes=0-9,20-29"})
    assert r.status_code == 206
    assert r.headers["content-type"].startswith("multipart/byteranges")
    # 不可满足的区间审计 size=0（仍写审计，便于追查）
    rows = [x for x in _audit_rows(depot) if x.get("action") == "download"]
    assert rows[-2].get("size") == 0 and rows[-2].get("partial") is True


def test_full_get_audit_size_and_metric(ctx, files):
    c, _, depot, _ = ctx
    data, _ = files
    before = _download_metric()
    r = c.get(DOWNLOAD, params={"bucket": "b", "key": "big.bin", "token": ADMIN})
    assert r.status_code == 200
    assert r.content == data
    assert _download_metric() == before + 1
    rows = [x for x in _audit_rows(depot) if x.get("action") == "download"]
    assert rows[-1].get("size") == len(data)
    assert not rows[-1].get("partial")


def test_range_304_still_works(ctx, files):
    c, _, _, _ = ctx
    _, sha = files
    r = c.get(DOWNLOAD, params={"bucket": "b", "key": "big.bin", "token": ADMIN},
              headers={"If-None-Match": f'"{sha}"'})
    assert r.status_code == 304


# --------------------------------------------------------------------------
# 5. 并发多 Range（模拟桌面客户端/网页并行下载）
# --------------------------------------------------------------------------

def test_concurrent_ranges_correct(ctx, files):
    """4 路并发 Range：每段内容正确、拼接后与源文件一致、审计 4 条 partial。"""
    c, _, depot, _ = ctx
    data, _ = files

    import httpx

    async def fetch_all():
        from artifactdepot.main import app
        transport = httpx.ASGITransport(app=app)
        size = len(data)
        step = 3000
        spans = [(s, min(s + step, size) - 1) for s in range(0, size, step)]

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            async def fetch(span):
                s, e = span
                r = await ac.get(DOWNLOAD, params={"bucket": "b", "key": "big.bin"},
                                 headers={"Authorization": f"Bearer {ADMIN}",
                                          "Range": f"bytes={s}-{e}"})
                return s, e, r

            return spans, await asyncio.gather(*[fetch(s) for s in spans])

    spans, results = asyncio.run(fetch_all())
    parts = {}
    for s, e, r in results:
        assert r.status_code == 206, r.text
        assert r.headers["content-range"] == f"bytes {s}-{e}/{len(data)}"
        assert len(r.content) == e - s + 1
        parts[s] = r.content
    rebuilt = b"".join(parts[s] for s, _ in spans)
    assert rebuilt == data

    rows = [x for x in _audit_rows(depot) if x.get("action") == "download" and x.get("partial")]
    assert len(rows) >= len(spans)
    assert all(x.get("size", 0) > 0 for x in rows[-len(spans):])


def test_threaded_clients_head_and_range(ctx, files):
    """多线程独立 TestClient 并发 HEAD/Range，不串内容、不 500。"""
    c, _, _, _ = ctx
    data, _ = files

    def one(i: int):
        from starlette.testclient import TestClient
        from artifactdepot.main import app
        with TestClient(app) as tc:
            h = tc.head(DOWNLOAD, params={"bucket": "b", "key": "big.bin", "token": ADMIN})
            s = i * 100
            r = tc.get(DOWNLOAD, params={"bucket": "b", "key": "big.bin", "token": ADMIN},
                       headers={"Range": f"bytes={s}-{s + 99}"})
            return h.status_code, r.status_code, r.content

    with ThreadPoolExecutor(max_workers=4) as ex:
        outs = list(ex.map(one, range(4)))
    for i, (hs, rs, body) in enumerate(outs):
        assert hs == 200 and rs == 206
        assert body == data[i * 100:(i * 100) + 100]


# --------------------------------------------------------------------------
# 6. 权限目录保持不新增 scope
# --------------------------------------------------------------------------

def test_permission_catalog_unchanged(ctx):
    c, _, _, _ = ctx
    d = c.get("/api/auth/permissions").json()["data"]
    assert len(d["permissions"]) == 22
    keys = {p["key"] for p in d["permissions"]}
    assert "object:download" in keys
    assert not any(k.startswith("download:") for k in keys), "分块下载复用 object:download，不新增权限点"


def test_range_auth_scope_enforced(ctx, files):
    """只有 object:list 的 token 不能下载（即使带 Range）。"""
    c, _, _, _ = ctx
    tok = c.post("/api/tokens", params={"token": ADMIN},
                 json={"user": "reader", "role": "viewer"}).json()["data"]["token"]
    r = c.get(DOWNLOAD, params={"bucket": "b", "key": "big.bin", "token": tok},
              headers={"Range": "bytes=0-9"})
    assert r.status_code == 403


def test_oversized_key_rejected_not_500(ctx):
    """超长 key 必须 400（底层 stat 会抛 ENAMETOOLONG，历史表现为 500）；HEAD/GET 一致。"""
    c, _, _, _ = ctx
    key = "x" * 5000
    for method in ("GET", "HEAD"):
        r = c.request(method, DOWNLOAD,
                      params={"bucket": "b", "key": key, "token": ADMIN})
        assert r.status_code == 400, f"{method} 超长 key 应 400，实际 {r.status_code}"
        if method == "GET":          # HEAD 响应无 body
            assert "过长" in r.text
