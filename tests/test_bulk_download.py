# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""一键下载目录（单文件直下 / 流式 ZIP）后端回归测试 —— 契约 §2.4 的 11 项

覆盖：
  1. 单文件 → 直下：200、原文件名/原 Content-Type、非 zip、Range 206、ETag
  2. 两个文件 → ZIP：application/zip、顶层 root/ 文件夹、内容正确
  3. 1 文件 + 子目录（嵌套/空目录）→ ZIP：目录条目（含空目录）与嵌套文件
  4. 隐藏文件/隐藏目录/.keep/.warehouse.json 不进 ZIP；符号链接不打包、不跟随
  5. 顶层文件夹命名：bucket 根 = bucket 名；子目录 prefix = 最后一段（含中文）
  6. 空目录/prefix 不存在/prefix 是文件/bucket 不存在 → 404
  7. 401（无/坏 token）、403（缺 object:download / 资源范围越界）、200（范围内/管理员/Bearer）
  8. 非法 prefix（../、隐藏段）→ 400；跨 bucket / 越出仓库的符号链接 prefix → 400
  9. 审计：直下 download + extra.bulk，ZIP download_bulk + extra{mode,files,dirs,aborted}；
     DOWNLOADS 每成功请求 +1；只读（不产生临时 zip）
 10. UI 标记：index.html 含 data-action="download-dir"，app.js 含下载端点
 11. 全量回归由命令行执行（见任务说明）；另测 100000 项上限与流式中断

运行：
    cd ArtifactDepot/ArtifactDepot
    ../.testenv/bin/python -m pytest -q tests/test_bulk_download.py
"""
import asyncio
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "bulk-download-admin-0123456789"
BULK = "/api/objects/download-bulk"

# 状态文件不属于业务数据，只读性快照对比时排除
_SNAP_EXCLUDE = {"tokens.json", "audit.log", "signed_links.json", ".state.lock"}


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

def _write_config(cfg: Path, depot: Path) -> None:
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "ui_enabled": True, "max_upload_mb": 0,
        "require_read_token": False,
    }), encoding="utf-8")


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
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, depot, tmp_path
    config._CONFIG = None


def _audit(depot: Path) -> list:
    p = depot / "audit.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _snapshot(depot: Path) -> dict:
    """业务数据快照：目录 + 文件内容（排除 tokens/audit/signed_links/锁文件）。"""
    out = {}
    for p in sorted(depot.rglob("*")):
        rel = p.relative_to(depot).as_posix()
        if rel in _SNAP_EXCLUDE:
            continue
        if p.is_dir():
            out[rel + "/"] = None
        elif p.is_file():
            out[rel] = p.read_bytes()
    return out


def _metric(c, name: str) -> float:
    for line in c.get("/metrics").text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _detail(resp) -> str:
    try:
        return str(resp.json().get("detail", ""))
    except Exception:
        return resp.text


def _mkbucket(c, name: str) -> None:
    r = c.post("/api/buckets", params={"token": ADMIN}, json={"bucket": name})
    assert r.status_code == 200, r.text


def _up(c, bucket: str, key: str, data: bytes = b"x") -> None:
    r = c.post("/api/objects", params={"token": ADMIN},
               data={"bucket": bucket, "key": key},
               files={"file": (Path(key).name, data)})
    assert r.status_code == 200, r.text


def _mkdir(c, bucket: str, key: str) -> None:
    r = c.post("/api/objects/mkdir", params={"token": ADMIN},
               json={"bucket": bucket, "key": key})
    assert r.status_code == 200, r.text


def _token(c, user: str, scopes=None, buckets=None, prefixes=None) -> str:
    body = {"user": user, "role": "custom"}
    if scopes is not None:
        body["scopes"] = scopes
    if buckets is not None:
        body["allow_buckets"] = buckets
    if prefixes is not None:
        body["allow_prefixes"] = prefixes
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _dl(c, bucket: str, prefix: str = "", token: str = ADMIN,
        headers=None):
    params = {"bucket": bucket}
    if prefix:
        params["prefix"] = prefix
    if token is not None:
        params["token"] = token
    return c.get(BULK, params=params, headers=headers or {})


def _zip_of(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip"), resp.headers
    return zipfile.ZipFile(io.BytesIO(resp.content))


# ===========================================================================
# 1. 单文件直下
# ===========================================================================

def test_single_file_direct_download(ctx):
    """仅有 1 个可见文件且无子目录：原文件名直下，支持 Range 与 ETag。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "only.txt", b"HELLO-DIRECT")

    snap = _snapshot(depot)
    n_audit = len(_audit(depot))
    d0 = _metric(c, "artifactdepot_objects_downloaded_total")

    r = _dl(c, "demo")
    assert r.status_code == 200, r.text
    assert r.content == b"HELLO-DIRECT"
    assert r.headers["content-type"].startswith("text/plain")
    disp = r.headers["content-disposition"]
    assert 'filename="only.txt"' in disp
    assert not r.content.startswith(b"PK"), "单文件不应返回 ZIP"
    assert r.headers.get("etag"), "直下应带 ETag"

    # Range 206
    r = _dl(c, "demo", headers={"Range": "bytes=0-4"})
    assert r.status_code == 206 and r.content == b"HELLO"
    assert r.headers["content-range"] == "bytes 0-4/12"

    # 审计：action=download + extra.bulk=true / mode=file
    rows = _audit(depot)
    assert len(rows) == n_audit + 2
    for row in rows[n_audit:]:
        assert row["action"] == "download"
        assert row["bucket"] == "demo" and row["key"] == "only.txt"
        assert row["size"] == len(b"HELLO-DIRECT")
        assert row["bulk"] is True and row["mode"] == "file"
    # 指标：每次成功直下 +1
    assert _metric(c, "artifactdepot_objects_downloaded_total") == d0 + 2
    # 只读：不产生任何临时文件
    assert _snapshot(depot) == snap


# ===========================================================================
# 2. 多文件 ZIP（流式）
# ===========================================================================

def test_two_files_zip_streaming(ctx):
    """两个可见文件 → 流式 ZIP：顶层 root/ 文件夹 + 文件内容正确，无 Content-Length。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "a.txt", b"AAA")
    _up(c, "demo", "b.txt", b"BBBB")
    n_audit = len(_audit(depot))
    d0 = _metric(c, "artifactdepot_objects_downloaded_total")
    snap = _snapshot(depot)

    r = _dl(c, "demo")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")
    assert "attachment" in r.headers["content-disposition"]
    assert "content-length" not in {k.lower() for k in r.headers}
    z = _zip_of(r)
    names = z.namelist()
    assert names[0] == "demo/"
    assert "demo/a.txt" in names and "demo/b.txt" in names
    assert z.read("demo/a.txt") == b"AAA"
    assert z.read("demo/b.txt") == b"BBBB"

    # 审计：download_bulk + size=实际发送字节数 + extra
    row = _audit(depot)[-1]
    assert len(_audit(depot)) == n_audit + 1
    assert row["action"] == "download_bulk"
    assert row["bucket"] == "demo" and row["key"] == ""
    assert row["size"] == len(r.content)
    assert row["mode"] == "zip" and row["files"] == 2 and row["dirs"] == 0
    assert row["aborted"] is False
    assert _metric(c, "artifactdepot_objects_downloaded_total") == d0 + 1
    assert _snapshot(depot) == snap, "ZIP 下载必须是只读、不得落临时文件"


# ===========================================================================
# 3. 含子目录 / 空目录的 ZIP
# ===========================================================================

def test_zip_with_subdirs_and_empty_dir(ctx):
    """1 文件 + 子目录（嵌套/空目录）：目录条目（含空目录）与嵌套文件都进 ZIP。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "sub/nested.txt", b"NESTED")
    _up(c, "demo", "sub/deep/inner.bin", b"INNER")
    _mkdir(c, "demo", "sub/empty")            # 只有隐藏 .keep，应作为空目录条目出现
    n_audit = len(_audit(depot))

    r = _dl(c, "demo", prefix="sub")
    z = _zip_of(r)
    names = z.namelist()
    assert names[0] == "sub/"
    assert "sub/deep/" in names and "sub/empty/" in names
    assert "sub/deep/inner.bin" in names and "sub/nested.txt" in names
    assert z.read("sub/deep/inner.bin") == b"INNER"
    assert z.read("sub/nested.txt") == b"NESTED"
    # 空目录条目的 is_dir 语义
    assert z.getinfo("sub/empty/").is_dir() is True
    # 隐藏 .keep 不进 ZIP
    assert not any(n.endswith(".keep") for n in names)
    # 文件条目保留源文件 mtime（不再是 zipfile 默认的 1980-01-01）
    assert z.getinfo("sub/nested.txt").date_time[0] >= 2020

    row = _audit(depot)[n_audit]
    assert row["action"] == "download_bulk" and row["key"] == "sub"
    assert row["mode"] == "zip" and row["files"] == 2 and row["dirs"] == 2

    # 边界：恰好 1 个可见文件 + 1 个可见子目录 → 仍是 ZIP（不是直下）
    _mkbucket(c, "oneplusdir")
    _up(c, "oneplusdir", "only.txt", b"ONLY")
    _mkdir(c, "oneplusdir", "emptydir")
    r = _dl(c, "oneplusdir")
    z = _zip_of(r)
    assert z.namelist() == ["oneplusdir/", "oneplusdir/emptydir/", "oneplusdir/only.txt"]
    assert "content-disposition" in r.headers and "attachment" in r.headers["content-disposition"]


# ===========================================================================
# 4. 隐藏项 / 清单 / 符号链接
# ===========================================================================

def test_hidden_items_and_symlinks_skipped(ctx):
    """隐藏文件/目录、.keep、.warehouse.json 不进 ZIP；符号链接不打包、不跟随。"""
    c, depot, tmp_path = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "visible.txt", b"VISIBLE")
    _up(c, "demo", "sub/ok.txt", b"OK")
    (depot / "demo" / ".hidden").write_bytes(b"HIDDEN-CONTENT")
    (depot / "demo" / ".hdir").mkdir()
    (depot / "demo" / ".hdir" / "secret.txt").write_bytes(b"HIDDEN-DIR-CONTENT")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"OUTSIDE-TARGET")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "target.txt").write_bytes(b"OUTSIDE-DIR-TARGET")
    os.symlink(outside_file, depot / "demo" / "link.txt")
    os.symlink(outside_dir, depot / "demo" / "dirlink", target_is_directory=True)

    r = _dl(c, "demo")
    z = _zip_of(r)
    names = z.namelist()
    assert "demo/visible.txt" in names and "demo/sub/ok.txt" in names
    for bad in ("demo/.hidden", "demo/.keep", "demo/.warehouse.json",
                "demo/link.txt", "demo/dirlink", "demo/.hdir/",
                "demo/.hdir/secret.txt"):
        assert bad not in names, bad
    # 链接目标内容绝不进入 ZIP（未跟随）
    assert b"OUTSIDE-TARGET" not in r.content
    assert b"OUTSIDE-DIR-TARGET" not in r.content
    assert b"HIDDEN-CONTENT" not in r.content
    assert b"HIDDEN-DIR-CONTENT" not in r.content
    # 符号链接与原文件均未被动过（只读）
    assert (depot / "demo" / "link.txt").is_symlink()
    assert outside_file.read_bytes() == b"OUTSIDE-TARGET"


# ===========================================================================
# 5. 顶层文件夹命名 + 文件名头
# ===========================================================================

def test_root_name_bucket_and_chinese_prefix(ctx):
    """bucket 根 ZIP 顶层为 bucket 名；子目录 prefix 顶层为最后一段（中文用 filename*）。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "f1.txt", b"1")
    _up(c, "demo", "f2.txt", b"2")
    _up(c, "demo", "task/交付物/x.txt", b"X")
    _up(c, "demo", "task/交付物/y.txt", b"Y")

    z = _zip_of(_dl(c, "demo"))
    assert z.namelist()[0] == "demo/"

    r = _dl(c, "demo", prefix="task/交付物")
    z = _zip_of(r)
    names = z.namelist()
    assert names[0] == "交付物/"
    assert "交付物/x.txt" in names and "交付物/y.txt" in names
    disp = r.headers["content-disposition"]
    assert 'filename="download.zip"' in disp, disp       # 中文目录的 ASCII 回退
    assert "filename*=UTF-8''" in disp
    # RFC 5987：中文按 UTF-8 百分号编码
    assert "%E4%BA%A4%E4%BB%98%E7%89%A9.zip" in disp


# ===========================================================================
# 6. 404 场景
# ===========================================================================

def test_empty_and_missing_404(ctx):
    """空目录 / 不存在 / prefix 是文件 / bucket 不存在 → 404。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")                         # 只有隐藏 .keep → 空目录
    _up(c, "demo", "f.txt", b"F")
    (depot / "demo" / "rawempty").mkdir()        # 真的空目录（无 .keep）

    r = _dl(c, "demo", prefix="rawempty")
    assert r.status_code == 404 and "为空" in _detail(r)
    assert r.status_code != 200
    assert _dl(c, "demo", prefix="nope").status_code == 404
    assert _dl(c, "demo", prefix="f.txt").status_code == 404
    assert _dl(c, "ghost").status_code == 404
    # 空 bucket 根（仅 .keep）也为 404，不返回 0 字节 zip
    _mkbucket(c, "emptybucket")
    r = _dl(c, "emptybucket")
    assert r.status_code == 404 and "为空" in _detail(r)


# ===========================================================================
# 7. 鉴权 / 权限 / 资源范围
# ===========================================================================

def test_auth_scope_matrix(ctx):
    """401/403/200 矩阵；范围内可下载；管理员与 Bearer 均可。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _mkbucket(c, "secret")
    _up(c, "demo", "a/1.txt", b"ONE")
    _up(c, "demo", "a/2.txt", b"TWO")
    _up(c, "demo", "b/3.txt", b"THREE")
    _up(c, "secret", "s.txt", b"SECRET")
    snap = _snapshot(depot)

    # 无 token / 坏 token → 401
    assert _dl(c, "demo", token=None).status_code == 401
    assert _dl(c, "demo", token="bad-token").status_code == 401

    # 缺 object:download → 403
    nobody = _token(c, "nobody", scopes=["object:list"])
    r = _dl(c, "demo", token=nobody)
    assert r.status_code == 403 and "object:download" in _detail(r)

    # 有 scope 且范围内：prefix=a/（2 文件 → ZIP）
    actor = _token(c, "actor", scopes=["object:download"],
                   buckets=["demo"], prefixes={"demo": ["a/"]})
    r = _dl(c, "demo", prefix="a", token=actor)
    z = _zip_of(r)
    assert "a/1.txt" in z.namelist() and "a/2.txt" in z.namelist()

    # 资源范围外：越界前缀 / 根目录 / 越界 bucket → 403，且数据不变
    n_audit = len(_audit(depot))
    for params in (("demo", "b"), ("demo", ""), ("secret", "")):
        r = _dl(c, params[0], prefix=params[1], token=actor)
        assert r.status_code == 403, (params, r.text)
        assert "资源范围" in _detail(r)
    assert _snapshot(depot) == snap
    assert len(_audit(depot)) == n_audit, "403 不得写审计"

    # 管理员共享 token → 200；Authorization: Bearer 也可
    r = _dl(c, "demo", token=None, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/zip")
    # 单文件 scope 范围内直下也正常
    single = _token(c, "single", scopes=["object:download"],
                    buckets=["secret"], prefixes={"secret": []})
    assert _dl(c, "secret", token=single).status_code == 200


# ===========================================================================
# 8. 400：非法 prefix / 跨 bucket 符号链接
# ===========================================================================

def test_invalid_prefix_and_symlink_escape_400(ctx):
    """穿越/隐藏段 prefix → 400；符号链接 prefix 解析到 bucket 外 → 400（防跨 bucket）。"""
    c, depot, tmp_path = ctx
    _mkbucket(c, "demo")
    _mkbucket(c, "secret")
    _up(c, "demo", "f.txt", b"F")
    _up(c, "secret", "s.txt", b"SECRET")
    snap = _snapshot(depot)

    for bad in ("../", "a/../b", ".hidden", "sub/.hidden"):
        r = _dl(c, "demo", prefix=bad)
        assert r.status_code == 400, (bad, r.text)
    assert _snapshot(depot) == snap

    # 跨 bucket 符号链接
    os.symlink(depot / "secret", depot / "demo" / "linkdir")
    r = _dl(c, "demo", prefix="linkdir")
    assert r.status_code == 400 and "bucket" in _detail(r)
    # 指向仓库外的符号链接同样 400
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    os.symlink(outside, depot / "demo" / "outlink")
    assert _dl(c, "demo", prefix="outlink").status_code == 400
    # 文件没有泄漏出去
    assert b"SECRET" not in r.content


# ===========================================================================
# 9. 审计 / 指标 / 只读性（失败不计数）
# ===========================================================================

def test_audit_metrics_and_failures_not_counted(ctx):
    """ZIP/直下审计字段正确；失败（403/404）不写审计、不加指标。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "x.txt", b"XX")
    _up(c, "demo", "y.txt", b"YYY")
    n_audit = len(_audit(depot))
    d0 = _metric(c, "artifactdepot_objects_downloaded_total")

    # 失败请求：不写审计、不加指标
    assert _dl(c, "demo", prefix="nope").status_code == 404
    assert _dl(c, "demo", token="bad").status_code == 401
    assert len(_audit(depot)) == n_audit
    assert _metric(c, "artifactdepot_objects_downloaded_total") == d0

    # ZIP 成功：一条 download_bulk + 指标 +1
    r = _dl(c, "demo")
    assert r.status_code == 200
    rows = _audit(depot)
    assert len(rows) == n_audit + 1
    row = rows[-1]
    assert row["action"] == "download_bulk" and row["mode"] == "zip"
    assert row["files"] == 2 and row["dirs"] == 0 and row["aborted"] is False
    assert row["size"] == len(r.content)
    assert _metric(c, "artifactdepot_objects_downloaded_total") == d0 + 1

    # 直下（单文件 bucket）：download + extra.bulk
    _mkbucket(c, "solo")
    _up(c, "solo", "solo.bin", b"SOLO")
    r = _dl(c, "solo")
    assert r.status_code == 200 and r.content == b"SOLO"
    row = _audit(depot)[-1]
    assert row["action"] == "download" and row["bulk"] is True
    assert row["mode"] == "file" and row["key"] == "solo.bin" and row["size"] == 4
    assert _metric(c, "artifactdepot_objects_downloaded_total") == d0 + 2


# ===========================================================================
# 10. UI 标记
# ===========================================================================

def test_ui_markers():
    """前端已加入「下载本目录」按钮并调用 download-bulk 端点。"""
    base = ROOT / "src" / "artifactdepot" / "resources"
    html = (base / "index.html").read_text(encoding="utf-8")
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-action="download-dir"' in html
    assert "/api/objects/download-bulk" in js


# ===========================================================================
# 附加：项数上限 / 大文件分块 / 流式中断
# ===========================================================================

def test_item_limit_400_and_not_counted(ctx, monkeypatch):
    """files+dirs 超过 MAX_BULK_DOWNLOAD_ITEMS → 400，且不写审计/不加指标。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    _up(c, "demo", "1.txt", b"1")
    _up(c, "demo", "2.txt", b"2")
    from artifactdepot import storage
    monkeypatch.setattr(storage, "MAX_BULK_DOWNLOAD_ITEMS", 1)
    n_audit = len(_audit(depot))
    d0 = _metric(c, "artifactdepot_objects_downloaded_total")
    r = _dl(c, "demo")
    assert r.status_code == 400 and "过大" in _detail(r)
    assert len(_audit(depot)) == n_audit
    assert _metric(c, "artifactdepot_objects_downloaded_total") == d0


def test_large_file_zip_integrity(ctx):
    """> READ_CHUNK 的大文件按固定块读取，ZIP 内容逐字节一致（不 read() 全量）。"""
    c, depot, _ = ctx
    _mkbucket(c, "demo")
    big = bytes(range(256)) * (700 * 1024 // 256)      # 700 KiB，跨多个 256KiB 块
    _up(c, "demo", "big.bin", big)
    _up(c, "demo", "small.txt", b"SMALL")
    r = _dl(c, "demo")
    z = _zip_of(r)
    assert z.read("demo/big.bin") == big
    assert z.read("demo/small.txt") == b"SMALL"
    assert _audit(depot)[-1]["files"] == 2


def test_zip_stream_abort_marks_audit_once(tmp_path):
    """直接关闭异步生成器（模拟客户端中断）：审计恰好一次且 aborted=true。"""
    from artifactdepot import zipstream
    # 文件明显大于队列缓冲（READ_CHUNK * QUEUE_MAX = 2 MiB），保证中断发生在写文件中途
    size = 4 * 1024 * 1024
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * size)
    entries = [{"name": "f.bin", "path": p, "is_dir": False, "size": size}]
    calls = []

    def on_finish(sent, files, dirs, aborted):
        calls.append((sent, files, dirs, aborted))

    async def run():
        agen = zipstream.zip_stream(entries, "root", on_finish)
        chunk = await agen.__anext__()
        assert chunk
        await agen.aclose()

    asyncio.run(run())
    assert len(calls) == 1, calls
    sent, files, dirs, aborted = calls[0]
    assert aborted is True
    assert files == 0, "中断时尚未完成任何文件条目"
    assert sent > 0
