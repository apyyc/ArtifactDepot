"""S3-like 对象 API：上传 / 列表 / 下载（Range）/ 删除 / 预签名

所有写操作解析操作者身份（actor）并写审计日志：
- 共享 access_token → "系统/工具"
- 用户 token（tokens.json 注册表）→ 用户名
"""
import mimetypes
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from artifactdepot import storage
from artifactdepot.auth import extract_token, require_admin, require_write_token, resolve_actor_with_sync
from artifactdepot.config import get_config

router = APIRouter(prefix="/api/objects", tags=["Objects"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _public_ip(request: Request) -> str:
    """前端上报的公网 IP（query public_ip / 上传表单字段）；无则空"""
    return (request.query_params.get("public_ip") or "").strip()


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

    写临时文件 → 流式落盘到 <depot_dir>/<bucket>/<key>，计算 SHA-256 记入清单与审计。
    """
    # 令牌：query > 表单字段 > Bearer 头（未命中本地注册表时先拉 DataHub 一次）
    tok = request.query_params.get("token") or token
    if not tok:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    actor = await resolve_actor_with_sync(tok)
    if not actor:
        raise HTTPException(401, detail="无效的访问令牌")

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
        result = storage.put_object(bucket, key, tmp_path, source_url, overwrite, uploader=actor)
    except HTTPException:
        raise
    finally:
        tmp_path.unlink(missing_ok=True)

    storage.audit("upload", bucket, key, actor, _client_ip(request),
                  size=result["size"], sha256=result["sha256"],
                  public_ip=public_ip or _public_ip(request))
    return {"code": 0, "message": "success", "data": result}


@router.get("/list")
async def list_objects(bucket: str, prefix: str = ""):
    """列对象（ListObjects）。bucket=项目，prefix=任务路径（可空）"""
    items = storage.list_objects(bucket, prefix)
    return {"code": 0, "message": "success",
            "data": {"bucket": bucket, "prefix": prefix, "items": items}}


@router.get("/head")
async def head(request: Request, bucket: str, key: str,
               actor: str = Depends(require_write_token)):
    """对象元信息探测（0.6.0）：轻量判断文件/目录是否存在；不存在时 HTTP 仍为 200，exists=false"""
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
    token: str = Query("", description="访问令牌（非签名链接时必填，管理员或用户 token）"),
):
    """下载对象（GetObject）。Starlette FileResponse 原生支持 Range(206)，视频可拖动。

    鉴权优先级：
    1. link+tk（注册表签名链接）→ 校验次数/过期/作废，免 token
    2. expires+sig（旧式 HMAC）→ 校验签名，免 token
    3. 否则 → 必须带有效 token（管理员或用户），actor 从 token 推导
    """
    p = storage.object_path(bucket, key)

    if link is not None or tk is not None:
        # 先确认对象存在，再消费次数，避免「对象已不在但链接次数仍被扣掉」
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
        token = token or extract_token(request)
        actor = await resolve_actor_with_sync(token)
        if not actor:
            raise HTTPException(401, detail="无效的访问令牌")

    if not p.is_file():
        raise HTTPException(404, detail=f"对象不存在：{bucket}/{key}")

    storage.audit("download", bucket, key,
                  actor=actor, ip=_client_ip(request),
                  size=p.stat().st_size, public_ip=_public_ip(request))
    media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media, filename=p.name)


@router.delete("")
async def delete(bucket: str, key: str,
                 request: Request, actor: str = Depends(require_admin)):
    """删除对象或目录（DeleteObject）。仅管理员共享 token 可删"""
    result = storage.delete_object(bucket, key)
    storage.audit("delete", bucket, key, actor, _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success", "data": result}


class MkdirRequest(BaseModel):
    bucket: str
    key: str


@router.post("/mkdir")
async def mkdir(body: MkdirRequest, request: Request, actor: str = Depends(require_write_token)):
    """新建目录（任一有效 token）：按 key 创建目录树 + 隐藏 .keep 占位"""
    result = storage.mkdir(body.bucket, body.key)
    storage.audit("mkdir", body.bucket, body.key, actor, _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success", "data": result}


class RenameRequest(BaseModel):
    bucket: str
    key: str
    new_key: str
    overwrite: bool = False


@router.post("/rename")
async def rename(body: RenameRequest, request: Request, actor: str = Depends(require_write_token)):
    """重命名/移动目录或对象（0.6.0）：整体迁移前缀下对象与 .keep 占位，失败回滚"""
    result = storage.rename_object(body.bucket, body.key, body.new_key, body.overwrite)
    storage.audit("rename", body.bucket, body.key, actor, _client_ip(request),
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
                  request: Request, actor: str = Depends(require_write_token)):
    """生成签名下载链接（任一有效 token 可签）。mode: count/time/permanent"""
    entry = storage.create_signed_link(body.bucket, body.key, body.mode,
                                       count=body.count, expires=body.expires,
                                       created_by=actor)
    url = (f"/api/objects/download?bucket={entry['bucket']}&key={entry['key']}"
           f"&link={entry['id']}&tk={entry['token']}")
    storage.audit("presign", entry["bucket"], entry["key"], actor, _client_ip(request), public_ip=_public_ip(request))
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
async def list_signed_links(request: Request, actor: str = Depends(require_write_token)):
    """列签名链接：任一有效 token 可见全部条目；完整链接 URL 仅管理员/创建者可看，密钥不回传"""
    links = storage.load_signed_links()
    for l in links:
        is_mine = actor == "系统/工具" or l.get("created_by") == actor
        if is_mine:
            l["url"] = (f"/api/objects/download?bucket={l.get('bucket','')}"
                        f"&key={l.get('key','')}&link={l.get('id','')}&tk={l.get('token','')}")
        else:
            l["url"] = ""
        l.pop("token", None)
    return {"code": 0, "message": "success", "data": links}


@router.post("/signed-links/{link_id}/revoke")
async def revoke_link(link_id: str, request: Request, actor: str = Depends(require_write_token)):
    """作废签名链接：管理员可作废任意；用户只能作废自己创建的"""
    entry = storage.get_signed_link(link_id)
    if not entry:
        raise HTTPException(404, detail="链接不存在")
    if actor != "系统/工具" and entry.get("created_by") != actor:
        raise HTTPException(403, detail="只能作废自己创建的链接")
    storage.revoke_signed_link(link_id)
    storage.audit("revoke", entry.get("bucket", ""), entry.get("key", ""), actor, _client_ip(request), public_ip=_public_ip(request))
    return {"code": 0, "message": "success"}


# ---------------------------------------------------------------------------
# 多线程分片上传
# ---------------------------------------------------------------------------

CHUNK_SIZE = 8 * 1024 * 1024


@router.post("/initiate")
async def initiate_upload(request: Request, actor: str = Depends(require_write_token)):
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
    actor: str = Depends(require_write_token),
):
    """上传一个分片（multipart：upload_id + index + chunk 文件）"""
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
    actor: str = Depends(require_write_token),
):
    """合并分片并完成上传"""
    result = storage.finalize_chunk_upload(
        upload_id, bucket, key, total_chunks,
        source_url, uploader=actor, overwrite=overwrite,
    )
    storage.audit("upload", bucket, key, actor, _client_ip(request),
                  size=result["size"], sha256=result["sha256"],
                  public_ip=public_ip or _public_ip(request))
    return {"code": 0, "message": "success", "data": result}


@router.post("/abort")
async def abort_upload(
    request: Request,
    upload_id: str = Form(...),
    actor: str = Depends(require_write_token),
):
    """取消分片上传并清理临时分片"""
    result = storage.abort_chunk_session(upload_id)
    return {"code": 0, "message": "success", "data": result}