#!/usr/bin/env python3
# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""ArtifactDepot 极简客户端（单文件、零第三方依赖）

功能：
  1. 选择本地文件；
  2. 选择数据仓库位置（bucket + 目录）；
  3. 上传（>8MB 自动走服务端分片接口，失败重试，支持断点续传）；
  4. 下载（大文件按服务端下发的分块大小/并发数多线程 Range 下载，可重试、可取消、
     完成后按 ETag(SHA-256) 校验；小文件/目录 ZIP/签名链接回退单流）。

用法：
  GUI（默认）:  python3 artifactdepot_upload.py
  CLI:          python3 artifactdepot_upload.py \
                    --server http://192.168.18.48:8004 \
                    --token <token> --bucket apyyc --path carryvideo/20261024 \
                    --file /path/to/file.mp4

依赖：Python 3.9+；GUI 需要 tkinter（Windows/macOS 自带；Debian/Ubuntu 需
`sudo apt install python3-tk`）。网络只用标准库 urllib/threading。

说明：
  - 大文件按服务端返回的 chunk_size（默认 8MB）顺序上传；单片失败最多重试 3 次。
  - 伴随文件本地化：上传断点状态写在**源文件同目录**
    `.<文件名>.artifactdepot-upload.json`（0600 原子写），不再放用户 home；
    源目录不可写时回退 `~/.artifactdepot/upload-state/<sha1>.json` 并提示。
    成功/会话失效删除，失败/暂停/取消保留；下次选择同一文件、同一目标、
    文件大小/mtime 未变时自动续传（以服务端返回的 uploaded 为唯一事实源）。
  - 支持暂停/恢复：分片边界检查 pause_event（set=暂停）；失败不 abort，
    保留会话与状态，再次开始上传即从断点续传。
  - 下载参数优先取 GET /api/auth/permissions 的 data.download
    （chunk_size / chunk_concurrency / request_timeout_seconds），失败回退
    8MiB / 4 路 / 1h，并夹紧到 1–64MiB / 1–16 / 60s–7d。
  - 下载先写同名 .part，成功后 os.replace；分片并发下载另有伴随状态文件
    `.<目标名>.artifactdepot-download.json` 记录已完成分片（0600）：
    暂停/失败保留、取消删除；恢复时校验 size/ETag 后只补缺失分片。
  - Token 只保存在内存，不写入磁盘；状态文件权限 0600。
  - 所需的 token 权限点：upload:initiate / upload:chunk / upload:complete / upload:abort
    （object:head 可选，用于完成后的核对）；管理员共享 token 或 uploader/operator 角色均可。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:  # GUI 可选：CLI 环境没有 tkinter 也能用
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception:  # pragma: no cover - 取决于运行环境
    tk = None
    filedialog = messagebox = ttk = None

DEFAULT_SERVER = "http://192.168.18.48:8004"
CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_REQUEST_TIMEOUT = 86400.0  # 默认 24h；以服务端 config.upload.request_timeout_seconds 为准

# 下载参数默认值（服务端不可用/字段缺失时回退）；实际值优先取 /api/auth/permissions
# 返回的 data.download，并夹紧到与服务端一致的合法区间。
DEFAULT_DOWNLOAD_CHUNK = 8 * 1024 * 1024        # 每片 8MiB
DEFAULT_DOWNLOAD_CONCURRENCY = 4                # 同时下载 4 片
DEFAULT_DOWNLOAD_TIMEOUT = 3600.0               # 单片请求超时 1h
DOWNLOAD_CHUNK_MIN = 1 * 1024 * 1024
DOWNLOAD_CHUNK_MAX = 64 * 1024 * 1024
DOWNLOAD_CONCURRENCY_MAX = 16
DOWNLOAD_TIMEOUT_MIN = 60.0
DOWNLOAD_TIMEOUT_MAX = 7 * 24 * 3600.0
DOWNLOAD_CHUNK_RETRIES = 3                      # 单片失败最多重试 3 次（指数退避）
DOWNLOAD_READ_SIZE = 256 * 1024                 # 单次读取/写入块

# 伴随文件（都放在目标/源文件所在目录，0600 原子写）：
#   上传断点：<源文件目录>/.<文件名>.artifactdepot-upload.json
#   下载断点：<目标文件目录>/.<目标名>.artifactdepot-download.json
# 源目录不可写时，上传断点回退到 ~/.artifactdepot/upload-state/<sha1>.json 并提示。
UPLOAD_STATE_VERSION = 1
DOWNLOAD_STATE_VERSION = 1
UPLOAD_STATE_SUFFIX = ".artifactdepot-upload.json"
DOWNLOAD_STATE_SUFFIX = ".artifactdepot-download.json"
LEGACY_STATE_PATH = Path.home() / ".artifactdepot_upload_state.json"  # 旧版全局状态（仅做一次迁移）
UPLOAD_STATE_DIR = Path.home() / ".artifactdepot" / "upload-state"
UI_STATE_PATH = Path.home() / ".artifactdepot_client_ui.json"  # 应用配置，不属于伴随文件
USER_AGENT = "ArtifactDepotUpload/1.0"

ProgressCB = Callable[[int, int, float, str], None]
LogCB = Callable[[str], None]


def normalize_server(server: str) -> str:
    """服务地址规范化：允许只填 IP:端口，自动补 http://；去掉末尾 /。"""
    s = (server or "").strip()
    if not s:
        return DEFAULT_SERVER
    if "://" not in s:
        s = "http://" + s
    return s.rstrip("/")


def upload_state_path(file_path) -> Path:
    """上传断点状态路径：与源文件同目录的隐藏伴随文件。"""
    path = Path(file_path)
    return path.parent / f".{path.name}{UPLOAD_STATE_SUFFIX}"


def fallback_upload_state_path(server: str, bucket: str, key: str) -> Path:
    """源目录不可写时的回退状态路径（按 server|bucket|key 的 SHA-1 命名）。"""
    raw = f"{(server or '').rstrip('/')}|{bucket}|{key}".encode("utf-8")
    return UPLOAD_STATE_DIR / (hashlib.sha1(raw).hexdigest() + ".json")


def download_state_path(dest) -> Path:
    """下载断点状态路径：与目标文件同目录的隐藏伴随文件。"""
    dest = Path(dest)
    return dest.parent / f".{dest.name}{DOWNLOAD_STATE_SUFFIX}"


def _atomic_write_json(path: Path, data: dict) -> None:
    """0600 权限原子写 JSON（同目录 .tmp + os.replace）；失败抛 OSError。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json_dict(path: Path) -> dict:
    """读取 JSON 对象；不存在/损坏/非对象时返回 {}。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_ui_state() -> dict:
    """读取 GUI 上次填写的服务地址（不保存 token）。"""
    try:
        data = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ui_state(**updates) -> None:
    """保存 GUI 的服务地址等非敏感状态；失败不影响使用。"""
    data = load_ui_state()
    if updates.get("server") is not None:
        updates["server"] = normalize_server(updates["server"])
    for key, value in updates.items():
        if value is not None:
            data[key] = value
    try:
        _atomic_write_json(UI_STATE_PATH, data)
    except OSError:
        pass




def _filename_from_disposition(cd: str) -> str:
    """从 Content-Disposition 提取文件名（优先 RFC5987 filename*）。"""
    if not cd:
        return ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    if m:
        try:
            return urllib.parse.unquote(m.group(1))
        except Exception:
            pass
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    return m.group(1) if m else ""


def _safe_local_name(name: str, fallback: str = "download.bin") -> str:
    base = Path(name or "").name.strip()
    if not base or base in (".", ".."):
        return fallback
    return base


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem}-{int(time.time())}{suffix}")

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, detail: str, url: str = ""):
        self.status = int(status or 0)
        self.detail = (detail or "").strip()
        self.url = url
        msg = f"HTTP {self.status}: {self.detail}" if self.status else self.detail
        super().__init__(msg)


class SessionLost(Exception):
    """服务端分片会话已失效（超过 24h / 服务重启 / 被清理）。"""


class Cancelled(Exception):
    """用户取消。"""


class _LocalWriteError(Exception):
    """本地写临时文件失败（磁盘满/权限等，不重试，避免反复重试）。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def format_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def format_speed(bps: float) -> str:
    if not bps or bps <= 0:
        return "-"
    return format_bytes(bps) + "/s"


def make_key(prefix: str, filename: str) -> str:
    p = (prefix or "").strip().strip("/")
    name = Path(filename).name
    return f"{p}/{name}" if p else name


def _detail_from_body(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return "空响应"
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "detail" in obj:
            return str(obj["detail"])
    except Exception:
        pass
    return text[:300]


def _int_or(value, default: int = 0) -> int:
    """宽松整数解析：None/空串/非法值 → default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    """解析整数并夹紧到 [lo, hi]；非法值回退 default（同样夹紧）。"""
    val = _int_or(value, default)
    return max(int(lo), min(int(hi), val))


def _query_params(path: str) -> dict:
    try:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    except Exception:
        return {}


def _path_is_signed(path: str) -> bool:
    """签名下载链接（按次/限时）无法安全 HEAD 探测（会消耗次数），直接走单流。"""
    params = _query_params(path)
    return any(k in params for k in ("link", "tk", "expires", "sig"))


def _key_from_path(path: str) -> str:
    return ((_query_params(path).get("key") or [""])[0] or "").strip()


def _local_name(disposition: str, path: str) -> str:
    """本地文件名：Content-Disposition 优先，其次 URL 的 key 参数，最后 download.bin。"""
    name = _filename_from_disposition(disposition or "") or _key_from_path(path)
    return _safe_local_name(name, "download.bin")


def _prepare_dest(dest: Path, name: str) -> Path:
    """dest 是目录（或带结尾分隔符）时拼文件名并去重；否则按指定文件路径。"""
    dest = Path(dest)
    if dest.is_dir() or str(dest).endswith(os.sep):
        dest.mkdir(parents=True, exist_ok=True)
        return _unique_path(dest / name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _etag_sha256(etag: str) -> str:
    """ETag 形如 "<64位hex>"（服务端用对象 SHA-256 作强 ETag）时返回 hex，否则 ""。"""
    value = (etag or "").strip().strip('"').lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _download_retryable(err: ApiError) -> bool:
    """分片下载是否值得重试：网络错误/超时/限流/5xx 重试；4xx（含 416）不重试。"""
    if err.status == 0 or err.status in (408, 429):
        return True
    return 500 <= err.status < 600


def _sleep_or_cancel(delay: float, cancel: Optional[threading.Event],
                     abort: threading.Event) -> bool:
    """退避等待；返回 True 表示等待期间被取消/中止。"""
    deadline = time.monotonic() + max(0.0, float(delay))
    while True:
        if abort.is_set() or (cancel is not None and cancel.is_set()):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if cancel is not None:
            cancel.wait(min(0.2, remaining))
        else:
            time.sleep(min(0.2, remaining))


# ---------------------------------------------------------------------------
# 伴随文件：上传/下载断点状态
# ---------------------------------------------------------------------------

@dataclass
class ResumeState:
    """上传断点状态（落在源文件同目录的伴随文件里；本地只做缓存，服务端 uploaded 为准）。"""

    server: str
    bucket: str
    key: str
    file_path: str
    file_size: int
    file_mtime_ns: int
    chunk_size: int
    upload_id: str
    uploaded: list = field(default_factory=list)
    updated_at: float = 0.0
    total_chunks: int = 0
    staging: str = ""
    version: int = UPLOAD_STATE_VERSION

    def matches(self, server: str, bucket: str, key: str, path: Path, stat: os.stat_result) -> bool:
        return (
            self.server == server.rstrip("/")
            and self.bucket == bucket
            and self.key == key
            and self.file_path == str(path.resolve())
            and self.file_size == stat.st_size
            and self.file_mtime_ns == stat.st_mtime_ns
        )

    def _candidates(self) -> list:
        """首选源文件目录；源目录不可写时回退用户 home 下的状态目录。"""
        primary = upload_state_path(Path(self.file_path))
        fallback = fallback_upload_state_path(self.server, self.bucket, self.key)
        return [primary] if primary == fallback else [primary, fallback]

    def save(self, log: Optional[LogCB] = None) -> Optional[Path]:
        """0600 原子保存续传状态；成功返回实际路径，失败返回 None（不阻断上传）。

        优先写源文件目录的伴随文件；目录不可写时自动回退并提示一次。
        """
        self.version = UPLOAD_STATE_VERSION
        self.updated_at = time.time()
        target = getattr(self, "state_path", None)
        candidates = [target] if target is not None else self._candidates()
        last_error: Optional[OSError] = None
        for idx, candidate in enumerate(candidates):
            try:
                _atomic_write_json(candidate, asdict(self))
            except OSError as e:
                last_error = e
                if target is not None:
                    break
                continue
            self.state_path = candidate
            if log and idx > 0:
                log(f"源目录不可写（{last_error}），续传状态回退到 {candidate}")
            return candidate
        if log and not getattr(self, "_save_warned", False):
            self._save_warned = True
            log(f"警告：续传状态保存失败：{last_error}")
        return None

    @classmethod
    def load(cls, path: Path) -> Optional["ResumeState"]:
        raw = _read_json_dict(path)
        if not raw:
            return None
        try:
            state = cls(
                server=str(raw["server"]),
                bucket=str(raw["bucket"]),
                key=str(raw["key"]),
                file_path=str(raw["file_path"]),
                file_size=int(raw["file_size"]),
                file_mtime_ns=int(raw["file_mtime_ns"]),
                chunk_size=int(raw["chunk_size"]),
                upload_id=str(raw["upload_id"]),
                uploaded=[int(x) for x in (raw.get("uploaded") or [])],
                updated_at=float(raw.get("updated_at") or 0),
                total_chunks=int(raw.get("total_chunks") or 0),
                staging=str(raw.get("staging") or ""),
                version=int(raw.get("version") or UPLOAD_STATE_VERSION),
            )
        except Exception:
            return None
        state.state_path = Path(path)
        return state

    def clear(self) -> None:
        """删除本文件的续传状态（源目录与回退目录都尝试清理）。"""
        for path in self._candidates():
            for candidate in (path, Path(str(path) + ".tmp")):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass


def _migrate_legacy_state(server: str, bucket: str, key: str, path: Path,
                          stat: os.stat_result,
                          log: Optional[LogCB] = None) -> Optional[ResumeState]:
    """读取旧版全局 ~/.artifactdepot_upload_state.json 做一次兼容迁移。

    只有状态与当前文件/目标完全匹配时才迁移并删除旧文件；不匹配的旧状态
    可能属于另一个文件，保留给那次上传自己处理。
    """
    if not LEGACY_STATE_PATH.exists():
        return None
    state = ResumeState.load(LEGACY_STATE_PATH)
    if state is None:
        try:
            LEGACY_STATE_PATH.unlink()
        except OSError:
            pass
        return None
    if not state.matches(server, bucket, key, path, stat):
        return None
    if state.total_chunks <= 0:
        state.total_chunks = max(1, (stat.st_size + state.chunk_size - 1) // state.chunk_size)
    # 旧文件不是新位置：清掉缓存路径，save() 会重新选择源目录（或回退目录）
    state.state_path = None
    saved = state.save(log=log)
    try:
        LEGACY_STATE_PATH.unlink()
    except OSError:
        pass
    if log:
        log(f"已迁移旧版全局续传状态到 {saved or '（保存失败）'} 并删除旧文件")
    return state


@dataclass
class DownloadState:
    """下载断点状态（落在目标文件同目录的伴随文件里，记录已完成分片索引）。"""

    size: int = 0
    etag: str = ""
    chunk_size: int = 0
    done: list = field(default_factory=list)
    updated_at: float = 0.0
    version: int = DOWNLOAD_STATE_VERSION

    def save(self, path: Path, log: Optional[LogCB] = None) -> bool:
        self.version = DOWNLOAD_STATE_VERSION
        self.updated_at = time.time()
        try:
            _atomic_write_json(path, asdict(self))
            return True
        except OSError as e:
            if log:
                log(f"警告：下载断点状态保存失败：{e}")
            return False

    @classmethod
    def load(cls, path: Path) -> Optional["DownloadState"]:
        raw = _read_json_dict(path)
        if not raw:
            return None
        try:
            return cls(
                size=int(raw.get("size") or 0),
                etag=str(raw.get("etag") or ""),
                chunk_size=int(raw.get("chunk_size") or 0),
                done=[int(x) for x in (raw.get("done") or [])],
                updated_at=float(raw.get("updated_at") or 0),
                version=int(raw.get("version") or DOWNLOAD_STATE_VERSION),
            )
        except Exception:
            return None

    @classmethod
    def clear(cls, path: Path) -> None:
        for candidate in (Path(path), Path(str(path) + ".tmp")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

class ArtifactDepotClient:
    def __init__(self, server: str, token: str, timeout: float = 30):
        self.server = normalize_server(server)
        self.token = (token or "").strip()
        self.timeout = timeout
        self.request_timeout = UPLOAD_REQUEST_TIMEOUT
        self._download_settings_cache: Optional[dict] = None

    # ---- 基础请求 ----
    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, *, data: Optional[bytes] = None,
                 extra_headers: Optional[dict] = None, timeout: Optional[float] = None) -> tuple:
        url = self.server + path
        req = urllib.request.Request(
            url, data=data, headers=self._headers(extra_headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return int(resp.status), resp.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, _detail_from_body(e.read()), url)
        except urllib.error.URLError as e:
            raise ApiError(0, f"连接失败：{e.reason}", url)
        except (TimeoutError, OSError) as e:
            raise ApiError(0, f"网络错误：{e}", url)

    @staticmethod
    def _decode(raw: bytes, status: int, path: str) -> dict:
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
            return obj if isinstance(obj, dict) else {"data": obj}
        except Exception:
            raise ApiError(status, "服务端响应不是 JSON：" + raw[:200].decode("utf-8", "replace"), path)

    def _json(self, method: str, path: str, payload=None, timeout: Optional[float] = None) -> dict:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self._request(method, path, data=data, extra_headers=headers, timeout=timeout)
        return self._decode(raw, status, path)

    @staticmethod
    def _q(path: str, **params) -> str:
        clean = {k: v for k, v in params.items() if v is not None}
        query = urllib.parse.urlencode(clean)
        return path + ("?" + query if query else "")

    # ---- 元数据 ----
    def check(self) -> dict:
        return (self._json("GET", "/api/auth/check").get("data") or {})

    def list_buckets(self) -> list:
        data = self._json("GET", "/api/buckets").get("data") or []
        if isinstance(data, dict):
            data = data.get("buckets") or []
        return [str(x) for x in data]

    def list_dirs(self, bucket: str, prefix: str = "") -> list:
        path = self._q("/api/objects/list", bucket=bucket, prefix=prefix or "",
                       limit=2000, offset=0)
        data = self._json("GET", path).get("data") or {}
        items = data.get("items") or [] if isinstance(data, dict) else []
        return [it for it in items if isinstance(it, dict) and it.get("is_dir")]

    def head(self, bucket: str, key: str) -> Optional[dict]:
        path = self._q("/api/objects/head", bucket=bucket, key=key)
        try:
            return self._json("GET", path).get("data") or {}
        except ApiError as e:
            if e.status in (401, 403, 404):
                return None
            raise

    # ---- 通用 / 对象 / 管理 API ----
    def get_json(self, path: str, params: Optional[dict] = None) -> dict:
        return self._json("GET", self._q(path, **(params or {})))

    def post_json(self, path: str, payload=None, timeout: Optional[float] = None) -> dict:
        return self._json("POST", path, payload, timeout=timeout)

    def put_json(self, path: str, payload=None) -> dict:
        return self._json("PUT", path, payload)

    def delete_json(self, path: str, params: Optional[dict] = None) -> dict:
        return self._json("DELETE", self._q(path, **(params or {})))

    def permissions(self) -> dict:
        return self._json("GET", "/api/auth/permissions").get("data") or {}

    def list_objects(self, bucket: str, prefix: str = "", limit: int = 0,
                     offset: int = 0) -> dict:
        data = self._json("GET", self._q(
            "/api/objects/list", bucket=bucket, prefix=prefix or "",
            limit=limit, offset=offset)).get("data") or {}
        return data if isinstance(data, dict) else {}

    def mkdir(self, bucket: str, key: str) -> dict:
        return self._json("POST", "/api/objects/mkdir",
                          {"bucket": bucket, "key": key}).get("data") or {}

    def delete_object(self, bucket: str, key: str) -> dict:
        return self.delete_json("/api/objects", {"bucket": bucket, "key": key}).get("data") or {}

    def bulk_delete(self, bucket: str, prefix: str, mode: str,
                    dry_run: bool, confirm: str = "") -> dict:
        return self._json("POST", "/api/objects/bulk-delete", {
            "bucket": bucket, "prefix": prefix or "", "mode": mode,
            "dry_run": bool(dry_run), "confirm": confirm or "",
        }).get("data") or {}

    def create_signed_link(self, bucket: str, key: str, mode: str,
                           count: int = 1, expires: int = 3600) -> dict:
        return self._json("POST", "/api/objects/presign", {
            "bucket": bucket, "key": key, "mode": mode,
            "count": int(count), "expires": int(expires),
        }).get("data") or {}

    def signed_link_config(self) -> dict:
        return self._json("GET", "/api/objects/signed-links/config").get("data") or {}

    def list_signed_links(self) -> list:
        data = self._json("GET", "/api/objects/signed-links").get("data") or []
        return data if isinstance(data, list) else []

    def revoke_signed_link(self, link_id: str) -> dict:
        return self._json("POST", f"/api/objects/signed-links/{urllib.parse.quote(link_id)}/revoke").get("data") or {}

    def list_tokens(self, reveal: bool = False) -> dict:
        path = "/api/tokens" + ("?reveal=true" if reveal else "")
        data = self._json("GET", path).get("data") or {}
        return data if isinstance(data, dict) else {}

    def create_token(self, payload: dict) -> dict:
        return self._json("POST", "/api/tokens", payload).get("data") or {}

    def update_token(self, value: str, payload: dict) -> dict:
        return self._json("PUT", "/api/tokens/" + urllib.parse.quote(value), payload).get("data") or {}

    def delete_token(self, value: str) -> dict:
        return self.delete_json("/api/tokens", {"value": value}).get("data") or {}

    def reload_config(self) -> dict:
        return self._json("POST", "/api/config/reload").get("data") or {}

    def query_audit(self, bucket: str = "", key: str = "", actor: str = "",
                    since: str = "", limit: int = 500) -> list:
        data = self._json("GET", self._q(
            "/api/audit", bucket=bucket, key=key, actor=actor,
            since=since, limit=int(limit))).get("data") or []
        return data if isinstance(data, list) else []

    # ---- 下载 ----
    def download_settings(self) -> dict:
        """读取服务端下发的下载参数（GET /api/auth/permissions 的 data.download）。

        结果在实例内缓存；服务端不可用、响应缺字段或数值非法时逐项回退默认值，
        并按与服务端一致的区间夹紧（分片 1–64MiB、并发 1–16、超时 60s–7d）。
        """
        if self._download_settings_cache is not None:
            return dict(self._download_settings_cache)
        settings = {
            "chunk_size": DEFAULT_DOWNLOAD_CHUNK,
            "chunk_concurrency": DEFAULT_DOWNLOAD_CONCURRENCY,
            "request_timeout_seconds": int(DEFAULT_DOWNLOAD_TIMEOUT),
        }
        try:
            data = self._json("GET", "/api/auth/permissions",
                              timeout=min(float(self.timeout or 30), 15.0)).get("data") or {}
            raw = data.get("download") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                settings["chunk_size"] = _clamp_int(
                    raw.get("chunk_size"), DEFAULT_DOWNLOAD_CHUNK,
                    DOWNLOAD_CHUNK_MIN, DOWNLOAD_CHUNK_MAX)
                settings["chunk_concurrency"] = _clamp_int(
                    raw.get("chunk_concurrency"), DEFAULT_DOWNLOAD_CONCURRENCY,
                    1, DOWNLOAD_CONCURRENCY_MAX)
                settings["request_timeout_seconds"] = _clamp_int(
                    raw.get("request_timeout_seconds"), int(DEFAULT_DOWNLOAD_TIMEOUT),
                    int(DOWNLOAD_TIMEOUT_MIN), int(DOWNLOAD_TIMEOUT_MAX))
        except Exception:
            pass
        self._download_settings_cache = settings
        return dict(settings)

    def _probe_download(self, path: str, timeout: float) -> Optional[dict]:
        """HEAD 探测下载元信息（大小 / Range / ETag / 文件名）。

        服务端不支持 HEAD（405/501）时返回 None，调用方回退单流下载。
        """
        url = self.server + path
        req = urllib.request.Request(url, headers=self._headers(), method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                headers = resp.headers
                size = _int_or(headers.get("Content-Length"), 0)
                accept = (headers.get("Accept-Ranges") or "").strip().lower()
                return {
                    "size": size if size > 0 else 0,
                    "accept_ranges": "bytes" in accept,
                    "etag": (headers.get("ETag") or "").strip(),
                    "disposition": headers.get("Content-Disposition") or "",
                }
        except urllib.error.HTTPError as e:
            if e.code in (405, 501):
                return None
            raise ApiError(e.code, _detail_from_body(e.read()), url)
        except urllib.error.URLError as e:
            raise ApiError(0, f"连接失败：{e.reason}", url)
        except (TimeoutError, OSError) as e:
            raise ApiError(0, f"网络错误：{e}", url)

    def download_to(self, path: str, dest: Path,
                    progress: Optional[Callable[[int, int], None]] = None,
                    timeout: float = 3600,
                    concurrency: Optional[int] = None,
                    cancel: Optional[threading.Event] = None,
                    pause_event: Optional[threading.Event] = None) -> Path:
        """下载 URL 到本地（大文件自动分块并发；可重试、可取消/暂停）。

        行为约定：
        - dest 为目录时用 Content-Disposition（其次 URL 的 key）取名，重名自动加 (n)；
        - HEAD 探测到服务端支持 Range 且文件大于分块大小时，多线程按 Range 并发下载，
          写临时 .part 后 os.replace；否则回退原有单流实现（含 download-bulk ZIP）；
        - 每片失败最多重试 DOWNLOAD_CHUNK_RETRIES 次（指数退避）；
        - progress(done, total)：total 未知（如 ZIP 流）时为 0，并发下载最终 done == total；
        - cancel（threading.Event）置位时尽快停止、删除 .part/断点状态并抛 Cancelled；
        - pause_event（threading.Event，set=暂停）在分片边界暂停，保留 .part + 断点状态；
          再次下载时校验 size/ETag/分片一致后只补缺失分片（仅并发下载支持）；
        - 失败保留 .part + 断点状态（可重试续传）；ETag 为对象 SHA-256 时完成后校验内容，
          不一致说明本地数据已损坏，删除 .part/状态并报错。
        """
        timeout = float(timeout or 0) or float(DEFAULT_DOWNLOAD_TIMEOUT)
        settings = self.download_settings()
        want = settings["chunk_concurrency"] if concurrency is None else concurrency
        workers = _clamp_int(want, DEFAULT_DOWNLOAD_CONCURRENCY, 1, DOWNLOAD_CONCURRENCY_MAX)
        chunk_size = _clamp_int(settings.get("chunk_size"), DEFAULT_DOWNLOAD_CHUNK,
                                DOWNLOAD_CHUNK_MIN, DOWNLOAD_CHUNK_MAX)

        probe = None
        # 签名链接（link/tk/expires/sig）按次数计费，HEAD 探测可能消耗次数；
        # download-bulk 是 ZIP 流（无 Content-Length），两者都直接走单流，避免多余请求。
        if not _path_is_signed(path) and "download-bulk" not in path:
            probe = self._probe_download(path, timeout)

        if (probe is not None and workers > 1 and probe["size"] > chunk_size
                and probe["accept_ranges"]):
            return self._download_parallel(path, dest, probe, chunk_size, workers,
                                           progress, timeout, cancel, pause_event)
        return self._download_stream(path, dest, progress, timeout, cancel)

    def _download_stream(self, path: str, dest: Path,
                         progress: Optional[Callable[[int, int], None]],
                         timeout: float,
                         cancel: Optional[threading.Event] = None) -> Path:
        """单流下载（保持原有行为：.part 临时文件 + os.replace；支持取消）。"""
        url = self.server + path
        req = urllib.request.Request(url, headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, _detail_from_body(e.read()), url)
        except urllib.error.URLError as e:
            raise ApiError(0, f"连接失败：{e.reason}", url)
        try:
            cd = resp.headers.get("Content-Disposition", "")
            name = _safe_local_name(_filename_from_disposition(cd), "download.bin")
            dest = _prepare_dest(Path(dest), name)
            total = _int_or(resp.headers.get("Content-Length"), 0)
            if total < 0:
                total = 0
            downloaded = 0
            tmp = dest.with_name(dest.name + ".part")
            try:
                with open(tmp, "wb") as f:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled("用户取消")
                        chunk = resp.read(DOWNLOAD_READ_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            try:
                                progress(downloaded, total)
                            except Exception:
                                pass
                if cancel is not None and cancel.is_set():
                    raise Cancelled("用户取消")
                os.replace(tmp, dest)
            except BaseException:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return dest
        finally:
            resp.close()

    def _download_parallel(self, path: str, dest: Path, probe: dict, chunk_size: int,
                           workers: int,
                           progress: Optional[Callable[[int, int], None]],
                           timeout: float,
                           cancel: Optional[threading.Event] = None,
                           pause_event: Optional[threading.Event] = None) -> Path:
        """多线程 Range 分块下载：预分配 .part，按区间并发拉取并写入对应偏移。

        伴随状态文件 `.<目标名>.artifactdepot-download.json` 每完成一片即更新：
        - 暂停（pause_event）在分片边界生效，保留 .part + 状态；
        - 失败保留 .part + 状态（再次下载可续传，只补缺失分片）；
        - 取消或数据校验失败（本地数据不可信）删除 .part + 状态。
        """
        total = int(probe["size"])
        name = _safe_local_name(
            _filename_from_disposition(probe.get("disposition") or "")
            or _key_from_path(path), "download.bin")
        dest = _prepare_dest(Path(dest), name)
        tmp = dest.with_name(dest.name + ".part")
        state_file = download_state_path(dest)
        url = self.server + path
        base_headers = self._headers()
        etag = (probe.get("etag") or "").strip()

        chunk_count = max(1, (total + chunk_size - 1) // chunk_size)
        ranges = [(i * chunk_size, min(total, (i + 1) * chunk_size) - 1)
                  for i in range(chunk_count)]
        chunk_bytes = [end - start + 1 for start, end in ranges]

        # ---- 断点：size/ETag/分片大小一致且 .part 大小正确才续传，否则丢弃重下 ----
        done_chunks: set = set()
        state = DownloadState.load(state_file)
        if state is not None:
            try:
                part_size = tmp.stat().st_size
            except OSError:
                part_size = -1
            if (int(state.size) == total and int(state.chunk_size) == chunk_size
                    and state.etag == etag and part_size == total):
                done_chunks = {i for i in state.done if 0 <= i < chunk_count}
            else:
                # 对象已变化（size/ETag）或 .part 不可信：丢弃后从头下载
                DownloadState.clear(state_file)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                done_chunks = set()
        pending = [(i, ranges[i][0], ranges[i][1])
                   for i in range(chunk_count) if i not in done_chunks]
        resumed_bytes = sum(chunk_bytes[i] for i in done_chunks)

        lock = threading.Lock()
        state_lock = threading.Lock()
        abort = threading.Event()
        done = [resumed_bytes]
        errors: list = []
        next_index = [0]

        def _persist_state() -> None:
            """把已完成分片写入伴随状态文件（0600 原子写；失败不阻断下载）。"""
            with state_lock:
                snapshot = DownloadState(size=total, etag=etag, chunk_size=chunk_size,
                                         done=sorted(done_chunks))
                snapshot.save(state_file)

        def _stopped() -> bool:
            return bool((cancel is not None and cancel.is_set()) or abort.is_set())

        def _report(count: int) -> None:
            with lock:
                done[0] += count
                current = done[0]
            if progress:
                try:
                    progress(current, total)
                except Exception:
                    pass

        def _fetch(fh, start: int, end: int) -> None:
            """拉取闭区间 [start, end]；失败按指数退避重试，从已写入位置继续。

            pos 始终指向“下一个待写入字节”，因此重试不会重复下载/重复计数。
            """
            pos = start
            retries = 0
            while pos <= end:
                if _stopped():
                    return
                try:
                    req = urllib.request.Request(
                        url, headers=dict(base_headers, Range=f"bytes={pos}-{end}"))
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        status = int(getattr(resp, "status", 0) or 0)
                        if status != 206:
                            raise ApiError(
                                0, f"服务端未按 Range 返回 206（HTTP {status}）", url)
                        content_range = (resp.headers.get("Content-Range") or "").strip()
                        m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range)
                        if not m:
                            raise ApiError(
                                0, f"缺少合法 Content-Range：{content_range!r}", url)
                        got_start, got_end, got_total = (
                            int(m.group(1)), int(m.group(2)), int(m.group(3)))
                        if (got_start, got_end, got_total) != (pos, end, total):
                            raise ApiError(
                                0, f"Range 响应不匹配：期望 {pos}-{end}/{total}，"
                                   f"实际 {got_start}-{got_end}/{got_total}", url)
                        while pos <= end:
                            if _stopped():
                                return
                            piece = resp.read(min(DOWNLOAD_READ_SIZE, end - pos + 1))
                            if not piece:
                                raise ApiError(0, f"分片 {pos}-{end} 响应提前结束", url)
                            try:
                                with lock:
                                    fh.seek(pos)
                                    fh.write(piece)
                            except OSError as e:
                                raise _LocalWriteError(
                                    f"写入临时文件失败：{e}") from e
                            pos += len(piece)
                            _report(len(piece))
                except ApiError as e:
                    if _stopped() or not _download_retryable(e) \
                            or retries >= DOWNLOAD_CHUNK_RETRIES:
                        raise
                    retries += 1
                    if _sleep_or_cancel(2 ** (retries - 1), cancel, abort):
                        return
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    wrapped = ApiError(0, f"网络错误：{e}", url)
                    if _stopped() or retries >= DOWNLOAD_CHUNK_RETRIES:
                        raise wrapped
                    retries += 1
                    if _sleep_or_cancel(2 ** (retries - 1), cancel, abort):
                        return
                except _LocalWriteError as e:
                    raise ApiError(0, str(e), url)
                except Exception as e:
                    raise ApiError(0, f"下载分片 {pos}-{end} 失败：{e}", url)

        def _paused() -> bool:
            return pause_event is not None and pause_event.is_set()

        def _worker() -> None:
            try:
                fh = open(tmp, "r+b")
            except OSError as e:
                with lock:
                    errors.append(ApiError(0, f"打开临时文件失败：{e}", url))
                abort.set()
                return
            try:
                while not _stopped():
                    if _paused():
                        # 分片边界暂停：等待「继续」或取消（已下载的分片保持不动）
                        if _sleep_or_cancel(0.2, cancel, abort):
                            return
                        continue
                    with lock:
                        if next_index[0] >= len(pending):
                            return
                        idx, start, end = pending[next_index[0]]
                        next_index[0] += 1
                    try:
                        _fetch(fh, start, end)
                    except BaseException as e:
                        with lock:
                            errors.append(e)
                        abort.set()
                        return
                    if _stopped():
                        return
                    with state_lock:
                        done_chunks.add(idx)
                    _persist_state()
            finally:
                try:
                    fh.close()
                except OSError:
                    pass

        discard = [False]
        try:
            # 预分配到目标大小，各 worker 直接写到自己的偏移；续传时保留已下载分片
            with open(tmp, "r+b" if done_chunks else "wb") as prep:
                prep.truncate(total)
            threads = [threading.Thread(target=_worker, daemon=True)
                       for _ in range(min(workers, len(pending)))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            if cancel is not None and cancel.is_set():
                raise Cancelled("用户取消")
            if errors:
                raise errors[0]
            if done[0] != total:
                raise ApiError(0, f"下载不完整：{done[0]}/{total} 字节", url)
            try:
                actual = tmp.stat().st_size
            except OSError:
                actual = -1
            if actual != total:
                discard[0] = True
                raise ApiError(0, f"下载大小校验失败：期望 {total}，实际 {actual} 字节", url)
            expected_sha = _etag_sha256(etag)
            if expected_sha:
                actual_sha = _sha256_file(tmp)
                if actual_sha != expected_sha:
                    discard[0] = True
                    raise ApiError(
                        0, f"下载校验失败：SHA-256 不一致（期望 {expected_sha}，"
                           f"实际 {actual_sha}）", url)
            os.replace(tmp, dest)
        except BaseException as exc:
            # 取消 / 本地数据不可信 → 删除 .part+状态；网络或服务端失败 → 保留可续传
            if discard[0] or isinstance(exc, Cancelled) \
                    or (cancel is not None and cancel.is_set()):
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                DownloadState.clear(state_file)
            raise
        else:
            DownloadState.clear(state_file)
        if progress:
            try:
                progress(total, total)
            except Exception:
                pass
        return dest

    # ---- 分片 ----
    def initiate(self, bucket: str = "", key: str = "", size: int = 0, *,
                 resume: bool = False, upload_id: str = "") -> dict:
        """创建/复用分片上传会话，返回服务端 data。

        始终带 bucket/key/size（让服务端把分片暂存到目标文件目录）；续传时
        resume=true + upload_id（本地有状态）或只看 bucket/key/size 找回会话；
        返回 uploaded（服务端磁盘上的真实分片索引）作为续传唯一事实源。
        """
        fields: dict = {}
        if bucket:
            fields["bucket"] = bucket
        if key:
            fields["key"] = key
        if int(size or 0) > 0:
            fields["size"] = str(int(size))
        if resume:
            fields["resume"] = "true"
        if upload_id:
            fields["upload_id"] = upload_id
        data = self._post_form("/api/objects/initiate", fields).get("data") or {}
        upload_id = str(data.get("upload_id") or "")
        if not upload_id:
            raise ApiError(0, "服务端未返回 upload_id")
        data["upload_id"] = upload_id
        chunk_size = _int_or(data.get("chunk_size"), CHUNK_SIZE)
        if chunk_size <= 0:
            chunk_size = CHUNK_SIZE
        data["chunk_size"] = chunk_size
        try:
            server_timeout = float(data.get("request_timeout_seconds") or 0)
        except (TypeError, ValueError):
            server_timeout = 0
        if server_timeout > 0:
            self.request_timeout = server_timeout
        return data

    @staticmethod
    def _multipart(fields: dict, file_field: Optional[str],
                   filename: str, data: bytes) -> tuple:
        boundary = "----ArtifactDepot" + uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")
        if file_field:
            raw_name = filename or "upload.bin"
            # multipart 的 filename 头只用 ASCII 安全字符；真正的对象 key 由 form 的 key 字段决定，
            # 因此中文文件名不会丢失（只影响服务端识别后缀，这里保留原扩展名）。
            safe_name = "".join(
                ch if ch.isascii() and ch not in '"\\\r\n' else "_"
                for ch in raw_name
            ) or "upload.bin"
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(
                f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
                .encode("utf-8"))
            parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
            parts.append(data)
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def _post_form(self, path: str, fields: dict, *, file_field: Optional[str] = None,
                   filename: str = "", data: bytes = b"", timeout: Optional[float] = None) -> dict:
        body, ctype = self._multipart(fields, file_field, filename, data)
        status, raw = self._request(
            "POST", path, data=body, extra_headers={"Content-Type": ctype}, timeout=timeout)
        return self._decode(raw, status, path)

    def upload_chunk(self, upload_id: str, index: int, data: bytes) -> dict:
        return self._post_form(
            "/api/objects/chunk",
            {"upload_id": upload_id, "index": str(index)},
            file_field="chunk", filename=f"chunk_{index}.bin", data=data,
            timeout=self.request_timeout)

    def complete(self, upload_id: str, bucket: str, key: str, total_chunks: int,
                 overwrite: bool, source_url: str = "") -> dict:
        return self._post_form(
            "/api/objects/complete",
            {"upload_id": upload_id, "bucket": bucket, "key": key,
             "total_chunks": str(total_chunks), "source_url": source_url or "",
             "overwrite": "true" if overwrite else "false"},
            timeout=self.request_timeout)

    def abort(self, upload_id: str) -> dict:
        return self._post_form("/api/objects/abort", {"upload_id": upload_id}, timeout=60)

    def upload_direct(self, bucket: str, key: str, filename: str,
                      data: bytes, overwrite: bool, source_url: str = "") -> dict:
        return self._post_form(
            "/api/objects",
            {"bucket": bucket, "key": key, "source_url": source_url or "",
             "overwrite": "true" if overwrite else "false"},
            file_field="file", filename=Path(filename).name, data=data, timeout=3600)


# ---------------------------------------------------------------------------
# 上传实现
# ---------------------------------------------------------------------------

def _retry(call, *, what: str, log: Optional[LogCB], attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            return call()
        except ApiError as e:
            last = e
            # 参数/权限/大小/不存在等错误重试无意义
            if e.status in (400, 401, 403, 404, 409, 413):
                raise
            if i >= attempts - 1:
                raise
            delay = 2 ** i
            if log:
                log(f"{what}失败（{e}），{delay}s 后重试 {i + 1}/{attempts - 1}")
            time.sleep(delay)
    if last:
        raise last
    raise RuntimeError(f"{what}失败")


def _is_session_error(e: ApiError) -> bool:
    return e.status == 400 and (
        "分片会话" in e.detail or "upload_id" in e.detail or "缺少分片" in e.detail)


def _notify(progress: Optional[ProgressCB], uploaded: int, total: int,
            speed: float, message: str) -> None:
    if progress:
        try:
            progress(int(uploaded), int(total), float(speed), str(message))
        except Exception:
            pass


def _upload_direct(client: ArtifactDepotClient, path: Path, stat: os.stat_result,
                   bucket: str, key: str, overwrite: bool,
                   progress: Optional[ProgressCB], log: Optional[LogCB],
                   source_url: str = "") -> dict:
    size = stat.st_size
    if log:
        log(f"小文件直传：{path.name}（{format_bytes(size)}）")
    _notify(progress, 0, size, 0, "准备上传")
    data = path.read_bytes()
    try:
        result = client.upload_direct(bucket, key, path.name, data, overwrite, source_url)
    except ApiError as e:
        # 响应丢失但服务端可能已落盘：用 head 核对
        remote = client.head(bucket, key)
        if remote is not None and int(remote.get("size", -1)) == size:
            if log:
                log("上传响应异常，但服务端已存在同大小对象，按成功处理")
            result = remote
        else:
            raise e
    _notify(progress, size, size, 0, "上传完成")
    if log:
        log(f"上传成功：{key}（{format_bytes(size)}）")
    return result


def _upload_chunked(client: ArtifactDepotClient, path: Path, stat: os.stat_result,
                    bucket: str, key: str, overwrite: bool, resume: bool,
                    progress: Optional[ProgressCB], log: Optional[LogCB],
                    cancel_event: Optional[threading.Event],
                    pause_event: Optional[threading.Event] = None,
                    source_url: str = "") -> dict:
    size = stat.st_size

    def chunk_len(index: int, chunk_size_: int) -> int:
        return max(0, min(chunk_size_, size - index * chunk_size_))

    def _wait_if_paused() -> None:
        """暂停：在分片边界阻塞等待「继续」；取消优先。"""
        if pause_event is None:
            return
        while pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled("用户取消")
            pause_event.wait(0.2)
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("用户取消")

    # ---- 1) 读本地续传状态（含旧版全局状态的一次性兼容迁移）----
    state: Optional[ResumeState] = None
    if resume:
        state = ResumeState.load(upload_state_path(path))
        if state is None:
            state = _migrate_legacy_state(client.server, bucket, key, path, stat, log)
        if state is not None and not state.matches(client.server, bucket, key, path, stat):
            state = None

    # ---- 2) initiate：始终带 bucket/key/size；续传以服务端 uploaded 为事实源 ----
    if resume and state is not None:
        try:
            data = client.initiate(bucket, key, size, resume=True,
                                   upload_id=state.upload_id)
        except ApiError as e:
            if not _is_session_error(e):
                raise
            if log:
                log("服务端分片会话已失效；本地续传状态已清除，将重新开始上传。")
            state.clear()
            state = None
            # 契约 C2：会话失效 → 清状态、新建会话重传
            data = client.initiate(bucket, key, size, resume=False)
    elif resume:
        # 本地没有状态，但服务端可能仍有会话（状态文件被删/换机器）：按 bucket/key/size 找回
        data = client.initiate(bucket, key, size, resume=True)
    else:
        data = client.initiate(bucket, key, size, resume=False)

    upload_id = str(data["upload_id"])
    chunk_size = int(data["chunk_size"])
    total_chunks = max(1, (size + chunk_size - 1) // chunk_size)
    uploaded = {i for i in (_int_or(x, -1) for x in (data.get("uploaded") or []))
                if 0 <= i < total_chunks}
    staging = str(data.get("staging") or ("target" if bucket and key else "legacy"))
    resumed = bool(data.get("resumed")) and bool(uploaded)

    if resumed and log:
        log(f"续传：服务端已有 {len(uploaded)}/{total_chunks} 片"
            f"（{format_bytes(sum(chunk_len(i, chunk_size) for i in uploaded))}），只补缺失分片")
    elif log:
        log(f"分片上传：{format_bytes(size)}，{total_chunks} 片 × {format_bytes(chunk_size)}")

    # 用服务端状态刷新本地缓存（新建或续传）
    state = ResumeState(
        server=client.server, bucket=bucket, key=key,
        file_path=str(path.resolve()), file_size=size,
        file_mtime_ns=stat.st_mtime_ns, chunk_size=chunk_size,
        upload_id=upload_id, uploaded=sorted(uploaded),
        total_chunks=total_chunks, staging=staging)
    saved = state.save(log=log)
    if saved is not None and log and not resumed:
        log(f"续传状态：{saved}")

    loaded = sum(chunk_len(i, chunk_size) for i in uploaded)
    sent = 0
    started = time.monotonic()
    _notify(progress, loaded, size, 0, f"准备上传（{len(uploaded)}/{total_chunks} 已传）")

    with path.open("rb") as fh:
        for i in range(total_chunks):
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled("用户取消")
            _wait_if_paused()
            if i in uploaded:
                continue
            fh.seek(i * chunk_size)
            payload = fh.read(chunk_size)
            if not payload:
                raise ApiError(0, f"读取分片 {i} 为空（文件可能被修改）")

            def _send(idx=i, data=payload):
                return client.upload_chunk(upload_id, idx, data)

            try:
                _retry(_send, what=f"分片 {i + 1}/{total_chunks} ", log=log)
            except ApiError as e:
                if _is_session_error(e):
                    state.clear()
                    raise SessionLost(
                        "服务端分片会话已失效（可能超过 24 小时、服务重启或被清理）；"
                        "本地续传状态已清除，请重新开始上传。")
                # 其他失败：不 abort，保留会话与续传状态（异常上抛，后续可续传）
                raise

            uploaded.add(i)
            state.uploaded = sorted(uploaded)
            state.save(log=log)
            sent += len(payload)
            loaded += len(payload)
            elapsed = max(0.001, time.monotonic() - started)
            _notify(progress, loaded, size, sent / elapsed,
                    f"分片 {i + 1}/{total_chunks}")

    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("用户取消")

    _notify(progress, size, size, 0, "服务端合并 / 校验中…")
    if log:
        log("全部分片已上传，等待服务端合并（大文件可能需要数分钟）…")
    try:
        result = client.complete(upload_id, bucket, key, total_chunks, overwrite, source_url)
    except ApiError as e:
        remote = client.head(bucket, key)
        if remote is not None and int(remote.get("size", -1)) == size:
            if log:
                log("complete 响应异常，但服务端已存在同大小对象，按成功处理")
            result = remote
        elif _is_session_error(e):
            state.clear()
            raise SessionLost(
                "服务端分片会话已失效或分片不完整；本地续传状态已清除，请重新开始上传。")
        else:
            # 合并失败（如配额/磁盘）：保留状态以便重试，不 abort
            raise

    state.clear()
    _notify(progress, size, size, 0, "上传完成")
    if log:
        log(f"上传成功：{key}（{format_bytes(size)}）")
    return result


def upload_file(client: ArtifactDepotClient, file_path: str, bucket: str, key: str,
                *, overwrite: bool = True, resume: bool = True,
                progress: Optional[ProgressCB] = None,
                log: Optional[LogCB] = None,
                cancel_event: Optional[threading.Event] = None,
                pause_event: Optional[threading.Event] = None,
                source_url: str = "") -> dict:
    """上传一个文件。小文件直传；>8MB 分片（支持续传/暂停/重试）。返回服务端 data。

    pause_event（threading.Event，set=暂停）在分片边界暂停；失败不 abort，
    保留服务端会话与本地续传状态，再次调用（同文件/目标）即从断点续传。
    """
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    stat = path.stat()
    if stat.st_size <= CHUNK_SIZE:
        return _upload_direct(client, path, stat, bucket, key, overwrite, progress, log,
                              source_url)
    return _upload_chunked(client, path, stat, bucket, key, overwrite, resume,
                           progress, log, cancel_event, pause_event, source_url)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(args: argparse.Namespace) -> int:
    token = (args.token or os.environ.get("ARTIFACTDEPOT_TOKEN") or "").strip()
    if not token and sys.stdin.isatty():
        token = getpass.getpass("Token: ").strip()
    if not token:
        print("缺少 token：请用 --token 或环境变量 ARTIFACTDEPOT_TOKEN", file=sys.stderr)
        return 2

    client = ArtifactDepotClient(args.server, token)
    key = make_key(args.path, args.file)
    last = [0.0]

    def progress(uploaded: int, total: int, speed: float, msg: str) -> None:
        now = time.monotonic()
        if uploaded < total and now - last[0] < 0.2:
            return
        last[0] = now
        pct = (uploaded / total * 100) if total else 0
        line = (f"\r{msg:<24} {pct:6.1f}%  {format_bytes(uploaded)} / {format_bytes(total)}"
                f"  {format_speed(speed)}")
        sys.stdout.write(line.ljust(110))
        sys.stdout.flush()

    def log(msg: str) -> None:
        sys.stdout.write("\r" + " " * 110 + "\r")
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    try:
        result = upload_file(
            client, args.file, args.bucket, key,
            overwrite=not args.no_overwrite, resume=not args.no_resume,
            progress=progress, log=log, source_url=args.source_url)
        sys.stdout.write("\r" + " " * 110 + "\r")
        print("上传成功：", json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print("\n已中断；已上传分片与续传状态保留，重跑同一命令将从断点继续。",
              file=sys.stderr)
        return 130
    except Exception as e:
        sys.stdout.write("\r" + " " * 110 + "\r")
        print(f"上传失败：{e}", file=sys.stderr)
        try:
            chunked = Path(args.file).expanduser().resolve().stat().st_size > CHUNK_SIZE
        except OSError:
            chunked = False
        if chunked and not args.no_resume:
            print("已保留续传状态，重跑同一命令将从断点继续"
                  "（分片会话在服务端保留；加 --no-resume 可放弃续传重新开始）。",
                  file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class LocationDialog:
    """选择 bucket + 目录（只列目录，双击进入）。"""

    def __init__(self, parent, client: ArtifactDepotClient, bucket: str, prefix: str):
        if tk is None:
            raise RuntimeError("当前 Python 没有 tkinter，无法使用图形界面")
        self.client = client
        self.prefix = (prefix or "").strip().strip("/")
        self.result: Optional[tuple] = None
        self.dirs: list = []

        self.top = tk.Toplevel(parent)
        self.top.title("选择上传位置")
        self.top.geometry("560x440")
        self.top.transient(parent)
        self.top.grab_set()

        box = ttk.Frame(self.top, padding=10)
        box.pack(fill="both", expand=True)

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Bucket:").pack(side="left")
        self.bucket_var = tk.StringVar(value=bucket or "")
        self.bucket_combo = ttk.Combobox(row, textvariable=self.bucket_var, width=28)
        self.bucket_combo.pack(side="left", padx=6)
        self.bucket_combo.bind("<<ComboboxSelected>>", lambda e: self._load_dirs())
        self.bucket_combo.bind("<Return>", lambda e: self._load_dirs())
        ttk.Button(row, text="刷新", command=self._reload).pack(side="left", padx=4)

        self.path_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.path_var, foreground="#666").pack(
            anchor="w", pady=(6, 4))

        mid = ttk.Frame(box)
        mid.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(mid, activestyle="dotbox")
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.listbox.yview)
        scroll.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=scroll.set)
        self.listbox.bind("<Double-Button-1>", lambda e: self._enter())

        btns = ttk.Frame(box)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="上一级", command=self._up).pack(side="left")
        ttk.Button(btns, text="进入选中", command=self._enter).pack(side="left", padx=6)
        ttk.Button(btns, text="刷新", command=self._reload).pack(side="left")
        ttk.Button(btns, text="选择当前目录", command=self._choose).pack(side="right")
        ttk.Button(btns, text="取消", command=self.top.destroy).pack(side="right", padx=6)

        self._load_buckets()

    def _load_buckets(self) -> None:
        try:
            buckets = self.client.list_buckets()
        except Exception as e:
            buckets = []
            messagebox.showwarning("加载 bucket 失败", str(e), parent=self.top)
        self.bucket_combo["values"] = buckets
        if not self.bucket_var.get().strip() and buckets:
            self.bucket_var.set(buckets[0])
        self._load_dirs()

    def _reload(self) -> None:
        self._load_buckets()

    def _load_dirs(self) -> None:
        bucket = self.bucket_var.get().strip()
        self.listbox.delete(0, "end")
        self.dirs = []
        self.path_var.set(f"{bucket} / {self.prefix + '/' if self.prefix else ''}")
        if not bucket:
            return
        try:
            self.dirs = self.client.list_dirs(bucket, self.prefix)
        except Exception as e:
            messagebox.showerror("加载目录失败", str(e), parent=self.top)
            return
        for item in self.dirs:
            self.listbox.insert("end", str(item.get("name", "")) + "/")

    def _enter(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self.dirs[int(sel[0])].get("name", "")
        if not name:
            return
        self.prefix = (self.prefix + "/" + name).strip("/")
        self._load_dirs()

    def _up(self) -> None:
        parts = [p for p in self.prefix.split("/") if p]
        if parts:
            parts.pop()
        self.prefix = "/".join(parts)
        self._load_dirs()

    def _choose(self) -> None:
        bucket = self.bucket_var.get().strip()
        if not bucket:
            messagebox.showwarning("缺少 bucket", "请先输入或选择 bucket", parent=self.top)
            return
        self.result = (bucket, self.prefix)
        self.top.destroy()


class UploadApp:
    def __init__(self):
        if tk is None:
            raise RuntimeError(
                "当前 Python 没有 tkinter。\n"
                "Windows/macOS 请用官方 Python；Debian/Ubuntu 可安装 python3-tk。\n"
                "也可以直接用命令行模式：--file ...")
        self.root = tk.Tk()
        self.root.title("ArtifactDepot 文件上传")
        self.root.geometry("860x620")
        self.root.minsize(760, 560)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            from tkinter import font as tkfont
            families = set(tkfont.families())
            preferred = next((f for f in (
                "Noto Sans CJK SC", "Microsoft YaHei UI", "PingFang SC",
                "Noto Sans", "DejaVu Sans") if f in families), None)
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
                opts = {"size": 10}
                if preferred:
                    opts["family"] = preferred
                tkfont.nametofont(name).configure(**opts)
        except Exception:
            pass

        self.uploading = False
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None

        saved = load_ui_state()
        initial_server = normalize_server(
            os.environ.get("ARTIFACTDEPOT_SERVER") or saved.get("server") or DEFAULT_SERVER)
        self.server_var = tk.StringVar(value=initial_server)
        self.token_var = tk.StringVar()
        self.bucket_var = tk.StringVar()
        self.path_var = tk.StringVar()
        self.file_var = tk.StringVar()
        self.overwrite_var = tk.BooleanVar(value=True)
        self.resume_var = tk.BooleanVar(value=True)

        self._build()

    def _build(self) -> None:
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, minsize=86)
        main.columnconfigure(1, weight=1)

        ttk.Label(main, text="服务地址").grid(row=0, column=0, sticky="w", pady=5)
        self.server_entry = ttk.Entry(main, textvariable=self.server_var)
        self.server_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=5)

        ttk.Label(main, text="访问令牌").grid(row=1, column=0, sticky="w", pady=5)
        self.token_entry = ttk.Entry(main, textvariable=self.token_var, show="*")
        self.token_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=5)

        ttk.Label(main, text="存储桶").grid(row=2, column=0, sticky="w", pady=5)
        bucket_row = ttk.Frame(main)
        bucket_row.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=5)
        bucket_row.columnconfigure(0, weight=1)
        self.bucket_entry = ttk.Entry(bucket_row, textvariable=self.bucket_var)
        self.bucket_entry.grid(row=0, column=0, sticky="ew")
        self.loc_btn = ttk.Button(bucket_row, text="选择位置…", command=self._choose_location)
        self.loc_btn.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(main, text="上传目录").grid(row=3, column=0, sticky="w", pady=5)
        self.path_entry = ttk.Entry(main, textvariable=self.path_var)
        self.path_entry.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=5)

        ttk.Label(main, text="本地文件").grid(row=4, column=0, sticky="w", pady=5)
        file_row = ttk.Frame(main)
        file_row.grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=5)
        file_row.columnconfigure(0, weight=1)
        self.file_entry = ttk.Entry(file_row, textvariable=self.file_var, state="readonly")
        self.file_entry.grid(row=0, column=0, sticky="ew")
        self.file_btn = ttk.Button(file_row, text="选择文件…", command=self._choose_file)
        self.file_btn.grid(row=0, column=1, padx=(8, 0))

        opts = ttk.Frame(main)
        opts.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.overwrite_chk = ttk.Checkbutton(opts, text="覆盖同名文件", variable=self.overwrite_var)
        self.overwrite_chk.pack(side="left")
        self.resume_chk = ttk.Checkbutton(opts, text="断点续传", variable=self.resume_var)
        self.resume_chk.pack(side="left", padx=(16, 0))
        self.start_btn = ttk.Button(opts, text="开始上传", command=self._start)
        self.start_btn.pack(side="left", padx=(24, 0))
        self.cancel_btn = ttk.Button(opts, text="取消", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(14, 4))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(2, 6))

        note = "大文件自动分片上传 · 失败自动重试 · 中断后选择同一文件可续传 · 令牌不写入磁盘"
        ttk.Label(main, text=note, foreground="#777", wraplength=820,
                  justify="left").grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 8))

        ttk.Label(main, text="上传日志", foreground="#666").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(0, 4))
        log_box = ttk.Frame(main)
        log_box.grid(row=10, column=0, columnspan=2, sticky="nsew")
        main.rowconfigure(10, weight=1)
        self.log_text = tk.Text(log_box, height=10, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=log_scroll.set)

    # ---- UI 动作 ----
    def _choose_file(self) -> None:
        file_path = filedialog.askopenfilename(title="选择要上传的文件")
        if file_path:
            self.file_var.set(file_path)

    def _choose_location(self) -> None:
        server = self.server_var.get().strip()
        if not server:
            messagebox.showwarning("缺少服务地址", "请填写服务地址", parent=self.root)
            return
        server = normalize_server(server)
        self.server_var.set(server)
        save_ui_state(server=server)
        token = self.token_var.get().strip()
        try:
            client = ArtifactDepotClient(server, token)
            dlg = LocationDialog(self.root, client, self.bucket_var.get().strip(),
                                 self.path_var.get().strip())
            self.root.wait_window(dlg.top)
            if dlg.result:
                self.bucket_var.set(dlg.result[0])
                self.path_var.set(dlg.result[1])
        except Exception as e:
            messagebox.showerror("选择位置失败", str(e), parent=self.root)

    def _log(self, msg: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.uploading = running
        state = "disabled" if running else "normal"
        for w in (self.server_entry, self.token_entry, self.bucket_entry, self.path_entry,
                  self.loc_btn, self.file_btn, self.start_btn, self.overwrite_chk,
                  self.resume_chk):
            try:
                w.config(state=state)
            except Exception:
                pass
        self.cancel_btn.config(state="normal" if running else "disabled")

    def _start(self) -> None:
        if self.uploading:
            return
        server = self.server_var.get().strip()
        token = self.token_var.get().strip()
        bucket = self.bucket_var.get().strip()
        target = self.path_var.get().strip()
        file_path = self.file_var.get().strip()
        if not server:
            messagebox.showwarning("缺少服务地址", "请填写服务地址", parent=self.root); return
        server = normalize_server(server)
        self.server_var.set(server)
        save_ui_state(server=server)
        if not token:
            messagebox.showwarning("缺少 Token", "请填写 Token", parent=self.root); return
        if not bucket:
            messagebox.showwarning("缺少 Bucket", "请填写或选择 bucket", parent=self.root); return
        if not file_path or not Path(file_path).is_file():
            messagebox.showwarning("文件无效", "请选择存在的本地文件", parent=self.root); return

        key = make_key(target, file_path)
        self.cancel_event.clear()
        self.progress["value"] = 0
        self.status_var.set("准备中…")
        self._log(f"目标：{bucket} / {key}")
        self._set_running(True)

        client = ArtifactDepotClient(server, token)

        def progress(uploaded: int, total: int, speed: float, msg: str) -> None:
            self.root.after(0, self._on_progress, uploaded, total, speed, msg)

        def log(msg: str) -> None:
            self.root.after(0, self._log, msg)

        def worker() -> None:
            try:
                result = upload_file(
                    client, file_path, bucket, key,
                    overwrite=self.overwrite_var.get(),
                    resume=self.resume_var.get(),
                    progress=progress, log=log,
                    cancel_event=self.cancel_event)
                self.root.after(0, self._done, result)
            except Cancelled:
                self.root.after(0, self._cancelled)
            except Exception as e:
                self.root.after(0, self._error, e)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _on_progress(self, uploaded: int, total: int, speed: float, msg: str) -> None:
        pct = (uploaded / total * 100) if total else 0
        self.progress["value"] = max(0, min(100, pct))
        self.status_var.set(
            f"{msg}  {format_bytes(uploaded)} / {format_bytes(total)}"
            f"  ({pct:.1f}%)  {format_speed(speed)}")

    def _cancelled(self) -> None:
        self._set_running(False)
        self.status_var.set("已取消（已传分片保留，下次可续传）")
        self._log("已取消；已上传分片保留，重新选择同一文件/目标可续传。")

    def _done(self, result: dict) -> None:
        self._set_running(False)
        self.progress["value"] = 100
        size = int(result.get("size") or 0)
        self.status_var.set(f"上传完成：{format_bytes(size)}")
        self._log(f"上传完成：{json.dumps(result, ensure_ascii=False)}")
        messagebox.showinfo("上传完成", f"文件已上传。\n大小：{format_bytes(size)}", parent=self.root)

    def _error(self, exc: Exception) -> None:
        self._set_running(False)
        self.status_var.set("上传失败")
        self._log(f"上传失败：{exc}")
        messagebox.showerror("上传失败", str(exc), parent=self.root)

    def _cancel(self) -> None:
        if self.uploading:
            self.cancel_event.set()
            self.status_var.set("正在取消（当前分片完成后停止）…")
            self._log("已请求取消…")

    def _on_close(self) -> None:
        save_ui_state(server=normalize_server(self.server_var.get()))
        if self.uploading:
            if not messagebox.askyesno("正在上传", "仍在传：关闭后已传分片保留，下次可续传。确定关闭？",
                                       parent=self.root):
                return
            self.cancel_event.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_gui() -> int:
    try:
        app = UploadApp()
    except Exception as e:
        print(f"无法启动图形界面：{e}", file=sys.stderr)
        print("可改用 CLI：--file <本地文件> --bucket <bucket> --path <目录> --token <token>",
              file=sys.stderr)
        return 2
    app.run()
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ArtifactDepot 极简上传客户端（GUI 默认；带 --file 为 CLI 模式）")
    p.add_argument("--server", default=os.environ.get("ARTIFACTDEPOT_SERVER", DEFAULT_SERVER),
                   help=f"服务地址（默认 {DEFAULT_SERVER}）")
    p.add_argument("--token", default="", help="访问 token（也可用环境变量 ARTIFACTDEPOT_TOKEN）")
    p.add_argument("--bucket", default="", help="目标 bucket")
    p.add_argument("--path", default="", help="目标目录（bucket 内前缀，如 carryvideo/20261024）")
    p.add_argument("--source-url", default="", help="可选：来源 URL（写入对象元数据）")
    p.add_argument("--file", default="", help="要上传的本地文件；提供则走 CLI 模式")
    p.add_argument("--no-overwrite", action="store_true", help="不覆盖同名对象")
    p.add_argument("--no-resume", action="store_true",
                   help="不续传上次未完成的分片会话（忽略本地续传状态，新建会话重传）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.file:
        if not args.bucket:
            print("CLI 模式必须提供 --bucket", file=sys.stderr)
            return 2
        return _cli(args)
    try:
        from artifactdepot_gui import run_gui as new_gui
        return new_gui()
    except ImportError:
        # 没有 bundled GUI 模块时回退到旧界面（保持兼容）
        return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
