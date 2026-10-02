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
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx

from fastapi import HTTPException

from artifactdepot.config import get_config

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


def validate_bucket(bucket: str) -> str:
    """校验 bucket 名：允许中文等 Unicode，但拒绝路径分隔符/穿越/隐藏/控制字符"""
    if not bucket:
        raise HTTPException(400, detail="bucket 名不能为空")
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
    segs = k.split("/")
    if any(s in ("", "..", ".") for s in segs):
        raise HTTPException(400, detail=f"非法 key：{key!r}")
    # 隐藏点文件/目录不允许作为对象 key：避免与 .warehouse.json、.keep 等隐藏约定混淆
    if any(s.startswith(".") for s in segs):
        raise HTTPException(400, detail=f"非法 key（路径段不允许以 . 开头）：{key!r}")
    return k


def bucket_dir(bucket: str) -> Path:
    validate_bucket(bucket)
    root = get_root().resolve()
    d = (root / bucket).resolve()
    if not str(d).startswith(str(root)):
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
    if not str(p).startswith(str(root)):
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
    p = _manifest_path(bucket)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 对象操作
# ---------------------------------------------------------------------------

def put_object(bucket: str, key: str, src_path: Path, source_url: str = "",
               overwrite: bool = True, uploader: str = "") -> dict:
    """把已落盘的临时文件放入对象存储（流式拷贝，计算 SHA-256，记录元数据）"""
    validate_bucket(bucket)
    k = validate_key(key)
    dst = object_path(bucket, k)
    if dst.exists() and not overwrite:
        raise HTTPException(409, detail=f"对象已存在：{bucket}/{key}")

    dst.parent.mkdir(parents=True, exist_ok=True)
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
    manifest = _manifest_load(bucket)
    manifest[k] = meta
    _manifest_save(bucket, manifest)
    return {"bucket": bucket, "key": k, "size": size, "sha256": sha256}


def _dir_recursive_info(d: Path) -> dict:
    """递归统计目录的大小与最新修改时间（跳过隐藏文件，如 .keep 占位）。

    大小 = 所有文件（含嵌套子目录 + 当前目录）的总字节数；
    时间 = 所有文件里最新的 mtime（从最底层到最外层统一取最大）。
    """
    total_size = 0
    latest = 0.0
    for p in d.rglob("*"):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        total_size += st.st_size
        if st.st_mtime > latest:
            latest = st.st_mtime
    if latest:
        mtime = datetime.fromtimestamp(latest, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        # 空目录没有文件可统计，退回目录自身 mtime，而不是返回当前时间
        try:
            mtime = datetime.fromtimestamp(d.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            mtime = _utc_now_iso()
    return {"size": total_size, "mtime": mtime}


def list_objects(bucket: str, prefix: str = "") -> list:
    """列对象：prefix 限定时列出该目录下的直接子项；文件系统扫描为准"""
    validate_bucket(bucket)
    base = bucket_dir(bucket)
    if not base.exists():
        return []
    pdir = base
    rel_prefix = validate_key(prefix)
    if rel_prefix:
        pdir = (base / rel_prefix).resolve()
        if not str(pdir).startswith(str(base)):
            raise HTTPException(400, detail="prefix 越界")
        if not pdir.exists():
            return []
        if pdir.is_file():
            return []   # prefix 指向文件而非目录

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
            meta = manifest.get(rel, {}) or {}
            mtime = _ensure_utc_z(meta.get("mtime", ""))
            if not mtime:
                # 手工放入、没有清单记录的文件：回退到文件系统 mtime
                try:
                    mtime = datetime.fromtimestamp(
                        child.stat().st_mtime, timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                except OSError:
                    mtime = ""
            items.append({
                "name": child.name,
                "key": rel,
                "is_dir": False,
                "size": child.stat().st_size,
                "mtime": mtime,
                "sha256": meta.get("sha256", ""),
                "source_url": meta.get("source_url", ""),
                "uploader": meta.get("uploader", ""),
            })
    return items


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
        manifest = _manifest_load(bucket)
        manifest.pop(k, None)
        _manifest_save(bucket, manifest)
        p.unlink()
    return {"deleted": f"{bucket}/{k}"}


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
    if not str(dst).startswith(str(base)):
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
    """轻量探测文件或目录是否存在；不存在时 exists=false（HTTP 仍为 200）"""
    validate_bucket(bucket)
    k = validate_key(key)
    p = object_path(bucket, key)
    if not p.exists():
        return {"exists": False, "bucket": bucket, "key": k}
    if p.is_dir():
        info = _dir_recursive_info(p)
        return {"exists": True, "is_dir": True, "bucket": bucket, "key": k + "/",
                "size": info.get("size", 0), "mtime": int(p.stat().st_mtime)}
    entry = _manifest_load(bucket).get(k, {}) or {}
    return {"exists": True, "is_dir": False, "bucket": bucket, "key": k,
            "size": p.stat().st_size, "sha256": entry.get("sha256", ""),
            "mtime": int(p.stat().st_mtime)}


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
    p = _signed_links_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")


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
    """校验并消费一个链接（count 模式递减剩余次数）。返回 (ok, actor, error)"""
    links = load_signed_links()
    for e in links:
        if e.get("id") == link_id:
            if e.get("revoked"):
                return False, "", "链接已作废"
            if e.get("token") != secret:
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
    if not isinstance(scopes, list) or not scopes:
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
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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

    raw0 = _read_tokens_raw()
    if keep_existing_permissions and token in raw0:
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
        clean_prefixes[b] = [str(x).strip().strip("/") for x in prefixes if str(x).strip()]

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
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def query_audit(bucket: str = "", key: str = "", actor: str = "",
                since: str = "", limit: int = 500) -> list:
    """按条件过滤审计记录（返回最近 limit 条，倒序）"""
    if limit <= 0:
        return []
    p = _audit_path()
    if not p.exists():
        return []
    result = []
    with open(p, encoding="utf-8") as f:
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
            rec["time"] = _ensure_utc_z(rec.get("time", ""))
            result.append(rec)
    return result[-limit:]


# ---------------------------------------------------------------------------
# 分片上传会话（多线程分片上传）
# ---------------------------------------------------------------------------

def _chunk_dir() -> Path:
    return Path(tempfile.gettempdir()) / "dw_chunks"


def _chunk_session_path(upload_id: str) -> Path:
    return _chunk_dir() / upload_id


def _chunk_part_path(upload_id: str, index: int) -> Path:
    return _chunk_dir() / upload_id / f"chunk_{index}.part"


def create_chunk_session() -> str:
    """创建分片上传会话，返回 upload_id

    顺手清理过期会话：客户端 abort 前崩溃/关页会残留 /tmp/dw_chunks/<id>，
    无 TTL 就永久堆积占满磁盘；这里把 mtime 超过 CHUNK_SESSION_TTL 的旧会话删掉。"""
    upload_id = secrets.token_urlsafe(16)
    d = _chunk_dir() / upload_id
    d.mkdir(parents=True, exist_ok=True)
    cleanup_stale_chunk_sessions()
    return upload_id


CHUNK_SESSION_TTL = 24 * 3600  # 分片会话有效期（秒）：超时视为客户端已放弃


def cleanup_stale_chunk_sessions() -> int:
    """删除过期分片会话目录（目录 mtime 超过 CHUNK_SESSION_TTL），返回删除数

    惰性触发（每次 initiate 顺手执行）而非后台定时，服务小流量场景够用；
    会在新会话目录创建之后运行，绝不误删本次的会话（刚建 mtime 必然最新）。"""
    root = _chunk_dir()
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            if now - d.stat().st_mtime < CHUNK_SESSION_TTL:
                continue
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


def store_chunk(upload_id: str, index: int, src_path: Path) -> dict:
    """保存一个分片（src_path 是已落盘临时文件）到会话目录"""
    d = _chunk_session_path(upload_id)
    if not d.exists():
        raise HTTPException(400, detail=f"无效或已失效的分片会话：{upload_id}")
    if not isinstance(index, int) or index < 0 or index > 99999:
        raise HTTPException(400, detail=f"非法分片索引：{index}")
    part = _chunk_part_path(upload_id, index)
    # 原子写入
    tmp = part.with_suffix(".part.tmp")
    shutil.copyfile(src_path, tmp)
    tmp.rename(part)
    return {"upload_id": upload_id, "index": index, "size": part.stat().st_size}


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
                          overwrite: bool = True) -> dict:
    """合并分片并写入仓库"""
    if total_chunks < 1:
        raise HTTPException(400, detail="total_chunks 必须 >= 1")
    d = _chunk_session_path(upload_id)
    if not d.exists():
        raise HTTPException(400, detail=f"无效或已失效的分片会话：{upload_id}")
    # 检查分片齐全
    for i in range(total_chunks):
        if not _chunk_part_path(upload_id, i).exists():
            raise HTTPException(400, detail=f"分片 {i} 缺失")
    # 容量检查
    max_mb = get_config().get("max_upload_mb", 0) or 0
    total_size = _sum_chunk_sizes(upload_id, total_chunks)
    if max_mb and total_size > max_mb * 1024 * 1024:
        raise HTTPException(413, detail=f"超过单文件上传上限 {max_mb}MB")
    # 合并到临时文件
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        _merge_chunks(upload_id, total_chunks, Path(tmp.name))
    try:
        result = put_object(bucket, key, Path(tmp.name), source_url, overwrite, uploader=uploader)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    # 清理会话
    shutil.rmtree(d, ignore_errors=True)
    return result


def abort_chunk_session(upload_id: str) -> dict:
    d = _chunk_session_path(upload_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return {"aborted": upload_id}
