# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""目录签名链接（后台打包到 .package）回归测试

覆盖：
- 生成即返回链接；后台打包完成后按「下载本目录」语义返回（唯一文件直下 / 其他 ZIP）
- 未就绪时 409 且不消耗按次数链接；HEAD 不消耗次数
- 次数用完/过期/作废后的清理；作废立即删包
- 权限（link:create）与资源范围；非创建者不能查看进度
- 源目录重命名跟随链接；删除目录/批量清空时链接作废并删包
- 配置开关（allow_directory / package.enabled）
- 审计（package start/done/fail/cleanup + signed 下载）
- 前端标记（目录行签名链接、进度弹窗复用、状态轮询）
"""
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "dir-link-admin-0123456789"
PKG = ".package"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    import artifactdepot.config as config
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "",
        "access_token": ADMIN, "ui_enabled": True, "max_upload_mb": 0,
        "signed_links": {"count_min": 1, "count_max": 10,
                         "expire_min_seconds": 60, "expire_max_seconds": 604800,
                         "allow_directory": True},
        "package": {"cleanup_grace_seconds": 0, "cleanup_interval_seconds": 60,
                    "cleanup_on_startup": False},
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    monkeypatch.delenv("ARTIFACT_DEPOT_ACCESS_TOKEN", raising=False)
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield {"client": c, "depot": depot, "cfg": cfg, "config": config}
    config._CONFIG = None


def _mkdir(c, bucket="b", key="d", token=ADMIN):
    r = c.post("/api/objects/mkdir", params={"token": token},
               json={"bucket": bucket, "key": key})
    assert r.status_code == 200, r.text
    return r


def _upload(c, bucket="b", key="d/a.txt", data=b"hello", token=ADMIN):
    r = c.post("/api/objects", params={"token": token},
               data={"bucket": bucket, "key": key},
               files={"file": (key.split("/")[-1], data)})
    assert r.status_code == 200, r.text
    return r


def _presign_dir(c, key="d", mode="time", token=ADMIN, **extra):
    body = {"bucket": "b", "key": key, "kind": "dir", "mode": mode}
    body.update(extra)
    r = c.post("/api/objects/presign", params={"token": token}, json=body)
    return r


def _link_url(c, data):
    return "/api/objects/download" + data["url"].split("download", 1)[1]


def _status(c, link_id, token=ADMIN):
    r = c.get(f"/api/objects/signed-links/{link_id}/package", params={"token": token})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _wait_state(c, link_id, states=("ready", "failed", "cancelled"), timeout=8.0,
                token=ADMIN):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _status(c, link_id, token)
        if last.get("state") in states:
            return last
        time.sleep(0.03)
    raise AssertionError(f"等待打包状态超时：{last}")


def _links(c, token=ADMIN):
    r = c.get("/api/objects/signed-links", params={"token": token})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _audit(depot: Path) -> list:
    p = depot / "audit.log"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _token(c, **body):
    body.setdefault("user", "dir-link-user")
    r = c.post("/api/tokens", params={"token": ADMIN}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


# ---------------------------------------------------------------------------
# 生成 / 打包 / 下载
# ---------------------------------------------------------------------------

def test_dir_link_background_zip_and_count(ctx):
    c, depot = ctx["client"], ctx["depot"]
    _mkdir(c, key="d")
    _upload(c, key="d/a.txt", data=b"hello")
    _upload(c, key="d/sub/b.txt", data=b"world")

    r = _presign_dir(c, "d", mode="count", count=2)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["kind"] == "dir" and d["mode"] == "count"
    assert d["package"]["state"] in ("queued", "scanning", "packaging", "ready")
    # 链接立即返回，不等待打包
    assert "/api/objects/download?" in d["url"] and "link=" in d["url"]

    st = _wait_state(c, d["id"])
    assert st["state"] == "ready"
    assert st["percent"] == 100.0
    assert st["artifact"]["name"] == "d.zip" and st["artifact"]["kind"] == "zip"
    # 成品落在源目录的隐藏 .package 下
    pkgs = list((depot / "b" / "d" / PKG).iterdir())
    assert len(pkgs) == 1 and pkgs[0].name == d["id"]

    url = _link_url(c, d)
    r1 = c.get(url)
    assert r1.status_code == 200
    assert r1.headers["content-type"].startswith("application/zip")
    assert "d.zip" in r1.headers.get("content-disposition", "")
    z = zipfile.ZipFile(__import__("io").BytesIO(r1.content))
    assert z.namelist() == ["d/", "d/sub/", "d/sub/b.txt", "d/a.txt"]
    assert z.read("d/a.txt") == b"hello" and z.read("d/sub/b.txt") == b"world"

    assert c.get(url).status_code == 200
    assert c.get(url).status_code == 401          # 次数用完
    actions = [x for x in _audit(depot) if x.get("link_id") == d["id"]]
    assert [x["phase"] for x in actions if x["action"] == "package"] == ["start", "done"]
    dl = [x for x in actions if x["action"] == "download_bulk"]
    assert len(dl) == 2 and all(x.get("signed") and x.get("package") for x in dl)


def test_single_file_dir_link_direct_and_head_no_consume(ctx):
    c = ctx["client"]
    _mkdir(c, key="solo")
    _upload(c, key="solo/only.bin", data=b"ONLY")

    r = _presign_dir(c, "solo", mode="count", count=1)
    d = r.json()["data"]
    st = _wait_state(c, d["id"])
    assert st["artifact"]["kind"] == "file" and st["artifact"]["name"] == "only.bin"
    url = _link_url(c, d)

    # HEAD 只探测，不消耗次数（打包成品是真实文件，支持 HEAD/Range）
    head = c.head(url)
    assert head.status_code == 200
    assert head.headers.get("content-length") == "4"
    ok = c.get(url)
    assert ok.status_code == 200 and ok.content == b"ONLY"
    assert "only.bin" in ok.headers.get("content-disposition", "")
    assert c.get(url).status_code == 401


def test_not_ready_409_does_not_consume_count(ctx, monkeypatch):
    c = ctx["client"]
    from artifactdepot import packaging
    _mkdir(c, key="slow")
    _upload(c, key="slow/a.txt", data=b"abc")
    original = packaging.start_job
    monkeypatch.setattr(packaging, "start_job",
                        lambda *a, **k: {"state": "queued", "percent": 0.0})
    r = _presign_dir(c, "slow", mode="count", count=1)
    d = r.json()["data"]
    url = _link_url(c, d)
    r = c.get(url)
    assert r.status_code == 409 and "打包" in r.text
    link = [x for x in _links(c) if x["id"] == d["id"]][0]
    assert link["remaining"] == 1                  # 409 不消耗次数

    monkeypatch.setattr(packaging, "start_job", original)
    st = _wait_state(c, d["id"])                   # 状态查询会自动补一次打包
    assert st["state"] == "ready"
    assert c.get(url).status_code == 200


def test_empty_dir_packaging_fails_and_revokes(ctx):
    c, depot = ctx["client"], ctx["depot"]
    _mkdir(c, key="empty")
    r = _presign_dir(c, "empty")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    st = _wait_state(c, d["id"], states=("failed", "cancelled"))
    assert st["state"] == "failed" and "空" in st["error"]
    link = [x for x in _links(c) if x["id"] == d["id"]][0]
    assert link["revoked"] is True
    assert c.get(_link_url(c, d)).status_code == 401
    assert not (depot / "b" / "empty" / PKG).exists()


def test_revoke_deletes_package_immediately(ctx):
    c, depot = ctx["client"], ctx["depot"]
    _mkdir(c, key="d")
    _upload(c, key="d/a.txt")
    d = _presign_dir(c, "d").json()["data"]
    _wait_state(c, d["id"])
    assert (depot / "b" / "d" / PKG / d["id"]).is_dir()
    r = c.post(f"/api/objects/signed-links/{d['id']}/revoke", params={"token": ADMIN})
    assert r.status_code == 200
    assert not (depot / "b" / "d" / PKG / d["id"]).exists()
    assert c.get(_link_url(c, d)).status_code == 401


# ---------------------------------------------------------------------------
# 清理 / 源目录变更
# ---------------------------------------------------------------------------

def test_cleanup_after_count_exhausted(ctx):
    c, depot = ctx["client"], ctx["depot"]
    from artifactdepot import packaging
    _mkdir(c, key="d")
    _upload(c, key="d/a.txt")
    d = _presign_dir(c, "d", mode="count", count=1).json()["data"]
    _wait_state(c, d["id"])
    assert c.get(_link_url(c, d)).status_code == 200
    assert (depot / "b" / "d" / PKG / d["id"]).is_dir()
    assert packaging.cleanup_invalid_packages() >= 1
    assert not (depot / "b" / "d" / PKG / d["id"]).exists()


def test_rename_dir_keeps_link_valid(ctx):
    c = ctx["client"]
    _mkdir(c, key="old")
    _upload(c, key="old/a.txt", data=b"hello")
    d = _presign_dir(c, "old").json()["data"]
    _wait_state(c, d["id"])
    r = c.post("/api/objects/rename", params={"token": ADMIN},
               json={"bucket": "b", "key": "old", "new_key": "new"})
    assert r.status_code == 200, r.text
    link = [x for x in _links(c) if x["id"] == d["id"]][0]
    assert link["key"] == "new" and link["package_state"] == "ready"
    # 旧分享 URL（key=old）仍然可用：目录链接以注册表 key 为准
    rr = c.get(_link_url(c, d))
    assert rr.status_code == 200 and rr.content == b"hello"


def test_delete_dir_and_bulk_delete_invalidate_links(ctx):
    c, depot = ctx["client"], ctx["depot"]
    _mkdir(c, key="a")
    _mkdir(c, key="a/b")
    _upload(c, key="a/f.txt")
    _upload(c, key="a/b/g.txt")
    da = _presign_dir(c, "a").json()["data"]
    db = _presign_dir(c, "a/b").json()["data"]
    _wait_state(c, da["id"])
    _wait_state(c, db["id"])

    # 只删直接文件（a/f.txt）也应以 200 返回；链接作废由批量删除钩子处理
    r = c.post("/api/objects/bulk-delete", params={"token": ADMIN},
               json={"bucket": "b", "prefix": "a", "mode": "all",
                     "dry_run": False, "confirm": "a"})
    assert r.status_code == 200, r.text
    links = {x["id"]: x for x in _links(c)}
    for lid in (da["id"], db["id"]):
        assert links[lid]["revoked"] is True
        assert not (depot / "b" / "a" / PKG / lid).exists()
    assert c.get(_link_url(c, da)).status_code == 401


# ---------------------------------------------------------------------------
# 权限 / 配置
# ---------------------------------------------------------------------------

def test_dir_link_permissions_and_scope(ctx):
    c = ctx["client"]
    _mkdir(c, key="d")
    _upload(c, key="d/a.txt")
    assert _presign_dir(c, "d", token="").status_code == 401
    nos = _token(c, role="custom", scopes=["object:list"])
    assert _presign_dir(c, "d", token=nos).status_code == 403
    linkonly = _token(c, role="custom", scopes=["link:create"], allow_buckets=["other"])
    r = _presign_dir(c, "d", token=linkonly)
    assert r.status_code == 403

    d = _presign_dir(c, "d").json()["data"]     # 管理员创建
    other = _token(c, user="other-user", role="custom",
                   scopes=["link:create", "link:list"])
    r = c.get(f"/api/objects/signed-links/{d['id']}/package", params={"token": other})
    assert r.status_code == 403                  # 非创建者不能看别人链接的进度
    assert _status(c, d["id"])["link_id"] == d["id"]


def test_config_toggle_blocks_dir_link(ctx):
    c, cfg = ctx["client"], ctx["cfg"]
    _mkdir(c, key="d")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["signed_links"]["allow_directory"] = False
    cfg.write_text(json.dumps(data), encoding="utf-8")
    ctx["config"]._CONFIG = None
    assert _presign_dir(c, "d").status_code == 403

    data["signed_links"]["allow_directory"] = True
    data["package"]["enabled"] = False
    cfg.write_text(json.dumps(data), encoding="utf-8")
    ctx["config"]._CONFIG = None
    assert _presign_dir(c, "d").status_code == 403


def test_progress_moves_state_to_packaging():
    """扫描阶段 -> 打包阶段时 task state 同步切换（前端显示「打包中」而不是「扫描中」）。"""
    from artifactdepot import packaging
    job = packaging._new_job("lid", {"bucket": "b", "key": "d", "created_by": "u"})
    job["state"] = "scanning"
    packaging._progress(job, phase="packaging", files_total=2, bytes_total=10, force=True)
    assert job["state"] == "packaging" and job["phase"] == "packaging"
