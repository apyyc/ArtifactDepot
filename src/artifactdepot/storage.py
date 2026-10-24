# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""对象存储引擎 — S3 风格（bucket/key），文件系统为唯一事实源

存储布局：
  <depot_dir>/<bucket>/<key>...                # bucket=项目，key=任务/文件（扁平路径式）
  <depot_dir>/<bucket>/.depot.json             # 每 bucket 元数据清单（隐藏，列表跳过）

要点：
  - 列目录以文件系统扫描为准（手工放入的文件也能列出），清单只做元数据补充
  - 路径穿越防护：bucket 名校验 + key 拒绝绝对路径/`..`/空段/反斜杠/隐藏点前缀
  - 上传时计算 SHA-256 记入清单，下载走 Starlette FileResponse（原生支持 Range 206）
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

try:
    import fcntl  # POSIX 文件锁（跨进程）
except ImportError:  # pragma: no cover - 非 POSIX 平台
    fcntl = None

import httpx

from fastapi import HTTPException

from artifactdepot.config import get_config, get_upload_settings

# 隐藏清单文件名（上传/列表均跳过）
MANIFEST = ".warehouse.json"

_DEFAULT_META = {"size": 0, "sha256": "", "mtime": "", "source_url": "", "uploader": ""}


# ---------------------------------------------------------------------------
# 路径解析与安全
# ---------------------------------------------------------------------------

def _migrate_legacy_dir(root: Path) -> None:
    """一次性迁移（ArtifactDepot 0.6.1 改名）：旧 warehouse/ → depot/。

    仅当旧目录存在、新目录不存在且为空位时执行同名 rename（同一文件系统，瞬间完成，
    不移动文件内容）。已迁移/不存在则静默跳过。"""
    legacy = root.parent / "warehouse"
    if legacy.is_dir() and not root.exists():
        try:
            legacy.rename(root)
            print(f"[artifactdepot] 已迁移数据目录: {legacy} -> {root}", flush=True)
        except OSError as e:
            print(f"[artifactdepot] 数据目录迁移失败（{e}），将继续使用旧目录", flush=True)
            raise


def get_root() -> Path:
    root = Path(get_config()["depot_dir"])
    _migrate_legacy_dir(root)
    return root


def _meta_path(name: str) -> Path:
    """仓库状态文件（tokens.json / signed_links.json / audit.log）路径。

    config.meta_dir 非空时放在该目录（相对路径按 depot_dir 解析），
    否则默认放在 depot_dir 下。"""
    base = get_root()
    d = get_config().get("meta_dir", "").strip()
    if d:
        base = Path(d)
        if not base.is_absolute():
            base = get_root() / base
    return base / name


# ---------------------------------------------------------------------------
# 状态文件原子写 + 并发锁
#
# tokens.json / signed_links.json / .warehouse.json 都是「读-改-写」，
# 并发请求或进程崩溃会造成丢更新 / 半截文件。这里统一：
#   1. 写临时文件 + fsync + os.replace 原子替换；
#   2. 进程内 threading 锁 + 跨进程 fcntl.flock 串行化 RMW。
# 锁文件放在 meta_dir 下，状态文件本身仍走原子替换。
# ---------------------------------------------------------------------------

_STATE_MUTEX = threading.RLock()
_STATE_TLS = threading.local()


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """原子写文本：临时文件 + fsync + os.replace；失败清理临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def state_lock():
    """串行化状态文件读-改-写。

    - 进程内：threading.RLock（可重入，避免同线程嵌套死锁）；
    - 跨进程：fcntl.flock(meta_dir/.state.lock)（仅 POSIX）。
    嵌套调用会复用外层锁，不会重复 flock。
    """
    depth = getattr(_STATE_TLS, "depth", 0)
    if depth:
        _STATE_TLS.depth = depth + 1
        try:
            yield
        finally:
            _STATE_TLS.depth -= 1
        return
    with _STATE_MUTEX:
        _STATE_TLS.depth = 1
        lock_file = None
        try:
            if fcntl is not None:
                lock_path = _meta_path(".state.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_file = open(lock_path, "a+")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if lock_file is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                if lock_file is not None:
                    lock_file.close()
                _STATE_TLS.depth = 0


def validate_bucket(bucket: str) -> str:
    """校验 bucket 名：允许中文等 Unicode，但拒绝路径分隔符/穿越/隐藏/控制字符"""
    if not bucket:
        raise HTTPException(400, detail="bucket 名不能为空")
    if bucket != bucket.strip():
        # 首尾空白会造成「授权按 strip 后比较、取数按原样」的错配（跨 bucket 越权），直接拒绝。
        raise HTTPException(400, detail=f"非法 bucket 名（首尾不允许空白字符）：{bucket!r}")
    if len(bucket) > 63:
        raise HTTPException(400, detail=f"bucket 名过长（最长 63 字符）：{bucket!r}")
    if bucket in (".", "..") or bucket.startswith("."):
        raise HTTPException(400, detail=f"非法 bucket 名（不允许以 . 开头）：{bucket!r}")
    if "/" in bucket or "\\" in bucket or any(ord(ch) < 32 for ch in bucket):
        raise HTTPException(400, detail=f"非法 bucket 名（不允许路径分隔符/控制字符）：{bucket!r}")
    return bucket


def validate_key(key: str) -> str:
    """校验并规范化对象 key（相对路径式），去掉首尾斜杠，拒绝穿越"""
    if key is None:
        return ""
    k = unquote(key).strip("/")
    if not k:
        return ""
    if "\\" in k:
        raise HTTPException(400, detail=f"非法 key：{key!r}")
    # 与 bucket 一致：控制字符（含 CR/LF/TAB）不能进入 key；否则会污染日志/审计，
    # 并在下载响应 Content-Disposition 中触发 header 注入或 500。
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in k):
        raise HTTPException(400, detail=f"非法 key（不允许控制字符）：{key!r}")
    segs = k.split("/")
    if any(s in ("", "..", ".") for s in segs):
        raise HTTPException(400, detail=f"非法 key：{key!r}")
    # 隐藏点文件/目录不允许作为对象 key：避免与 .warehouse.json、.keep 等隐藏约定混淆
    if any(s.startswith(".") for s in segs):
        raise HTTPException(400, detail=f"非法 key（路径段不允许以 . 开头）：{key!r}")
    return k


def normalize_prefixes(prefixes) -> list:
    """规范化令牌的路径前缀列表：URL-decode、去首尾斜杠、去重，并拒绝非法写法。

    前缀在 `auth.resource_allowed()` 里按路径段边界匹配；若放过 `a/../secret`、
    `%2e%2e`、反斜杠等写法，会造出「能通过创建校验、但永远匹配不上」的死 token，
    故直接 400。另外 `"/"` 这类 normalize 后为空的写法也拒绝——若静默丢弃，
    会从「只允许根」变成「不限前缀」，与用户意图相反。
    """
    out = []
    for p in prefixes or []:
        if p is None:
            raise HTTPException(400, detail="路径前缀不能为 null")
        raw = str(p).strip()
        if not raw:
            continue                      # 完全空字符串：忽略
        s = unquote(raw).strip().strip("/")
        if not s:
            raise HTTPException(400, detail=f"路径前缀不能为空：{p!r}")
        if "\\" in s or any(seg in ("", ".", "..") for seg in s.split("/")):
            raise HTTPException(
                400, detail=f"非法路径前缀（不允许反斜杠 / 空段 / . / ..）：{p!r}")
        if s not in out:
            out.append(s)
    return out


def validate_scope_range(allow_buckets, allow_prefixes) -> None:
    """校验资源范围的内部一致性：写了路径前缀，就必须同时把该 bucket 纳入 allow_buckets。

    否则 `allow_buckets=[]`（= 不限 bucket）配合 `allow_prefixes={"demo": [...]}`
    只会收窄 demo，**其余 bucket 反而完全开放**——与「只给 demo:a/」的直觉相反。
    网页 UI 早已阻止这种组合，API 侧现在同样以 `400` 拒绝（不静默放大权限）。
    """
    if allow_prefixes is not None and not isinstance(allow_prefixes, dict):
        raise HTTPException(400, detail="allow_prefixes 必须是对象")
    buckets = [str(b).strip() for b in (allow_buckets or []) if str(b).strip()]
    pfx_keys = {str(b).strip() for b in (allow_prefixes or {}) if str(b).strip()}
    if not pfx_keys:
        return
    if not buckets:
        raise HTTPException(
            400,
            detail="设置 allow_prefixes 时必须同时设置 allow_buckets，"
                   "否则未被点名的 bucket 将不受任何限制")
    extra = sorted(pfx_keys - set(buckets))
    if extra:
        raise HTTPException(
            400,
            detail=f"allow_prefixes 里的 bucket 必须在 allow_buckets 中（缺少：{', '.join(extra)}）")


def bucket_dir(bucket: str) -> Path:
    validate_bucket(bucket)
    root = get_root().resolve()
    d = (root / bucket).resolve()
    if not d.is_relative_to(root):
        raise HTTPException(400, detail="bucket 路径越界")
    return d


def object_path(bucket: str, key: str) -> Path:
    """安全解析对象在磁盘上的路径（文件或目录），确保落在仓库根内"""
    validate_bucket(bucket)
    k = validate_key(key)
    if not k:
        raise HTTPException(400, detail="key 不能为空")
    root = get_root().resolve()
    p = (bucket_dir(bucket) / k).resolve()
    if not p.is_relative_to(root):
        raise HTTPException(400, detail="key 越界")
    return p


# ---------------------------------------------------------------------------
# 元数据清单
# ---------------------------------------------------------------------------

def _manifest_path(bucket: str) -> Path:
    return bucket_dir(bucket) / MANIFEST


def _manifest_load(bucket: str) -> dict:
    p = _manifest_path(bucket)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _manifest_save(bucket: str, manifest: dict) -> None:
    _atomic_write_text(_manifest_path(bucket),
                       json.dumps(manifest, ensure_ascii=False, indent=2), mode=0o644)


# ---------------------------------------------------------------------------
# 对象操作
# ---------------------------------------------------------------------------

def put_object(bucket: str, key: str, src_path: Path, source_url: str = "",
               overwrite: bool = True, uploader: str = "") -> dict:
    """把已落盘的临时文件放入对象存储（流式拷贝，计算 SHA-256，记录元数据）"""
    validate_bucket(bucket)
    k = validate_key(key)
    dst = object_path(bucket, k)
    if dst.is_dir():
        # 目录不能当对象覆盖；明确 409，避免 shutil.copyfile 抛 IsADirectoryError -> 500
        raise HTTPException(409, detail=f"目标路径是目录，不能写入对象：{bucket}/{key}")
    if dst.exists() and not overwrite:
        raise HTTPException(409, detail=f"对象已存在：{bucket}/{key}")

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError, OSError) as e:
        # 父路径是文件/不可创建目录：返回可理解的冲突，而不是未捕获 500
        raise HTTPException(409, detail=f"父路径不是目录，不能写入对象：{bucket}/{key}") from e
    # 计算 SHA-256 + 大小（流式，避免大文件占内存）
    h = hashlib.sha256()
    size = 0
    with open(src_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    sha256 = h.hexdigest()
    # 流式拷贝到最终位置
    shutil.copyfile(src_path, dst)
    meta = _DEFAULT_META | {
        "size": size,
        "sha256": sha256,
        "mtime": _utc_now_iso(),
        "source_url": source_url or "",
        "uploader": uploader or "",
    }
    with state_lock():
        manifest = _manifest_load(bucket)
        manifest[k] = meta
        _manifest_save(bucket, manifest)
    return {"bucket": bucket, "key": k, "size": size, "sha256": sha256}


def _mtime_iso(ts: float) -> str:
    """epoch 秒 -> UTC ISO8601（统一列表/探测接口的时间格式）。"""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return ""


def _dir_recursive_info(d: Path) -> dict:
    """递归统计目录的大小与最新修改时间（跳过隐藏文件与隐藏目录内的内容）。

    大小 = 所有可见文件（含嵌套子目录 + 当前目录）的总字节数；
    时间 = 所有可见文件里最新的 mtime（从最底层到最外层统一取最大）。
    用 os.walk 剪枝隐藏目录，避免把 .git/.cache 等算进去、也少扫一遍。
    """
    total_size = 0
    latest = 0.0
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            try:
                st = (Path(root) / name).stat()
            except OSError:
                continue
            total_size += st.st_size
            if st.st_mtime > latest:
                latest = st.st_mtime
    if latest:
        mtime = _mtime_iso(latest)
    else:
        # 空目录没有文件可统计，退回目录自身 mtime，而不是返回当前时间
        try:
            mtime = _mtime_iso(d.stat().st_mtime)
        except OSError:
            mtime = _utc_now_iso()
    return {"size": total_size, "mtime": mtime}


def list_objects(bucket: str, prefix: str = "",
                 limit: int = 0, offset: int = 0) -> dict:
    """列对象：prefix 限定时列出该目录下的直接子项；文件系统扫描为准。

    返回 {"items", "total", "offset", "limit", "truncated"}。
    limit>0 时按当前排序做 offset 分页（目录在前、名称排序），兼容旧调用（limit=0 全量）。
    """
    validate_bucket(bucket)
    base = bucket_dir(bucket)
    if not base.exists():
        return {"items": [], "total": 0, "offset": 0, "limit": 0, "truncated": False}
    pdir = base
    rel_prefix = validate_key(prefix)
    if rel_prefix:
        pdir = (base / rel_prefix).resolve()
        if not pdir.is_relative_to(base):
            raise HTTPException(400, detail="prefix 越界")
        if not pdir.exists() or pdir.is_file():
            return {"items": [], "total": 0, "offset": 0, "limit": 0, "truncated": False}

    manifest = _manifest_load(bucket)
    items = []
    for child in sorted(pdir.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name == MANIFEST or child.name.startswith("."):
            continue
        rel = child.relative_to(base).as_posix()
        if child.is_dir():
            info = _dir_recursive_info(child)
            items.append({"name": child.name, "key": rel + "/", "is_dir": True,
                          "size": info["size"], "mtime": info["mtime"]})
        else:
            try:
                st = child.stat()
            except OSError:
                continue   # 并发删除/改名，跳过而不是 500
            meta = manifest.get(rel, {}) or {}
            mtime = _ensure_utc_z(meta.get("mtime", ""))
            if not mtime:
                # 手工放入、没有清单记录的文件：回退到文件系统 mtime
                mtime = _mtime_iso(st.st_mtime)
            items.append({
                "name": child.name,
                "key": rel,
                "is_dir": False,
                "size": st.st_size,
                "mtime": mtime,
                "sha256": meta.get("sha256", ""),
                "source_url": meta.get("source_url", ""),
                "uploader": meta.get("uploader", ""),
            })
    total = len(items)
    truncated = False
    if limit and limit > 0:
        page = items[offset:offset + limit] if offset > 0 else items[:limit]
        truncated = (offset + len(page)) < total
        items = page
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "truncated": truncated}


def mkdir(bucket: str, key: str) -> dict:
    """新建目录：按 key 创建目录树，并放一个隐藏的 .keep 占位文件（列表会跳过隐藏文件）

    key 可含多级（如 task_5/交付物），自动逐级创建。"""
    validate_bucket(bucket)
    k = validate_key(key)
    if not k:
        raise HTTPException(400, detail="目录名不能为空")
    d = object_path(bucket, k)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".keep").touch()
    return {"bucket": bucket, "key": k + "/"}


def delete_object(bucket: str, key: str) -> dict:
    """删除对象或目录。

    安全策略：目录仅允许删除**空目录**（内部没有非隐藏的文件/子目录）；
    含内容的目录返回 400，需先清空（逐个删除文件/子目录）才能删除。
    隐藏占位文件（.keep 等）不算内容，删除空目录时一并移除。"""
    validate_bucket(bucket)
    k = validate_key(key)
    p = object_path(bucket, k)
    if not p.exists():
        raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")

    if p.is_dir():
        visible = [c for c in p.iterdir() if not c.name.startswith(".")]
        if visible:
            raise HTTPException(400,
                detail=f"目录非空（含 {len(visible)} 项文件/子目录），不允许删除，请先清空")
        # 删除隐藏占位 + 空目录
        for c in p.iterdir():
            try:
                if c.is_file():
                    c.unlink()
            except OSError:
                pass
        try:
            p.rmdir()
        except OSError:
            raise HTTPException(400, detail="目录删除失败（可能仍有内容）")
    else:
        with state_lock():
            manifest = _manifest_load(bucket)
            manifest.pop(k, None)
            _manifest_save(bucket, manifest)
        p.unlink()
    return {"deleted": f"{bucket}/{k}"}


# 批量删除（一键清空目录内容）单次最多处理的项数：files + dirs 超过即 400，
# 避免误点把整个大仓库扫一遍（dry_run 预览也受限，防止预览本身成为扫描炸弹）。
MAX_BULK_DELETE_ITEMS = 50000

# errors 明细最多返回条数（契约 §1.3：最多 20 条，剩余失败只影响计数不刷屏）
_MAX_BULK_DELETE_ERRORS = 20


def _bulk_error(errors: list, bucket_root: Path, path: Path, exc: OSError) -> None:
    """记录一条部分失败明细（key 相对 bucket 根），超过上限后不再追加。"""
    if len(errors) >= _MAX_BULK_DELETE_ERRORS:
        return
    try:
        key = path.relative_to(bucket_root).as_posix()
    except ValueError:
        key = path.as_posix()
    errors.append({"key": key, "error": str(exc)})


def _bulk_walk(root_dir: Path, bucket_root: Path, errors: list) -> tuple[list, list]:
    """递归收集 root_dir 下全部待删项（不修改磁盘）。

    - 返回 (files, dirs)：files 为 [(Path, size)]；dirs **自底向上**排列，便于 rmdir；
    - 符号链接用 `is_symlink()` 识别，按文件处理（只删链接本身），**绝不跟随**；
    - 隐藏文件/隐藏目录一并收集（mode=all 语义）；
    - bucket 根目录的 `.warehouse.json` 永不收集（清单文件受保护）；
    - 目录遍历失败记入 errors 并跳过该目录，不中断整体。
    """
    files = []
    stack = [root_dir]
    preorder = []           # 先序访问顺序：父目录一定排在子目录之前
    while stack:
        d = stack.pop()
        preorder.append(d)
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            _bulk_error(errors, bucket_root, d, e)
            continue
        for entry in entries:
            p = Path(entry.path)
            if p == bucket_root / MANIFEST:
                continue                  # 保护 bucket 根清单
            try:
                if entry.is_symlink():
                    files.append((p, entry.stat(follow_symlinks=False).st_size))
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(p)
                else:
                    files.append((p, entry.stat(follow_symlinks=False).st_size))
            except OSError as e:
                _bulk_error(errors, bucket_root, p, e)
    # 反转先序 = 子目录优先；去掉 root_dir 自身（prefix 目录必须保留）
    return files, list(reversed(preorder[1:]))


def bulk_delete(bucket: str, prefix: str = "", mode: str = "files",
                dry_run: bool = True) -> dict:
    """批量删除目录内容（一键清空）。prefix 目录本身保留；返回统计与错误明细。

    - mode="files"：只删 base 的直接**普通文件**；子目录、符号链接、隐藏项
      （含 `.keep`）与 `.warehouse.json` 全部保留；
    - mode="all"：递归删除 base 下所有文件与子目录（含隐藏项）；符号链接只删链接、
      不跟随；base 本身保留；bucket 根 `.warehouse.json` 永不删除；
    - 先收集目标并检查项数上限，再执行（dry_run 在删除前返回）；
    - 先删文件、再自底向上删目录；清单在 state_lock 内一次性清理；
    - FileNotFoundError 视为「已不存在」跳过，其余 OSError 记入 errors 继续。
    """
    validate_bucket(bucket)
    k = validate_key(prefix)
    if mode not in ("files", "all"):
        raise HTTPException(400, detail=f"非法 mode：{mode!r}（仅支持 files / all）")
    root = bucket_dir(bucket)
    if not root.is_dir():
        raise HTTPException(404, detail=f"bucket 不存在：{bucket}")
    base = object_path(bucket, k) if k else root
    # 安全加固：object_path 只保证解析结果落在**仓库根**内。若 bucket 内手工放了
    # 指向其他 bucket 的符号链接，prefix 会解析到别的 bucket；批量递归删除绝不能
    # 跨 bucket 生效（否则会清空别人数据、清单却记在当前 bucket），一律拒绝。
    if not base.is_relative_to(root):
        raise HTTPException(400, detail="prefix 越界：符号链接指向 bucket 之外")
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, detail=f"目录不存在：{bucket}/{k}")

    errors: list = []
    if mode == "files":
        files: list = []
        dirs: list = []
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError as e:
            _bulk_error(errors, root, base, e)
            children = []
        for child in children:
            if child.name.startswith(".") or child.name == MANIFEST:
                continue                  # 跳过隐藏项与清单
            try:
                if child.is_symlink() or not child.is_file():
                    continue              # 子目录/符号链接保留（files 模式不递归）
                files.append((child, child.stat().st_size))
            except OSError as e:
                _bulk_error(errors, root, child, e)
    else:
        files, dirs = _bulk_walk(base, root, errors)

    total = len(files) + len(dirs)
    if total > MAX_BULK_DELETE_ITEMS:
        raise HTTPException(
            400,
            detail=f"目录过大（共 {total} 项，单次上限 {MAX_BULK_DELETE_ITEMS} 项），请缩小范围后再试")

    if dry_run:
        return {"bucket": bucket, "prefix": k, "mode": mode, "dry_run": True,
                "files": len(files), "dirs": len(dirs),
                "bytes": sum(size for _, size in files), "errors": errors}

    deleted_files = deleted_dirs = deleted_bytes = 0
    deleted_keys: list = []
    for path, size in files:
        try:
            path.unlink()
        except FileNotFoundError:
            continue                      # 并发删除：已不存在，不计错误
        except OSError as e:
            _bulk_error(errors, root, path, e)
            continue
        deleted_files += 1
        deleted_bytes += size
        try:
            deleted_keys.append(path.relative_to(root).as_posix())
        except ValueError:
            pass

    dir_keys: list = []
    for d in dirs:
        try:
            d.rmdir()
        except FileNotFoundError:
            continue
        except OSError as e:
            _bulk_error(errors, root, d, e)
            continue
        deleted_dirs += 1
        try:
            dir_keys.append(d.relative_to(root).as_posix())
        except ValueError:
            pass

    # 清单一次性清理：被删文件 key + 被删目录前缀下的全部条目
    if deleted_keys or dir_keys:
        with state_lock():
            manifest = _manifest_load(bucket)
            new_manifest = dict(manifest)
            for key in deleted_keys:
                new_manifest.pop(key, None)
            dir_prefixes = tuple(dk + "/" for dk in dir_keys)
            for mkey in list(new_manifest):
                if mkey in dir_keys or mkey.startswith(dir_prefixes):
                    new_manifest.pop(mkey, None)
            if new_manifest != manifest:
                _manifest_save(bucket, new_manifest)

    return {"bucket": bucket, "prefix": k, "mode": mode, "dry_run": False,
            "files": deleted_files, "dirs": deleted_dirs,
            "bytes": deleted_bytes, "errors": errors}


# 一键下载（ZIP 打包）单次最多包含的项数（files + dirs），防御异常大的目录
MAX_BULK_DOWNLOAD_ITEMS = 100000


def bulk_download_scan(bucket: str, prefix: str = "") -> dict:
    """只读扫描「一键下载」目标目录：返回可见文件/子目录清单与统计。

    供 API 层做「单文件直下 / ZIP」判定与 ZIP 打包，不修改任何数据：
    - 隐藏项（名字以 "." 开头，含 `.keep` / `.warehouse.json`）与符号链接全部跳过；
    - prefix 解析（含符号链接）后必须仍在 bucket 内，否则 400（防跨 bucket 读取）；
    - bucket / prefix 不存在或不是目录 → 404；没有任何可见内容 → 404；
    - files + dirs 超过 MAX_BULK_DOWNLOAD_ITEMS → 400；
    - 输出顺序稳定：每层先子目录（深度优先）再本层文件；目录名不带结尾 "/"，
      由 zip 生成器统一拼接。entries 项：{"name", "path", "is_dir", "size"}。
    """
    validate_bucket(bucket)
    k = validate_key(prefix)
    bdir = bucket_dir(bucket)
    if not bdir.is_dir():
        raise HTTPException(404, detail=f"bucket 不存在：{bucket}")
    if k:
        base = (bdir / k).resolve()
        # 关键加固：符号链接解析后必须仍在**本 bucket** 内，不能借 prefix 读别的 bucket
        if not base.is_relative_to(bdir):
            raise HTTPException(400, detail=f"prefix 越界（解析到 bucket 之外）：{prefix!r}")
    else:
        base = bdir
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, detail=f"目录不存在：{bucket}/{k}")

    entries: list = []
    counts = {"files": 0, "dirs": 0}

    def _check_limit() -> None:
        if counts["files"] + counts["dirs"] > MAX_BULK_DOWNLOAD_ITEMS:
            raise HTTPException(
                400,
                detail=f"目录过大（超过 {MAX_BULK_DOWNLOAD_ITEMS} 项），请缩小范围后再下载")

    def _walk(d: Path, rel: str) -> None:
        try:
            with os.scandir(d) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError:
            return                                # 目录读不了：跳过（不阻断其余内容）
        subdirs = []
        level_files = []
        for entry in children:
            if entry.name.startswith("."):
                continue                          # 跳过隐藏文件/目录（.keep/.warehouse.json）
            try:
                if entry.is_symlink():
                    continue                      # 符号链接不跟随、不打包
                rel_name = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    counts["dirs"] += 1
                    _check_limit()
                    entries.append({"name": rel_name, "path": Path(entry.path),
                                    "is_dir": True, "size": 0,
                                    "mtime": st.st_mtime})
                    subdirs.append((Path(entry.path), rel_name))
                else:
                    st = entry.stat(follow_symlinks=False)
                    counts["files"] += 1
                    _check_limit()
                    level_files.append({"name": rel_name, "path": Path(entry.path),
                                        "is_dir": False, "size": st.st_size,
                                        "mtime": st.st_mtime})
            except OSError:
                continue                          # 并发删除/权限问题：跳过该项
        for sub_path, sub_rel in subdirs:
            _walk(sub_path, sub_rel)
        entries.extend(level_files)

    _walk(base, "")
    if counts["files"] == 0 and counts["dirs"] == 0:
        raise HTTPException(404, detail="目录为空，没有可下载的内容")

    single_file = None
    single_file_key = ""
    if counts["files"] == 1 and counts["dirs"] == 0:
        single_file = entries[0]
        single_file_key = f"{k}/{single_file['name']}" if k else single_file["name"]

    return {"bucket": bucket, "prefix": k, "base": base,
            "root_name": k.split("/")[-1] if k else bucket,
            "files": counts["files"], "dirs": counts["dirs"],
            "entries": entries,
            "single_file": single_file, "single_file_key": single_file_key}


def create_bucket(bucket: str) -> dict:
    """显式创建 bucket（幂等）：内部放 .keep 占位；已存在返回 created=false"""
    d = bucket_dir(bucket)
    created = not d.exists()
    if created:
        d.mkdir(parents=True)
        (d / ".keep").touch()
    return {"bucket": d.name, "created": created}


def rename_object(bucket: str, key: str, new_key: str, overwrite: bool = False) -> dict:
    """重命名/移动对象或目录：整体迁移该 key（前缀）下的全部对象与 .keep 占位。

    - key 指向文件：仅重命名该对象（清单记录一并迁移）
    - key 指向目录：迁移该前缀下所有对象与隐藏占位，失败整体回滚
    - new_key 位于 key 子目录内返回 400；源不存在 404；目标冲突且未 overwrite 时 409
    """
    validate_bucket(bucket)
    src = object_path(bucket, key)
    if not src.exists():
        raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")
    k = validate_key(key)
    nk = validate_key(new_key)
    if not nk:
        raise HTTPException(400, detail="new_key 不能为空")
    if nk == k:
        raise HTTPException(400, detail="new_key 与 key 相同")
    if nk.startswith(k + "/"):
        raise HTTPException(400, detail="不允许把目录移动到自身子目录")

    base = bucket_dir(bucket)
    dst = (base / nk).resolve()
    if not dst.is_relative_to(base):
        raise HTTPException(400, detail="new_key 越界")

    if src.is_file():
        if dst.exists() and not overwrite:
            raise HTTPException(409, detail=f"目标已存在：{bucket}/{nk}")
        moves = [(src, dst)]
    else:
        moves = []
        for path in sorted(src.rglob("*")):
            if path.is_file():
                target = base / nk / path.relative_to(src)
                if target.exists() and not overwrite:
                    raise HTTPException(
                        409, detail=f"目标已存在：{bucket}/{target.relative_to(base).as_posix()}")
                moves.append((path, target))

    try:
        for path, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            path.rename(target)
        if src.is_dir():
            for path in sorted(src.rglob("*"), reverse=True):
                if path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        raise HTTPException(400, detail="源目录仍非空，已回滚")
            (src / ".keep").unlink(missing_ok=True)
            try:
                src.rmdir()
            except OSError:
                raise HTTPException(400, detail="源目录仍非空，已回滚")
    except HTTPException:
        for path, target in moves:
            if target.exists() and not path.exists():
                target.rename(path)
        raise

    # 清单记录 key 前缀整体替换
    with state_lock():
        manifest = _manifest_load(bucket)
        new_manifest = {}
        prefix = k + "/"
        for mk, mv in manifest.items():
            if mk == k:
                new_manifest[nk] = mv
            elif mk.startswith(prefix):
                new_manifest[f"{nk}/{mk[len(prefix):]}"] = mv
            else:
                new_manifest[mk] = mv
        _manifest_save(bucket, new_manifest)
    return {"bucket": bucket, "from_key": k, "to_key": nk, "moved_objects": len(moves)}


def head_object(bucket: str, key: str) -> dict:
    """轻量探测文件或目录是否存在；mtime 统一为 UTC ISO8601（与 list 一致）。"""
    validate_bucket(bucket)
    k = validate_key(key)
    p = object_path(bucket, key)
    if not p.exists():
        return {"exists": False, "bucket": bucket, "key": k}
    if p.is_dir():
        info = _dir_recursive_info(p)
        try:
            mtime = _mtime_iso(p.stat().st_mtime)
        except OSError:
            mtime = ""
        return {"exists": True, "is_dir": True, "bucket": bucket, "key": k + "/",
                "size": info.get("size", 0), "mtime": mtime}
    try:
        st = p.stat()
    except OSError:
        raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")
    entry = _manifest_load(bucket).get(k, {}) or {}
    return {"exists": True, "is_dir": False, "bucket": bucket, "key": k,
            "size": st.st_size, "sha256": entry.get("sha256", ""),
            "mtime": _mtime_iso(st.st_mtime)}


def object_etag(bucket: str, key: str) -> str:
    """对象 ETag：优先用清单里的 SHA-256（强 ETag），否则回退 size-mtime。

    文件不存在或 key 指向目录时返回 ""。
    """
    p = object_path(bucket, key)
    if not p.is_file():
        return ""
    k = validate_key(key)
    entry = _manifest_load(bucket).get(k, {}) or {}
    sha = (entry.get("sha256") or "").strip()
    if sha:
        return f'"{sha}"'
    try:
        st = p.stat()
    except OSError:
        return ""
    return f'"{st.st_size}-{int(st.st_mtime)}"'


def list_buckets() -> list:
    root = get_root()
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


# ---------------------------------------------------------------------------
# 预签名 URL（HMAC-SHA256）
# ---------------------------------------------------------------------------

def _signature(secret: str, bucket: str, key: str, expires: int) -> str:
    msg = f"{bucket}\n{key}\n{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def presign(bucket: str, key: str, expires: int = 3600) -> dict:
    """生成限时签名下载 URL（secret = access_token）"""
    validate_bucket(bucket)
    k = validate_key(key)
    secret = get_config().get("access_token", "")
    if not secret:
        raise HTTPException(400, detail="未配置 access_token，无法生成签名链接")
    exp = int(time.time()) + max(1, expires)
    sig = _signature(secret, bucket, k, exp)
    url = f"/api/objects/download?bucket={bucket}&key={k}&expires={exp}&sig={sig}"
    return {"url": url, "expires_at": exp}


def verify_signature(bucket: str, key: str, expires: str, sig: str) -> bool:
    secret = get_config().get("access_token", "")
    if not secret or not sig:
        return False
    try:
        exp = int(expires)
    except (TypeError, ValueError):
        return False
    if time.time() > exp:
        return False
    # 编码为字节再比较（compare_digest 不接受非 ASCII，避免恶意签名 500）
    return hmac.compare_digest(_signature(secret, bucket, key, exp).encode("utf-8"),
                               str(sig).encode("utf-8"))


# ---------------------------------------------------------------------------
# 签名链接注册表（1-10次 / 一小时 / 永久；管理员可作废全部、创建者可作废自己的）
# ---------------------------------------------------------------------------

def _signed_links_path() -> Path:
    return _meta_path("signed_links.json")


def _utc_now_iso() -> str:
    """统一 UTC 时间（ISO-8601 + Z），避免容器时区导致记录偏差"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_utc_z(ts: str) -> str:
    """兼容旧数据：UTC ISO 时间缺 Z 后缀时补 Z（时间修复前的旧数据存 UTC 但未加 Z），
    使前端能正确转本地时区；空值 / 已带 Z / 非标准格式原样返回"""
    if not ts:
        return ""
    ts = ts.strip()
    if ts.endswith("Z") or ts.endswith("z"):
        return ts
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$", ts):
        return ts + "Z"
    return ts


def load_signed_links() -> list:
    try:
        links = json.loads(_signed_links_path().read_text(encoding="utf-8")) or []
    except Exception:
        return []
    for link in links:
        if isinstance(link, dict):
            link["created_at"] = _ensure_utc_z(link.get("created_at", ""))
    return links


def save_signed_links(links: list) -> None:
    _atomic_write_text(_signed_links_path(),
                       json.dumps(links, ensure_ascii=False, indent=2), mode=0o600)


def create_signed_link(bucket: str, key: str, mode: str = "time",
                       count: int = 1, expires: int = 3600, created_by: str = "") -> dict:
    """创建签名链接。mode: count(按次数) / time(按时效) / permanent(永久，可作废)

    次数与时效的上下限由 config.signed_links 配置（count_min/max、expire_min/max 秒）。"""
    validate_bucket(bucket)
    k = validate_key(key)
    if not k:
        raise HTTPException(400, detail="key 不能为空")
    if mode not in ("count", "time", "permanent"):
        raise HTTPException(400, detail="mode 必须为 count / time / permanent")

    cfg = get_config().get("signed_links", {})
    count_min = int(cfg.get("count_min", 1) or 1)
    count_max = int(cfg.get("count_max", 10) or 10)
    exp_min = int(cfg.get("expire_min_seconds", 60) or 60)
    exp_max = int(cfg.get("expire_max_seconds", 604800) or 604800)

    if mode == "count":
        count = int(count)
        if count < count_min or count > count_max:
            raise HTTPException(400, detail=f"次数需在 {count_min}-{count_max} 之间")
    else:
        count = None

    if mode == "time":
        expires = int(expires)
        if expires < exp_min or expires > exp_max:
            raise HTTPException(400, detail=f"时效需在 {exp_min}-{exp_max} 秒之间")
        expires_ts = int(time.time()) + expires
    else:
        expires_ts = None

    entry = {
        "id": secrets.token_urlsafe(8),
        "token": secrets.token_urlsafe(16),
        "bucket": bucket,
        "key": k,
        "mode": mode,
        "max_uses": count,
        "remaining": count,
        "expires": expires_ts,
        "created_by": created_by or "",
        "created_at": _utc_now_iso(),
        "revoked": False,
    }
    with state_lock():
        links = load_signed_links()
        links.append(entry)
        save_signed_links(links)
    return entry


def get_signed_link(link_id: str):
    for e in load_signed_links():
        if e.get("id") == link_id:
            return e
    return None


def consume_signed_link(link_id: str, secret: str, bucket: str, key: str):
    """校验并消费一个链接（count 模式递减剩余次数）。返回 (ok, actor, error)

    整段读-改-写在 state_lock 内完成，避免并发下载把按次数链接用超。
    """
    with state_lock():
        links = load_signed_links()
        for e in links:
            if e.get("id") == link_id:
                if e.get("revoked"):
                    return False, "", "链接已作废"
                if not hmac.compare_digest(str(e.get("token") or ""), str(secret or "")):
                    return False, "", "链接无效"
                if e.get("bucket") != bucket or e.get("key") != key:
                    return False, "", "链接与文件不符"
                if e.get("mode") == "time" and time.time() > (e.get("expires") or 0):
                    return False, "", "链接已过期"
                if e.get("mode") == "count":
                    if (e.get("remaining") or 0) <= 0:
                        return False, "", "链接次数已用完"
                    e["remaining"] = e["remaining"] - 1
                    save_signed_links(links)
                return True, e.get("created_by") or "", ""
        return False, "", "链接不存在"


def revoke_signed_link(link_id: str) -> bool:
    with state_lock():
        links = load_signed_links()
        for e in links:
            if e.get("id") == link_id:
                e["revoked"] = True
                save_signed_links(links)
                return True
        return False


# ---------------------------------------------------------------------------
# Token 注册表（token → 用户名）— 自维护映射，由平台推送 / 网页管理
# ---------------------------------------------------------------------------

def _tokens_path() -> Path:
    return _meta_path("tokens.json")


# 角色预设（延迟导入，避免循环依赖）
def _role_preset_scopes(role: str):
    from artifactdepot.permissions import role_scopes
    return role_scopes(role)


def _default_token_record(user: str, role: str = "user") -> dict:
    """构造一个默认 token 记录；scopes 为角色预设的有效权限点列表"""
    scopes = _role_preset_scopes(role) if role != "custom" else []
    if scopes is None:
        role = "user"
        scopes = _role_preset_scopes(role) or []
    return {
        "user": (user or "").strip(),
        "role": role,
        "scopes": list(scopes),
        "allow_buckets": [],
        "allow_prefixes": {},
        "enabled": True,
        "expires_at": "",
        "description": "",
        "created_by": "",
        "created_at": _utc_now_iso(),
        "last_used_at": "",
    }


def _normalize_token_record(value, token: str = "") -> dict:
    """把任意历史形态归一化为记录 dict。

    - 旧格式：`"用户名"` → role=user 的完整记录（保持历史全部非管理权限）
    - 新格式：dict；缺字段补默认，scopes 缺失时按 role 预设补
    """
    if isinstance(value, str):
        return _default_token_record(value, "user")
    if not isinstance(value, dict):
        return _default_token_record("", "user")
    rec = dict(value)
    user = (rec.get("user") or rec.get("name") or "").strip()
    role = (rec.get("role") or "user").strip() or "user"
    scopes = rec.get("scopes")
    # 仅“字段缺失 / 非数组”时回退到角色预设；显式空数组是合法语义（= 无任何权限，
    # 见 docs/api_rules.md 12.4），必须原样保留，否则无法生成“无权限 token”。
    if scopes is None or not isinstance(scopes, list):
        preset = _role_preset_scopes(role)
        scopes = list(preset or [])
    rec["user"] = user
    rec["role"] = role
    rec["scopes"] = [str(x).strip() for x in scopes if str(x).strip()]
    if not isinstance(rec.get("allow_buckets"), list):
        rec["allow_buckets"] = []
    if not isinstance(rec.get("allow_prefixes"), dict):
        rec["allow_prefixes"] = {}
    rec["enabled"] = bool(rec.get("enabled", True))
    rec["expires_at"] = str(rec.get("expires_at") or "").strip()
    rec["description"] = str(rec.get("description") or "").strip()
    rec["created_by"] = str(rec.get("created_by") or "").strip()
    rec["created_at"] = str(rec.get("created_at") or "").strip()
    rec["last_used_at"] = str(rec.get("last_used_at") or "").strip()
    return rec


def _read_tokens_raw() -> dict:
    """读取 tokens.json，返回原始 token → value 字典（兼容 v1/v2）"""
    p = _tokens_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # v2：{"_version":2, "tokens":{...}}
    if "tokens" in data and isinstance(data.get("tokens"), dict):
        return data["tokens"]
    # v1：{"<token>": "用户名"}
    return {k: v for k, v in data.items() if k != "_version"}


def load_tokens() -> dict:
    """返回 {token: 记录} 映射（未知/旧格式自动归一化）"""
    return {tok: _normalize_token_record(val, tok) for tok, val in _read_tokens_raw().items()}


def _save_tokens(mapping: dict) -> None:
    """写回 tokens.json（统一 v2 结构；value 可为字符串，自动归一化）"""
    p = _tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = {}
    for tok, val in mapping.items():
        if not tok:
            continue
        normalized[tok] = _normalize_token_record(val, tok) if not isinstance(val, str) \
            else _default_token_record(val, "user")
    payload = {"_version": 2, "tokens": normalized}
    _atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2), mode=0o600)


def _parse_ts(ts: str):
    """解析 expires_at（支持 YYYY-MM-DD / ISO 8601 / 带 Z）"""
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        text = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None


def token_record_expired(rec: dict) -> bool:
    exp = _parse_ts((rec or {}).get("expires_at", ""))
    if exp is None:
        return False
    return datetime.now(timezone.utc) >= exp


def get_token_record(token: str):
    """按 token 取有效记录；不存在/被禁用/已过期返回 None（过期状态由调用方区分）"""
    token = (token or "").strip()
    if not token:
        return None
    raw = _read_tokens_raw()
    if token not in raw:
        return None
    rec = _normalize_token_record(raw[token], token)
    if not rec.get("enabled", True):
        return None
    if token_record_expired(rec):
        return None
    return rec


def touch_token(token: str) -> None:
    """记录 token 最近使用时间（尽力而为，失败不阻断；>=300s 才写一次，避免每请求落盘）"""
    token = (token or "").strip()
    if not token:
        return
    try:
        with state_lock():
            raw = _read_tokens_raw()
            if token not in raw:
                return
            rec = _normalize_token_record(raw[token], token)
            last = _parse_ts(rec.get("last_used_at", ""))
            if last is not None and (datetime.now(timezone.utc) - last).total_seconds() < 300:
                return
            rec["last_used_at"] = _utc_now_iso()
            raw[token] = rec
            _save_tokens(raw)
    except Exception:
        pass


def add_token(token: str, user: str, role: str = "", scopes=None,
              allow_buckets=None, allow_prefixes=None, expires_at: str = "",
              description: str = "", created_by: str = "",
              keep_existing_permissions: bool = False) -> dict:
    """登记/更新 token → 权限记录（一个 token 只属一个用户）。

    - token 为空时由服务端生成（secrets.token_urlsafe(32)）
    - scopes 显式传入则优先；否则用 role 预设；role 缺省为 user（兼容旧行为）
    - keep_existing_permissions=True 时（平台按旧协议只推 token+user）仅更新用户名，
      保留已有 role/scopes/资源范围，避免协作平台登记动作把精细权限重置成 role=user
    - 返回记录（含明文 token），由调用方决定是否回显
    """
    token = (token or "").strip()
    user = (user or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
    if not user:
        raise HTTPException(400, detail="user 不能为空")

    if keep_existing_permissions:
        with state_lock():
            raw0 = _read_tokens_raw()
            if token in raw0:
                rec = _normalize_token_record(raw0[token], token)
                rec["user"] = user
                raw0[token] = rec
                _save_tokens(raw0)
                out = dict(rec)
                out["token"] = token
                return out

    role = (role or "user").strip() or "user"
    if role != "custom" and _role_preset_scopes(role) is None:
        raise HTTPException(400, detail=f"未知角色：{role}")
    if scopes is None:
        # 未显式勾选：用角色预设；role 缺省为 user（兼容旧行为）
        preset = _role_preset_scopes(role)
        if preset is None:
            raise HTTPException(400, detail=f"未知角色：{role}")
        scopes = list(preset)
    if not isinstance(scopes, (list, tuple)):
        raise HTTPException(400, detail="scopes 必须是数组")
    if isinstance(scopes, (list, tuple)) and len(scopes) == 0 and role not in ("custom", ""):
        # 显式传空数组 = 明确不带任何权限；保留 role 作为标签
        pass

    from artifactdepot.permissions import is_valid_scope
    clean_scopes = []
    for sc in scopes:
        sc = str(sc).strip()
        if not sc:
            continue
        if not is_valid_scope(sc):
            raise HTTPException(400, detail=f"未知权限点：{sc}")
        if sc not in clean_scopes:
            clean_scopes.append(sc)

    if allow_buckets is None:
        allow_buckets = []
    if not isinstance(allow_buckets, list):
        raise HTTPException(400, detail="allow_buckets 必须是数组")
    allow_buckets = [str(b).strip() for b in allow_buckets if str(b).strip()]

    if allow_prefixes is None:
        allow_prefixes = {}
    if not isinstance(allow_prefixes, dict):
        raise HTTPException(400, detail="allow_prefixes 必须是对象")
    clean_prefixes = {}
    for b, prefixes in allow_prefixes.items():
        b = str(b).strip()
        if not b:
            continue
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if not isinstance(prefixes, list):
            raise HTTPException(400, detail=f"allow_prefixes[{b}] 必须是数组")
        clean_prefixes[b] = normalize_prefixes(prefixes)

    validate_scope_range(allow_buckets, clean_prefixes)
    with state_lock():
        raw = _read_tokens_raw()
        existing = _normalize_token_record(raw.get(token, {}), token) if token in raw else None
        rec = _default_token_record(user, role)
        rec["scopes"] = clean_scopes
        rec["allow_buckets"] = allow_buckets
        rec["allow_prefixes"] = clean_prefixes
        rec["expires_at"] = (expires_at or "").strip()
        rec["description"] = (description or "").strip()
        rec["created_by"] = (created_by or "").strip() or "系统/工具"
        if existing:
            rec["created_at"] = existing.get("created_at") or rec["created_at"]
            rec["last_used_at"] = existing.get("last_used_at") or ""
        raw[token] = rec
        _save_tokens(raw)
    out = dict(rec)
    out["token"] = token
    return out


def remove_token(token: str) -> dict:
    token = (token or "").strip()
    with state_lock():
        raw = _read_tokens_raw()
        if token not in raw:
            raise HTTPException(404, detail="token 不存在")
        rec = _normalize_token_record(raw.pop(token), token)
        _save_tokens(raw)
    return {"removed": token, "user": rec.get("user", "")}


def resolve_user(token: str):
    """按 token 查用户名（未登记/禁用/过期返回 None）"""
    rec = get_token_record(token)
    return rec.get("user") if rec else None


# ---------------------------------------------------------------------------
# 从 DataHub 拉取用户 token（collab 为权威源，本地为缓存副本）
# ---------------------------------------------------------------------------

_last_sync = 0.0
SYNC_INTERVAL = 60  # 秒：惰性刷新最小间隔，避免无效 token 时反复打 DataHub


async def _fetch_datahub_users(datahub_url: str):
    """从 DataHub 拉 users.json。返回 (users列表, error)；失败时 users=None、error 为原因"""
    if not datahub_url:
        return None, "未配置 datahub_url"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.get(f"{datahub_url.rstrip('/')}/users.json")
            resp.raise_for_status()
            data = resp.json()
        users = data.get("users", []) if isinstance(data, dict) else []
        return users, None
    except Exception as e:
        return None, str(e) or "DataHub 不可达"


def _merge_datahub_users(users) -> dict:
    """把 DataHub 用户合并进本地记录：只补/更新身份，绝不覆盖权限配置。

    - 已存在 token：仅更新 user 名（保留 role/scopes/资源范围/过期）
    - 新 token：按默认 role=user 建立记录（与旧行为一致）
    """
    with state_lock():
        raw = _read_tokens_raw()
        for u in users or []:
            tok = (u.get("api_token") or "").strip()
            name = (u.get("name") or "").strip()
            if not tok or not name:
                continue
            if tok in raw:
                rec = _normalize_token_record(raw[tok], tok)
                rec["user"] = name
            else:
                rec = _default_token_record(name, "user")
            raw[tok] = rec
        _save_tokens(raw)
    return load_tokens()


async def sync_tokens_from_datahub(datahub_url: str, force: bool = False) -> dict:
    """从 DataHub users.json 拉取并合并用户 token（只增不删、权限字段保留）"""
    global _last_sync
    now = time.time()
    if not force and now - _last_sync < SYNC_INTERVAL:
        return load_tokens()
    users, err = await _fetch_datahub_users(datahub_url)
    if err is not None:
        return load_tokens()
    merged = _merge_datahub_users(users)
    _last_sync = time.time()
    return merged


async def sync_tokens_detailed(datahub_url: str, force: bool = True) -> dict:
    """手动同步接口用：返回 {mapping, ok, error, datahub_url}"""
    global _last_sync
    now = time.time()
    if not force and now - _last_sync < SYNC_INTERVAL:
        return {"mapping": load_tokens(), "ok": True, "error": "", "datahub_url": datahub_url}
    users, err = await _fetch_datahub_users(datahub_url)
    if err is not None:
        return {"mapping": load_tokens(), "ok": False, "error": err, "datahub_url": datahub_url}
    merged = _merge_datahub_users(users)
    _last_sync = time.time()
    return {"mapping": merged, "ok": True, "error": "", "datahub_url": datahub_url}


# ---------------------------------------------------------------------------
# 审计日志（追加写，JSONL 一行一条）
# ---------------------------------------------------------------------------

def _audit_path() -> Path:
    return _meta_path("audit.log")


def audit(action: str, bucket: str, key: str, actor: str = "",
          ip: str = "", size: int = 0, sha256: str = "", public_ip: str = "",
          extra: dict | None = None) -> None:
    """追加一条审计记录（只增不改，防篡改靠不可变历史）

    ip：服务端看到的客户端地址（局域网内为 192.168.x.x）
    public_ip：前端上报的浏览器公网 IP（尽力而为，签名链接直连时可能为空）
    """
    rec = {
        "time": _utc_now_iso(),
        "action": action,          # upload / delete / download / presign / revoke
        "bucket": bucket or "",
        "key": key or "",
        "actor": actor or "",      # 系统/工具 或 用户名
        "ip": ip or "",
        "public_ip": public_ip or "",
        "size": size or 0,
        "sha256": sha256 or "",
    }
    if extra:
        rec.update(extra)
    p = _audit_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        _maybe_rotate_audit()
    except OSError as e:
        # 审计写入失败不应阻断业务（audit.log 属主不对 / 磁盘满等）
        logging.getLogger("artifactdepot").warning("审计日志写入失败：%s", e)


def _maybe_rotate_audit() -> None:
    """audit.log 超过 config.audit_max_mb 时轮转，保留 audit_backups 个历史文件。"""
    p = _audit_path()
    max_mb = int(get_config().get("audit_max_mb", 50) or 0)
    if max_mb <= 0:
        return
    try:
        if p.stat().st_size < max_mb * 1024 * 1024:
            return
    except OSError:
        return
    backups = max(1, int(get_config().get("audit_backups", 5) or 5))
    with state_lock():
        try:
            if p.stat().st_size < max_mb * 1024 * 1024:
                return
        except OSError:
            return
        oldest = p.with_name(f"audit.log.{backups}")
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for i in range(backups - 1, 0, -1):
            src = p.with_name(f"audit.log.{i}")
            if src.exists():
                try:
                    os.replace(src, p.with_name(f"audit.log.{i + 1}"))
                except OSError:
                    pass
        try:
            os.replace(p, p.with_name("audit.log.1"))
        except OSError:
            pass


def query_audit(bucket: str = "", key: str = "", actor: str = "",
                since: str = "", limit: int = 500, predicate=None) -> list:
    """按条件过滤审计记录（返回最近 limit 条，倒序）

    `predicate(rec) -> bool`：可选的额外过滤（在计数前应用，因此不会占用 limit 名额）。
    `/api/audit` 用它把非共享 token 限制在其 bucket/前缀资源范围内。
    """
    if limit <= 0:
        return []
    base = _audit_path()
    backups = max(0, int(get_config().get("audit_backups", 5) or 5))
    # 从最旧到最新读取：audit.log.N ... audit.log.1, audit.log
    paths = [base.with_name(f"audit.log.{i}") for i in range(backups, 0, -1)]
    paths.append(base)
    out = deque(maxlen=limit)   # 只保留最近 limit 条，避免全量读入内存
    for p in paths:
        if not p.exists():
            continue
        try:
            f = open(p, encoding="utf-8")
        except OSError:
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if bucket and rec.get("bucket") != bucket:
                    continue
                if key and rec.get("key", "") != key:
                    continue
                if actor and rec.get("actor") != actor:
                    continue
                if since and rec.get("time", "") < since:
                    continue
                if predicate is not None and not predicate(rec):
                    continue
                rec["time"] = _ensure_utc_z(rec.get("time", ""))
                out.append(rec)
    return list(out)


# ---------------------------------------------------------------------------
# 分片上传会话（多线程分片上传）
# ---------------------------------------------------------------------------

def _chunk_dir() -> Path:
    return Path(tempfile.gettempdir()) / "dw_chunks"


# upload_id 由服务端 secrets.token_urlsafe(16) 生成（22 个 [A-Za-z0-9_-] 字符）。
# 客户端回传时必须严格匹配该字符集：否则 `Path(base) / "../../x"` 或绝对路径
# （如 "/"）会逃出 /tmp/dw_chunks，abort 时 shutil.rmtree 可删除任意目录（严重漏洞）。
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_TOTAL_CHUNKS = 100000          # total_chunks 上限，避免超大 range 循环 DoS
MAX_CHUNK_BYTES = 64 * 1024 * 1024       # 单分片上限 64MB（前端分片 8MB，留足余量）
MAX_SESSION_BYTES = 200 * 1024 ** 3      # max_upload_mb=0 时的会话硬上限 200GB，防 /tmp 写满
_SESSION_META = ".session.json"


def _max_chunk_bytes() -> int:
    """单分片上限：config.upload.max_chunk_mb，同时受代码常量 MAX_CHUNK_BYTES 兜底。"""
    cfg = get_upload_settings()
    return min(cfg["max_chunk_mb"] * 1024 * 1024, MAX_CHUNK_BYTES)


def _max_total_chunks() -> int:
    """分片总数上限：config.upload.max_chunks，同时受代码常量 MAX_TOTAL_CHUNKS 兜底。"""
    return min(get_upload_settings()["max_chunks"], MAX_TOTAL_CHUNKS)


def _validate_upload_id(upload_id: str) -> str:
    uid = (upload_id or "").strip()
    if not _UPLOAD_ID_RE.match(uid):
        raise HTTPException(400, detail="非法或无效的 upload_id")
    return uid


def _chunk_session_path(upload_id: str) -> Path:
    """分片会话目录；严格校验 upload_id，并二次确认解析结果落在会话根内。"""
    uid = _validate_upload_id(upload_id)
    base = _chunk_dir().resolve()
    d = (base / uid).resolve()
    if not d.is_relative_to(base):
        raise HTTPException(400, detail="非法或无效的 upload_id")
    return d


def _chunk_part_path(upload_id: str, index: int) -> Path:
    return _chunk_session_path(upload_id) / f"chunk_{index}.part"


def _session_meta_path(upload_id: str) -> Path:
    return _chunk_session_path(upload_id) / _SESSION_META


def _write_session_meta(upload_id: str, meta: dict) -> None:
    try:
        _atomic_write_text(_session_meta_path(upload_id),
                           json.dumps(meta, ensure_ascii=False), mode=0o600)
    except OSError:
        pass


def _load_chunk_session(upload_id: str):
    """返回 (会话目录, 元数据)；目录不存在返回 (None, {})。会校验 upload_id 合法性。"""
    d = _chunk_session_path(upload_id)
    if not d.is_dir():
        return None, {}
    meta = {}
    p = d / _SESSION_META
    if p.is_file():
        try:
            val = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(val, dict):
                meta = val
        except Exception:
            meta = {}
    return d, meta


def get_chunk_session(upload_id: str) -> dict:
    """读取分片会话元数据（不存在返回 {}）。"""
    return _load_chunk_session(upload_id)[1]


def _assert_session_access(upload_id: str, actor: str = "", is_admin: bool = False):
    """校验会话存在且归属当前主体，返回 (会话目录, 元数据)。

    新会话一律绑定创建者；历史无 owner 的会话按“无主”放行（向后兼容）。
    """
    d, meta = _load_chunk_session(upload_id)
    if d is None:
        raise HTTPException(400, detail=f"无效或已失效的分片会话：{upload_id}")
    owner = (meta.get("owner") or "").strip()
    if owner and not is_admin and (actor or "").strip() != owner:
        raise HTTPException(403, detail="无权访问该分片会话")
    return d, meta


def create_chunk_session(owner: str = "") -> str:
    """创建分片上传会话，返回 upload_id；会话绑定创建者，避免被他人接管。

    顺手清理过期会话：客户端 abort 前崩溃/关页会残留 /tmp/dw_chunks/<id>，
    无 TTL 就永久堆积占满磁盘；这里把 mtime 超过 CHUNK_SESSION_TTL 的旧会话删掉。"""
    upload_id = secrets.token_urlsafe(16)
    d = _chunk_session_path(upload_id)
    d.mkdir(parents=True, exist_ok=True)
    _write_session_meta(upload_id, {
        "owner": (owner or "").strip(),
        "created_at": _utc_now_iso(),
        "received_bytes": 0,
    })
    cleanup_stale_chunk_sessions(get_upload_settings()["session_idle_ttl_seconds"])
    cleanup_expired_chunk_sessions(get_upload_settings()["session_ttl_seconds"])
    return upload_id


CHUNK_SESSION_TTL = 24 * 3600  # 分片会话绝对有效期（秒）：超过即视为放弃
CHUNK_SESSION_IDLE_TTL = 6 * 3600  # 代码兜底；实际默认取 config.upload.session_idle_ttl_seconds（24h）


def cleanup_stale_chunk_sessions(ttl_seconds=None) -> int:
    """删除过期分片会话目录（目录 mtime 超过 TTL），返回删除数

    - 默认 `CHUNK_SESSION_TTL`（24h，可由 config.upload.session_ttl_seconds 覆盖）；
    - 传 `CHUNK_SESSION_IDLE_TTL`（代码兜底 6h；实际默认取
      `config.upload.session_idle_ttl_seconds`，默认 24h 且不小于请求超时）：
      供后台任务定期清理浏览器被杀/断网后没有后续请求的会话。
    活跃上传每写一个分片都会更新会话目录 mtime，因此不会误删正在上传的会话。
    """
    if ttl_seconds is None:
        ttl_seconds = get_upload_settings()["session_ttl_seconds"]
    ttl = int(ttl_seconds)
    root = _chunk_dir()
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for d in root.iterdir():
        if not d.is_dir() or d.is_symlink():
            continue
        try:
            if now - d.stat().st_mtime < ttl:
                continue
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


def cleanup_expired_chunk_sessions(ttl_seconds=None) -> int:
    """删除超过绝对有效期的分片会话（按 .session.json 的 created_at）。

    `cleanup_stale_chunk_sessions()` 按目录 mtime 清理“多久没写入”，用于空闲兜底；
    本函数按会话创建时间清理“总寿命”，即使客户端一直有分片写入，也不能超过
    config.upload.session_ttl_seconds。两者职责不同，启动/新建/后台任务都会同时执行。
    """
    if ttl_seconds is None:
        ttl_seconds = get_upload_settings()["session_ttl_seconds"]
    ttl = int(ttl_seconds)
    root = _chunk_dir()
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for d in root.iterdir():
        if not d.is_dir() or d.is_symlink():
            continue
        meta = {}
        try:
            p = d / _SESSION_META
            if p.is_file():
                val = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(val, dict):
                    meta = val
        except Exception:
            meta = {}
        created = _parse_ts(meta.get("created_at", ""))
        try:
            created_ts = created.timestamp() if created is not None else d.stat().st_mtime
            if now - created_ts < ttl:
                continue
        except (OSError, ValueError, OverflowError):
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


def cleanup_all_chunk_sessions() -> int:
    """删除全部分片会话目录（服务启动时调用）。

    容器/进程重启时不可能还有活跃上传，上一次进程留下的
    ``/tmp/dw_chunks/<upload_id>`` 可能占几十 GB（浏览器被杀、容器重启等），
    在这里一次性释放；正常上传的成功/失败路径由 finalize/abort 负责清理。
    返回删除的会话数。
    """
    root = _chunk_dir()
    if not root.exists():
        return 0
    removed = 0
    for d in root.iterdir():
        if not d.is_dir() or d.is_symlink():
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


def _session_size_limit() -> int:
    """会话总大小上限：优先 max_upload_mb；否则 config.upload.max_session_gb（≤200GB 兜底）。"""
    max_mb = int(get_config().get("max_upload_mb", 0) or 0)
    if max_mb:
        return max_mb * 1024 * 1024
    cfg_bytes = get_upload_settings()["max_session_gb"] * 1024 ** 3
    return min(cfg_bytes, MAX_SESSION_BYTES)


def store_chunk(upload_id: str, index: int, src_path: Path,
                actor: str = "", is_admin: bool = False) -> dict:
    """保存一个分片（src_path 是已落盘临时文件）到会话目录。

    在 state_lock 内完成「配额校验 + 落盘 + 更新 received_bytes」，避免并发分片
    绕过 max_upload_mb、或把 /tmp 写满。
    """
    size = src_path.stat().st_size
    max_chunk = _max_chunk_bytes()
    if size > max_chunk:
        raise HTTPException(413,
            detail=f"单个分片超过上限 {max_chunk // (1024 * 1024)}MB")
    with state_lock():
        _d, meta = _assert_session_access(upload_id, actor, is_admin)
        if not isinstance(index, int) or index < 0 or index >= _max_total_chunks():
            raise HTTPException(400, detail=f"非法分片索引：{index}")
        part = _chunk_part_path(upload_id, index)
        old_size = part.stat().st_size if part.exists() else 0
        total = int(meta.get("received_bytes") or 0)
        limit = _session_size_limit()
        if total - old_size + size > limit:
            raise HTTPException(413,
                detail=f"超过单文件上传上限 {limit // (1024 * 1024)}MB")
        # 原子写入；失败时清理 .part.tmp，避免分片写失败留下残片
        tmp = part.with_suffix(".part.tmp")
        try:
            shutil.copyfile(src_path, tmp)
            tmp.rename(part)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        meta["received_bytes"] = total - old_size + size
        _write_session_meta(upload_id, meta)
    return {"upload_id": upload_id, "index": index, "size": size}


def _sum_chunk_sizes(upload_id: str, total_chunks: int) -> int:
    total = 0
    for i in range(total_chunks):
        p = _chunk_part_path(upload_id, i)
        if not p.exists():
            raise HTTPException(400, detail=f"缺少分片：{i}")
        total += p.stat().st_size
    return total


def _merge_chunks(upload_id: str, total_chunks: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as out:
        for i in range(total_chunks):
            part = _chunk_part_path(upload_id, i)
            with open(part, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    out.write(chunk)


def finalize_chunk_upload(upload_id: str, bucket: str, key: str, total_chunks: int,
                          source_url: str = "", uploader: str = "",
                          overwrite: bool = True,
                          actor: str = "", is_admin: bool = False) -> dict:
    """合并分片并写入仓库；成功或硬失败时清理本次会话。

    - 校验类失败（缺片 / total_chunks 越界 / 超出 max_upload_mb / 权限 / 409 冲突）
      **保留会话**，允许补齐分片或调整参数后重试/续传；
    - 进入合并、sha256、put_object 落盘阶段后的硬失败（磁盘满、I/O 异常、未知异常）
      以及成功，**清理**本次会话与合并临时文件，避免 72GB 级残留长期占 /tmp。
    """
    d, _meta = _assert_session_access(upload_id, actor, is_admin)
    max_chunks = _max_total_chunks()
    if not isinstance(total_chunks, int) or total_chunks < 1 or total_chunks > max_chunks:
        raise HTTPException(400,
            detail=f"total_chunks 必须在 1-{max_chunks} 之间")
    # 检查分片齐全
    for i in range(total_chunks):
        if not _chunk_part_path(upload_id, i).exists():
            raise HTTPException(400, detail=f"分片 {i} 缺失")
    # 容量检查
    max_mb = get_config().get("max_upload_mb", 0) or 0
    total_size = _sum_chunk_sizes(upload_id, total_chunks)
    if max_mb and total_size > max_mb * 1024 * 1024:
        raise HTTPException(413, detail=f"超过单文件上传上限 {max_mb}MB")

    merged = None
    keep_session = False
    try:
        # 合并到临时文件（失败属于硬失败，finally 会清理）
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            merged = Path(tmp.name)
        _merge_chunks(upload_id, total_chunks, merged)
        try:
            return put_object(bucket, key, merged, source_url, overwrite, uploader=uploader)
        except HTTPException as e:
            # overwrite=false 且目标已存在：可改参数重试，保留分片会话；
            # 目标本身是目录属于不可恢复冲突，清理会话（避免残留分片）。
            if e.status_code == 409 and not object_path(bucket, key).is_dir():
                keep_session = True
            raise
    finally:
        if merged is not None:
            merged.unlink(missing_ok=True)
        if not keep_session:
            shutil.rmtree(d, ignore_errors=True)


def abort_chunk_session(upload_id: str, actor: str = "", is_admin: bool = False) -> dict:
    """取消分片会话：校验 upload_id 合法性与归属后删除会话目录（幂等）。"""
    d, _ = _load_chunk_session(upload_id)   # 非法 upload_id 直接 400
    if d is None:
        return {"aborted": upload_id, "existed": False}
    _assert_session_access(upload_id, actor, is_admin)
    shutil.rmtree(d, ignore_errors=True)
    return {"aborted": upload_id, "existed": True}
