# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""0.9.0 服务端：伴随文件本地化 + initiate 续传 回归测试（契约 B9 十项）

覆盖：
  1. initiate 带 bucket/key → 会话落在目标父目录 .artifactdepot-uploads/<id>，不在 /tmp
  2. 不带 bucket/key → 会话落在 <staging>/<id>（legacy）
  3. initiate(resume=true, upload_id) 返回磁盘真实 uploaded；失效/参数不一致 400（detail 含「分片会话」）
  4. initiate(resume=true, bucket/key/size) 自动找回会话；找不到则新建 resumed=false
  5. chunk 落点/.incoming 清理；complete 原子落盘 + 会话删除 + 无残留
  6. 直传临时文件落在目标目录、成功后清理
  7. 列表忽略内部目录；bulk_delete(mode=all) 保留；rename 跳过；ZIP 扫描不含
  8. 重启后目标目录会话保留且可续传；legacy 会话被清理；旧 /tmp/dw_chunks 被清理
  9. 绝对/空闲 TTL 清理（含 registry 更新）
 10. 无 /tmp 残留（系统临时目录无新增文件）
"""
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "upload-resume-admin-0123456789"


@contextmanager
def _client():
    """按当前测试配置启动一个 TestClient（每次都会跑一遍 lifespan：建 staging + 启动扫描）。"""
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def env(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    # 把「系统临时目录」也隔离到 tmp_path：lifespan 会先清 <systmp>/dw_chunks，
    # 之后才把 tempfile.tempdir 指向 staging（退出恢复），因此测试不会碰真实 /tmp。
    monkeypatch.setattr("tempfile.tempdir", str(systmp))
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "ui_enabled": True, "max_upload_mb": 0,
        # 分片 1MB：用很小的数据就能构造多分片会话（避免测试写大文件）
        "upload": {"chunk_size_mb": 1, "max_chunk_mb": 1},
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot import storage
    storage.clear_upload_registry()
    yield {"depot": depot, "systmp": systmp, "tmp": tmp_path,
           "staging": depot / ".staging", "storage": storage,
           "internal": ".artifactdepot-uploads"}
    config._CONFIG = None
    storage.clear_upload_registry()


def _init(c, token=ADMIN, *, form=True, **params):
    """调用 initiate：form=True 走 multipart 表单，False 走 query 参数。"""
    if form and params:
        return c.post("/api/objects/initiate", params={"token": token},
                      data={k: str(v) for k, v in params.items()})
    return c.post("/api/objects/initiate",
                  params={"token": token, **{k: str(v) for k, v in params.items()}})


def _init_ok(c, token=ADMIN, **params):
    r = _init(c, token, **params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _chunk(c, uid: str, index: int, data: bytes, token=ADMIN):
    return c.post("/api/objects/chunk", params={"token": token},
                  data={"upload_id": uid, "index": str(index)},
                  files={"chunk": (f"chunk_{index}.bin", data)})


def _chunk_ok(c, uid: str, index: int, data: bytes) -> dict:
    r = _chunk(c, uid, index, data)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _complete(c, uid: str, bucket: str, key: str, total: int, token=ADMIN):
    return c.post("/api/objects/complete", params={"token": token},
                  data={"upload_id": uid, "bucket": bucket, "key": key,
                        "total_chunks": str(total)})


def _upload(c, bucket: str, key: str, data: bytes, token=ADMIN):
    return c.post("/api/objects", params={"token": token},
                  data={"bucket": bucket, "key": key}, files={"file": (key, data)})


def _detail(resp) -> str:
    try:
        return str(resp.json().get("detail", ""))
    except Exception:
        return resp.text


# ---------------------------------------------------------------------------
# 1) 目标目录会话布局
# ---------------------------------------------------------------------------

def test_target_session_in_target_dir_not_tmp(env):
    top_chunk = 1 * 1024 * 1024
    with _client() as c:
        d = _init_ok(c, bucket="b", key="dir/a.bin", size=3 * top_chunk)
        assert d["staging"] == "target" and d["resumed"] is False
        assert d["chunk_size"] == top_chunk
        assert d["size"] == 3 * top_chunk and d["total_chunks"] == 3
        assert d["uploaded"] == [] and d["received_bytes"] == 0

        sess = env["depot"] / "b" / "dir" / env["internal"] / d["upload_id"]
        assert sess.is_dir(), "会话必须在目标文件父目录的 .artifactdepot-uploads 下"
        meta = json.loads((sess / ".session.json").read_text(encoding="utf-8"))
        for field in ("owner", "created_at", "updated_at", "received_bytes",
                      "bucket", "key", "size", "chunk_size", "total_chunks", "staging"):
            assert field in meta, f"会话 meta 缺字段 {field}"
        assert meta["bucket"] == "b" and meta["key"] == "dir/a.bin"
        assert meta["size"] == 3 * top_chunk and meta["total_chunks"] == 3
        assert meta["chunk_size"] == top_chunk and meta["staging"] == "target"
        # 不在系统临时目录（/tmp）；staging 目录本身仍存在
        assert not str(sess).startswith(str(env["systmp"]))
        assert env["staging"].is_dir()
        # 响应不得回显服务端绝对路径
        assert str(env["depot"]) not in json.dumps(d, ensure_ascii=False)


def test_legacy_session_in_staging_dir(env):
    with _client() as c:
        d = _init_ok(c)                       # 空 body：旧客户端兼容
        assert d["staging"] == "legacy" and d["resumed"] is False
        assert d["size"] == 0 and d["total_chunks"] == 0 and d["uploaded"] == []
        sess = env["staging"] / d["upload_id"]
        assert sess.is_dir(), "无 bucket/key 时会话必须在 <staging>/<id>"
        assert not str(sess).startswith(str(env["systmp"]))
        assert "dw_chunks" not in sess.as_posix()
        # query 参数形式同样可用
        d2 = _init_ok(c, form=False, bucket="b", key="q.bin", size=1024)
        assert d2["staging"] == "target"
        assert (env["depot"] / "b" / env["internal"] / d2["upload_id"]).is_dir()


def test_tempfile_tempdir_points_staging_and_restores(env):
    """B1：启动时 tempfile.tempdir 指向 <depot>/.staging，退出 lifespan 恢复原值。"""
    before = tempfile.tempdir
    assert before == str(env["systmp"])
    with _client():
        assert Path(tempfile.gettempdir()).resolve() == env["staging"].resolve()
        assert env["staging"].is_dir()
    assert tempfile.tempdir == before, "lifespan 退出必须恢复 tempfile.tempdir"


# ---------------------------------------------------------------------------
# 3) 按 upload_id 续传
# ---------------------------------------------------------------------------

def test_resume_by_upload_id_returns_real_uploaded(env):
    top_chunk = 1 * 1024 * 1024
    with _client() as c:
        d = _init_ok(c, bucket="b", key="resume.bin", size=3 * top_chunk)
        uid = d["upload_id"]
        _chunk_ok(c, uid, 0, b"aa")
        _chunk_ok(c, uid, 1, b"bbb")

        r = _init(c, resume="true", upload_id=uid, bucket="b",
                  key="resume.bin", size=3 * top_chunk)
        assert r.status_code == 200, r.text
        info = r.json()["data"]
        assert info["resumed"] is True and info["upload_id"] == uid
        assert info["uploaded"] == [0, 1]
        assert info["received_bytes"] == 5
        assert info["total_chunks"] == 3

        # size 不一致 → 400，detail 含「分片会话」
        r = _init(c, resume="true", upload_id=uid, bucket="b",
                  key="resume.bin", size=9 * top_chunk)
        assert r.status_code == 400 and "分片会话" in _detail(r), r.text
        # 会话不存在/非法 → 400，detail 含「分片会话」
        for bad in ("deadbeefdeadbeefdead", "bad"):
            r = _init(c, resume="true", upload_id=bad, bucket="b",
                      key="resume.bin", size=3 * top_chunk)
            assert r.status_code == 400 and "分片会话" in _detail(r), r.text

        # 他人不能续传（owner 绑定；admin 例外）
        tok = c.post("/api/tokens", params={"token": ADMIN},
                     json={"user": "bob", "role": "uploader"}).json()["data"]["token"]
        r = c.post("/api/objects/initiate",
                   params={"token": tok, "resume": "true", "upload_id": uid,
                           "bucket": "b", "key": "resume.bin",
                           "size": 3 * top_chunk})
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 4) 按 bucket/key/size 自动找回
# ---------------------------------------------------------------------------

def test_resume_auto_find_by_bucket_key_size(env):
    top_chunk = 1 * 1024 * 1024
    with _client() as c:
        d = _init_ok(c, bucket="b", key="auto.bin", size=2 * top_chunk)
        uid = d["upload_id"]
        _chunk_ok(c, uid, 0, b"aa")

        r = _init(c, resume="true", bucket="b", key="auto.bin", size=2 * top_chunk)
        assert r.status_code == 200, r.text
        info = r.json()["data"]
        assert info["resumed"] is True and info["upload_id"] == uid
        assert info["uploaded"] == [0]

        # size 不匹配 → 找不到 → 新建会话（resumed=false），旧会话保留
        r = _init(c, resume="true", bucket="b", key="auto.bin", size=7 * top_chunk)
        info2 = r.json()["data"]
        assert info2["resumed"] is False and info2["upload_id"] != uid
        assert (env["depot"] / "b" / env["internal"] / uid).is_dir()


# ---------------------------------------------------------------------------
# 5) chunk 落点 / .incoming / complete 原子落盘
# ---------------------------------------------------------------------------

def test_chunk_incoming_and_complete_atomic(env):
    top_chunk = 1 * 1024 * 1024
    with _client() as c:
        d = _init_ok(c, bucket="b", key="chunks/out.bin", size=2 * top_chunk)
        uid = d["upload_id"]
        sess = env["depot"] / "b" / "chunks" / env["internal"] / uid

        _chunk_ok(c, uid, 0, b"aa")
        _chunk_ok(c, uid, 1, b"bbb")
        assert (sess / "chunk_0.part").read_bytes() == b"aa"
        assert (sess / "chunk_1.part").read_bytes() == b"bbb"
        incoming = sess / ".incoming"
        assert not incoming.exists() or not list(incoming.iterdir()), \
            "分片请求临时文件必须被清理"
        assert not list(sess.glob("*.part.tmp"))
        meta = json.loads((sess / ".session.json").read_text(encoding="utf-8"))
        assert meta["received_bytes"] == 5, "每写一片都要更新 received_bytes"
        assert meta["updated_at"] >= meta["created_at"]

        r = _complete(c, uid, "b", "chunks/out.bin", 2)
        assert r.status_code == 200, r.text
        assert (env["depot"] / "b" / "chunks" / "out.bin").read_bytes() == b"aabbb"
        assert not sess.exists(), "完成后必须删除会话目录"
        assert not (env["depot"] / "b" / "chunks" / env["internal"]).exists(), \
            "会话删空后 .artifactdepot-uploads 应一并删除"
        assert not list(env["depot"].rglob(".merged"))
        assert not list(env["depot"].rglob("*.part*"))


def test_complete_writes_source_and_note_metadata(env):
    """complete 支持「来源」（自由文本）与「备注」，写入清单并由 list 返回。"""
    with _client() as c:
        d = _init_ok(c, bucket="b", key="meta/out.bin", size=3)
        uid = d["upload_id"]
        _chunk_ok(c, uid, 0, b"xyz")
        r = c.post("/api/objects/complete", params={"token": ADMIN},
                   data={"upload_id": uid, "bucket": "b", "key": "meta/out.bin",
                         "total_chunks": "1", "source_url": "客户B / 录屏",
                         "note": "  一句话备注  "})
        assert r.status_code == 200, r.text
        entry = json.loads(
            (env["depot"] / "b" / ".warehouse.json").read_text(encoding="utf-8"))["meta/out.bin"]
        assert entry["source_url"] == "客户B / 录屏"
        assert entry["note"] == "一句话备注", "备注去首尾空白后落清单"
        items = c.get("/api/objects/list",
                      params={"token": ADMIN, "bucket": "b", "prefix": "meta"}
                      ).json()["data"]["items"]
        assert items[0]["source_url"] == "客户B / 录屏"
        assert items[0]["note"] == "一句话备注"


# ---------------------------------------------------------------------------
# 6) 直传临时文件落在目标目录
# ---------------------------------------------------------------------------

def test_direct_upload_temp_in_target_dir(env, monkeypatch):
    from artifactdepot import storage
    real_put = storage.put_object
    seen = {}

    def spy(bucket, key, src_path, *a, **kw):
        seen["src"] = Path(src_path)
        seen["move"] = kw.get("move")
        return real_put(bucket, key, src_path, *a, **kw)

    monkeypatch.setattr(storage, "put_object", spy)
    payload = b"D" * (2 * 1024 * 1024 + 10)          # 超过 Starlette spool 阈值
    with _client() as c:
        r = _upload(c, "b", "sub/direct.bin", payload)
        assert r.status_code == 200, r.text
        target_dir = (env["depot"] / "b" / "sub").resolve()
        assert seen["src"].parent == target_dir, "直传临时文件必须落在目标文件父目录"
        assert "artifactdepot-incoming" in seen["src"].name
        assert seen["move"] is True
        assert (env["depot"] / "b" / "sub" / "direct.bin").read_bytes() == payload
        left = list(target_dir.glob("*artifactdepot-incoming*"))
        assert left == [], "成功后必须清理直传临时文件"


# ---------------------------------------------------------------------------
# 7) 列表 / bulk_delete / rename / ZIP 跳过内部目录
# ---------------------------------------------------------------------------

def test_list_bulk_delete_rename_skip_internal_uploads(env):
    from artifactdepot import storage
    top_chunk = 1 * 1024 * 1024
    with _client() as c:
        assert _upload(c, "b", "dir/a.txt", b"hello").status_code == 200
        d = _init_ok(c, bucket="b", key="dir/b.bin", size=2 * top_chunk)
        uid = d["upload_id"]
        _chunk_ok(c, uid, 0, b"xx")
        _chunk_ok(c, uid, 1, b"yy")
        sess = env["depot"] / "b" / "dir" / env["internal"] / uid

        # 列表忽略内部目录（目录内与 bucket 根都不得出现）
        r = c.get("/api/objects/list", params={"bucket": "b", "prefix": "dir/"})
        names = [i["name"] for i in r.json()["data"]["items"]]
        assert env["internal"] not in names and names == ["a.txt"]
        # 目录大小统计跳过内部目录 → 只有 a.txt 的 5 字节
        r = c.get("/api/objects/list", params={"bucket": "b", "prefix": ""})
        dir_item = [i for i in r.json()["data"]["items"] if i["name"] == "dir"][0]
        assert dir_item["size"] == 5, "目录大小统计必须跳过 .artifactdepot-uploads"
        assert env["internal"] not in [i["name"] for i in r.json()["data"]["items"]]

        # ZIP/一键下载扫描跳过内部目录
        scan = storage.bulk_download_scan("b", "dir")
        assert all(env["internal"] not in e["name"] for e in scan["entries"])
        assert [e["name"] for e in scan["entries"]] == ["a.txt"]

        # bulk_delete(mode=all) 保留内部目录
        r = c.post("/api/objects/bulk-delete", params={"token": ADMIN},
                   json={"bucket": "b", "prefix": "dir", "mode": "all",
                         "dry_run": False, "confirm": "dir"})
        assert r.status_code == 200, r.text
        assert not (env["depot"] / "b" / "dir" / "a.txt").exists()
        assert sess.is_dir(), "批量删除不得删除活跃上传会话"

        # 文件级 rename 正常；目录级 rename 遇到活跃会话 → 400 且会话不被搬走
        assert _upload(c, "b", "dir/keep.txt", b"K").status_code == 200
        r = c.post("/api/objects/rename", params={"token": ADMIN},
                   json={"bucket": "b", "key": "dir/keep.txt",
                         "new_key": "dir/renamed.txt"})
        assert r.status_code == 200, r.text
        assert (env["depot"] / "b" / "dir" / "renamed.txt").exists()
        assert sess.is_dir()
        r = c.post("/api/objects/rename", params={"token": ADMIN},
                   json={"bucket": "b", "key": "dir", "new_key": "dir2"})
        assert r.status_code in (400, 409), r.text
        assert sess.is_dir(), "rename 不得搬走/删除活跃上传会话"
        assert not (env["depot"] / "b" / "dir2" / env["internal"]).exists()


# ---------------------------------------------------------------------------
# 8) 重启：目标会话保留可续传；legacy + 旧 /tmp/dw_chunks 被清理
# ---------------------------------------------------------------------------

def test_restart_keeps_target_sessions(env):
    storage = env["storage"]
    top_chunk = 1 * 1024 * 1024
    with _client() as c1:
        d = _init_ok(c1, bucket="b", key="restart.bin", size=2 * top_chunk)
        uid = d["upload_id"]
        _chunk_ok(c1, uid, 0, b"aa")
        legacy = _init_ok(c1)["upload_id"]
    sess = env["depot"] / "b" / env["internal"] / uid
    legacy_dir = env["staging"] / legacy
    assert sess.is_dir() and legacy_dir.is_dir()

    # 模拟旧版进程留下的 /tmp/dw_chunks 残留（此时 tempfile.tempdir 已恢复为 systmp）
    residue = env["systmp"] / "dw_chunks" / "legacyuploadid_0123456"
    residue.mkdir(parents=True)
    (residue / "chunk_0.part").write_bytes(b"x")

    with _client() as c2:
        assert sess.is_dir(), "重启后目标目录会话必须保留（支持续传）"
        info = _init_ok(c2, resume="true", upload_id=uid, bucket="b",
                        key="restart.bin", size=2 * top_chunk)
        assert info["resumed"] is True and info["uploaded"] == [0]
        assert not legacy_dir.exists(), "legacy staging 会话应在启动时清理"
        assert not (env["systmp"] / "dw_chunks").exists(), "旧 /tmp/dw_chunks 应被清理"
        assert uid in storage.registered_upload_sessions(), "启动扫描应注册可续传会话"


# ---------------------------------------------------------------------------
# 9) 绝对 / 空闲 TTL 清理（registry 同步更新）
# ---------------------------------------------------------------------------

def test_ttl_cleanup_updates_registry(env):
    storage = env["storage"]
    top_chunk = 1 * 1024 * 1024
    with _client() as c:
        abs_uid = _init_ok(c, bucket="b", key="abs.bin", size=top_chunk)["upload_id"]
        idle_uid = _init_ok(c, bucket="b", key="idle.bin", size=top_chunk)["upload_id"]
        internal = env["depot"] / "b" / env["internal"]
        abs_dir, idle_dir = internal / abs_uid, internal / idle_uid
        assert abs_dir.is_dir() and idle_dir.is_dir()

        meta_path = abs_dir / ".session.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["created_at"] = "2020-01-01T00:00:00Z"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        old = time.time() - 3600
        os.utime(idle_dir, (old, old))

        removed = storage.cleanup_expired_chunk_sessions(60)
        assert removed >= 1
        assert not abs_dir.exists(), "绝对 TTL 到期会话应删除"
        assert idle_dir.is_dir(), "未到期会话不能被绝对 TTL 误删"

        removed = storage.cleanup_stale_chunk_sessions(60)
        assert removed >= 1
        assert not idle_dir.exists(), "空闲超时会话应删除"
        registered = storage.registered_upload_sessions()
        assert abs_uid not in registered and idle_uid not in registered
        assert not internal.exists(), "会话删空后 .artifactdepot-uploads 应删除"


# ---------------------------------------------------------------------------
# 10) 系统 /tmp 无残留
# ---------------------------------------------------------------------------

def test_no_system_tmp_residue(env):
    top_chunk = 1 * 1024 * 1024
    with _client() as c:
        before = set(env["systmp"].iterdir())
        d = _init_ok(c, bucket="b", key="nontmp/out.bin", size=2 * top_chunk)
        uid = d["upload_id"]
        _chunk_ok(c, uid, 0, b"aa")
        _chunk_ok(c, uid, 1, b"bb")
        assert _complete(c, uid, "b", "nontmp/out.bin", 2).status_code == 200
        # 直传超过 spool 阈值（2MB），验证框架 spool 也落到 staging 而非 /tmp
        assert _upload(c, "b", "nontmp/direct.bin", b"D" * (2 * 1024 * 1024)).status_code == 200

        assert set(env["systmp"].iterdir()) == before, "系统临时目录不得有新增伴随文件"
        assert not (env["systmp"] / "dw_chunks").exists()
        assert not list(env["depot"].rglob("dw_chunks"))
        assert not list(env["depot"].rglob("*.part"))
        assert not list(env["depot"].rglob(".merged"))
        assert not list(env["depot"].rglob("*artifactdepot-incoming*"))
        assert not list(env["staging"].iterdir()), "上传完成后 staging 不应残留会话"


# ---------------------------------------------------------------------------
# B3 错误码（parent 是文件 409 / 非法 key 400 / 参数缺失 400）
# ---------------------------------------------------------------------------

def test_initiate_validation_errors(env):
    with _client() as c:
        assert _upload(c, "b", "file.txt", b"x").status_code == 200
        r = _init(c, bucket="b", key="file.txt/child", size=10)
        assert r.status_code == 409, r.text
        r = _init(c, bucket="b", key="a/../x", size=10)
        assert r.status_code == 400, r.text
        r = _init(c, bucket="b")
        assert r.status_code == 400 and "同时" in _detail(r), r.text
        r = _init(c, bucket="b", key="x.bin", size="not-a-number")
        assert r.status_code == 400 and "size" in _detail(r), r.text
