# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""S3-like 对象 API：上传 / 列表 / 下载（Range）/ 删除 / 预签名 / 分片

所有受保护接口按「权限点（scope）」鉴权（见 permissions.py / auth.py）：
- 每个接口对应一个 scope，可逐接口为 token 勾选启用；
- 管理员共享 access_token 放行全部；
- token 可限定 bucket / 路径前缀范围。
"""
import mimetypes
import secrets
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from artifactdepot import netutil, packaging, storage, zipstream
from artifactdepot.auth import (
    Principal, ensure_scope, extract_form_token, extract_token, optional_read_principal,
    require_scope, resolve_principal, resource_allowed,
)
from artifactdepot.observability import DELETES, DOWNLOADS, UPLOADS
from artifactdepot.config import get_config, public_package_settings, public_upload_settings

router = APIRouter(prefix="/api/objects", tags=["Objects"])


def _client_ip(request: Request) -> str:
    return netutil.client_ip(request)


def _public_ip(request: Request) -> str:
    # 只采信可信代理的 X-Forwarded-For，忽略客户端自报的 public_ip 参数
    return netutil.resolved_public_ip(request)


def _safe_header_filename(name: str) -> str:
    """FileResponse 的 filename 会进入 Content-Disposition；去掉 CR/LF/控制字符防 header 注入。"""
    clean = "".join(ch if (ord(ch) >= 32 and ord(ch) != 127) else "_" for ch in (name or ""))
    return clean or "download"


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """判断 If-None-Match 是否命中当前 ETag（支持 *、逗号列表、W/ 弱前缀）。"""
    if not if_none_match or not etag:
        return False
    header = if_none_match.strip()
    if header == "*":
        return True
    target = etag.strip().strip('"')
    for part in header.split(","):
        part = part.strip()
        if part.startswith("W/"):
            part = part[2:].strip()
        if part.strip().strip('"') == target:
            return True
    return False


def _range_span(range_header: str, total: int):
    """解析单区间 Range（bytes=a-b / bytes=a- / bytes=-n），返回 (start, end) 闭区间。

    分块并发下载按单区间请求；多区间（逗号列表）或非法/不可满足的区间返回 None，
    由 Starlette FileResponse 自行返回 400/416。审计只记录可解析区间的实际字节数。
    """
    if not range_header or total <= 0:
        return None
    value = range_header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if not spec or "," in spec:
        return None
    first, sep, last = spec.partition("-")
    if not sep:
        return None
    try:
        if first == "":
            suffix = int(last)
            if suffix <= 0:
                return None
            return max(0, total - suffix), total - 1
        start = int(first)
        end = int(last) if last else total - 1
        if start < 0 or end < start or start >= total:
            return None
        return start, min(end, total - 1)
    except ValueError:
        return None


# 读接口鉴权统一走 auth.optional_read_principal（fail-closed）：
# 不带 token 时按 require_read_token 决定是否公开；带 token 时必须有效、具备 scope，
# 且资源范围越界一律 403（不再降级为匿名，否则 bucket 范围会被绕过）。


@router.post("")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    bucket: str = Form(...),
    key: str = Form(...),
    token: str = Form(""),
    source_url: str = Form(""),
    note: str = Form(""),
    overwrite: bool = Form(True),
    public_ip: str = Form(""),
):
    """上传对象（PutObject）。multipart：file + bucket + key + token + source_url + note + public_ip

    source_url（来源）与 note（备注）为用户可选元数据，写入对象清单；
    来源允许填 URL 或自由文本说明。权限：object:upload；token 有 bucket/前缀范围时按资源校验。
    """
    tok = await extract_form_token(request) or token.strip()
    principal = await resolve_principal(tok)
    ensure_scope(principal, "object:upload", bucket=bucket, key=key)
    storage.touch_token(tok)

    max_mb = get_config().get("max_upload_mb", 0) or 0
    # 伴随文件本地化：直传临时文件写在目标文件父目录（隐藏名，列表会跳过），
    # 成功后 put_object(move=True) 用 os.replace 原子落盘，不再经 /tmp 中转。
    dst = storage.object_path(bucket, key)
    if dst.is_dir():
        raise HTTPException(409, detail=f"目标路径是目录，不能写入对象：{bucket}/{key}")
    # 记录本次为落盘临时文件而新建的父目录：读取失败/超限时回滚，保证失败零副作用
    created_dirs = []
    probe = dst.parent
    while not probe.exists() and probe != probe.parent:
        created_dirs.append(probe)
        probe = probe.parent
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError, OSError) as e:
        raise HTTPException(409, detail=f"父路径不是目录，不能写入对象：{bucket}/{key}") from e
    safe_name = dst.name[:80] or "upload"
    tmp_path = None
    success = False
    try:
        with tempfile.NamedTemporaryFile(
                delete=False, dir=str(dst.parent), suffix=".tmp",
                prefix=f".{safe_name}.artifactdepot-incoming-") as tmp:
            tmp_path = Path(tmp.name)
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if max_mb and size > max_mb * 1024 * 1024:
                    raise HTTPException(413, detail=f"超过单文件上传上限 {max_mb}MB")
                tmp.write(chunk)
        result = storage.put_object(bucket, key, tmp_path, source_url, overwrite,
                                    uploader=principal.actor, move=True, note=note)
        success = True
    finally:
        # 读取/超限/落盘任一阶段失败都删除临时文件（Starlette spool 由框架自行关闭）
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if not success:
            # 回滚为写临时文件而新建的空父目录（自底向上，非空则停止）
            for d in created_dirs:
                try:
                    d.rmdir()
                except OSError:
                    break

    storage.audit("upload", bucket, key, principal.actor, _client_ip(request),
                  size=result["size"], sha256=result["sha256"],
                  public_ip=_public_ip(request))
    UPLOADS.inc()
    return {"code": 0, "message": "success", "data": result}


@router.get("/list")
async def list_objects(request: Request, bucket: str, prefix: str = "",
                       limit: int = Query(0, ge=0, le=10000,
                                          description="0=不限；>0 时按 offset 分页"),
                       offset: int = Query(0, ge=0)):
    """列对象（ListObjects）。默认公开；require_read_token=true 时需 object:list。

    鉴权（**fail-closed**）：**不带 token** 时默认公开；**带 token** 时必须是有效 token
    且具备 `object:list`（缺失 401/403），并且 bucket/prefix 必须落在 token 的资源范围内，
    **越界一律 403**——不再「降级为匿名放行」，否则资源范围在公开模式下形同虚设。
    """
    principal = await optional_read_principal(request, "object:list")
    if principal is not None and not principal.is_admin:
        if not resource_allowed(principal, bucket, prefix):
            raise HTTPException(
                403, detail="权限不足：token 的资源范围不允许该 bucket / 路径")
    result = storage.list_objects(bucket, prefix, limit=limit, offset=offset)
    return {"code": 0, "message": "success",
            "data": {"bucket": bucket, "prefix": prefix, **result}}


@router.get("/head")
async def head(request: Request, response: Response, bucket: str, key: str,
               actor: Principal = Depends(require_scope("object:head"))):
    """对象元信息探测（S3 HeadObject 语义）：不存在 404；支持 ETag / If-None-Match(304)。"""
    ensure_scope(actor, "object:head", bucket=bucket, key=key)
    data = storage.head_object(bucket, key)
    if not data.get("exists"):
        raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")
    etag = storage.object_etag(bucket, key)
    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers={"ETag": etag})
    if etag:
        response.headers["ETag"] = etag
    return {"code": 0, "message": "success", "data": data}


async def _download_dir_link(request: Request, bucket: str, key: str,
                             link_id: str, tk: str):
    """目录签名链接下载：成品是 .package 里的真实文件（复制文件 / ZIP）。

    - 先用 inspect（不消费次数）校验链接，再检查成品是否就绪，避免「打包中」也扣次数；
    - 打包未完成 → 409 + Retry-After（并尝试补一次后台打包）；
    - HEAD 只探测不消耗次数；GET 成功才消费次数并写下载审计。
    """
    entry = storage.get_signed_link(link_id)
    if not entry or entry.get("kind") != "dir":
        raise HTTPException(401, detail="链接不存在")
    # 目录重命名后注册表 key 已更新；以链接注册表为准，旧分享 URL 里的 key 不再生效，
    # 这样目录改名不会让已经发出去的链接失效（链接本身是能力凭证）。
    bucket = str(entry.get("bucket") or bucket)
    key = str(entry.get("key") or key)
    ok, entry, err = storage.inspect_signed_link(link_id, tk, bucket, key)
    if not ok:
        raise HTTPException(401, detail=err)
    ready = packaging.read_ready(bucket, key, link_id)
    if ready is None:
        packaging.ensure_job(link_id, actor=entry.get("created_by") or "",
                             ip=_client_ip(request))
        raise HTTPException(409, detail="目录打包中，请稍后再试",
                            headers={"Retry-After": "2"})
    art = packaging.artifact_path(bucket, key, link_id)
    if art is None:
        raise HTTPException(409, detail="目录打包成品缺失，请重新生成链接",
                            headers={"Retry-After": "2"})
    actor = entry.get("created_by") or ""
    ip = _client_ip(request)
    public_ip = _public_ip(request)
    kind = str(ready.get("kind") or "zip")
    filename = str(ready.get("artifact") or art.name)
    media = (ready.get("media_type")
             or ("application/zip" if kind == "zip" else "application/octet-stream"))
    if request.method.upper() == "HEAD":
        storage.audit("download", bucket, key, actor=actor, ip=ip, size=0,
                      public_ip=public_ip,
                      extra={"head": True, "signed": True, "package": True,
                             "link_id": link_id})
        return FileResponse(art, media_type=media,
                            filename=_safe_header_filename(filename))
    ok, actor, err = storage.consume_signed_link(link_id, tk, bucket, key)
    if not ok:
        raise HTTPException(401, detail=err)
    size = int(ready.get("size") or 0)
    if kind == "zip":
        storage.audit("download_bulk", bucket, key, actor=actor, ip=ip, size=size,
                      public_ip=public_ip,
                      extra={"mode": "zip",
                             "files": int(ready.get("files") or 0),
                             "dirs": int(ready.get("dirs") or 0),
                             "signed": True, "package": True, "link_id": link_id})
    else:
        storage.audit("download", bucket, key, actor=actor, ip=ip, size=size,
                      public_ip=public_ip,
                      extra={"bulk": True, "mode": "file", "signed": True,
                             "package": True, "link_id": link_id})
    DOWNLOADS.inc()
    return FileResponse(art, media_type=media, filename=_safe_header_filename(filename))


@router.head("/download", include_in_schema=False)
@router.get("/download")
async def download(
    request: Request,
    bucket: str,
    key: str,
    expires: str = Query(None, description="旧式 HMAC 签名过期时间戳（兼容）"),
    sig: str = Query(None, description="旧式 HMAC 签名（兼容）"),
    link: str = Query(None, description="签名链接 ID（注册表）"),
    tk: str = Query(None, description="签名链接密钥"),
    token: str = Query("", description="访问令牌（非签名链接时必填）"),
):
    """下载对象（GetObject）。签名链接（link+tk / expires+sig）免 token；否则需 object:download。

    鉴权优先级：
    1. link+tk（注册表签名链接）→ 校验次数/过期/作废，免 token；
       - kind=dir：返回后台打包好的成品（唯一文件直下 / ZIP，支持 HEAD/Range）；
         打包未完成时 409，不消耗次数；
    2. expires+sig（旧式 HMAC）→ 校验签名，免 token（仅文件）；
    3. 否则 → token 必须含 object:download 且资源范围允许
    """
    p = storage.object_path(bucket, key)

    if link is not None or tk is not None:
        entry = storage.get_signed_link(link or "")
        if entry and entry.get("kind") == "dir":
            return await _download_dir_link(request, bucket, key, link or "", tk or "")
        if not p.is_file():
            raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")
        ok, actor, err = storage.consume_signed_link(link or "", tk or "", bucket, key)
        if not ok:
            raise HTTPException(401, detail=err)
    elif expires is not None or sig is not None:
        if not storage.verify_signature(bucket, key, expires or "", sig or ""):
            raise HTTPException(401, detail="签名无效或已过期")
        actor = "signed-link"
    else:
        tok = token or extract_token(request)
        principal = await resolve_principal(tok)
        ensure_scope(principal, "object:download", bucket=bucket, key=key)
        actor = principal.actor
        storage.touch_token(tok)

    if not p.is_file():
        raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")

    # ETag / 条件请求：命中 If-None-Match 直接 304，不再传输文件体
    etag = storage.object_etag(bucket, key)
    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        extra = {"not_modified": True}
        if request.method.upper() == "HEAD":
            extra["head"] = True
        storage.audit("download", bucket, key, actor=actor, ip=_client_ip(request),
                      size=0, public_ip=_public_ip(request), extra=extra)
        return Response(status_code=304, headers={"ETag": etag})

    try:
        total = p.stat().st_size
    except OSError:
        raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")
    ip = _client_ip(request)
    public_ip = _public_ip(request)
    range_header = request.headers.get("range", "").strip()

    if request.method.upper() == "HEAD":
        # 分块下载的元信息探测：只返回头（Content-Length/Accept-Ranges/ETag），不算一次下载
        storage.audit("download", bucket, key, actor=actor, ip=ip, size=0,
                      public_ip=public_ip, extra={"head": True})
    elif range_header:
        # 分块并发下载：每个 Range 请求写一条审计，size 记实际区间字节数（不再记整文件大小）
        span = _range_span(range_header, total)
        sent = (span[1] - span[0] + 1) if span else 0
        storage.audit("download", bucket, key, actor=actor, ip=ip, size=sent,
                      public_ip=public_ip,
                      extra={"partial": True, "range": range_header})
        if span is not None:
            DOWNLOADS.inc()
    else:
        storage.audit("download", bucket, key, actor=actor, ip=ip, size=total,
                      public_ip=public_ip)
        DOWNLOADS.inc()

    media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media, filename=_safe_header_filename(p.name),
                        headers={"ETag": etag} if etag else {})


@router.get("/download-bulk")
async def download_bulk(request: Request, bucket: str, prefix: str = "",
                        actor: Principal = Depends(require_scope("object:download"))):
    """一键下载当前目录（DownloadBulk）。需要 object:download，沿用资源范围校验。

    规则（契约 §1.2）：
    - 目录里只有 1 个可见文件且没有可见子目录 → FileResponse 直下（原文件名、支持 Range）；
    - 其他非空情况 → 流式 ZIP（ZIP_STORED/ZIP64、无 Content-Length、不落临时文件）；
    - 空目录 / 不存在 / 不是目录 → 404；prefix 解析到 bucket 外 → 400。
    """
    ensure_scope(actor, "object:download", bucket=bucket, key=prefix)
    scan = storage.bulk_download_scan(bucket, prefix)
    ip = _client_ip(request)
    public_ip = _public_ip(request)

    single = scan["single_file"]
    if single is not None:
        key = scan["single_file_key"]
        p = single["path"]
        etag = storage.object_etag(bucket, key)
        if _etag_matches(request.headers.get("if-none-match", ""), etag):
            storage.audit("download", bucket, key, actor.actor, ip, size=0,
                          public_ip=public_ip,
                          extra={"bulk": True, "mode": "file", "not_modified": True})
            return Response(status_code=304, headers={"ETag": etag} if etag else {})
        storage.audit("download", bucket, key, actor.actor, ip,
                      size=single["size"], public_ip=public_ip,
                      extra={"bulk": True, "mode": "file"})
        DOWNLOADS.inc()
        media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return FileResponse(p, media_type=media, filename=_safe_header_filename(p.name),
                            headers={"ETag": etag} if etag else {})

    # ZIP：审计在流结束（含客户端中断）时写，size 为实际发送字节数
    def _audit_zip(sent: int, files: int, dirs: int, aborted: bool) -> None:
        storage.audit("download_bulk", bucket, scan["prefix"], actor.actor, ip,
                      size=sent, public_ip=public_ip,
                      extra={"mode": "zip", "files": files,
                             "dirs": dirs, "aborted": aborted})

    DOWNLOADS.inc()
    headers = {"Content-Disposition": zipstream.content_disposition(scan["root_name"])}
    return StreamingResponse(
        zipstream.zip_stream(scan["entries"], scan["root_name"], _audit_zip),
        media_type="application/zip", headers=headers)


@router.delete("")
async def delete(bucket: str, key: str,
                 request: Request, actor: Principal = Depends(require_scope("object:delete"))):
    """删除对象或目录（DeleteObject）。需要 object:delete 权限"""
    ensure_scope(actor, "object:delete", bucket=bucket, key=key)
    result = storage.delete_object(bucket, key)
    storage.audit("delete", bucket, key, actor.actor, _client_ip(request), public_ip=_public_ip(request))
    DELETES.inc()
    return {"code": 0, "message": "success", "data": result}


class BulkDeleteRequest(BaseModel):
    """批量删除目录内容（一键清空）请求体，字段见契约 §1.2。"""
    bucket: str
    prefix: str = ""
    mode: str                       # files = 只删直接文件；all = 递归删文件+子目录
    dry_run: bool = False
    confirm: str = ""               # 执行时必填：prefix 最后一段（根目录为 bucket 名）


@router.post("/bulk-delete")
async def bulk_delete(body: BulkDeleteRequest, request: Request,
                      actor: Principal = Depends(require_scope("object:delete_bulk"))):
    """批量删除当前目录内容（一键清空）。需要 object:delete_bulk。

    - `dry_run=true` 只预览统计，不落盘、不写审计；
    - 执行（`dry_run=false`）必须在服务端二次校验 `confirm`（目录名 / 根目录为 bucket 名）；
    - 只清理 prefix 目录的**内容**，prefix 目录本身保留；bucket 根 `.warehouse.json` 永不删除。
    """
    # 资源范围先校验（越界 403），再做参数/确认校验
    ensure_scope(actor, "object:delete_bulk", bucket=body.bucket, key=body.prefix)
    prefix = storage.validate_key(body.prefix)
    if body.mode not in ("files", "all"):
        raise HTTPException(400, detail=f"非法 mode：{body.mode!r}（仅支持 files / all）")
    expected = prefix.split("/")[-1] if prefix else body.bucket
    if not body.dry_run and body.confirm.strip() != expected:
        raise HTTPException(400, detail=f"确认名称不匹配：请输入「{expected}」后再执行")

    started = time.perf_counter()
    result = storage.bulk_delete(body.bucket, prefix, body.mode, body.dry_run)
    elapsed_ms = max(0, int(round((time.perf_counter() - started) * 1000)))

    if not body.dry_run:
        storage.audit("delete_bulk", result["bucket"], result["prefix"],
                      actor.actor, _client_ip(request), size=result["bytes"],
                      public_ip=_public_ip(request),
                      extra={"mode": result["mode"], "files": result["files"],
                             "dirs": result["dirs"]})
        DELETES.inc()
    data = {**result, "elapsed_ms": elapsed_ms}
    return {"code": 0, "message": "success", "data": data}


class MkdirRequest(BaseModel):
    bucket: str
    key: str


@router.post("/mkdir")
async def mkdir(body: MkdirRequest, request: Request,
                actor: Principal = Depends(require_scope("object:mkdir"))):
    """新建目录：按 key 创建目录树 + 隐藏 .keep 占位"""
    ensure_scope(actor, "object:mkdir", bucket=body.bucket, key=body.key)
    result = storage.mkdir(body.bucket, body.key)
    storage.audit("mkdir", body.bucket, body.key, actor.actor, _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success", "data": result}


class RenameRequest(BaseModel):
    bucket: str
    key: str
    new_key: str
    overwrite: bool = False


@router.post("/rename")
async def rename(body: RenameRequest, request: Request,
                 actor: Principal = Depends(require_scope("object:rename"))):
    """重命名/移动目录或对象：整体迁移前缀下对象与 .keep 占位，失败回滚"""
    ensure_scope(actor, "object:rename", bucket=body.bucket, key=body.key)
    ensure_scope(actor, "object:rename", bucket=body.bucket, key=body.new_key)
    result = storage.rename_object(body.bucket, body.key, body.new_key, body.overwrite)
    storage.audit("rename", body.bucket, body.key, actor.actor, _client_ip(request),
                  public_ip=_public_ip(request),
                  extra={"from_key": result["from_key"], "to_key": result["to_key"]})
    return {"code": 0, "message": "success", "data": result}


class PresignRequest(BaseModel):
    """生成签名链接请求（文件链接 / 目录打包链接）。"""
    bucket: str
    key: str
    kind: str = "file"      # file=普通文件链接；dir=目录打包链接（后台打包，见 packaging.py）
    mode: str = "time"      # count(1-10次) / time(默认1小时) / permanent(永久)
    count: int = 1          # mode=count 时次数（1-10）
    expires: int = 3600     # mode=time 时秒数（默认 1 小时）


@router.post("/presign")
async def presign(body: PresignRequest,
                  request: Request, actor: Principal = Depends(require_scope("link:create"))):
    """生成签名下载链接。需要 link:create；资源范围允许的 bucket/key 才能签。

    - kind=file（默认）：返回文件下载链接（行为与历史一致）；
    - kind=dir：先校验目录存在，立即创建链接并启动后台打包；响应带 package 进度快照，
      可通过 GET /api/objects/signed-links/{id}/package 轮询状态。
    """
    ensure_scope(actor, "link:create", bucket=body.bucket, key=body.key)
    kind = (body.kind or "file").strip().lower()
    if kind not in ("file", "dir"):
        raise HTTPException(400, detail="kind 必须为 file / dir")
    if kind == "dir":
        pub = public_package_settings()
        if not pub["allow_directory"]:
            raise HTTPException(403, detail="目录签名链接已关闭（signed_links.allow_directory=false）")
        if not pub["enabled"]:
            raise HTTPException(403, detail="目录打包已关闭（package.enabled=false）")
        dir_key = storage.validate_key(body.key)
        if not dir_key or not storage.object_path(body.bucket, dir_key).is_dir():
            raise HTTPException(404, detail=f"目录不存在：{body.bucket}/{body.key}")
    entry = storage.create_signed_link(body.bucket, body.key, body.mode,
                                       count=body.count, expires=body.expires,
                                       created_by=actor.actor, kind=kind)
    url = (f"/api/objects/download?bucket={entry['bucket']}&key={entry['key']}"
           f"&link={entry['id']}&tk={entry['token']}")
    storage.audit("presign", entry["bucket"], entry["key"], actor.actor, _client_ip(request), public_ip=_public_ip(request))
    data = {"url": url, "id": entry["id"], "mode": entry["mode"],
            "kind": kind,
            "max_uses": entry["max_uses"], "remaining": entry["remaining"],
            "expires": entry["expires"]}
    if kind == "dir":
        # 链接已创建并返回；打包在后台继续，前端轮询 package 状态显示进度
        data["package"] = packaging.start_job(entry["id"], actor=actor.actor,
                                              ip=_client_ip(request))
    return {"code": 0, "message": "success", "data": data}


def _dir_link_for_actor(link_id: str, actor: Principal) -> dict:
    """加载目录链接并做「存在 / 类型 / 权限 / 创建者」校验（状态与取消接口共用）。"""
    entry = storage.get_signed_link(link_id)
    if not entry:
        raise HTTPException(404, detail="链接不存在")
    ensure_scope(actor, "link:create", bucket=entry.get("bucket", ""),
                 key=entry.get("key", ""))
    if entry.get("kind") != "dir":
        raise HTTPException(400, detail="该链接不是目录打包链接")
    if not actor.is_admin and entry.get("created_by") != actor.actor:
        raise HTTPException(403, detail="只能查看/操作自己创建的链接")
    return entry


@router.get("/signed-links/{link_id}/package", include_in_schema=False)
async def package_status(link_id: str, request: Request,
                         actor: Principal = Depends(require_scope("link:create"))):
    """目录打包进度（网页内部轮询；与 link:create 同权限，创建者/管理员可见）。"""
    entry = _dir_link_for_actor(link_id, actor)
    if packaging.link_valid(entry) and packaging.read_ready(
            entry.get("bucket", ""), entry.get("key", ""), link_id) is None:
        # 服务重启/任务丢失后自动补一次打包
        packaging.ensure_job(link_id, actor=actor.actor, ip=_client_ip(request))
    return {"code": 0, "message": "success", "data": packaging.job_status(link_id)}


@router.post("/signed-links/{link_id}/package/cancel", include_in_schema=False)
async def package_cancel(link_id: str, request: Request,
                         actor: Principal = Depends(require_scope("link:create"))):
    """取消目录打包：链接自动作废、半成品删除（网页内部接口）。"""
    entry = _dir_link_for_actor(link_id, actor)
    data = packaging.cancel_job(link_id, actor=actor.actor, ip=_client_ip(request))
    storage.audit("package", entry.get("bucket", ""), entry.get("key", ""),
                  actor.actor, _client_ip(request),
                  public_ip=_public_ip(request),
                  extra={"phase": "cancel_request", "link_id": link_id})
    return {"code": 0, "message": "success", "data": data}


@router.get("/signed-links/config")
async def signed_links_config():
    """返回签名链接次数/时效上下限 + 目录打包能力，供前端渲染校验与提示。"""
    cfg = get_config().get("signed_links", {})
    return {"code": 0, "message": "success", "data": {
        "count_min": int(cfg.get("count_min", 1) or 1),
        "count_max": int(cfg.get("count_max", 10) or 10),
        "expire_min_seconds": int(cfg.get("expire_min_seconds", 60) or 60),
        "expire_max_seconds": int(cfg.get("expire_max_seconds", 604800) or 604800),
        "package": public_package_settings(),
    }}


@router.get("/signed-links")
async def list_signed_links(request: Request,
                            actor: Principal = Depends(require_scope("link:list"))):
    """列签名链接：有 link:list 权限可见；完整链接 URL 仅管理员/创建者可看"""
    links = storage.load_signed_links()
    out = []
    for l in links:
        if not actor.is_admin and not _link_in_scope(actor, l):
            continue
        l = dict(l)
        is_mine = actor.is_admin or l.get("created_by") == actor.actor
        if is_mine:
            l["url"] = (f"/api/objects/download?bucket={l.get('bucket','')}"
                        f"&key={l.get('key','')}&link={l.get('id','')}&tk={l.get('token','')}")
        else:
            l["url"] = ""
        l.pop("token", None)
        l["kind"] = l.get("kind") or "file"
        l["package_state"] = packaging.link_package_state(l)
        out.append(l)
    return {"code": 0, "message": "success", "data": out}


def _link_in_scope(principal: Principal, link: dict) -> bool:
    from artifactdepot.auth import resource_allowed
    return resource_allowed(principal, link.get("bucket", ""), link.get("key", ""))


@router.post("/signed-links/{link_id}/revoke")
async def revoke_link(link_id: str, request: Request,
                      actor: Principal = Depends(require_scope("link:revoke"))):
    """作废签名链接：管理员可作废任意；用户只能作废自己创建的"""
    entry = storage.get_signed_link(link_id)
    if not entry:
        raise HTTPException(404, detail="链接不存在")
    ensure_scope(actor, "link:revoke", bucket=entry.get("bucket", ""), key=entry.get("key", ""))
    if not actor.is_admin and entry.get("created_by") != actor.actor:
        raise HTTPException(403, detail="只能作废自己创建的链接")
    storage.revoke_signed_link(link_id)
    # 目录链接：作废后按配置立即删包/打断后台打包（失败不影响作废本身）
    try:
        packaging.on_revoke(link_id)
    except Exception:                              # noqa: BLE001 - 清理失败不影响作废
        pass
    storage.audit("revoke", entry.get("bucket", ""), entry.get("key", ""), actor.actor,
                  _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success"}


# ---------------------------------------------------------------------------
# 多线程分片上传
# ---------------------------------------------------------------------------

CHUNK_SIZE = 8 * 1024 * 1024  # 兼容旧调用；实际值以 config.upload.chunk_size_mb 为准


def _initiate_bool(raw) -> bool:
    return str(raw if raw is not None else "").strip().lower() in ("1", "true", "yes", "on")


async def _initiate_params(request: Request) -> dict:
    """initiate 参数同时支持 query 与 form（multipart / urlencoded），允许空 body。

    旧客户端 `POST /api/objects/initiate`（无 body）必须继续可用；新客户端既能放
    query，也能放 FormData 字段。同名时以表单值优先。
    """
    params = {k: str(v) for k, v in request.query_params.items()}
    ctype = (request.headers.get("content-type") or "").lower()
    if (ctype.startswith("multipart/form-data")
            or ctype.startswith("application/x-www-form-urlencoded")):
        try:
            form = await request.form()
        except Exception:
            return params
        for k, v in form.items():
            if isinstance(v, str):
                params[k] = v
    return params


def _parse_optional_int(raw, field: str):
    text = str(raw).strip() if raw is not None else ""
    if text == "":
        return None
    try:
        val = int(text)
    except (TypeError, ValueError):
        raise HTTPException(400, detail=f"非法 {field}：{text!r}（必须是整数）")
    if val < 0:
        raise HTTPException(400, detail=f"非法 {field}：{val}（不能为负数）")
    return val


@router.post("/initiate")
async def initiate_upload(request: Request,
                          actor: Principal = Depends(require_scope("upload:initiate"))):
    """创建/恢复分片上传会话，返回 upload_id、上传配置与续传信息。

    参数（query 与 multipart form 均可，允许空 body 向后兼容）：
      `bucket`/`key`（提供时会话落在目标文件父目录 `.artifactdepot-uploads/<id>/`，
      并追加资源范围校验）、`size`（总字节数）、`resume`（默认 false）、
      `upload_id`（resume=true 时指定复用）。

    续传信息（`resumed`/`uploaded`/`received_bytes`/`total_chunks`/`size`/
    `chunk_size`/`staging`）以服务端磁盘为准；会话失效返回 400 且 detail 含
    「分片会话」，客户端按会话丢失处理。**不提供** GET upload/{id}（不新增权限点）。
    """
    params = await _initiate_params(request)
    bucket = (params.get("bucket") or "").strip()
    key = (params.get("key") or "").strip()
    upload_id = (params.get("upload_id") or "").strip()
    size = _parse_optional_int(params.get("size"), "size")
    resume = _initiate_bool(params.get("resume"))
    if bool(bucket) != bool(key):
        raise HTTPException(400, detail="bucket 与 key 必须同时提供")
    if bucket:
        ensure_scope(actor, "upload:initiate", bucket=bucket, key=key)

    resumed = False
    info = None
    if resume:
        if upload_id:
            info = storage.validate_resume_session(
                upload_id, owner=actor.actor, bucket=bucket, key=key,
                size=size or 0, is_admin=actor.is_admin)
            resumed = True
        elif bucket:
            found = storage.find_resumable_session(
                actor.actor, bucket, key, size or 0, actor.is_admin)
            if found:
                info = storage.session_info(found)
                resumed = True
    if info is None:
        new_id = storage.create_chunk_session(
            owner=actor.actor, bucket=bucket, key=key, size=size or 0)
        info = storage.session_info(new_id)

    # 前端/CLI 以服务端返回为准：分片大小、并发、请求超时（默认 24h）都可从 config.json 调整
    return {"code": 0, "message": "success", "data": {
        "upload_id": info["upload_id"],
        **public_upload_settings(),
        "chunk_size": info["chunk_size"],
        "resumed": resumed,
        "uploaded": info["uploaded"],
        "received_bytes": info["received_bytes"],
        "total_chunks": info["total_chunks"],
        "size": info["size"],
        "staging": info["staging"],
    }}


@router.post("/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str = Form(...),
    index: int = Form(...),
    chunk: UploadFile = File(...),
    token: str = Form(""),
):
    """上传一个分片（multipart：upload_id + index + chunk 文件）。权限 upload:chunk

    请求体临时文件写在会话目录 `.incoming/<rand>.tmp`（与分片同文件系统，随后
    `store_chunk` 直接 os.replace，省一次拷贝；不再落 /tmp）。
    """
    tok = await extract_form_token(request) or token.strip()
    principal = await resolve_principal(tok)
    ensure_scope(principal, "upload:chunk")
    storage.touch_token(tok)
    # 先校验会话存在/归属，再读请求体；临时文件落在会话 .incoming（失败清理）
    incoming = storage.open_session_incoming(
        upload_id, actor=principal.actor, is_admin=principal.is_admin)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp",
                                         dir=str(incoming)) as tmp:
            tmp_path = Path(tmp.name)
            while part := await chunk.read(1024 * 1024):
                tmp.write(part)
        result = storage.store_chunk(upload_id, index, tmp_path,
                                      actor=principal.actor, is_admin=principal.is_admin)
    finally:
        # 读取失败 / 配额失败 / 落盘失败都删除本次请求的临时文件
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        try:
            incoming.rmdir()          # 无并发分片时顺手清掉空 .incoming（非空失败忽略）
        except OSError:
            pass
    return {"code": 0, "message": "success", "data": result}


@router.post("/complete")
async def complete_upload(
    request: Request,
    upload_id: str = Form(...),
    bucket: str = Form(...),
    key: str = Form(...),
    total_chunks: int = Form(...),
    source_url: str = Form(""),
    note: str = Form(""),
    overwrite: bool = Form(True),
    public_ip: str = Form(""),
    token: str = Form(""),
):
    """合并分片并完成上传。权限 upload:complete，并校验 bucket/key 资源范围

    source_url（来源，允许自由文本）与 note（备注）随合并写入对象元数据。
    """
    tok = await extract_form_token(request) or token.strip()
    principal = await resolve_principal(tok)
    ensure_scope(principal, "upload:complete", bucket=bucket, key=key)
    storage.touch_token(tok)
    result = storage.finalize_chunk_upload(
        upload_id, bucket, key, total_chunks,
        source_url, uploader=principal.actor, overwrite=overwrite,
        actor=principal.actor, is_admin=principal.is_admin, note=note,
    )
    storage.audit("upload", bucket, key, principal.actor, _client_ip(request),
                  size=result["size"], sha256=result["sha256"],
                  public_ip=_public_ip(request))
    UPLOADS.inc()
    return {"code": 0, "message": "success", "data": result}


@router.get("/uploads", include_in_schema=False)
async def list_pending_uploads(
        actor: Principal = Depends(require_scope("upload:abort"))):
    """列出**当前令牌**名下尚未完成的分片上传会话（网页「未完成上传」用）。

    - 只返回 `actor.actor` 创建的会话，不跨 token（同一 token 同一身份才可见）；
    - 权限复用 `upload:abort`（能取消会话的令牌才能看到并清理自己的半成品），
      不新增权限点；该端点供网页 UI 内部使用，不进 OpenAPI，保持对外操作数不变；
    - 数据来自磁盘会话，页面刷新 / 服务重启后仍可显示；只返回相对信息，不暴露绝对路径。
    """
    items = storage.list_owner_sessions(actor.actor)
    return {"code": 0, "message": "success", "data": {"items": items, "count": len(items)}}


@router.post("/abort")
async def abort_upload(
    request: Request,
    upload_id: str = Form(...),
    token: str = Form(""),
):
    """取消分片上传并清理临时分片。权限 upload:abort"""
    tok = await extract_form_token(request) or token.strip()
    principal = await resolve_principal(tok)
    ensure_scope(principal, "upload:abort")
    storage.touch_token(tok)
    result = storage.abort_chunk_session(
        upload_id, actor=principal.actor, is_admin=principal.is_admin)
    return {"code": 0, "message": "success", "data": result}
