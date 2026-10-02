"""访问令牌校验、权限（scope）判定与资源范围约束

模型：
- 共享 `access_token` = 超级管理员，放行全部权限；
- 用户 token 在 `tokens.json` 注册表中登记，带 role + scopes + 可选的
  bucket / 路径前缀白名单 / 过期时间；
- 每个受保护接口对应一个权限点（见 permissions.py），通过
  `require_scope("object:upload")` 依赖或端点内 `ensure_scope()` 校验。

旧 token（tokens.json 里字符串形式的“token → 用户名”）自动按 role=user 解释，
保持历史行为：上传/下载/建目录/改名/签名/建 bucket，但不能删除、管理 token、看审计。
"""
import hmac

from fastapi import HTTPException, Request

from artifactdepot import storage
from artifactdepot.config import get_config


class Principal:
    """已认证主体：管理员共享 token 或用户 token"""

    def __init__(self, actor: str, role: str = "", scopes=None,
                 allow_buckets=None, allow_prefixes=None,
                 token: str = "", is_admin: bool = False, record=None):
        self.actor = actor or ""
        self.role = role or ""
        self.scopes = list(scopes or [])
        self.allow_buckets = list(allow_buckets or [])
        self.allow_prefixes = dict(allow_prefixes or {})
        self.token = token or ""
        self.is_admin = bool(is_admin)
        self.record = record or {}

    def has_scope(self, scope: str) -> bool:
        if self.is_admin:
            return True
        if "*" in self.scopes or "admin:*" in self.scopes:
            return True
        return scope in self.scopes

    def to_public(self) -> dict:
        return {
            "actor": self.actor,
            "role": self.role,
            "is_admin": self.is_admin,
            "scopes": self.scopes,
            "allow_buckets": self.allow_buckets,
            "allow_prefixes": self.allow_prefixes,
            "expires_at": self.record.get("expires_at", ""),
            "description": self.record.get("description", ""),
        }


def check_admin_token(token: str) -> bool:
    """常量时间比较令牌是否与配置的共享 access_token 一致（管理员/工具身份）

    编码为 UTF-8 字节再比较：`hmac.compare_digest` 不接受非 ASCII 字符串，
    直接比较带中文/特殊符号的 token 会抛 TypeError → 500；转字节后非 ASCII 也安全。"""
    expected = get_config().get("access_token", "")
    if not expected:
        raise HTTPException(500, detail="服务端未配置 access_token，请先修改 resources/config.json")
    if not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def extract_token(request: Request) -> str:
    """从 query `token=` 或 `Authorization: Bearer` 提取令牌"""
    token = request.query_params.get("token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token or ""


async def extract_form_token(request: Request) -> str:
    """从 multipart 表单字段 `token` 提取令牌（仅在 query/Bearer 都没有时尝试）"""
    token = extract_token(request)
    if token:
        return token
    try:
        form = await request.form()
        val = form.get("token")
        if isinstance(val, str):
            return val.strip()
    except Exception:
        pass
    return ""


def principal_from_admin(token: str) -> Principal:
    return Principal(actor="系统/工具", role="admin", scopes=["admin:*"],
                     token=token, is_admin=True)


def principal_from_record(token: str, rec: dict) -> Principal:
    return Principal(
        actor=rec.get("user", ""),
        role=rec.get("role", ""),
        scopes=rec.get("scopes", []),
        allow_buckets=rec.get("allow_buckets", []),
        allow_prefixes=rec.get("allow_prefixes", {}),
        token=token,
        is_admin=False,
        record=rec,
    )


def _principal_no_sync(token: str):
    if not token:
        return None
    if check_admin_token(token):
        return principal_from_admin(token)
    rec = storage.get_token_record(token)
    if not rec:
        return None
    return principal_from_record(token, rec)


async def resolve_principal(token: str, sync: bool = True):
    """解析令牌对应的主体；本地未命中时从 DataHub 惰性同步一次再判定"""
    principal = _principal_no_sync(token)
    if principal:
        return principal
    if not sync:
        return None
    await storage.sync_tokens_from_datahub(get_config().get("datahub_url", ""))
    return _principal_no_sync(token)


def scope_allowed(principal: Principal, scope: str) -> bool:
    return bool(principal) and principal.has_scope(scope)


def _key_in_prefix(key: str, prefix: str) -> bool:
    """路径前缀边界判断：prefix='a/b' 匹配 'a/b' 与 'a/b/...'，不匹配 'a/bc'"""
    k = (key or "").strip("/")
    p = (prefix or "").strip("/")
    if not p:
        return True
    return k == p or k.startswith(p + "/")


def resource_allowed(principal: Principal, bucket: str = "", key: str = "") -> bool:
    """资源范围校验：bucket 白名单 + bucket 内路径前缀白名单"""
    if principal is None:
        return False
    if principal.is_admin:
        return True
    bucket = (bucket or "").strip()
    key = (key or "").strip("/")
    if principal.allow_buckets and bucket and bucket not in principal.allow_buckets:
        return False
    prefixes = principal.allow_prefixes.get(bucket) if bucket else None
    if prefixes:
        if not any(_key_in_prefix(key, p) for p in prefixes):
            return False
    return True


def ensure_scope(principal: Principal, scope: str,
                 bucket: str = "", key: str = "") -> None:
    """端点内校验：无主体 401，缺权限 403，资源越界 403"""
    if principal is None:
        raise HTTPException(401, detail="缺少或无效的访问令牌")
    if not principal.has_scope(scope):
        raise HTTPException(403, detail=f"权限不足：需要 {scope}")
    if (bucket or key) and not resource_allowed(principal, bucket, key):
        raise HTTPException(403, detail="权限不足：token 的资源范围不允许该 bucket / 路径")


async def require_principal(request: Request, allow_form: bool = False) -> Principal:
    """从请求解析主体（query / Bearer；allow_form 时兼容 multipart token 字段）"""
    token = await extract_form_token(request) if allow_form else extract_token(request)
    principal = await resolve_principal(token)
    if not principal:
        raise HTTPException(401, detail="无效的访问令牌")
    if not principal.is_admin:
        storage.touch_token(token)
    return principal


def require_scope(scope: str):
    """FastAPI 依赖工厂：要求携带具备指定权限点的 token"""

    async def _dep(request: Request) -> Principal:
        principal = await require_principal(request)
        ensure_scope(principal, scope)
        return principal

    return _dep


async def require_admin(request: Request) -> str:
    """管理接口旧依赖：仅接受管理员共享 token（保留兼容）"""
    token = extract_token(request)
    if not token:
        raise HTTPException(401, detail="缺少管理员令牌")
    if check_admin_token(token):
        return "系统/工具"
    principal = await resolve_principal(token)
    if principal:
        raise HTTPException(403, detail="需要管理员权限")
    raise HTTPException(401, detail="无效的管理员令牌")


async def require_write_token(request: Request) -> str:
    """旧依赖：任一有效 token（保留兼容；新代码请用 require_scope）"""
    principal = await require_principal(request)
    return principal.actor


# 兼容旧代码中的私有名
_extract_token = extract_token
