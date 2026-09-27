"""系统 API：健康检查、bucket 列表、Token 注册表管理、审计查询、token 校验"""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from artifactdepot import storage
from artifactdepot.auth import extract_token, require_admin, require_write_token, resolve_actor_with_sync
from artifactdepot.config import get_config

router = APIRouter(tags=["System"])


@router.get("/api/auth/check")
async def auth_check(request: Request, token: str = Query("")):
    """轻量校验 token：返回是否有效 + 操作者身份（前端上传/下载前先审核）

    支持 query `?token=` 或 `Authorization: Bearer`。
    """
    token = token or extract_token(request)
    actor = await resolve_actor_with_sync(token)
    return {"code": 0, "message": "success",
            "data": {"valid": bool(actor), "actor": actor or ""}}


@router.get("/health")
async def health():
    root = storage.get_root()
    return {"status": "ok", "depot_dir": str(root), "exists": root.exists()}


@router.get("/api/buckets")
async def list_buckets():
    return {"code": 0, "message": "success", "data": storage.list_buckets()}


class CreateBucketRequest(BaseModel):
    bucket: str


@router.post("/api/buckets")
async def create_bucket(body: CreateBucketRequest, request: Request,
                        actor: str = Depends(require_write_token)):
    """显式创建 bucket（0.6.0，幂等）：已存在返回 created=false"""
    result = storage.create_bucket(body.bucket)
    storage.audit("create_bucket", result["bucket"], "", actor,
                  request.client.host if request.client else "",
                  public_ip=(request.query_params.get("public_ip") or "").strip())
    return {"code": 0, "message": "success", "data": result}


# ---------------------------------------------------------------------------
# Token 注册表管理（仅管理员共享 token）
# ---------------------------------------------------------------------------

@router.get("/api/tokens")
async def list_tokens(request: Request, _admin: str = Depends(require_admin)):
    """查看 token→用户名 映射"""
    return {"code": 0, "message": "success", "data": storage.load_tokens()}


class TokenEntry(BaseModel):
    token: str
    user: str


@router.post("/api/tokens")
async def add_token_entry(body: TokenEntry, request: Request,
                          _admin: str = Depends(require_admin)):
    """登记/更新 token→用户名（平台注册/查Token时自动调用；也可网页手动添加）"""
    result = storage.add_token(body.token, body.user)
    return {"code": 0, "message": "success", "data": result}


@router.delete("/api/tokens")
async def del_token(value: str, request: Request, _admin: str = Depends(require_admin)):
    """移除 token（如用户重置/注销）；value 为要删除的 token"""
    result = storage.remove_token(value)
    return {"code": 0, "message": "success", "data": result}


@router.post("/api/tokens/sync")
async def sync_tokens(request: Request, _admin: str = Depends(require_admin)):
    """从 DataHub users.json 拉取并合并用户 token（collab 为权威源，本地为副本）

    ok=false 时说明 datahub_url 不可达/解析失败，前端应提示，避免误以为同步成功。"""
    result = await storage.sync_tokens_detailed(
        get_config().get("datahub_url", ""), force=True)
    return {"code": 0, "message": "success", "data": result}


# ---------------------------------------------------------------------------
# 审计查询（仅管理员共享 token）
# ---------------------------------------------------------------------------

@router.get("/api/audit")
async def list_audit(request: Request,
                     bucket: str = Query("", description="按 bucket 过滤"),
                     key: str = Query("", description="按 key 精确过滤"),
                     actor: str = Query("", description="按操作者过滤"),
                     since: str = Query("", description="只取 >= 此时间（YYYY-MM-DD 或完整）"),
                     limit: int = Query(500, ge=0, description="最多返回条数（0 表示不返回）"),
                     _admin: str = Depends(require_admin)):
    """查询审计日志（谁在何时上传/下载/删除/生成签名）"""
    rows = storage.query_audit(bucket, key, actor, since, limit)
    return {"code": 0, "message": "success", "data": rows}
