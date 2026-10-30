#!/usr/bin/env python3
# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""桌面客户端分块并发下载测试（真实 uvicorn 子进程 + 真实 HTTP）。

覆盖：
  1. 大文件（> 分块大小）并发下载：SHA-256 一致、进度多次回调且最终 done == total、
     .part 与下载断点状态文件均已清理；
  2. 小文件（< 分块大小）回退单流也能成功（不产生断点状态）；
  3. concurrency=1 时强制单流（不再发 Range 请求）；
  4. 取消：首个进度回调后置位 cancel，抛 Cancelled 且清理 .part 与断点状态；
  5. 对象不存在：HEAD 探测 / 下载返回 ApiError(404)；
  6. 服务端 audit.log 出现 >= 2 条 extra.partial 的 download 记录（证明确实多 Range）；
  7. 目录下载（download-bulk ZIP）保持单流；
  8. 断点续传：已有 .part + `.<目标名>.artifactdepot-download.json` 时只请求缺失分片；
  9. 网络失败保留 .part + 断点状态，恢复后只补缺失分片且内容一致；
  10. size/ETag 与断点状态不一致时丢弃 .part 从头下载。

隔离：服务端使用临时 config.json / depot / meta，access_token=admin-test，
ARTIFACT_DEPOT_CONFIG / ARTIFACT_DEPOT_ENV 指向临时文件，不读取仓库真实 .env；
测试结束终止 uvicorn 子进程并清理临时目录。

运行：cd ArtifactDepot && PYTHONPATH=src \
        /home/apyyc/work/ArtifactDepot/.venv/bin/python -m pytest \
        client/test_parallel_download.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PROJECT = Path(__file__).resolve().parents[1]
CLIENT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT.parent / ".venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

TOKEN = "admin-test"
BUCKET = "test-bucket"
BIG_KEY = "big/parallel.bin"
SMALL_KEY = "small/small.bin"
BIG_SIZE = 20 * 1024 * 1024 + 123   # 20MiB + 123B，> 默认 8MiB 分块
SMALL_SIZE = 1024 * 1024            # 1MiB，< 默认分块 → 单流


def _load_core():
    """从 client/artifactdepot_upload.py 导入核心模块（client 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "artifactdepot_upload_under_test", CLIENT_DIR / "artifactdepot_upload.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 需要能在 sys.modules 里找到模块
    spec.loader.exec_module(module)
    return module


core = _load_core()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _download_path(key: str) -> str:
    return "/api/objects/download?" + urllib.parse.urlencode(
        {"bucket": BUCKET, "key": key})


def _upload(http: httpx.Client, key: str, filename: str, data: bytes) -> None:
    resp = http.post(
        "/api/objects",
        data={"bucket": BUCKET, "key": key, "overwrite": "true"},
        files={"file": (filename, data, "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload.get("code") == 0, payload


def _audit_records(path: Path) -> list:
    records = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _partial_downloads(path: Path, key: str) -> list:
    return [rec for rec in _audit_records(path)
            if rec.get("action") == "download"
            and rec.get("key") == key
            and rec.get("partial")]


class _FakeRangeResponse:
    """模拟 206 Range 响应；fail_after 指定读取多少字节后抛网络错误。"""

    status = 206

    def __init__(self, body: bytes, start: int, end: int, total: int,
                 fail_after=None):
        self._body = body
        self._pos = 0
        self._fail_after = fail_after
        self.headers = {"Content-Range": f"bytes {start}-{end}/{total}"}

    def read(self, n=-1):
        if self._fail_after is not None and self._pos >= self._fail_after:
            raise OSError("模拟断网")
        limit = len(self._body) - self._pos
        if n is not None and n >= 0:
            limit = min(limit, n)
        if self._fail_after is not None:
            limit = min(limit, self._fail_after - self._pos)
        piece = self._body[self._pos:self._pos + limit]
        self._pos += len(piece)
        return piece

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_urlopen(data: bytes, total: int, *, fail_calls=(), corrupt=False):
    """构造 urlopen 替身，按请求 Range 返回 206；可让某次调用中途断网或内容损坏。"""
    payload = bytearray(data)
    if corrupt:
        payload[0] ^= 0xFF
    state = {"calls": 0}

    def fake_urlopen(req, timeout=None):
        value = req.headers.get("Range") or ""
        match = re.match(r"bytes=(\d+)-(\d+)", value)
        assert match, f"缺少 Range 头：{value!r}"
        start, end = int(match.group(1)), int(match.group(2))
        state["calls"] += 1
        fail_after = None
        if state["calls"] in fail_calls:
            fail_after = max(0, (end - start + 1) // 2)
        return _FakeRangeResponse(bytes(payload[start:end + 1]), start, end, total,
                                  fail_after)

    return fake_urlopen, state


def _parallel_probe(total: int, etag: str = "") -> dict:
    return {"size": total, "accept_ranges": True, "etag": etag,
            "disposition": 'attachment; filename="fake.bin"'}



def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path,
                timeout: float = 60.0) -> None:
    url = base + "/health/live"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                "uvicorn 启动失败（exit %s）：\n%s"
                % (proc.returncode, _tail(log_path)))
        try:
            resp = httpx.get(url, timeout=1.0, trust_env=False)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise AssertionError("uvicorn 60s 内未就绪：\n%s" % _tail(log_path))


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """启动真实 uvicorn 子进程（临时 config/depot/meta，随机端口），上传测试对象。"""
    work = tmp_path_factory.mktemp("ad-parallel-download")
    depot = work / "depot"
    meta = work / "meta"
    port = _free_port()
    config = {
        "depot_dir": str(depot),           # 临时仓库，绝对路径
        "meta_dir": str(meta),             # audit.log 等状态文件
        "access_token": TOKEN,
        "host": "127.0.0.1",
        "port": port,
        "require_read_token": False,
    }
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    env = os.environ.copy()
    # 清掉可能指向真实 .env / config 的环境变量，避免测试污染或误读密钥
    for key in list(env):
        if key.startswith(("ARTIFACT_DEPOT_", "WAREHOUSE_", "ARTIFACTDEPOT_")):
            env.pop(key, None)
    env.update({
        "ARTIFACT_DEPOT_CONFIG": str(config_path),
        "ARTIFACT_DEPOT_ENV": str(work / ".env.not-exist"),
        "PYTHONPATH": str(PROJECT / "src"),
        "PYTHONUNBUFFERED": "1",
    })

    log_path = work / "uvicorn.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "artifactdepot.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(PROJECT), env=env, stdout=log_file, stderr=subprocess.STDOUT)

    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base, proc, log_path)
        big_data = random.Random(20261024).randbytes(BIG_SIZE)
        small_data = random.Random(7).randbytes(SMALL_SIZE)
        with httpx.Client(base_url=base, timeout=120.0, trust_env=False,
                          headers={"Authorization": "Bearer " + TOKEN}) as http:
            _upload(http, BIG_KEY, "parallel.bin", big_data)
            _upload(http, SMALL_KEY, "small.bin", small_data)
    except BaseException:
        proc.kill()
        proc.wait(timeout=10)
        log_file.close()
        raise

    server = SimpleNamespace(
        base=base, token=TOKEN, work=work, meta_dir=meta, proc=proc,
        log_path=log_path, audit_path=meta / "audit.log",
        big_key=BIG_KEY, big_size=BIG_SIZE, big_sha=_sha256_bytes(big_data),
        small_key=SMALL_KEY, small_size=SMALL_SIZE, small_sha=_sha256_bytes(small_data),
    )
    try:
        yield server
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log_file.close()
        shutil.rmtree(work, ignore_errors=True)


def _client(server) -> "core.ArtifactDepotClient":
    return core.ArtifactDepotClient(server.base, server.token)


# ---------------------------------------------------------------------------
# 0. 服务端不可达时 download_settings 回退默认值
# ---------------------------------------------------------------------------

def test_download_settings_fallback_when_unreachable():
    client = core.ArtifactDepotClient("http://127.0.0.1:9", "token", timeout=0.5)
    settings = client.download_settings()
    assert settings == {
        "chunk_size": core.DEFAULT_DOWNLOAD_CHUNK,
        "chunk_concurrency": core.DEFAULT_DOWNLOAD_CONCURRENCY,
        "request_timeout_seconds": int(core.DEFAULT_DOWNLOAD_TIMEOUT),
    }


# ---------------------------------------------------------------------------
# 1. 大文件并发下载
# ---------------------------------------------------------------------------

def test_parallel_download_big_file(server, tmp_path):
    dest_dir = tmp_path / "dl-big"
    dest_dir.mkdir()
    calls = []
    lock = threading.Lock()

    def progress(done, total):
        with lock:
            calls.append((int(done), int(total)))

    saved = _client(server).download_to(
        _download_path(server.big_key), dest_dir, progress=progress)

    assert saved == dest_dir / "parallel.bin"
    assert saved.stat().st_size == server.big_size
    assert _sha256_bytes(saved.read_bytes()) == server.big_sha

    with lock:
        snapshot = list(calls)
    assert len(snapshot) >= 2, f"进度回调太少：{snapshot}"
    assert snapshot[-1] == (server.big_size, server.big_size)
    assert all(total == server.big_size for _, total in snapshot)
    assert not (dest_dir / "parallel.bin.part").exists()
    # 伴随状态文件：成功后必须删除（不留残余）
    assert not core.download_state_path(saved).exists(), "成功后应删除下载断点状态文件"


# ---------------------------------------------------------------------------
# 2. 小文件回退单流
# ---------------------------------------------------------------------------

def test_small_file_single_stream(server, tmp_path):
    dest_dir = tmp_path / "dl-small"
    dest_dir.mkdir()
    calls = []
    lock = threading.Lock()

    def progress(done, total):
        with lock:
            calls.append((int(done), int(total)))

    saved = _client(server).download_to(
        _download_path(server.small_key), dest_dir, progress=progress)

    assert saved == dest_dir / "small.bin"
    assert _sha256_bytes(saved.read_bytes()) == server.small_sha
    with lock:
        snapshot = list(calls)
    assert snapshot and snapshot[-1] == (server.small_size, server.small_size)
    assert not (dest_dir / "small.bin.part").exists()
    # 小文件不应产生 Range 分片审计
    assert _partial_downloads(server.audit_path, server.small_key) == []


# ---------------------------------------------------------------------------
# 3. concurrency=1 强制单流
# ---------------------------------------------------------------------------

def test_concurrency_one_forces_single_stream(server, tmp_path):
    target = tmp_path / "big-single.bin"
    before = len(_partial_downloads(server.audit_path, server.big_key))

    saved = _client(server).download_to(
        _download_path(server.big_key), target, concurrency=1)

    assert saved == target
    assert _sha256_bytes(saved.read_bytes()) == server.big_sha
    assert not target.with_name(target.name + ".part").exists()
    # 没有新增 Range 请求 → 确实走了单流
    after = len(_partial_downloads(server.audit_path, server.big_key))
    assert after == before, "concurrency=1 不应产生 Range 分片请求"


# ---------------------------------------------------------------------------
# 4. 取消
# ---------------------------------------------------------------------------

def test_cancel_stops_and_cleans_part(server, tmp_path):
    dest_dir = tmp_path / "dl-cancel"
    dest_dir.mkdir()
    cancel = threading.Event()

    def progress(done, total):
        if done > 0:
            cancel.set()

    with pytest.raises(core.Cancelled):
        _client(server).download_to(
            _download_path(server.big_key), dest_dir,
            progress=progress, cancel=cancel)

    assert not (dest_dir / "parallel.bin").exists()
    assert not (dest_dir / "parallel.bin.part").exists()
    # 取消（用户主动放弃）应同时删除伴随断点状态
    assert not core.download_state_path(dest_dir / "parallel.bin").exists()


# ---------------------------------------------------------------------------
# 5. 404
# ---------------------------------------------------------------------------

def test_missing_object_raises_api_error_404(server, tmp_path):
    client = _client(server)
    missing = "/api/objects/download?" + urllib.parse.urlencode(
        {"bucket": BUCKET, "key": "missing/nope.bin"})

    with pytest.raises(core.ApiError) as excinfo:
        client.download_to(missing, tmp_path / "nope.bin")
    assert excinfo.value.status == 404

    # HEAD 探测本身同样返回 404
    with pytest.raises(core.ApiError) as probe_exc:
        client._probe_download(missing, 10)
    assert probe_exc.value.status == 404


# ---------------------------------------------------------------------------
# 6. 服务端审计出现多条 extra.partial download 记录
# ---------------------------------------------------------------------------

def test_audit_records_multiple_partial_ranges(server, tmp_path):
    dest_dir = tmp_path / "dl-audit"
    dest_dir.mkdir()
    before = len(_partial_downloads(server.audit_path, server.big_key))

    saved = _client(server).download_to(
        _download_path(server.big_key), dest_dir, concurrency=4)
    assert saved.stat().st_size == server.big_size
    assert _sha256_bytes(saved.read_bytes()) == server.big_sha

    records = _partial_downloads(server.audit_path, server.big_key)
    new_records = records[before:]
    assert len(new_records) >= 2, (
        f"audit.log 中 extra.partial 的 download 记录不足 2 条：{new_records}")
    assert all(str(rec.get("range") or "").startswith("bytes=") for rec in new_records)
    assert all(int(rec.get("size") or 0) > 0 for rec in new_records)


# ---------------------------------------------------------------------------
# 8. 分片重试：从已写入位置续拉，不重复下载/重复计数
# ---------------------------------------------------------------------------

def test_chunk_retry_resumes_without_double_counting(tmp_path, monkeypatch):
    total = 3 * 1024 * 1024 + 123
    data = random.Random(99).randbytes(total)
    fake, state = _fake_urlopen(data, total, fail_calls=(2,))
    monkeypatch.setattr(core.urllib.request, "urlopen", fake)
    client = core.ArtifactDepotClient("http://127.0.0.1:9", "t")
    calls = []
    lock = threading.Lock()

    def progress(done, total_):
        with lock:
            calls.append((int(done), int(total_)))

    target = tmp_path / "retry.bin"
    saved = client._download_parallel(
        "/api/objects/download?bucket=b&key=fake.bin", target,
        _parallel_probe(total), 1024 * 1024, 2, progress, 5.0, None)

    assert saved == target
    assert saved.read_bytes() == data
    with lock:
        snapshot = list(calls)
    assert snapshot[-1] == (total, total)
    assert all(done <= total for done, _ in snapshot), "重试不应重复计数"
    assert state["calls"] >= 4, "应至少多出一次重试请求"
    assert not target.with_name(target.name + ".part").exists()


# ---------------------------------------------------------------------------
# 9. ETag(SHA-256) 校验失败：报错含「校验」且清理 .part
# ---------------------------------------------------------------------------

def test_sha256_etag_mismatch_reports_and_cleans_part(tmp_path, monkeypatch):
    total = 2 * 1024 * 1024
    data = random.Random(5).randbytes(total)
    fake, _ = _fake_urlopen(data, total, corrupt=True)
    monkeypatch.setattr(core.urllib.request, "urlopen", fake)
    client = core.ArtifactDepotClient("http://127.0.0.1:9", "t")
    target = tmp_path / "verify.bin"
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'

    with pytest.raises(core.ApiError) as excinfo:
        client._download_parallel(
            "/api/objects/download?bucket=b&key=fake.bin", target,
            _parallel_probe(total, etag), 1024 * 1024, 2, None, 5.0, None)

    assert "校验" in str(excinfo.value)
    assert not target.exists()
    assert not (tmp_path / "verify.bin.part").exists()


# ---------------------------------------------------------------------------
# 10. 断点续传：已有 .part + 下载断点状态时只补缺失分片
# ---------------------------------------------------------------------------

def _range_chunks(requested: list, chunk_size: int) -> list:
    """把请求的 Range 头换算成分片索引（按 chunk_size 对齐）。"""
    out = []
    for value in requested:
        match = re.match(r"bytes=(\d+)-", value or "")
        assert match, f"缺少 Range 头：{value!r}"
        out.append(int(match.group(1)) // chunk_size)
    return sorted(out)


def _write_download_state(path: Path, size: int, etag: str, chunk_size: int,
                          done) -> None:
    """写测试用下载断点状态（格式与客户端一致）。"""
    path.write_text(json.dumps({
        "version": core.DOWNLOAD_STATE_VERSION,
        "size": size, "etag": etag, "chunk_size": chunk_size,
        "done": sorted(int(i) for i in done), "updated_at": 0,
    }), encoding="utf-8")


def test_resume_only_fetches_missing_download_chunks(tmp_path, monkeypatch):
    total = 4 * 1024 * 1024 + 7
    chunk = 1024 * 1024
    data = random.Random(42).randbytes(total)
    target = tmp_path / "resume-dl.bin"
    part = target.with_name(target.name + ".part")
    state_path = core.download_state_path(target)
    kept = {0, 1}
    buf = bytearray(total)
    for index in kept:
        start = index * chunk
        buf[start:start + chunk] = data[start:start + chunk]
    part.write_bytes(bytes(buf))
    _write_download_state(state_path, total, "", chunk, kept)

    requested = []
    fake, _ = _fake_urlopen(data, total)

    def wrapped(req, timeout=None):
        requested.append(req.headers.get("Range") or "")
        return fake(req, timeout=timeout)

    monkeypatch.setattr(core.urllib.request, "urlopen", wrapped)
    client = core.ArtifactDepotClient("http://127.0.0.1:9", "t")
    saved = client._download_parallel(
        "/api/objects/download?bucket=b&key=fake.bin", target,
        _parallel_probe(total), chunk, 2, None, 5.0, None, None)

    assert saved == target
    assert target.read_bytes() == data
    assert _range_chunks(requested, chunk) == [2, 3, 4], "已下载分片不应重复请求"
    assert not part.exists()
    assert not state_path.exists(), "续传完成后应删除状态文件"


def test_download_failure_keeps_part_and_state_then_resumes(tmp_path, monkeypatch):
    total = 4 * 1024 * 1024
    chunk = 1024 * 1024
    data = random.Random(7).randbytes(total)
    target = tmp_path / "fail-dl.bin"
    part = target.with_name(target.name + ".part")
    state_path = core.download_state_path(target)
    client = core.ArtifactDepotClient("http://127.0.0.1:9", "t")

    # 第 1 片成功；第 2 片及其 3 次重试（共 4 次请求）全部断网 → 下载失败
    fake, _ = _fake_urlopen(data, total, fail_calls=(2, 3, 4, 5))
    monkeypatch.setattr(core.urllib.request, "urlopen", fake)
    # 跳过指数退避等待，避免测试变慢
    monkeypatch.setattr(core, "_sleep_or_cancel", lambda *a, **k: False)
    with pytest.raises(core.ApiError):
        client._download_parallel(
            "/api/objects/download?bucket=b&key=fake.bin", target,
            _parallel_probe(total), chunk, 1, None, 5.0, None, None)

    assert part.exists(), "失败应保留 .part（可续传）"
    assert state_path.exists(), "失败应保留下载断点状态"
    done = set(json.loads(state_path.read_text(encoding="utf-8"))["done"])
    assert done == {0}

    # 网络恢复：只补缺失分片，最终内容一致
    requested = []
    fake2, _ = _fake_urlopen(data, total)

    def wrapped(req, timeout=None):
        requested.append(req.headers.get("Range") or "")
        return fake2(req, timeout=timeout)

    monkeypatch.setattr(core.urllib.request, "urlopen", wrapped)
    saved = client._download_parallel(
        "/api/objects/download?bucket=b&key=fake.bin", target,
        _parallel_probe(total), chunk, 1, None, 5.0, None, None)

    assert saved == target
    assert target.read_bytes() == data
    assert _range_chunks(requested, chunk) == [1, 2, 3]
    assert not part.exists()
    assert not state_path.exists()


def test_download_state_mismatch_discards_and_redownloads(tmp_path, monkeypatch):
    total = 3 * 1024 * 1024
    chunk = 1024 * 1024
    data = random.Random(11).randbytes(total)
    target = tmp_path / "stale-dl.bin"
    part = target.with_name(target.name + ".part")
    state_path = core.download_state_path(target)
    part.write_bytes(b"\x00" * total)
    # 状态里的 etag 与当前对象不一致（对象已更新）→ 必须丢弃 .part 从头下载
    _write_download_state(state_path, total, "stale-etag", chunk, {0, 1})

    requested = []
    fake, _ = _fake_urlopen(data, total)

    def wrapped(req, timeout=None):
        requested.append(req.headers.get("Range") or "")
        return fake(req, timeout=timeout)

    monkeypatch.setattr(core.urllib.request, "urlopen", wrapped)
    client = core.ArtifactDepotClient("http://127.0.0.1:9", "t")
    etag = '"' + _sha256_bytes(data) + '"'
    saved = client._download_parallel(
        "/api/objects/download?bucket=b&key=fake.bin", target,
        _parallel_probe(total, etag), chunk, 2, None, 5.0, None, None)

    assert saved == target
    assert target.read_bytes() == data
    assert _range_chunks(requested, chunk) == [0, 1, 2], "ETag 不一致时应从头下载"
    assert not part.exists()
    assert not state_path.exists()



# ---------------------------------------------------------------------------
# 7. 目录下载（download-bulk ZIP）保持单流：无 Content-Length、不走 Range
# ---------------------------------------------------------------------------

def test_bulk_zip_download_stays_single_stream(server, tmp_path):
    with httpx.Client(base_url=server.base, timeout=60.0, trust_env=False,
                      headers={"Authorization": "Bearer " + server.token}) as http:
        _upload(http, "zipsub/a.txt", "a.txt", b"hello-a")
        _upload(http, "zipsub/b.txt", "b.txt", b"hello-b")

    path = "/api/objects/download-bulk?" + urllib.parse.urlencode(
        {"bucket": BUCKET, "prefix": "zipsub"})
    dest_dir = tmp_path / "dl-zip"
    dest_dir.mkdir()
    calls = []
    lock = threading.Lock()

    def progress(done, total):
        with lock:
            calls.append((int(done), int(total)))

    saved = _client(server).download_to(path, dest_dir, progress=progress, concurrency=4)

    assert saved.suffix == ".zip"
    data = saved.read_bytes()
    assert data[:2] == b"PK"
    assert b"hello-a" in data and b"hello-b" in data
    with lock:
        snapshot = list(calls)
    assert snapshot, "ZIP 下载也应该有进度回调"
    assert all(total == 0 for _, total in snapshot), "ZIP 无 Content-Length，total 应为 0"
    assert not (dest_dir / (saved.name + ".part")).exists()
    # ZIP 目录包是单流，目录内文件不应产生 partial 审计
    for key in ("zipsub/a.txt", "zipsub/b.txt"):
        assert _partial_downloads(server.audit_path, key) == []

