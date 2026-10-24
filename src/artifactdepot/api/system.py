# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""系统 API：健康检查、bucket 列表、权限目录、Token 注册表管理、审计查询、token 校验"""
import hashlib

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from typing import List, Optional

from artifactdepot import netutil, storage
from artifactdepot.auth import (
    Principal, ensure_can_grant, extract_token, optional_read_principal,
    read_requires_token, require_scope, resolve_principal,
    resource_allowed,
)
from artifactdepot.config import (ConfigError, config_snapshot, get_config,
                                  public_download_settings, public_token_settings,
                                  public_ui_settings, public_upload_settings,
                                  reload_config, update_config_file)
from artifactdepot.observability import render_metrics
from artifactdepot.permissions import (
    DEFAULT_ROLE, PERMISSION_CATALOG, PUBLIC_CATALOG, ROLE_PRESETS,
)

router = APIRouter(tags=["System"])


def _mask_token(token: str) -> str:
    """脱敏 token：只显示前 2 位与后 4 位，其余替换为 ****。

    - 长度 > 6：`前2 + **** + 后4`
    - 长度 <= 6（极短旧 token）：`前2 + ****`，不暴露后 4 位以免与前缀重叠
    - 空串返回空串

    唯一性由 `list_tokens` 在 key 冲突时追加短哈希兜底（正常情况下不显示哈希后缀）。
    """
    if not token:
        return ""
    if len(token) <= 6:
        return token[:2] + "****"
    return token[:2] + "****" + token[-4:]


def _public_catalog():
    return PERMISSION_CATALOG + PUBLIC_CATALOG


# 读接口鉴权统一走 auth.optional_read_principal（fail-closed）：
# 不带 token 时按 require_read_token 决定是否公开；带 token 时必须有效且具备 scope。


@router.get("/api/auth/permissions")
async def permissions_catalog():
    """公开：返回全部接口与权限点目录、角色预设（供“逐接口勾选 + 生成 token”前端渲染）"""
    return {"code": 0, "message": "success", "data": {
        "permissions": PERMISSION_CATALOG,
        "public": PUBLIC_CATALOG,
        "roles": [{"key": k, "label": v["label"], "scopes": v["scopes"]}
                  for k, v in ROLE_PRESETS.items()],
        "default_role": DEFAULT_ROLE,
        "require_read_token": read_requires_token(),
        # 上传参数（分片大小/并发/请求超时/上限），供网页与 CLI 动态读取
        "upload": public_upload_settings(),
        # 下载参数（分段大小/并发/请求超时），供网页与桌面客户端动态读取
        "download": public_download_settings(),
        # 网页分页参数（文件列表/审计查询），供网页动态读取
        "ui": public_ui_settings(),
        # 新建 token 的长度上下限（服务端自动生成与显式创建都受此约束）
        "token": public_token_settings(),
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


@router.get("/health/live")
async def health_live():
    """liveness：进程活着就返回 200。"""
    return {"status": "alive"}


@router.get("/health/ready")
async def health_ready():
    """readiness：depot 目录存在且可写才返回 200，否则 503。"""
    root = storage.get_root()
    if not root.exists() or not os.access(root, os.W_OK):
        raise HTTPException(503, detail="depot 目录不存在或不可写")
    return {"status": "ready", "depot_dir": str(root)}


@router.get("/metrics")
async def metrics():
    """Prometheus 文本格式指标；config.metrics_enabled=false 时 404。"""
    if not bool(get_config().get("metrics_enabled", True)):
        raise HTTPException(404, detail="metrics 未启用")
    return Response(render_metrics(),
                    media_type="text/plain; version=0.0.4; charset=utf-8")


@router.post("/api/config/reload")
async def config_reload(request: Request,
                        actor: Principal = Depends(require_scope("config:write"))):
    """手动重新加载 config/config.json + config/.env。

    鉴权：管理员共享 token，或具备 `config:write` 权限点的 token。
    返回 data = {ok, error, changed, needs_restart, warnings}：
    changed 已即时生效；needs_restart 值变了但需重启（保留旧值）。
    """
    result = reload_config()
    storage.audit("config_reload", "", "", actor.actor,
                  request.client.host if request.client else "",
                  extra={"changed": result.get("changed"),
                         "needs_restart": result.get("needs_restart")})
    if not result.get("ok"):
        raise HTTPException(400, detail=result.get("error") or "配置重载失败")
    return {"code": 0, "message": "success", "data": result}


class ConfigUpdateRequest(BaseModel):
    values: dict


@router.get("/api/config", include_in_schema=False)
async def get_runtime_config(actor: Principal = Depends(require_scope("config:write"))):
    """读取配置快照（管理员共享 token 或具备 `config:write` 的 token）。

    返回 config.json 文件值、当前生效值、每个字段的来源（config.json / .env / 环境变量）、
    需重启项与告警；`access_token` 等密钥一律脱敏。该接口不进入 OpenAPI；
    读取快照是安全修改配置的前提，因此与写入共用 `config:write` 权限点。
    """
    return {"code": 0, "message": "success", "data": config_snapshot()}


@router.post("/api/config", include_in_schema=False)
async def update_runtime_config(body: ConfigUpdateRequest, request: Request,
                                actor: Principal = Depends(require_scope("config:write"))):
    """在线修改 config.json 并热加载（管理员共享 token 或具备 `config:write` 的 token）。

    只接受已知配置项；写盘原子、重载失败自动回滚。`access_token` 留空或提交脱敏占位符
    表示保持原值。被 .env / 环境变量覆盖的字段会写入文件但不会生效，响应 `overridden`
    会明确列出。该接口不进入 OpenAPI，避免改变对外操作数。
    """
    values = body.values or {}
    # 防提权：config:write 只允许改配置项，不允许轮换管理员共享令牌
    # （access_token 是超级管理员凭据，轮换等价于接管整个实例）。
    if not actor.is_admin and isinstance(values, dict):
        raw_tok = values.get("access_token")
        if raw_tok is not None and str(raw_tok).strip() not in ("", "********"):
            raise HTTPException(
                403,
                detail="仅管理员共享令牌可修改 access_token（config:write 不能轮换管理员令牌）",
            )
    try:
        result = update_config_file(values)
    except ConfigError as e:
        raise HTTPException(400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(500, detail=f"写入配置文件失败：{e}") from e
    storage.audit("config_update", "", "", actor.actor,
                  request.client.host if request.client else "",
                  extra={"changed": result.get("changed"),
                         "needs_restart": result.get("needs_restart"),
                         "overridden": result.get("overridden")})
    return {"code": 0, "message": "success", "data": result}



@router.get("/api/buckets")
async def list_buckets(request: Request):
    """列 bucket。

    鉴权（fail-closed）：不带 token 时默认公开；带 token 时必须有效且具备 `bucket:list`；
    带资源范围的 token 只列出范围内的 bucket。
    """
    principal = await optional_read_principal(request, "bucket:list")
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
                  public_ip=netutil.resolved_public_ip(request))
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
        if plain:
            data[tok] = _record_public(tok, rec, True)
            continue
        # 脱敏 key 冲突（前 2 + 后 4 相同）时追加短哈希兜底，避免互相覆盖；
        # 正常情况下展示的就是「前2 + **** + 后4」，不带任何后缀。
        key = _mask_token(tok)
        if key in data:
            digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()[:6]
            key = f"{_mask_token(tok)}-{digest}"
            n = 2
            while key in data:
                key = f"{_mask_token(tok)}-{digest}-{n}"
                n += 1
        item = _record_public(tok, rec, False)
        item["token"] = key          # 展示值与 dict key 保持一致（前端按 key 移除）
        data[key] = item
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
    - **防提权**：非共享 token 不能创建出「scope / bucket / 前缀范围超出自己」的 token（403）
    - **覆写既有 token = 更新**：显式传入的 `token` 若已在注册表中，按 `PUT` 的**合并语义**
      处理（未传字段一律保留），并复用同一套防提权/范围校验与 `token_update` 审计。
      这样不会因为「重新登记一次」就把资源范围静默放宽为全部 bucket、或把
      已禁用 / 已过期的 token 复活。
    """
    legacy = (body.role in ("", None) and body.scopes is None
              and body.allow_buckets is None and body.allow_prefixes is None
              and not (body.expires_at or "").strip()
              and not (body.description or "").strip())

    existing_token = (body.token or "").strip()
    raw_existing = storage._read_tokens_raw() if existing_token else {}
    overwriting = bool(existing_token) and existing_token in raw_existing

    # 覆写（非 legacy）→ 完全走 PUT 的合并语义：未传字段保留、目标/结果双向防提权、
    # 范围自洽与前缀规范化、审计记 token_update。
    if overwriting and not legacy:
        explicit = body.model_fields_set
        updatable = ("user", "role", "scopes", "allow_buckets",
                     "allow_prefixes", "expires_at", "description")
        patch = {f: getattr(body, f) for f in updatable if f in explicit}
        resp = await update_token_entry(
            existing_token, TokenUpdate(**patch), request, actor)
        data = dict(resp.get("data") or {})
        # POST 契约要回带明文：调用方本就持有该 token 字符串
        data["token"] = existing_token
        return {"code": 0, "message": "success", "data": data}

    if not actor.is_admin:
        from artifactdepot.permissions import role_scopes
        # 目标 token 若**已存在**，无论 legacy 登记还是普通覆写，都等同于「修改既有 token」，
        # 因此必须先校验目标现有授权不超出自己（否则可以把更大的 token 降级、
        # 甚至把已禁用的 token 直接覆写复活）。用原始记录判断，避免 disabled/expired 被当作新 token。
        pat = (body.token or "").strip()
        raw_tokens = storage._read_tokens_raw() if pat else {}
        existing = (storage._normalize_token_record(raw_tokens[pat], pat)
                    if pat and pat in raw_tokens else None)
        if existing is not None:
            ensure_can_grant(actor, scopes=existing.get("scopes") or [],
                             allow_buckets=existing.get("allow_buckets") or [],
                             allow_prefixes=existing.get("allow_prefixes") or {},
                             action="登记")
        if legacy:
            # 旧协议「仅登记 token+user」：新建时按 role=user 预设建立，也要校验
            if existing is None:
                ensure_can_grant(actor, scopes=role_scopes(DEFAULT_ROLE) or [],
                                 allow_buckets=[], allow_prefixes={}, action="登记")
        else:
            req_scopes = body.scopes
            if req_scopes is None:
                req_scopes = role_scopes(body.role or DEFAULT_ROLE) or []
            ensure_can_grant(
                actor, scopes=req_scopes,
                allow_buckets=body.allow_buckets if body.allow_buckets is not None else [],
                allow_prefixes=body.allow_prefixes if body.allow_prefixes is not None else {},
                action="创建")
    result = storage.add_token(
        body.token, body.user, role=body.role or DEFAULT_ROLE,
        scopes=body.scopes, allow_buckets=body.allow_buckets,
        allow_prefixes=body.allow_prefixes, expires_at=body.expires_at,
        description=body.description, created_by=actor.actor,
        keep_existing_permissions=legacy,
    )
    # legacy「仅登记 token+user」命中已存在 token 时同样是更新 → 审计按 update 记
    storage.audit("token_update" if overwriting else "token_create", "", "", actor.actor,
                  request.client.host if request.client else "",
                  extra={"user": result.get("user"), "role": result.get("role"),
                         "scopes": result.get("scopes"),
                         "allow_buckets": result.get("allow_buckets")})
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
    with storage.state_lock():
        raw = storage._read_tokens_raw()
        if value not in raw:
            raise HTTPException(404, detail="token 不存在")
        rec = storage._normalize_token_record(raw[value], value)
        # 防提权（第一道）：不能改动「自身范围之外」的既有 token。
        # 否则受限用户可以把一个更大的 token 降级/改写为自身范围（实测曾返回 200）。
        ensure_can_grant(actor, scopes=rec.get("scopes") or [],
                         allow_buckets=rec.get("allow_buckets") or [],
                         allow_prefixes=rec.get("allow_prefixes") or {},
                         action="更新")
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
                clean_prefixes[b] = storage.normalize_prefixes(prefixes)
            rec["allow_prefixes"] = clean_prefixes
        if body.expires_at is not None:
            rec["expires_at"] = body.expires_at.strip()
        if body.description is not None:
            rec["description"] = body.description.strip()
        if body.enabled is not None:
            rec["enabled"] = bool(body.enabled)
        # 资源范围必须自洽：写了路径前缀就必须把该 bucket 纳入 allow_buckets，
        # 否则未被点名的 bucket 会不受限制（静默放大权限）。
        storage.validate_scope_range(rec.get("allow_buckets"), rec.get("allow_prefixes"))
        # 防提权：校验**更新后的最终授权**不超出操作者自身范围。
        # 这同时覆盖「目标 token 本来就比自己大」的情况（不允许受限用户改动更大的 token）。
        ensure_can_grant(actor, scopes=rec.get("scopes") or [],
                         allow_buckets=rec.get("allow_buckets") or [],
                         allow_prefixes=rec.get("allow_prefixes") or {},
                         action="更新")
        raw[value] = rec
        storage._save_tokens(raw)
    storage.audit("token_update", "", "", actor.actor,
                  request.client.host if request.client else "",
                  extra={"user": rec.get("user"), "role": rec.get("role"),
                         "scopes": rec.get("scopes"),
                         "allow_buckets": rec.get("allow_buckets"),
                         "allow_prefixes": rec.get("allow_prefixes"),
                         "enabled": rec.get("enabled", True),
                         "expires_at": rec.get("expires_at", "")})
    return {"code": 0, "message": "success", "data": _record_public(value, rec, False)}


@router.delete("/api/tokens")
async def del_token(value: str, request: Request,
                    actor: Principal = Depends(require_scope("token:delete"))):
    """移除 token（如用户重置/注销）；value 为要删除的 token

    防提权：非共享 token 只能删除「授权不超出自己」的 token（否则 403）。
    """
    if not actor.is_admin:
        # 用原始记录（而非 get_token_record）判断：目标即使已过期/禁用，
        # 也不能成为绕过范围限制的删除对象。
        raw = storage._read_tokens_raw()
        target = (value or "").strip()
        if target in raw:
            rec = storage._normalize_token_record(raw[target], target)
            ensure_can_grant(actor, scopes=rec.get("scopes") or [],
                             allow_buckets=rec.get("allow_buckets") or [],
                             allow_prefixes=rec.get("allow_prefixes") or {},
                             action="删除")
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
    """查询审计日志（谁在何时上传/下载/删除/生成签名）

    资源范围：**受限**的非共享 token（配置了 `allow_buckets` / `allow_prefixes`）
    只返回其范围内、且**归属到具体 bucket** 的记录。bucket 为空的全局事件
    （token_create / token_update / token_delete / config_reload 等）无法归属到任何范围，
    且会泄露其他 token 的 user/role/scopes，故对受限 token 不返回；
    未配置范围的普通 token 与共享 access_token 行为不变（可见全量）。
    """
    if _actor.is_admin:
        rows = storage.query_audit(bucket, key, actor, since, limit)
    elif _actor.allow_buckets or _actor.allow_prefixes:
        def _visible(rec):
            b = (rec.get("bucket") or "").strip()
            if not b:
                return False
            if not resource_allowed(_actor, b, rec.get("key", "") or ""):
                return False
            # rename 记录会把目标路径放在顶层 to_key：若目标落在外面前缀之外，
            # 只凭 key（源）放行会顺带泄露范围外的路径。
            to_key = rec.get("to_key")
            if to_key and not resource_allowed(_actor, b, str(to_key)):
                return False
            return True

        rows = storage.query_audit(bucket, key, actor, since, limit, predicate=_visible)
    else:
        rows = storage.query_audit(bucket, key, actor, since, limit)
    return {"code": 0, "message": "success", "data": rows}
