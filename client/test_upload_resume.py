#!/usr/bin/env python3
# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""桌面客户端伴随状态文件 + 上传/下载暂停恢复测试（真实 uvicorn 子进程 + 真实 HTTP）。

覆盖契约 C4：
  1. 上传断点状态位于源文件目录、权限 0600、含 upload_id/uploaded；成功后删除；
  2. 暂停 → 恢复只补缺失分片（服务端 initiate(resume=true) 返回 uploaded/received_bytes，
     断言不再从 0 重传；暂停期间不再发出分片请求）；
  3. 首次失败（monkeypatch upload_chunk 抛网络错）→ 再次上传从断点续传、最终 SHA-256 一致；
  4. 多文件并发上传各自的状态文件互不覆盖（文件名/upload_id/key 均独立）；
  5. 下载暂停/恢复只补缺失分片（统计 Range 请求），最终 SHA-256 一致；失败保留
     .part+状态、恢复只补缺失分片；
  6. 源目录不可写时回退 ~/.artifactdepot/upload-state/<sha1>.json 并提示（非 root）。

另有不依赖服务端的单元测试（旧全局状态一次性迁移、状态文件 0600 等），用 -k unit 可单独运行。

隔离：临时 config.json / depot / meta / 随机端口；ARTIFACT_DEPOT_CONFIG / ARTIFACT_DEPOT_ENV
指向临时文件，不读取仓库真实 .env；状态文件全部落在 pytest tmp_path，不写用户 home
（回退目录与旧状态文件路径通过 monkeypatch 重定向）。

运行：cd ArtifactDepot && PYTHONPATH=src \
        /home/apyyc/work/ArtifactDepot/.venv/bin/python -m pytest \
        client/test_upload_resume.py -q
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
import stat
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
CHUNK_SIZE = 1024 * 1024                 # 服务端配置 1MiB 分片
UPLOAD_SIZE = 9 * 1024 * 1024 + 123      # > 8MB → 分片上传；共 10 片
UPLOAD_CHUNKS = (UPLOAD_SIZE + CHUNK_SIZE - 1) // CHUNK_SIZE
DOWNLOAD_SIZE = 6 * 1024 * 1024 + 123    # 分片下载 7 片


def _load_core():
    """从 client/artifactdepot_upload.py 导入核心模块（client 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "artifactdepot_upload_resume_under_test",
        CLIENT_DIR / "artifactdepot_upload.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 需要能在 sys.modules 里找到模块
    spec.loader.exec_module(module)
    return module


core = _load_core()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _client(server) -> "core.ArtifactDepotClient":
    return core.ArtifactDepotClient(server.base, server.token)


def _state_file(path: Path) -> Path:
    return core.upload_state_path(path)


def _state_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _pauser(pause_event, threshold: int):
    """返回「只触发一次」的暂停器：done >= threshold 时置位 pause_event。"""
    state = {"armed": True}

    def maybe_pause(done: int) -> None:
        if state["armed"] and int(done) >= int(threshold):
            state["armed"] = False
            pause_event.set()

    return maybe_pause


def _range_chunks(requested: list, chunk_size: int) -> list:
    """把请求的 Range 头换算成分片索引。"""
    out = []
    for value in requested:
        match = re.match(r"bytes=(\d+)-", value or "")
        assert match, f"缺少 Range 头：{value!r}"
        out.append(int(match.group(1)) // chunk_size)
    return sorted(out)


def _record_initiate(monkeypatch) -> list:
    """记录客户端 initiate 的响应（断言服务端 uploaded/received_bytes/staging）。"""
    recorded = []
    original = core.ArtifactDepotClient.initiate

    def spy(self, *args, **kwargs):
        data = original(self, *args, **kwargs)
        recorded.append(data)
        return data

    monkeypatch.setattr(core.ArtifactDepotClient, "initiate", spy)
    return recorded


def _spy_upload_chunk(monkeypatch, *, fail_index=None, delay: float = 0.0) -> list:
    """记录每次 upload_chunk 的分片索引；可对指定分片抛一次网络错。"""
    sent = []
    original = core.ArtifactDepotClient.upload_chunk

    def spy(self, upload_id, index, data):
        sent.append(int(index))
        if delay:
            time.sleep(delay)
        if fail_index is not None and int(index) == int(fail_index):
            raise core.ApiError(0, "模拟网络中断")
        return original(self, upload_id, index, data)

    monkeypatch.setattr(core.ArtifactDepotClient, "upload_chunk", spy)
    return sent


def _spy_urlopen_ranges(monkeypatch, *, fail_start=None) -> list:
    """记录带 Range 的下载请求；fail_start 指定起始偏移的请求一律断网。"""
    requested = []
    original = core.urllib.request.urlopen

    def spy(req, timeout=None):
        value = req.headers.get("Range") or req.get_header("Range") or ""
        if value:
            requested.append(value)
            if fail_start is not None and re.match(rf"bytes={fail_start}-", value):
                raise OSError("模拟下载断网")
        return original(req, timeout=timeout)

    monkeypatch.setattr(core.urllib.request, "urlopen", spy)
    return requested


def _start_upload(client, path: Path, key: str, *, pause_event=None,
                  cancel_event=None, progress=None, log=None):
    """后台线程执行 upload_file，返回 (thread, results)。"""
    results = []

    def run():
        try:
            data = core.upload_file(
                client, str(path), BUCKET, key, progress=progress, log=log,
                pause_event=pause_event, cancel_event=cancel_event)
            results.append(("ok", data))
        except BaseException as exc:  # 测试里把异常带回来
            results.append(("err", exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, results


def _upload_http(server, key: str, filename: str, data: bytes) -> None:
    """用直传接口准备对象（不依赖分片续传 API）。"""
    with httpx.Client(base_url=server.base, timeout=120.0, trust_env=False,
                      headers={"Authorization": "Bearer " + TOKEN}) as http:
        resp = http.post(
            "/api/objects",
            data={"bucket": BUCKET, "key": key, "overwrite": "true"},
            files={"file": (filename, data, "application/octet-stream")})
        assert resp.status_code == 200, resp.text
        assert resp.json().get("code") == 0


def _object_bytes(server, key: str) -> bytes:
    with httpx.Client(base_url=server.base, timeout=120.0, trust_env=False,
                      headers={"Authorization": "Bearer " + TOKEN}) as http:
        resp = http.get("/api/objects/download",
                        params={"bucket": BUCKET, "key": key})
        assert resp.status_code == 200, resp.text
        return resp.content


def _download_url(key: str) -> str:
    return "/api/objects/download?" + urllib.parse.urlencode(
        {"bucket": BUCKET, "key": key})


def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path,
                timeout: float = 60.0) -> None:
    url = base + "/health/live"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("uvicorn 启动失败（exit %s）：\n%s"
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
    """启动真实 uvicorn 子进程（1MiB 分片，临时 config/depot/meta，随机端口）。"""
    work = tmp_path_factory.mktemp("ad-upload-resume")
    depot = work / "depot"
    meta = work / "meta"
    port = _free_port()
    config = {
        "depot_dir": str(depot),
        "meta_dir": str(meta),
        "access_token": TOKEN,
        "host": "127.0.0.1",
        "port": port,
        "datahub_url": "",
        "require_read_token": False,
        "upload": {"chunk_size_mb": 1},
        "download": {"chunk_size_mb": 1, "chunk_concurrency": 2},
    }
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    env = os.environ.copy()
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
    except BaseException:
        proc.kill()
        proc.wait(timeout=10)
        log_file.close()
        raise

    server = SimpleNamespace(base=base, token=TOKEN, work=work, proc=proc,
                             log_path=log_path, audit_path=meta / "audit.log")
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


def _make_upload_file(tmp_path: Path, name: str = "big.bin") -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    path = src / name
    path.write_bytes(random.Random(hash(name) & 0xFFFF).randbytes(UPLOAD_SIZE))
    return path


# ---------------------------------------------------------------------------
# 1. 状态文件位置 / 0600 / upload_id；成功后删除
# ---------------------------------------------------------------------------

def test_state_file_in_source_dir_and_removed_on_success(server, tmp_path,
                                                         monkeypatch):
    path = _make_upload_file(tmp_path, "state.bin")
    client = _client(server)
    initiated = _record_initiate(monkeypatch)

    progress_done = []
    pause = threading.Event()
    maybe_pause = _pauser(pause, 2 * CHUNK_SIZE)

    def progress(done, total, speed, msg):
        progress_done.append(int(done))
        # 传完 2 片后在分片边界暂停一次，便于检查伴随状态文件
        maybe_pause(done)

    thread, results = _start_upload(client, path, "resume/state.bin",
                                    pause_event=pause, progress=progress)
    state_file = _state_file(path)
    assert _wait_for(lambda: state_file.exists() and pause.is_set(), 60), \
        "上传暂停后应出现源目录伴随状态文件"
    time.sleep(0.3)  # 等分片边界生效，避免读到中间态

    # 状态文件就在源文件目录、文件名符合契约、权限 0600
    assert state_file.parent == path.parent
    assert state_file.name == "." + path.name + ".artifactdepot-upload.json"
    assert _state_mode(state_file) == 0o600

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["version"] == core.UPLOAD_STATE_VERSION
    assert payload["server"] == server.base
    assert payload["bucket"] == BUCKET
    assert payload["key"] == "resume/state.bin"
    assert payload["file_path"] == str(path.resolve())
    assert payload["file_size"] == UPLOAD_SIZE
    assert payload["chunk_size"] == CHUNK_SIZE
    assert payload["total_chunks"] == UPLOAD_CHUNKS
    assert payload["upload_id"]
    assert payload["uploaded"] == [0, 1]
    assert payload["staging"] in ("target", "")
    # initiate 始终带 bucket/key/size：服务端把会话落到目标目录
    first = initiated[0]
    assert first["chunk_size"] == CHUNK_SIZE
    assert int(first.get("total_chunks") or 0) in (0, UPLOAD_CHUNKS)

    # 暂停期间不应再发出分片请求（进度保持）
    sent_before = list(progress_done)
    time.sleep(0.5)
    assert progress_done == sent_before, "暂停期间不应继续上传分片"

    pause.clear()
    thread.join(120)
    assert results and results[0][0] == "ok", results
    assert not state_file.exists(), "上传成功后应删除源目录状态文件"
    assert _sha256_bytes(_object_bytes(server, "resume/state.bin")) == _sha256_file(path)


# ---------------------------------------------------------------------------
# 2. 暂停 → 取消 → 恢复只补缺失分片（服务端 uploaded 为事实源）
# ---------------------------------------------------------------------------

def test_pause_cancel_then_resume_only_missing_chunks(server, tmp_path,
                                                      monkeypatch):
    path = _make_upload_file(tmp_path, "pause.bin")
    client = _client(server)
    initiated = _record_initiate(monkeypatch)
    pause = threading.Event()
    maybe_pause = _pauser(pause, 2 * CHUNK_SIZE)

    def progress(done, total, speed, msg):
        maybe_pause(done)

    cancel = threading.Event()
    original_upload_chunk = core.ArtifactDepotClient.upload_chunk
    first_sent = _spy_upload_chunk(monkeypatch)
    thread, results = _start_upload(client, path, "resume/pause.bin",
                                    pause_event=pause, cancel_event=cancel,
                                    progress=progress)
    state_file = _state_file(path)
    assert _wait_for(lambda: state_file.exists() and pause.is_set(), 60)
    time.sleep(0.3)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["uploaded"] == [0, 1]
    assert sorted(set(first_sent)) == [0, 1], "暂停后不应再上传分片"

    # 暂停中取消：异常上抛、本地状态保留（已传分片可续传）
    cancel.set()
    thread.join(60)
    assert results and results[0][0] == "err"
    assert isinstance(results[0][1], core.Cancelled)
    assert state_file.exists(), "取消后应保留续传状态"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    uploaded_first = set(state["uploaded"])
    assert uploaded_first == {0, 1}

    # 第二次上传：initiate(resume=true, upload_id, bucket/key/size) 拿服务端 uploaded
    monkeypatch.setattr(core.ArtifactDepotClient, "upload_chunk",
                        original_upload_chunk)
    second_sent = _spy_upload_chunk(monkeypatch)
    thread, results = _start_upload(client, path, "resume/pause.bin")
    thread.join(120)
    assert results and results[0][0] == "ok", results

    resumed = [d for d in initiated if d.get("resumed")]
    assert resumed, "续传时应由服务端返回 resumed=true"
    server_uploaded = set(int(i) for i in (resumed[-1].get("uploaded") or []))
    assert server_uploaded == uploaded_first, "服务端 uploaded 应与已完成分片一致"
    received = int(resumed[-1].get("received_bytes") or 0)
    assert received > 0, "续传时服务端累计 received_bytes 不应从 0 开始"
    assert received == len(server_uploaded) * CHUNK_SIZE

    # 只补缺失分片：第二次请求与第一次完全不重叠，且并集覆盖全部分片
    assert set(second_sent).isdisjoint(uploaded_first)
    assert set(second_sent) | uploaded_first == set(range(UPLOAD_CHUNKS))
    assert not state_file.exists()
    assert _sha256_bytes(_object_bytes(server, "resume/pause.bin")) == _sha256_file(path)


# ---------------------------------------------------------------------------
# 3. 首次失败（网络错）→ 续传成功且 SHA-256 一致
# ---------------------------------------------------------------------------

def test_upload_failure_then_resume_sha256(server, tmp_path, monkeypatch):
    path = _make_upload_file(tmp_path, "fail.bin")
    client = _client(server)
    state_file = _state_file(path)

    # 跳过 _retry 的指数退避，避免测试变慢（失败语义保持）
    def fast_retry(call, *, what, log, attempts=3):
        last = None
        for i in range(attempts):
            try:
                return call()
            except core.ApiError as e:
                last = e
                if e.status in (400, 401, 403, 404, 409, 413):
                    raise
                if i >= attempts - 1:
                    raise
        if last:
            raise last
        raise RuntimeError(f"{what}失败")

    monkeypatch.setattr(core, "_retry", fast_retry)
    original_upload_chunk = core.ArtifactDepotClient.upload_chunk
    sent_first = _spy_upload_chunk(monkeypatch, fail_index=3)
    thread, results = _start_upload(client, path, "resume/fail.bin")
    thread.join(120)
    assert results and results[0][0] == "err", results
    assert isinstance(results[0][1], core.ApiError)

    # 失败不 abort：会话与状态都保留，已完成分片写入状态
    assert state_file.exists(), "失败后应保留续传状态"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["uploaded"] == [0, 1, 2]
    assert sent_first[:3] == [0, 1, 2]
    assert set(sent_first[3:]) == {3}, "分片 3 失败后应按重试次数重试"

    # 第二次上传：从最后位置继续，不重传 0/1/2（先恢复真实方法，避免叠加断网替身）
    monkeypatch.setattr(core.ArtifactDepotClient, "upload_chunk",
                        original_upload_chunk)
    second_sent = _spy_upload_chunk(monkeypatch)
    thread, results = _start_upload(client, path, "resume/fail.bin")
    thread.join(120)
    assert results and results[0][0] == "ok", results
    assert sorted(second_sent) == list(range(3, UPLOAD_CHUNKS))
    assert not state_file.exists()
    remote = _object_bytes(server, "resume/fail.bin")
    assert _sha256_bytes(remote) == _sha256_file(path)
    assert len(remote) == UPLOAD_SIZE


# ---------------------------------------------------------------------------
# 4. 多文件并发：各自状态文件互不覆盖
# ---------------------------------------------------------------------------

def test_concurrent_uploads_use_separate_state_files(server, tmp_path,
                                                     monkeypatch):
    client = _client(server)
    path_a = _make_upload_file(tmp_path, "multi-a.bin")
    path_b = _make_upload_file(tmp_path, "multi-b.bin")
    pause_a = threading.Event()
    pause_b = threading.Event()
    maybe_pause_a = _pauser(pause_a, 2 * CHUNK_SIZE)
    maybe_pause_b = _pauser(pause_b, 2 * CHUNK_SIZE)

    def progress_a(done, total, speed, msg):
        maybe_pause_a(done)

    def progress_b(done, total, speed, msg):
        maybe_pause_b(done)

    # 每个分片加一点延迟，保证两个上传真的并发并在暂停时都留下状态文件
    _spy_upload_chunk(monkeypatch, delay=0.01)
    state_a = _state_file(path_a)
    state_b = _state_file(path_b)
    thread_a, results_a = _start_upload(client, path_a, "resume/multi-a.bin",
                                        pause_event=pause_a, progress=progress_a)
    thread_b, results_b = _start_upload(client, path_b, "resume/multi-b.bin",
                                        pause_event=pause_b, progress=progress_b)

    assert _wait_for(lambda: state_a.exists() and state_b.exists()
                     and pause_a.is_set() and pause_b.is_set(), 120), \
        "两个文件应各自产生独立的状态文件"
    assert state_a != state_b
    assert state_a.parent == state_b.parent == path_a.parent

    data_a = json.loads(state_a.read_text(encoding="utf-8"))
    data_b = json.loads(state_b.read_text(encoding="utf-8"))
    assert data_a["key"] == "resume/multi-a.bin"
    assert data_b["key"] == "resume/multi-b.bin"
    assert data_a["file_path"] == str(path_a.resolve())
    assert data_b["file_path"] == str(path_b.resolve())
    assert data_a["upload_id"] != data_b["upload_id"], "并发上传的会话不能互相覆盖"

    pause_a.clear()
    pause_b.clear()
    thread_a.join(120)
    thread_b.join(120)
    assert results_a and results_a[0][0] == "ok", results_a
    assert results_b and results_b[0][0] == "ok", results_b
    assert not state_a.exists() and not state_b.exists()
    assert _sha256_bytes(_object_bytes(server, "resume/multi-a.bin")) == _sha256_file(path_a)
    assert _sha256_bytes(_object_bytes(server, "resume/multi-b.bin")) == _sha256_file(path_b)


# ---------------------------------------------------------------------------
# 5. 下载暂停/恢复：只补缺失分片（统计 Range 请求），SHA-256 一致
# ---------------------------------------------------------------------------

def _make_download_object(server, key: str) -> bytes:
    data = random.Random(2026).randbytes(DOWNLOAD_SIZE)
    _upload_http(server, key, Path(key).name, data)
    return data


def test_download_pause_resume_only_missing_chunks(server, tmp_path, monkeypatch):
    key = "resume/dl-pause.bin"
    data = _make_download_object(server, key)
    target = tmp_path / "dl-pause.bin"
    part = target.with_name(target.name + ".part")
    state_file = core.download_state_path(target)

    client = _client(server)
    requested = _spy_urlopen_ranges(monkeypatch)
    pause = threading.Event()
    maybe_pause = _pauser(pause, 2 * CHUNK_SIZE)

    def progress(done, total):
        maybe_pause(done)

    results = []

    def run():
        try:
            results.append(("ok", client.download_to(
                _download_url(key), target, progress=progress, pause_event=pause)))
        except BaseException as exc:
            results.append(("err", exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert _wait_for(lambda: pause.is_set() and state_file.exists(), 120), \
        "暂停后应保留下载断点状态"
    time.sleep(0.3)
    stable = list(requested)
    time.sleep(0.5)
    assert requested == stable, "暂停期间不应继续发出 Range 请求"
    assert thread.is_alive()

    assert part.exists(), "暂停应保留 .part（可续传）"
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["size"] == DOWNLOAD_SIZE
    assert payload["chunk_size"] == CHUNK_SIZE
    assert payload["done"], "状态文件应记录已完成分片"
    assert _state_mode(state_file) == 0o600

    pause.clear()
    thread.join(120)
    assert results and results[0][0] == "ok", results
    assert _sha256_file(target) == _sha256_bytes(data)
    assert not part.exists()
    assert not state_file.exists(), "下载成功后应删除断点状态"

    # 每个分片只请求一次（暂停/恢复不重复下载）
    chunk_count = (DOWNLOAD_SIZE + CHUNK_SIZE - 1) // CHUNK_SIZE
    assert _range_chunks(requested, CHUNK_SIZE) == list(range(chunk_count))


def test_download_failure_then_resume_only_missing(server, tmp_path, monkeypatch):
    key = "resume/dl-fail.bin"
    data = _make_download_object(server, key)
    target = tmp_path / "dl-fail.bin"
    part = target.with_name(target.name + ".part")
    state_file = core.download_state_path(target)
    client = _client(server)
    url = _download_url(key)
    probe = client._probe_download(url, 10)
    assert probe is not None and probe["size"] == DOWNLOAD_SIZE
    original_urlopen = core.urllib.request.urlopen

    # 串行下载：第 1 片成功落盘；起始偏移 = 1MiB 的第 2 片请求（含重试）全部断网
    _spy_urlopen_ranges(monkeypatch, fail_start=CHUNK_SIZE)
    monkeypatch.setattr(core, "_sleep_or_cancel", lambda *a, **k: False)
    with pytest.raises(core.ApiError):
        client._download_parallel(url, target, probe, CHUNK_SIZE, 1,
                                  None, 10.0, None, None)

    assert part.exists(), "失败应保留 .part"
    assert state_file.exists(), "失败应保留下载断点状态"
    done = set(json.loads(state_file.read_text(encoding="utf-8"))["done"])
    assert done == {0}, "已完成分片应写入断点状态"

    # 网络恢复：再次下载只补缺失分片（恢复真实 urlopen，避免复用断网替身）
    monkeypatch.setattr(core.urllib.request, "urlopen", original_urlopen)
    requested = _spy_urlopen_ranges(monkeypatch)
    saved = client.download_to(url, target, concurrency=2)

    assert saved == target
    assert _sha256_file(target) == _sha256_bytes(data)

    chunk_count = (DOWNLOAD_SIZE + CHUNK_SIZE - 1) // CHUNK_SIZE
    expected_missing = sorted(set(range(chunk_count)) - done)
    assert _range_chunks(requested, CHUNK_SIZE) == expected_missing, \
        "恢复时应只请求缺失分片"
    assert not part.exists()
    assert not state_file.exists()


# ---------------------------------------------------------------------------
# 6. 会话失效：清本地状态并新建会话重传（契约 C2 第 4 步）
# ---------------------------------------------------------------------------

def test_session_lost_clears_local_state_and_restarts(server, tmp_path,
                                                     monkeypatch):
    path = _make_upload_file(tmp_path, "lost.bin")
    client = _client(server)
    pause = threading.Event()
    maybe_pause = _pauser(pause, 2 * CHUNK_SIZE)

    def progress(done, total, speed, msg):
        maybe_pause(done)

    cancel = threading.Event()
    thread, results = _start_upload(client, path, "resume/lost.bin",
                                    pause_event=pause, cancel_event=cancel,
                                    progress=progress)
    state_file = _state_file(path)
    assert _wait_for(lambda: state_file.exists() and pause.is_set(), 60)
    time.sleep(0.3)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    upload_id = state["upload_id"]
    assert upload_id
    cancel.set()
    thread.join(60)
    assert results and results[0][0] == "err"

    # 主动 abort 服务端会话，模拟「超过 TTL / 服务重启 / 被清理」
    client.abort(upload_id)

    logs = []
    sent_again = _spy_upload_chunk(monkeypatch)
    thread, results = _start_upload(client, path, "resume/lost.bin",
                                    log=logs.append)
    thread.join(120)
    assert results and results[0][0] == "ok", results
    assert any("失效" in line and "重新开始" in line for line in logs), logs
    # 新会话：全部分片重传（旧会话已失效，服务端 uploaded 为空）
    assert sorted(sent_again) == list(range(UPLOAD_CHUNKS))
    assert not state_file.exists()
    assert _sha256_bytes(_object_bytes(server, "resume/lost.bin")) == _sha256_file(path)


# ---------------------------------------------------------------------------
# 7. 源目录不可写 → 回退 ~/.artifactdepot/upload-state/<sha1>.json（可选）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root 不受目录权限限制，跳过回退测试")
def test_unwritable_source_dir_falls_back_to_home_state(server, tmp_path,
                                                        monkeypatch):
    path = _make_upload_file(tmp_path, "readonly.bin")
    src = path.parent
    fallback_dir = tmp_path / "fallback-state"
    monkeypatch.setattr(core, "UPLOAD_STATE_DIR", fallback_dir)

    logs = []
    pause = threading.Event()
    maybe_pause = _pauser(pause, 2 * CHUNK_SIZE)

    def progress(done, total, speed, msg):
        maybe_pause(done)

    client = _client(server)
    os.chmod(src, 0o500)  # 不可写（r-x）
    try:
        state_primary = _state_file(path)
        fallback = core.fallback_upload_state_path(server.base, BUCKET,
                                                   "resume/readonly.bin")
        assert fallback.parent == fallback_dir
        thread, results = _start_upload(client, path, "resume/readonly.bin",
                                        pause_event=pause, progress=progress,
                                        log=logs.append)
        assert _wait_for(lambda: fallback.exists() and pause.is_set(), 120), \
            "源目录不可写时应回退到 home 状态目录"
        time.sleep(0.3)
        assert not state_primary.exists(), "源目录不可写时不应有主状态文件"
        assert _state_mode(fallback) == 0o600
        payload = json.loads(fallback.read_text(encoding="utf-8"))
        assert payload["upload_id"]
        assert payload["key"] == "resume/readonly.bin"
        assert any("回退" in line for line in logs), f"应提示回退路径：{logs}"

        pause.clear()
        thread.join(120)
        assert results and results[0][0] == "ok", results
        assert not fallback.exists(), "上传成功后应删除回退状态文件"
    finally:
        os.chmod(src, 0o700)
    assert _sha256_bytes(_object_bytes(server, "resume/readonly.bin")) == _sha256_file(path)


# ---------------------------------------------------------------------------
# 单元测试（不依赖服务端）：状态文件路径/权限、旧全局状态一次性迁移
# ---------------------------------------------------------------------------

def test_unit_state_paths_follow_companion_layout(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    path = src / "movie.mkv"
    path.write_bytes(b"x")

    assert core.upload_state_path(path) == src / ".movie.mkv.artifactdepot-upload.json"
    assert core.download_state_path(tmp_path / "dl.bin") == \
        tmp_path / ".dl.bin.artifactdepot-download.json"
    # 旧的全局状态路径只用于迁移，不再作为默认写入位置
    assert core.upload_state_path(path) != core.LEGACY_STATE_PATH
    fallback = core.fallback_upload_state_path("http://s", "b", "k")
    assert fallback.parent == core.UPLOAD_STATE_DIR
    assert fallback.name.endswith(".json")
    assert fallback == core.fallback_upload_state_path("http://s/", "b", "k")


def test_unit_state_files_are_0600_and_roundtrip(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")
    stat_result = path.stat()
    state = core.ResumeState(
        server="http://s", bucket="b", key="k",
        file_path=str(path.resolve()), file_size=5,
        file_mtime_ns=stat_result.st_mtime_ns, chunk_size=core.CHUNK_SIZE,
        upload_id="upload-1", uploaded=[0], total_chunks=1, staging="target")
    saved = state.save()
    assert saved == core.upload_state_path(path)
    assert _state_mode(saved) == 0o600
    loaded = core.ResumeState.load(saved)
    assert loaded is not None
    assert loaded.upload_id == "upload-1"
    assert loaded.uploaded == [0]
    assert loaded.version == core.UPLOAD_STATE_VERSION
    loaded.clear()
    assert not saved.exists()

    dl_state = core.DownloadState(size=10, etag='"abc"', chunk_size=5, done=[0])
    dl_path = tmp_path / ".dl.bin.artifactdepot-download.json"
    assert dl_state.save(dl_path)
    assert _state_mode(dl_path) == 0o600
    assert core.DownloadState.load(dl_path).done == [0]
    core.DownloadState.clear(dl_path)
    assert not dl_path.exists()


def test_unit_legacy_global_state_migrates_once(tmp_path, monkeypatch):
    path = tmp_path / "legacy.bin"
    path.write_bytes(b"legacy")
    stat_result = path.stat()
    legacy_path = tmp_path / "legacy-state.json"
    monkeypatch.setattr(core, "LEGACY_STATE_PATH", legacy_path)
    legacy_path.write_text(json.dumps({
        "server": "http://legacy", "bucket": "b", "key": "k",
        "file_path": str(path.resolve()), "file_size": 6,
        "file_mtime_ns": stat_result.st_mtime_ns,
        "chunk_size": core.CHUNK_SIZE, "upload_id": "legacy-upload",
        "uploaded": [0], "updated_at": 1.0,
    }), encoding="utf-8")

    logs = []
    state = core._migrate_legacy_state("http://legacy", "b", "k", path,
                                       stat_result, logs.append)

    assert state is not None
    assert state.upload_id == "legacy-upload"
    new_path = core.upload_state_path(path)
    assert new_path.exists(), "应迁移到源文件目录"
    payload = json.loads(new_path.read_text(encoding="utf-8"))
    assert payload["version"] == core.UPLOAD_STATE_VERSION
    assert payload["total_chunks"] == 1
    assert not legacy_path.exists(), "迁移后应删除旧全局状态文件"
    assert any("迁移" in line for line in logs)


def test_unit_legacy_state_for_other_file_is_kept(tmp_path, monkeypatch):
    path = tmp_path / "mine.bin"
    path.write_bytes(b"mine")
    stat_result = path.stat()
    legacy_path = tmp_path / "legacy-state.json"
    monkeypatch.setattr(core, "LEGACY_STATE_PATH", legacy_path)
    legacy_path.write_text(json.dumps({
        "server": "http://legacy", "bucket": "other", "key": "other",
        "file_path": str((tmp_path / "other.bin").resolve()),
        "file_size": 1, "file_mtime_ns": 1,
        "chunk_size": core.CHUNK_SIZE, "upload_id": "legacy-upload",
        "uploaded": [0], "updated_at": 1.0,
    }), encoding="utf-8")

    assert core._migrate_legacy_state("http://legacy", "b", "k", path,
                                      stat_result) is None
    assert legacy_path.exists(), "属于其他文件的旧状态不应被删除"
    assert not core.upload_state_path(path).exists()
