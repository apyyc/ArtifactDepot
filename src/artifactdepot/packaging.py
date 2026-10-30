# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""目录签名链接打包（.package）

设计目标（与 `signed_links` / `zipstream` 配合）：
- 目录签名链接创建后立即返回 URL，打包在**后台线程**完成，前端轮询进度；
- 成品落在源目录内的隐藏目录 `<bucket>/<目录>/.package/<link_id>/`：
  - 目录里只有 1 个可见文件且无可见子目录 → 直接复制该文件（与 download-bulk 语义一致）；
  - 其他非空目录 → 生成 `<目录名>.zip`（ZIP_STORED 默认，可配 deflate）；
- 成品先写临时文件，完成后 `os.replace` 原子改名，再写 `.ready.json`；
- 打包失败/取消 → 删除半成品并**自动作废**签名链接（管理端可重新生成）；
- 链接失效（过期/次数用完/作废）后台清理，或作废时立即清理（可配）；
- 链接注册表仍是唯一事实源；进程重启后按 link 注册表恢复未完成的打包任务。

线程模型：单进程（与上传会话一致）。打包任务在 daemon 线程执行，进度保存在
进程内 registry；`.ready.json` 是持久化完成标记，重启后仍可下载已完成包。
"""
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path

from artifactdepot import storage
from artifactdepot.config import get_package_settings

logger = logging.getLogger("artifactdepot")

# 完成标记（放在 .package/<link_id>/ 内，自身也是隐藏文件）
READY_FILE = ".ready.json"
# 临时文件前缀：成功后 os.replace；失败/取消由清理逻辑删除
_TMP_PREFIX = ".part-"
# link_id 由 secrets.token_urlsafe(8) 生成；读取磁盘/请求参数时仍做白名单校验
_LINK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 进度写回的最小间隔（避免大目录每个 256KiB 块都抢锁）
_PROGRESS_INTERVAL = 0.15
# 每个文件处理前检查一次链接是否仍有效（避免长任务被作废后继续跑）
_ACTIVE_CHECK_EVERY = 200

_JOBS: dict = {}
_JOBS_LOCK = threading.RLock()


class _Cancelled(Exception):
    """链接被作废 / 用户取消 / 服务关闭：打包线程用它快速退出。"""


# ---------------------------------------------------------------------------
# 路径与就绪标记
# ---------------------------------------------------------------------------

def _check_link_id(link_id: str) -> str:
    text = str(link_id or "")
    if not _LINK_ID_RE.match(text):
        raise ValueError(f"非法 link_id：{link_id!r}")
    return text


def package_dir(bucket: str, key: str, link_id: str) -> Path:
    """`<bucket>/<key>/.package/<link_id>/`（key 必须指向目录）。"""
    link_id = _check_link_id(link_id)
    base = storage.object_path(bucket, key)
    return base / storage.PACKAGE_DIR / link_id


def read_ready(bucket: str, key: str, link_id: str) -> dict | None:
    """读取就绪标记；文件不存在 / 解析失败 / 成品缺失时返回 None。"""
    try:
        p = package_dir(bucket, key, link_id)
    except Exception:                              # noqa: BLE001 - 非法 key/link_id 一律视为未就绪
        return None
    meta_path = p / READY_FILE
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("link_id") != link_id:
        return None
    artifact = str(data.get("artifact") or "")
    if not artifact or Path(artifact).name != artifact:
        return None
    if not (p / artifact).is_file():
        return None
    return data


def artifact_path(bucket: str, key: str, link_id: str) -> Path | None:
    """返回成品文件路径（仅当 `.ready.json` 存在且成品仍存在）。"""
    ready = read_ready(bucket, key, link_id)
    if ready is None:
        return None
    return package_dir(bucket, key, link_id) / str(ready["artifact"])


def _media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _is_dir_link(entry: dict | None) -> bool:
    return bool(entry) and entry.get("kind") == "dir"


def _link_invalid_at(entry: dict, now: float | None = None) -> float | None:
    """返回链接失效时间戳；仍有效返回 None（用于清理宽限期）。"""
    if not _is_dir_link(entry):
        return None
    now = time.time() if now is None else now
    if entry.get("revoked"):
        return float(entry.get("revoked_at") or 0) or now
    mode = entry.get("mode")
    if mode == "time":
        expires = float(entry.get("expires") or 0)
        if expires and now > expires:
            return expires
        return None
    if mode == "count" and int(entry.get("remaining") or 0) <= 0:
        return float(entry.get("exhausted_at") or 0) or now
    return None


def link_valid(entry: dict | None) -> bool:
    """链接当前是否仍可用（dir/file 通用；不含 tk 校验）。"""
    return bool(entry) and _link_invalid_at(entry) is None


# ---------------------------------------------------------------------------
# 任务 registry
# ---------------------------------------------------------------------------

def _new_job(link_id: str, entry: dict, actor: str = "", ip: str = "") -> dict:
    return {
        "link_id": link_id,
        "bucket": entry.get("bucket", ""),
        "key": entry.get("key", ""),
        "actor": actor or entry.get("created_by") or "",
        "ip": ip or "",
        "state": "queued",
        "phase": "queued",
        "files_total": 0,
        "files_done": 0,
        "bytes_total": 0,
        "bytes_done": 0,
        "error": "",
        "artifact": {},
        "started_at": time.time(),
        "ready_at": 0.0,
        "last_progress_at": 0.0,
        "cancel": threading.Event(),
        "thread": None,
        "cancel_reason": "",
        "keep_link": False,
    }


def _set_job(job: dict, **fields) -> None:
    with _JOBS_LOCK:
        job.update(fields)


def _progress(job: dict, *, phase: str | None = None, files_done: int | None = None,
              bytes_done: int | None = None, files_total: int | None = None,
              bytes_total: int | None = None, force: bool = False) -> None:
    now = time.monotonic()
    with _JOBS_LOCK:
        if phase is not None:
            job["phase"] = phase
        if files_total is not None:
            job["files_total"] = files_total
        if bytes_total is not None:
            job["bytes_total"] = bytes_total
        if files_done is not None:
            job["files_done"] = files_done
        if bytes_done is not None:
            job["bytes_done"] = bytes_done
        if not force and now - job.get("last_progress_at", 0.0) < _PROGRESS_INTERVAL:
            return
        job["last_progress_at"] = now


def _snapshot(job: dict, ready: dict | None = None) -> dict:
    with _JOBS_LOCK:
        total = int(job.get("bytes_total") or 0)
        done = int(job.get("bytes_done") or 0)
        state = job.get("state") or "queued"
        percent = 100.0 if state == "ready" else (min(99.9, done / total * 100) if total > 0 else 0.0)
        if state in ("failed", "cancelled") and total <= 0:
            percent = 0.0
        return {
            "link_id": job["link_id"],
            "bucket": job["bucket"],
            "key": job["key"],
            "state": state,
            "phase": job.get("phase") or "",
            "files_total": int(job.get("files_total") or 0),
            "files_done": int(job.get("files_done") or 0),
            "bytes_total": total,
            "bytes_done": done,
            "percent": round(percent, 1),
            "error": job.get("error") or "",
            "artifact": dict(job.get("artifact") or {}),
            "started_at": job.get("started_at") or 0,
            "ready_at": job.get("ready_at") or 0,
            "elapsed_ms": int(max(0.0, (job.get("ready_at") or time.time())
                                  - (job.get("started_at") or time.time())) * 1000),
            "ready_artifact": dict(ready or {}),
        }


def _ready_snapshot(link_id: str, entry: dict, ready: dict) -> dict:
    """没有进程内 job（重启/已完成）时，用就绪标记拼一个 status。"""
    now = time.time()
    return {
        "link_id": link_id,
        "bucket": entry.get("bucket", ""),
        "key": entry.get("key", ""),
        "state": "ready",
        "phase": "ready",
        "files_total": int(ready.get("files") or 0),
        "files_done": int(ready.get("files") or 0),
        "bytes_total": int(ready.get("input_bytes") or 0),
        "bytes_done": int(ready.get("input_bytes") or 0),
        "percent": 100.0,
        "error": "",
        "artifact": {
            "name": ready.get("artifact") or "",
            "size": int(ready.get("size") or 0),
            "kind": ready.get("kind") or "",
            "media_type": ready.get("media_type") or "",
        },
        "started_at": float(ready.get("created_at_ts") or 0),
        "ready_at": float(ready.get("created_at_ts") or 0),
        "elapsed_ms": int(float(ready.get("elapsed_ms") or 0)),
        "ready_artifact": dict(ready),
    }


def job_status(link_id: str) -> dict:
    """查询打包状态：内存 job → 就绪标记 → 失效/缺失。"""
    link_id = str(link_id or "")
    entry = storage.get_signed_link(link_id)
    with _JOBS_LOCK:
        job = _JOBS.get(link_id)
        if job is not None:
            ready = None
            if job.get("state") == "ready":
                ready = read_ready(job["bucket"], job["key"], link_id)
                if ready is None:
                    # 成品被清理/手工删除：状态回退为 missing，避免 UI 一直显示就绪
                    job["state"] = "missing"
                    job["phase"] = "missing"
                    job["error"] = "打包成品已不存在，请重新生成链接"
            return _snapshot(job, ready)
    if not entry:
        return {"link_id": link_id, "state": "missing", "phase": "missing",
                "error": "链接不存在", "percent": 0.0, "files_total": 0,
                "files_done": 0, "bytes_total": 0, "bytes_done": 0,
                "artifact": {}, "ready_artifact": {}}
    if entry.get("kind") != "dir":
        return {"link_id": link_id, "state": "missing", "phase": "missing",
                "error": "该链接不是目录打包链接", "percent": 0.0,
                "files_total": 0, "files_done": 0, "bytes_total": 0,
                "bytes_done": 0, "artifact": {}, "ready_artifact": {}}
    ready = read_ready(entry.get("bucket", ""), entry.get("key", ""), link_id)
    if ready is not None:
        return _ready_snapshot(link_id, entry, ready)
    if _link_invalid_at(entry) is not None:
        return {"link_id": link_id, "bucket": entry.get("bucket", ""),
                "key": entry.get("key", ""), "state": "invalid", "phase": "invalid",
                "error": "链接已失效", "percent": 0.0, "files_total": 0,
                "files_done": 0, "bytes_total": 0, "bytes_done": 0,
                "artifact": {}, "ready_artifact": {}}
    return {"link_id": link_id, "bucket": entry.get("bucket", ""),
            "key": entry.get("key", ""), "state": "missing", "phase": "missing",
            "error": "打包任务不存在（可重新生成链接）", "percent": 0.0,
            "files_total": 0, "files_done": 0, "bytes_total": 0,
            "bytes_done": 0, "artifact": {}, "ready_artifact": {}}


def link_package_state(entry: dict) -> str:
    """签名链接列表用：file 链接返回 ""，目录链接返回 ready/queued/running/failed/missing/invalid。"""
    if not _is_dir_link(entry):
        return ""
    link_id = str(entry.get("id") or "")
    with _JOBS_LOCK:
        job = _JOBS.get(link_id)
        if job is not None:
            state = job.get("state") or ""
            return "running" if state in ("scanning", "packaging") else state
    if read_ready(entry.get("bucket", ""), entry.get("key", ""), link_id) is not None:
        return "ready"
    if _link_invalid_at(entry) is not None:
        return "invalid"
    return "missing"


# ---------------------------------------------------------------------------
# 打包执行
# ---------------------------------------------------------------------------

def _acquire_slot(job: dict) -> bool:
    """按 package.max_concurrent_jobs 限流；排队期间可被取消。"""
    while True:
        with _JOBS_LOCK:
            if job["cancel"].is_set():
                return False
            limit = int(get_package_settings().get("max_concurrent_jobs", 1) or 1)
            active = sum(1 for j in _JOBS.values()
                         if j is not job and j.get("state") in ("scanning", "packaging"))
            if active < max(1, limit):
                job["state"] = "scanning"
                job["phase"] = "scanning"
                return True
        job["cancel"].wait(0.2)


def _ensure_active(job: dict) -> None:
    if job["cancel"].is_set():
        raise _Cancelled()
    entry = storage.get_signed_link(job["link_id"])
    if entry is None or entry.get("revoked"):
        raise _Cancelled()


def _audit(job: dict, phase: str, *, size: int = 0, extra: dict | None = None) -> None:
    body = {"phase": phase, "link_id": job["link_id"]}
    if extra:
        body.update(extra)
    storage.audit("package", job["bucket"], job["key"], job.get("actor", ""),
                  job.get("ip", ""), size=size, extra=body)


def _rm_package(bucket: str, key: str, link_id: str) -> None:
    """删除 .package/<link_id>；父 .package 空了也顺手清掉。"""
    try:
        p = package_dir(bucket, key, link_id)
    except (ValueError, OSError):
        return
    shutil.rmtree(p, ignore_errors=True)
    try:
        p.parent.rmdir()
    except OSError:
        pass


def _write_ready(dest: Path, meta: dict) -> None:
    storage._atomic_write_text(dest / READY_FILE,
                               json.dumps(meta, ensure_ascii=False, indent=2), mode=0o600)


def _copy_with_progress(job: dict, src: Path, dst: Path, total: int) -> int:
    """单文件复制（唯一文件直下场景），256KiB 分块 + 进度/取消。"""
    done = 0
    with open(src, "rb") as sf, open(dst, "wb") as df:
        while True:
            if job["cancel"].is_set():
                raise _Cancelled()
            chunk = sf.read(256 * 1024)
            if not chunk:
                break
            df.write(chunk)
            done += len(chunk)
            _progress(job, phase="packaging", files_done=1 if done >= total else 0,
                      bytes_done=done)
        df.flush()
        os.fsync(df.fileno())
    return done


def _zip_date_time(ts) -> tuple:
    try:
        lt = time.localtime(float(ts)) if ts is not None else time.localtime()
    except (TypeError, ValueError, OverflowError, OSError):
        lt = time.localtime()
    if lt.tm_year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)


def _zip_info(name: str, ts, is_dir: bool = False,
              compression: int = zipfile.ZIP_STORED) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.date_time = _zip_date_time(ts)
    info.compress_type = compression
    if is_dir:
        info.external_attr = (0o40755 << 16) | 0x10
    return info


def _build_zip(job: dict, entries: list, root_name: str, dst: Path,
               compression: int, total_bytes: int) -> dict:
    """把 scan 结果写成 ZIP 文件（ZIP_STORED / ZIP_DEFLATED，支持 ZIP64）。"""
    files_done = bytes_done = 0
    files_total = sum(1 for e in entries if not e["is_dir"])
    dirs_total = sum(1 for e in entries if e["is_dir"])
    with zipfile.ZipFile(dst, mode="w", compression=compression,
                         allowZip64=True) as zf:
        zf.writestr(_zip_info(root_name + "/", None, is_dir=True,
                              compression=compression), b"")
        for idx, ent in enumerate(entries):
            if job["cancel"].is_set():
                raise _Cancelled()
            if idx % _ACTIVE_CHECK_EVERY == 0:
                _ensure_active(job)
            name = ent["name"]
            if ent["is_dir"]:
                zf.writestr(_zip_info(f"{root_name}/{name}/", ent.get("mtime"),
                                      is_dir=True, compression=compression), b"")
                continue
            try:
                src = open(ent["path"], "rb")
            except FileNotFoundError:
                continue                  # 扫描后被并发删除：跳过，不破坏 ZIP
            except OSError:
                continue                  # 不可读文件：跳过
            with src, zf.open(_zip_info(f"{root_name}/{name}", ent.get("mtime"),
                                        compression=compression), mode="w") as zout:
                while True:
                    if job["cancel"].is_set():
                        raise _Cancelled()
                    chunk = src.read(256 * 1024)
                    if not chunk:
                        break
                    zout.write(chunk)
                    bytes_done += len(chunk)
                    _progress(job, phase="packaging", files_done=files_done,
                              bytes_done=bytes_done)
            files_done += 1
            _progress(job, phase="packaging", files_done=files_done,
                      bytes_done=bytes_done)
    return {"files": files_done, "dirs": dirs_total, "bytes_done": bytes_done,
            "files_total": files_total}


def _run_job(job: dict) -> None:
    link_id = job["link_id"]
    bucket = job["bucket"]
    key = job["key"]
    try:
        if not _acquire_slot(job):
            raise _Cancelled()
        settings = get_package_settings()
        if not settings["enabled"]:
            raise RuntimeError("目录打包已关闭（package.enabled=false）")
        _audit(job, "start")
        _ensure_active(job)
        _progress(job, phase="scanning", force=True)
        scan = storage.bulk_download_scan(bucket, key, max_items=settings["max_items"])
        total_bytes = sum(int(e["size"]) for e in scan["entries"] if not e["is_dir"])
        limit_mb = int(settings["max_total_mb"] or 0)
        if limit_mb and total_bytes > limit_mb * 1024 * 1024:
            raise RuntimeError(
                f"目录源文件总大小 {total_bytes} 字节超过 package.max_total_mb={limit_mb}MB")
        _progress(job, phase="packaging",
                  files_total=scan["files"], bytes_total=total_bytes, force=True)
        dest = package_dir(bucket, key, link_id)
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        single = scan["single_file"]
        if single is not None:
            artifact = Path(single["name"]).name
            tmp = dest / (_TMP_PREFIX + secrets.token_hex(6))
            _copy_with_progress(job, single["path"], tmp, total_bytes)
            os.replace(tmp, dest / artifact)
            kind = "file"
            out_size = total_bytes
            files = 1
            dirs = 0
            media = _media_type(artifact)
        else:
            root_name = scan["root_name"]
            artifact = f"{root_name}.zip"
            tmp = dest / (_TMP_PREFIX + secrets.token_hex(6) + ".zip")
            compression = (zipfile.ZIP_DEFLATED
                           if settings["zip_compression"] == "deflate"
                           else zipfile.ZIP_STORED)
            counted = _build_zip(job, scan["entries"], root_name, tmp,
                                 compression, total_bytes)
            os.replace(tmp, dest / artifact)
            kind = "zip"
            out_size = (dest / artifact).stat().st_size
            files = counted["files"]
            dirs = counted["dirs"]
            media = "application/zip"
        elapsed_ms = int(max(0.0, time.monotonic() - started) * 1000)
        ready = {
            "link_id": link_id,
            "bucket": bucket,
            "key": key,
            "kind": kind,                 # file（唯一文件直下）/ zip
            "artifact": artifact,
            "media_type": media,
            "size": int(out_size),
            "input_bytes": int(total_bytes),
            "files": int(files),
            "dirs": int(dirs),
            "zip_compression": settings["zip_compression"],
            "created_at": storage._utc_now_iso(),
            "created_at_ts": time.time(),
            "elapsed_ms": elapsed_ms,
        }
        _write_ready(dest, ready)
        with _JOBS_LOCK:
            job["state"] = "ready"
            job["phase"] = "ready"
            job["ready_at"] = time.time()
            job["files_done"] = files
            job["bytes_done"] = total_bytes
            job["artifact"] = {"name": artifact, "size": int(out_size),
                               "kind": kind, "media_type": media}
        _audit(job, "done", size=int(out_size),
               extra={"files": files, "dirs": dirs, "kind": kind,
                      "elapsed_ms": elapsed_ms, "compression": settings["zip_compression"]})
    except _Cancelled:
        keep = bool(job.get("keep_link"))
        reason = job.get("cancel_reason") or ""
        _rm_package(bucket, key, link_id)
        if not keep:
            storage.revoke_signed_link(link_id)
        with _JOBS_LOCK:
            job["state"] = "cancelled"
            job["phase"] = "cancelled"
            job["ready_at"] = time.time()
            job["error"] = ("打包任务已因目录变更中断，链接保留" if keep
                            else "打包已取消或链接已失效，链接已作废")
        _audit(job, "cancel", extra={"reason": reason} if reason else None)
    except BaseException as exc:                  # noqa: BLE001 - 后台线程必须兜底
        detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
        logger.warning("[打包] 目录打包失败 %s/%s：%s", bucket, key, detail)
        _rm_package(bucket, key, link_id)
        storage.revoke_signed_link(link_id)
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["phase"] = "failed"
            job["ready_at"] = time.time()
            job["error"] = str(detail)[:500]
        _audit(job, "fail", extra={"error": str(detail)[:500]})


def _job_thread(job: dict) -> None:
    try:
        _run_job(job)
    finally:
        with _JOBS_LOCK:
            job["thread"] = None


def start_job(link_id: str, actor: str = "", ip: str = "") -> dict:
    """启动（或复用）目录打包任务；返回当前状态快照。"""
    link_id = _check_link_id(link_id)
    entry = storage.get_signed_link(link_id)
    if not entry or entry.get("kind") != "dir":
        return job_status(link_id)
    ready = read_ready(entry.get("bucket", ""), entry.get("key", ""), link_id)
    if ready is not None:
        return _ready_snapshot(link_id, entry, ready)
    if not link_valid(entry):
        return job_status(link_id)
    with _JOBS_LOCK:
        job = _JOBS.get(link_id)
        if job is not None and job.get("state") in ("queued", "scanning", "packaging"):
            return _snapshot(job)
        job = _new_job(link_id, entry, actor=actor, ip=ip)
        _JOBS[link_id] = job
        thread = threading.Thread(target=_job_thread, args=(job,),
                                  name=f"package-{link_id}", daemon=True)
        job["thread"] = thread
        thread.start()
    return _snapshot(job)


def ensure_job(link_id: str, actor: str = "", ip: str = "") -> dict:
    """链接仍有效但既无成品也无任务时（重启/丢失）自动补一次打包。"""
    entry = storage.get_signed_link(link_id)
    if not entry or entry.get("kind") != "dir" or not link_valid(entry):
        return job_status(link_id)
    if read_ready(entry.get("bucket", ""), entry.get("key", ""), link_id) is not None:
        return job_status(link_id)
    return start_job(link_id, actor=actor, ip=ip)


def cancel_job(link_id: str, actor: str = "", ip: str = "") -> dict:
    """取消打包并作废链接（失败/取消会自动作废，保持一致）。"""
    link_id = str(link_id or "")
    entry = storage.get_signed_link(link_id)
    if not entry:
        return job_status(link_id)
    if actor:
        entry = dict(entry)   # 避免外部修改缓存对象
    with _JOBS_LOCK:
        job = _JOBS.get(link_id)
    if job is not None:
        job["cancel"].set()
    # 立即作废；线程收尾时还会再删一次包（幂等）
    storage.revoke_signed_link(link_id)
    settings = get_package_settings()
    if bool(settings.get("delete_on_revoke", True)):
        _rm_package(entry.get("bucket", ""), entry.get("key", ""), link_id)
    with _JOBS_LOCK:
        job = _JOBS.get(link_id)
        if job is not None and job.get("state") not in ("ready", "failed", "cancelled"):
            job["state"] = "cancelled"
            job["phase"] = "cancelled"
            job["ready_at"] = time.time()
            job["error"] = "已取消打包，链接已作废"
    return job_status(link_id)


def on_revoke(link_id: str, *, delete_now: bool | None = None) -> None:
    """作废链接后的清理钩子（API revoke 调用）：尽量立即释放包和线程。"""
    entry = storage.get_signed_link(link_id)
    with _JOBS_LOCK:
        job = _JOBS.get(link_id)
    if job is not None:
        job["cancel"].set()
    if entry and entry.get("kind") == "dir":
        do_delete = bool(get_package_settings().get("delete_on_revoke", True)) \
            if delete_now is None else bool(delete_now)
        if do_delete:
            _rm_package(entry.get("bucket", ""), entry.get("key", ""), link_id)
            _audit({"link_id": link_id, "bucket": entry.get("bucket", ""),
                    "key": entry.get("key", ""), "actor": entry.get("created_by", ""),
                    "ip": ""}, "cleanup", extra={"reason": "revoke"})


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

def cleanup_invalid_packages() -> int:
    """删除已失效（过期/次数用完/作废）超过宽限期的目录包；返回删除数量。"""
    settings = get_package_settings()
    grace = float(settings.get("cleanup_grace_seconds", 60) or 0)
    now = time.time()
    removed = 0
    for entry in storage.load_signed_links():
        if not _is_dir_link(entry):
            continue
        invalid_at = _link_invalid_at(entry, now)
        if invalid_at is None or now - invalid_at < grace:
            continue
        link_id = str(entry.get("id") or "")
        if not link_id:
            continue
        p = None
        try:
            p = package_dir(entry.get("bucket", ""), entry.get("key", ""), link_id)
        except (ValueError, OSError):
            p = None
        existed = bool(p is not None and p.exists())
        if p is not None:
            shutil.rmtree(p, ignore_errors=True)
            try:
                p.parent.rmdir()
            except OSError:
                pass
        with _JOBS_LOCK:
            _JOBS.pop(link_id, None)
        if not existed:
            continue                      # 包已不存在：只清理内存任务，不重复写审计
        removed += 1
        _audit({"link_id": link_id, "bucket": entry.get("bucket", ""),
                "key": entry.get("key", ""), "actor": entry.get("created_by", ""),
                "ip": ""}, "cleanup", extra={"reason": "invalid"})
    return removed


def cleanup_on_startup() -> dict:
    """启动清理：失效包删除；有效但未完成的包删除半成品并恢复打包任务。

    返回 {"removed": n, "resumed": m}（不抛异常，单个链接失败不影响其它链接）。
    """
    settings = get_package_settings()
    removed = resumed = 0
    if not bool(settings.get("cleanup_on_startup", True)):
        return {"removed": 0, "resumed": 0}
    for entry in storage.load_signed_links():
        if not _is_dir_link(entry):
            continue
        link_id = str(entry.get("id") or "")
        if not link_id:
            continue
        bucket, key = entry.get("bucket", ""), entry.get("key", "")
        try:
            if _link_invalid_at(entry) is not None:
                _rm_package(bucket, key, link_id)
                removed += 1
                continue
            if read_ready(bucket, key, link_id) is not None:
                continue
            # 有效但未完成：删除上次进程残留的半成品，重新排队
            _rm_package(bucket, key, link_id)
            if settings.get("enabled", True):
                start_job(link_id)
                resumed += 1
        except Exception:                          # noqa: BLE001 - 启动清理尽力而为
            logger.warning("[打包] 启动恢复失败：%s", link_id, exc_info=True)
    if removed:
        logger.info("[清理] 启动清理失效目录包：%s 个", removed)
    if resumed:
        logger.info("[打包] 启动恢复未完成的目录打包：%s 个", resumed)
    return {"removed": removed, "resumed": resumed}


# ---------------------------------------------------------------------------
# 源目录变更钩子（rename / delete / bulk-delete）
# ---------------------------------------------------------------------------

def _links_under(bucket: str, prefix: str, subtree: bool = True) -> list:
    prefix = (prefix or "").strip("/")
    out = []
    for entry in storage.load_signed_links():
        if entry.get("bucket") != bucket or not _is_dir_link(entry):
            continue
        k = (entry.get("key") or "").strip("/")
        if not prefix:
            # bucket 根：all 模式覆盖整个 bucket；exact 模式没有「根目录链接」这一说
            if subtree:
                out.append(entry)
            continue
        if k == prefix or (subtree and k.startswith(prefix + "/")):
            out.append(entry)
    return out


def prepare_rename(bucket: str, old_key: str) -> None:
    """目录重命名前：取消受影响目录链接的打包线程，清掉未完成的半成品。

    已完成的包会随目录一起被 rename 搬走，链接 key 由 storage.rename_object 同步更新。
    """
    for entry in _links_under(bucket, old_key, subtree=True):
        link_id = str(entry.get("id") or "")
        if not link_id:
            continue
        with _JOBS_LOCK:
            job = _JOBS.get(link_id)
        if job is None or job.get("state") == "ready":
            continue                      # 已完成的包随目录一起搬走，不打断
        job["keep_link"] = True
        job["cancel_reason"] = "rename"
        job["cancel"].set()
        thread = job.get("thread")
        if thread is not None:
            thread.join(5.0)
        _rm_package(bucket, entry.get("key", ""), link_id)
        with _JOBS_LOCK:
            _JOBS.pop(link_id, None)


def resume_links(bucket: str, prefix: str) -> int:
    """目录改名/回滚后：对仍未完成的有效目录链接重新排队打包。"""
    started = 0
    if not get_package_settings().get("enabled", True):
        return 0
    for entry in _links_under(bucket, prefix, subtree=True):
        if not link_valid(entry):
            continue
        link_id = str(entry.get("id") or "")
        if not link_id:
            continue
        if read_ready(entry.get("bucket", ""), entry.get("key", ""), link_id) is not None:
            continue
        with _JOBS_LOCK:
            job = _JOBS.get(link_id)
        if job is not None and job.get("state") in ("queued", "scanning", "packaging"):
            continue
        try:
            start_job(link_id)
            started += 1
        except Exception:                          # noqa: BLE001
            logger.warning("[打包] 恢复打包失败：%s", link_id, exc_info=True)
    return started


def prepare_delete(bucket: str, prefix: str, subtree: bool = True) -> int:
    """删除目录/清空目录内容前：作废相关目录链接并删包（返回处理链接数）。"""
    count = 0
    for entry in _links_under(bucket, prefix, subtree=subtree):
        link_id = str(entry.get("id") or "")
        if not link_id:
            continue
        with _JOBS_LOCK:
            job = _JOBS.get(link_id)
        if job is not None:
            job["cancel"].set()
        storage.revoke_signed_link(link_id)
        _rm_package(bucket, entry.get("key", ""), link_id)
        with _JOBS_LOCK:
            _JOBS.pop(link_id, None)
        count += 1
        _audit({"link_id": link_id, "bucket": bucket,
                "key": entry.get("key", ""), "actor": entry.get("created_by", ""),
                "ip": ""}, "cleanup", extra={"reason": "source_deleted"})
    return count
