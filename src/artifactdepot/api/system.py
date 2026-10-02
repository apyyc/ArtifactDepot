"""系统 API：健康检查、bucket 列表、权限目录、Token 注册表管理、审计查询、token 校验"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from typing import List, Optional

from artifactdepot import storage
from artifactdepot.auth import (
    Principal, extract_token, require_scope, resolve_principal,
)
from artifactdepot.config import get_config
from artifactdepot.permissions import (
    DEFAULT_ROLE, PERMISSION_CATALOG, PUBLIC_CATALOG, ROLE_PRESETS,
)

router = APIRouter(tags=["System"])


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return token[:2] + "****"
    return token[:4] + "****" + token[-4:]


def _public_catalog():
    return PERMISSION_CATALOG + PUBLIC_CATALOG


def _read_requires_token() -> bool:
    """读接口是否要求带 token（默认 false，保持历史公开行为）"""
    return bool(get_config().get("require_read_token", False))


async def _optional_read_principal(request: Request, scope: str):
    """读接口鉴权：require_read_token=true 时强制 scope；否则有 token 且具备 scope 才返回主体"""
    token = extract_token(request)
    principal = await resolve_principal(token) if token else None
    if _read_requires_token():
        from artifactdepot.auth import ensure_scope
        if not principal:
            raise HTTPException(401, detail="缺少或无效的访问令牌")
        ensure_scope(principal, scope)
        return principal
    if principal is None:
        return None
    # 公开模式下：token 没有该读权限时按匿名处理，不阻断（列表仍可能按资源范围过滤）
    if not principal.is_admin and not principal.has_scope(scope):
        return None
    return principal


@router.get("/api/auth/permissions")
async def permissions_catalog():
    """公开：返回全部接口与权限点目录、角色预设（供“逐接口勾选 + 生成 token”前端渲染）"""
    return {"code": 0, "message": "success", "data": {
        "permissions": PERMISSION_CATALOG,
        "public": PUBLIC_CATALOG,
        "roles": [{"key": k, "label": v["label"], "scopes": v["scopes"]}
                  for k, v in ROLE_PRESETS.items()],
        "default_role": DEFAULT_ROLE,
        "require_read_token": _read_requires_token(),
    }}


@router.get("/api/auth/check")
async def auth_check(request: Request, token: str = Query("")):
    """轻量校验 token：返回是否有效 + 操作者身份 + 权限（前端上传/下载前先审核）"""
    token = token or extract_token(request)
    principal = await resolve_principal(token)
    if principal is None:
        # 无效/被禁用/已过期：回带基本信息，便于前端区分“过期”与“无效”
        raw = storage._read_tokens_raw()
        rec = storage._normalize_token_record(raw.get(token, {}), token) if token in raw else {}
        return {"code": 0, "message": "success",
                "data": {"valid": False, "actor": rec.get("user", ""), "role": rec.get("role", ""),
                         "is_admin": False, "scopes": rec.get("scopes", []),
                         "allow_buckets": rec.get("allow_buckets", []),
                         "allow_prefixes": rec.get("allow_prefixes", {}),
                         "expires_at": rec.get("expires_at", ""),
                         "expired": storage.token_record_expired(rec) if rec else False,
                         "enabled": rec.get("enabled", True) if rec else True,
                         "description": rec.get("description", "")}}
    data = principal.to_public()
    data["valid"] = True
    return {"code": 0, "message": "success", "data": data}


@router.get("/health")
async def health():
    root = storage.get_root()
    return {"status": "ok", "depot_dir": str(root), "exists": root.exists()}


@router.get("/api/buckets")
async def list_buckets(request: Request):
    """列 bucket。默认公开；config.require_read_token=true 时需要 bucket:list 权限。"""
    principal = await _optional_read_principal(request, "bucket:list")
    buckets = storage.list_buckets()
    # 带资源范围的 token：只列出允许范围内的 bucket
    if principal is not None and not principal.is_admin and principal.allow_buckets:
        buckets = [b for b in buckets if b in principal.allow_buckets]
    return {"code": 0, "message": "success", "data": buckets}


class CreateBucketRequest(BaseModel):
    bucket: str


@router.post("/api/buckets")
async def create_bucket(body: CreateBucketRequest, request: Request,
                        actor: Principal = Depends(require_scope("bucket:create"))):
    """显式创建 bucket（幂等）：已存在返回 created=false"""
    from artifactdepot.auth import ensure_scope
    ensure_scope(actor, "bucket:create", bucket=body.bucket)
    result = storage.create_bucket(body.bucket)
    storage.audit("create_bucket", result["bucket"], "", actor.actor,
                  request.client.host if request.client else "",
                  public_ip=(request.query_params.get("public_ip") or "").strip())
    return {"code": 0, "message": "success", "data": result}


# ---------------------------------------------------------------------------
# Token 注册表管理
# ---------------------------------------------------------------------------

def _record_public(token: str, rec: dict, reveal: bool) -> dict:
    out = dict(rec)
    out["token"] = token if reveal else _mask_token(token)
    out["expired"] = storage.token_record_expired(rec)
    return out


@router.get("/api/tokens")
async def list_tokens(request: Request,
                      reveal: bool = Query(False, description="是否返回 token 明文（仅管理员）"),
                      actor: Principal = Depends(require_scope("token:read"))):
    """查看 token 注册表；非管理员默认脱敏，管理员可 reveal=true 看明文"""
    mapping = storage.load_tokens()
    is_admin = actor.is_admin
    data = {}
    for tok, rec in mapping.items():
        # 默认一律脱敏；仅管理员且 reveal=true 时回明文 key / 明文 token
        plain = bool(reveal and is_admin)
        key = tok if plain else _mask_token(tok)
        data[key] = _record_public(tok, rec, plain)
    return {"code": 0, "message": "success", "data": data}


class TokenEntry(BaseModel):
    user: str
    token: str = ""                       # 留空则服务端生成
    role: str = ""                        # viewer/downloader/uploader/publisher/operator/auditor/user/custom
    scopes: Optional[List[str]] = None    # 勾选的接口权限点；显式传入则以此为准
    allow_buckets: Optional[List[str]] = None
    allow_prefixes: Optional[dict] = None
    expires_at: str = ""
    description: str = ""


@router.post("/api/tokens")
async def add_token_entry(body: TokenEntry, request: Request,
                          actor: Principal = Depends(require_scope("token:write"))):
    """生成 / 登记带权限的 token。

    - `scopes` 为「逐接口勾选」结果；不传则用 `role` 预设
    - `token` 留空由服务端生成，响应一次性返回明文
    """
    legacy = (body.role in ("", None) and body.scopes is None
              and body.allow_buckets is None and body.allow_prefixes is None
              and not (body.expires_at or "").strip()
              and not (body.description or "").strip())
    result = storage.add_token(
        body.token, body.user, role=body.role or DEFAULT_ROLE,
        scopes=body.scopes, allow_buckets=body.allow_buckets,
        allow_prefixes=body.allow_prefixes, expires_at=body.expires_at,
        description=body.description, created_by=actor.actor,
        keep_existing_permissions=legacy,
    )
    storage.audit("token_create", "", "", actor.actor,
                  request.client.host if request.client else "",
                  extra={"user": result.get("user"), "role": result.get("role"),
                         "scopes": result.get("scopes")})
    return {"code": 0, "message": "success", "data": result}


class TokenUpdate(BaseModel):
    user: Optional[str] = None
    role: Optional[str] = None
    scopes: Optional[List[str]] = None
    allow_buckets: Optional[List[str]] = None
    allow_prefixes: Optional[dict] = None
    expires_at: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


@router.put("/api/tokens/{value}")
async def update_token_entry(value: str, body: TokenUpdate, request: Request,
                             actor: Principal = Depends(require_scope("token:write"))):
    """更新已有 token 的权限（不传则沿用原值）"""
    raw = storage._read_tokens_raw()
    if value not in raw:
        raise HTTPException(404, detail="token 不存在")
    rec = storage._normalize_token_record(raw[value], value)
    if body.user is not None and body.user.strip():
        rec["user"] = body.user.strip()
    if body.role is not None and body.role.strip():
        rec["role"] = body.role.strip()
        if body.scopes is None:
            from artifactdepot.permissions import role_scopes
            preset = role_scopes(rec["role"])
            if preset is None:
                raise HTTPException(400, detail=f"未知角色：{rec['role']}")
            rec["scopes"] = preset
    if body.scopes is not None:
        from artifactdepot.permissions import is_valid_scope
        clean = []
        for sc in body.scopes:
            sc = str(sc).strip()
            if not sc:
                continue
            if not is_valid_scope(sc):
                raise HTTPException(400, detail=f"未知权限点：{sc}")
            if sc not in clean:
                clean.append(sc)
        rec["scopes"] = clean
    if body.allow_buckets is not None:
        rec["allow_buckets"] = [str(b).strip() for b in body.allow_buckets if str(b).strip()]
    if body.allow_prefixes is not None:
        clean_prefixes = {}
        for b, prefixes in body.allow_prefixes.items():
            b = str(b).strip()
            if not b:
                continue
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            if not isinstance(prefixes, list):
                raise HTTPException(400, detail=f"allow_prefixes[{b}] 必须是数组")
            clean_prefixes[b] = [str(x).strip().strip("/") for x in prefixes if str(x).strip()]
        rec["allow_prefixes"] = clean_prefixes
    if body.expires_at is not None:
        rec["expires_at"] = body.expires_at.strip()
    if body.description is not None:
        rec["description"] = body.description.strip()
    if body.enabled is not None:
        rec["enabled"] = bool(body.enabled)
    raw[value] = rec
    storage._save_tokens(raw)
    return {"code": 0, "message": "success", "data": _record_public(value, rec, False)}


@router.delete("/api/tokens")
async def del_token(value: str, request: Request,
                    actor: Principal = Depends(require_scope("token:delete"))):
    """移除 token（如用户重置/注销）；value 为要删除的 token"""
    result = storage.remove_token(value)
    storage.audit("token_delete", "", "", actor.actor,
                  request.client.host if request.client else "",
                  extra={"user": result.get("user")})
    return {"code": 0, "message": "success", "data": result}


@router.post("/api/tokens/sync")
async def sync_tokens(request: Request,
                      actor: Principal = Depends(require_scope("token:sync"))):
    """从 DataHub users.json 拉取并合并用户 token（权限配置保留）"""
    result = await storage.sync_tokens_detailed(
        get_config().get("datahub_url", ""), force=True)
    return {"code": 0, "message": "success", "data": result}


# ---------------------------------------------------------------------------
# 审计查询
# ---------------------------------------------------------------------------

@router.get("/api/audit")
async def list_audit(request: Request,
                     bucket: str = Query("", description="按 bucket 过滤"),
                     key: str = Query("", description="按 key 精确过滤"),
                     actor: str = Query("", description="按操作者过滤"),
                     since: str = Query("", description="只取 >= 此时间（YYYY-MM-DD 或完整）"),
                     limit: int = Query(500, ge=0, description="最多返回条数（0 表示不返回）"),
                     _actor: Principal = Depends(require_scope("audit:read"))):
    """查询审计日志（谁在何时上传/下载/删除/生成签名）"""
    rows = storage.query_audit(bucket, key, actor, since, limit)
    return {"code": 0, "message": "success", "data": rows}
