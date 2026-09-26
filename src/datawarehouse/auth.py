"""访问令牌校验与操作者识别

- **写操作**（上传/删除/预签名）：
  - 共享 `access_token`（管理员/工具）→ actor = "系统/工具"
  - 命中 tokens.json 注册表的用户 token → actor = 用户名
  - 都匹配不上 → 401 拒绝
- **删除**：仅管理员共享 token
- **下载**：需有效 token（管理员或用户）；签名链接（expires+sig）免 token
- **管理接口**（/api/tokens、/api/audit）：仅管理员共享 token
- 列表接口（/api/objects/list）默认内网开放
"""
import hmac

from fastapi import HTTPException, Request

from datawarehouse import storage
from datawarehouse.config import get_config


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


def resolve_actor(token: str):
    """解析令牌对应的操作者身份；非法返回 None"""
    if not token:
        return None
    # 管理员/工具共享 token
    if check_admin_token(token):
        return "系统/工具"
    # 用户 token（tokens.json 注册表）
    return storage.resolve_user(token)


async def resolve_actor_with_sync(token: str):
    """解析操作者身份；本地未命中时先从 DataHub 同步一次再判断（惰性刷新）"""
    actor = resolve_actor(token)
    if actor:
        return actor
    # 惰性：从 DataHub users.json 拉一次（带防抖），新用户 token 无需等定时/重启
    await storage.sync_tokens_from_datahub(get_config().get("datahub_url", ""))
    return resolve_actor(token)


async def require_admin(request: Request) -> str:
    """FastAPI 依赖：管理接口，仅接受管理员共享 token

    错误语义：
    - 未带 token / token 无效：401
    - token 有效但不是管理员：403
    """
    token = extract_token(request)
    if not token:
        raise HTTPException(401, detail="缺少管理员令牌")
    if check_admin_token(token):
        return "系统/工具"
    # token 不是管理员：如果它是有效用户 token，返回 403 表示权限不足
    actor = await resolve_actor_with_sync(token)
    if actor:
        raise HTTPException(403, detail="需要管理员权限")
    raise HTTPException(401, detail="无效的管理员令牌")


async def require_write_token(request: Request) -> str:
    """FastAPI 依赖：写操作，接受管理员或已登记用户 token（含惰性同步），返回操作者身份"""
    token = extract_token(request)
    actor = await resolve_actor_with_sync(token)
    if not actor:
        raise HTTPException(401, detail="无效的访问令牌")
    return actor


def extract_token(request: Request) -> str:
    """从 query `token=` 或 `Authorization: Bearer` 提取令牌"""
    token = request.query_params.get("token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token or ""


# 兼容旧代码中的私有名
_extract_token = extract_token
