"""S3-like 对象 API：上传 / 列表 / 下载（Range）/ 删除 / 预签名 / 分片

所有受保护接口按「权限点（scope）」鉴权（见 permissions.py / auth.py）：
- 每个接口对应一个 scope，可逐接口为 token 勾选启用；
- 管理员共享 access_token 放行全部；
- token 可限定 bucket / 路径前缀范围。
"""
import mimetypes
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from artifactdepot import storage
from artifactdepot.auth import (
    Principal, ensure_scope, extract_form_token, extract_token, require_principal,
    require_scope, resolve_principal,
)
from artifactdepot.config import get_config

router = APIRouter(prefix="/api/objects", tags=["Objects"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _public_ip(request: Request) -> str:
    return (request.query_params.get("public_ip") or "").strip()


def _read_requires_token() -> bool:
    """读接口是否要求带 token（默认 false，保持历史公开行为）"""
    return bool(get_config().get("require_read_token", False))


async def _optional_read_principal(request: Request, scope: str):
    """读接口鉴权：require_read_token=true 时强制 scope；否则有 token 且具备 scope 才返回主体"""
    token = extract_token(request)
    principal = await resolve_principal(token) if token else None
    if _read_requires_token():
        if not principal:
            raise HTTPException(401, detail="缺少或无效的访问令牌")
        ensure_scope(principal, scope)
        return principal
    if principal is None:
        return None
    if not principal.is_admin and not principal.has_scope(scope):
        return None
    return principal


@router.post("")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    bucket: str = Form(...),
    key: str = Form(...),
    token: str = Form(""),
    source_url: str = Form(""),
    overwrite: bool = Form(True),
    public_ip: str = Form(""),
):
    """上传对象（PutObject）。multipart：file + bucket + key + token + source_url + public_ip

    权限：object:upload；token 有 bucket/前缀范围时按资源校验。
    """
    tok = await extract_form_token(request) or token.strip()
    principal = await resolve_principal(tok)
    ensure_scope(principal, "object:upload", bucket=bucket, key=key)
    storage.touch_token(tok)

    max_mb = get_config().get("max_upload_mb", 0) or 0
    suffix = Path(file.filename or "").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if max_mb and size > max_mb * 1024 * 1024:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(413, detail=f"超过单文件上传上限 {max_mb}MB")
            tmp.write(chunk)
        tmp_path = Path(tmp.name)

    try:
        result = storage.put_object(bucket, key, tmp_path, source_url, overwrite, uploader=principal.actor)
    except HTTPException:
        raise
    finally:
        tmp_path.unlink(missing_ok=True)

    storage.audit("upload", bucket, key, principal.actor, _client_ip(request),
                  size=result["size"], sha256=result["sha256"],
                  public_ip=public_ip or _public_ip(request))
    return {"code": 0, "message": "success", "data": result}


@router.get("/list")
async def list_objects(request: Request, bucket: str, prefix: str = ""):
    """列对象（ListObjects）。默认公开；require_read_token=true 时需 object:list。

    带资源范围的 token：在允许范围内正常列出；范围外按匿名处理（公开模式下不阻断、
    也不泄露额外信息）。require_read_token=true 时 `_optional_read_principal` 已强制校验，
    且资源范围越界必须返回 403（否则会绕过范围约束、泄露白名单外 bucket 的内容）。
    """
    principal = await _optional_read_principal(request, "object:list")
    if principal is not None and not principal.is_admin:
        from artifactdepot.auth import resource_allowed
        if not resource_allowed(principal, bucket, prefix):
            if _read_requires_token():
                raise HTTPException(
                    403, detail="权限不足：token 的资源范围不允许该 bucket / 路径")
            principal = None
    items = storage.list_objects(bucket, prefix)
    return {"code": 0, "message": "success",
            "data": {"bucket": bucket, "prefix": prefix, "items": items}}


@router.get("/head")
async def head(request: Request, bucket: str, key: str,
               actor: Principal = Depends(require_scope("object:head"))):
    """对象元信息探测：轻量判断文件/目录是否存在；不存在时 HTTP 仍为 200，exists=false"""
    ensure_scope(actor, "object:head", bucket=bucket, key=key)
    return {"code": 0, "message": "success", "data": storage.head_object(bucket, key)}


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
    1. link+tk（注册表签名链接）→ 校验次数/过期/作废，免 token
    2. expires+sig（旧式 HMAC）→ 校验签名，免 token
    3. 否则 → token 必须含 object:download 且资源范围允许
    """
    p = storage.object_path(bucket, key)

    if link is not None or tk is not None:
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

    storage.audit("download", bucket, key,
                  actor=actor, ip=_client_ip(request),
                  size=p.stat().st_size, public_ip=_public_ip(request))
    media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media, filename=p.name)


@router.delete("")
async def delete(bucket: str, key: str,
                 request: Request, actor: Principal = Depends(require_scope("object:delete"))):
    """删除对象或目录（DeleteObject）。需要 object:delete 权限"""
    ensure_scope(actor, "object:delete", bucket=bucket, key=key)
    result = storage.delete_object(bucket, key)
    storage.audit("delete", bucket, key, actor.actor, _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success", "data": result}


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
    bucket: str
    key: str
    mode: str = "time"      # count(1-10次) / time(默认1小时) / permanent(永久)
    count: int = 1          # mode=count 时次数（1-10）
    expires: int = 3600     # mode=time 时秒数（默认 1 小时）


@router.post("/presign")
async def presign(body: PresignRequest,
                  request: Request, actor: Principal = Depends(require_scope("link:create"))):
    """生成签名下载链接。需要 link:create；资源范围允许的 bucket/key 才能签"""
    ensure_scope(actor, "link:create", bucket=body.bucket, key=body.key)
    entry = storage.create_signed_link(body.bucket, body.key, body.mode,
                                       count=body.count, expires=body.expires,
                                       created_by=actor.actor)
    url = (f"/api/objects/download?bucket={entry['bucket']}&key={entry['key']}"
           f"&link={entry['id']}&tk={entry['token']}")
    storage.audit("presign", entry["bucket"], entry["key"], actor.actor, _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success",
            "data": {"url": url, "id": entry["id"], "mode": entry["mode"],
                     "max_uses": entry["max_uses"], "remaining": entry["remaining"],
                     "expires": entry["expires"]}}


@router.get("/signed-links/config")
async def signed_links_config():
    """返回签名链接次数/时效的上下限（config.signed_links），供前端渲染校验"""
    cfg = get_config().get("signed_links", {})
    return {"code": 0, "message": "success", "data": {
        "count_min": int(cfg.get("count_min", 1) or 1),
        "count_max": int(cfg.get("count_max", 10) or 10),
        "expire_min_seconds": int(cfg.get("expire_min_seconds", 60) or 60),
        "expire_max_seconds": int(cfg.get("expire_max_seconds", 604800) or 604800),
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
    storage.audit("revoke", entry.get("bucket", ""), entry.get("key", ""), actor.actor,
                  _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success"}


# ---------------------------------------------------------------------------
# 多线程分片上传
# ---------------------------------------------------------------------------

CHUNK_SIZE = 8 * 1024 * 1024


@router.post("/initiate")
async def initiate_upload(request: Request,
                          actor: Principal = Depends(require_scope("upload:initiate"))):
    """创建分片上传会话，返回 upload_id 与 chunk_size"""
    upload_id = storage.create_chunk_session()
    return {"code": 0, "message": "success",
            "data": {"upload_id": upload_id, "chunk_size": CHUNK_SIZE}}


@router.post("/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str = Form(...),
    index: int = Form(...),
    chunk: UploadFile = File(...),
    token: str = Form(""),
):
    """上传一个分片（multipart：upload_id + index + chunk 文件）。权限 upload:chunk"""
    tok = await extract_form_token(request) or token.strip()
    principal = await resolve_principal(tok)
    ensure_scope(principal, "upload:chunk")
    storage.touch_token(tok)
    suffix = Path(chunk.filename or "").suffix or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        while part := await chunk.read(1024 * 1024):
            tmp.write(part)
        tmp_path = Path(tmp.name)
    try:
        result = storage.store_chunk(upload_id, index, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"code": 0, "message": "success", "data": result}


@router.post("/complete")
async def complete_upload(
    request: Request,
    upload_id: str = Form(...),
    bucket: str = Form(...),
    key: str = Form(...),
    total_chunks: int = Form(...),
    source_url: str = Form(""),
    overwrite: bool = Form(True),
    public_ip: str = Form(""),
    token: str = Form(""),
):
    """合并分片并完成上传。权限 upload:complete，并校验 bucket/key 资源范围"""
    tok = await extract_form_token(request) or token.strip()
    principal = await resolve_principal(tok)
    ensure_scope(principal, "upload:complete", bucket=bucket, key=key)
    storage.touch_token(tok)
    result = storage.finalize_chunk_upload(
        upload_id, bucket, key, total_chunks,
        source_url, uploader=principal.actor, overwrite=overwrite,
    )
    storage.audit("upload", bucket, key, principal.actor, _client_ip(request),
                  size=result["size"], sha256=result["sha256"],
                  public_ip=public_ip or _public_ip(request))
    return {"code": 0, "message": "success", "data": result}


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
    result = storage.abort_chunk_session(upload_id)
    return {"code": 0, "message": "success", "data": result}
