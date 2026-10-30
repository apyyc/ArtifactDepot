# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""上传临时文件清理回归测试（0.9.0 伴随文件本地化后布局）

覆盖：
  1. 分片上传成功：complete 后删除 legacy staging 会话（不再有 /tmp/dw_chunks）
  2. 分片上传失败（put_object 抛错）：同样删除会话目录，不留几十 GB 残留
  3. complete 校验失败（缺片）：返回 400 并保留会话（补齐后可重试）
  4. store_chunk 复制失败：清理 .part.tmp，不留半截分片
  5. 启动清理：cleanup_all_chunk_sessions() 删除所有遗留 legacy 会话
  6. 启动 lifespan：清理上次进程遗留的 legacy 会话
  7. 空闲 TTL / 绝对 TTL：过期删除、活跃保留

0.9.0 起：无 bucket/key 的旧客户端会话落在 `<depot>/.staging/<upload_id>`
（不再是 /tmp/dw_chunks）；带 bucket/key 的会话落在目标文件父目录
`.artifactdepot-uploads/<upload_id>`，由 tests/test_upload_resume.py 覆盖。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "upload-cleanup-admin-0123456789"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    depot = tmp_path / "depot"
    depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "ui_enabled": True, "max_upload_mb": 0,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    # 隔离系统临时目录，避免测试触碰真实 /tmp（lifespan 会先清 <systmp>/dw_chunks）
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(systmp))
    import artifactdepot.config as config
    config._CONFIG = None

    from artifactdepot import storage
    staging = depot / ".staging"
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c, depot, staging, storage, tmp_path, systmp
    config._CONFIG = None


def _session_with_part(storage, tmp_path: Path, data: bytes = b"hello") -> str:
    """创建 legacy 会话（无 bucket/key，旧客户端语义）并写入一个分片。"""
    upload_id = storage.create_chunk_session("tester")
    src = tmp_path / "part.bin"
    src.write_bytes(data)
    storage.store_chunk(upload_id, 0, src, "tester")
    return upload_id


def test_finalize_success_removes_session(ctx):
    """成功 complete：对象落盘，本次 legacy staging 会话被删除。"""
    _, depot, staging, storage, tmp_path, _ = ctx
    upload_id = _session_with_part(storage, tmp_path)
    assert (staging / upload_id).is_dir()

    result = storage.finalize_chunk_upload(upload_id, "b", "k", 1,
                                           uploader="tester", actor="tester")

    assert result["size"] == 5
    assert (depot / "b" / "k").read_bytes() == b"hello"
    assert not (staging / upload_id).exists(), "成功后必须删除分片会话"


def test_finalize_failure_removes_session(ctx, monkeypatch):
    """put_object 落盘失败：会话目录也必须删除，不能把整份分片留在数据盘。"""
    _, _, staging, storage, tmp_path, _ = ctx
    upload_id = _session_with_part(storage, tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("模拟落盘失败（磁盘满）")

    monkeypatch.setattr(storage, "put_object", boom)
    with pytest.raises(RuntimeError):
        storage.finalize_chunk_upload(upload_id, "b", "k", 1,
                                      uploader="tester", actor="tester")

    assert not (staging / upload_id).exists(), "失败后必须删除分片会话"


def test_complete_missing_chunk_keeps_session_for_retry(ctx):
    """complete 发现缺片返回 400：保留会话（补齐后可重试），符合既有续传语义。"""
    c, _, staging, storage, *_ = ctx
    upload_id = storage.create_chunk_session("tester")

    r = c.post("/api/objects/complete", params={"token": ADMIN},
               data={"upload_id": upload_id, "bucket": "b", "key": "k", "total_chunks": "2"})

    assert r.status_code == 400, r.text
    assert (staging / upload_id).is_dir(), "校验类失败应保留会话以便补齐重试"


def test_store_chunk_copy_failure_cleans_part_tmp(ctx, monkeypatch):
    """分片复制失败：清理 .part.tmp，不留下半截文件。"""
    _, _, staging, storage, tmp_path, _ = ctx
    upload_id = storage.create_chunk_session("tester")
    src = tmp_path / "part.bin"
    src.write_bytes(b"x" * 1024)

    def fail_copy(*_a, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage.shutil, "copyfile", fail_copy)
    with pytest.raises(OSError):
        storage.store_chunk(upload_id, 0, src, "tester")

    session = staging / upload_id
    assert not list(session.glob("*.part.tmp")), "失败不能残留 .part.tmp"
    assert not list(session.glob("chunk_*.part")), "失败不能出现半截分片"


def test_cleanup_all_chunk_sessions_removes_legacy_residue(ctx):
    """启动清理：删除 legacy staging 根下所有遗留会话（含过去失败/被杀的上传）。"""
    _, _, staging, storage, *_ = ctx
    upload_id1 = storage.create_chunk_session("u1")
    upload_id2 = storage.create_chunk_session("u2")
    (staging / upload_id1 / "chunk_0.part").write_bytes(b"x")

    removed = storage.cleanup_all_chunk_sessions()

    assert removed >= 2
    assert not (staging / upload_id1).exists()
    assert not (staging / upload_id2).exists()


def test_startup_lifespan_cleans_previous_sessions(ctx):
    """模拟容器/进程重启：新 lifespan 启动时会清理上次留下的 legacy 会话目录。"""
    _, _, staging, storage, *_ = ctx
    upload_id = storage.create_chunk_session("tester")
    (staging / upload_id / "chunk_0.part").write_bytes(b"x")

    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app):
        pass

    assert not (staging / upload_id).exists(), "新进程启动时应清理上次遗留会话"


def test_cleanup_idle_sessions_keeps_active(ctx):
    """空闲清理只删除超过 TTL 无写入的会话，活跃会话保留。"""
    _, _, staging, storage, *_ = ctx
    stale = storage.create_chunk_session("stale")
    active = storage.create_chunk_session("active")
    old = time.time() - storage.CHUNK_SESSION_IDLE_TTL - 3600
    os.utime(staging / stale, (old, old))

    removed = storage.cleanup_stale_chunk_sessions(storage.CHUNK_SESSION_IDLE_TTL)

    assert removed >= 1
    assert not (staging / stale).exists(), "空闲超时会话应被后台清理"
    assert (staging / active).is_dir(), "活跃会话不能被误删"


def test_absolute_session_ttl_removes_expired_keeps_active(ctx):
    """绝对 TTL 到期（按 created_at）删除会话；未到期会话保留。"""
    _, _, staging, storage, *_ = ctx
    expired = storage.create_chunk_session("expired")
    active = storage.create_chunk_session("active")
    meta_path = staging / expired / ".session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["created_at"] = "2020-01-01T00:00:00Z"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    removed = storage.cleanup_expired_chunk_sessions(60)

    assert removed >= 1
    assert not (staging / expired).exists()
    assert (staging / active).is_dir()
