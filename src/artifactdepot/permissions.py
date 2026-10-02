"""权限目录与角色预设

设计：
- **一个受保护接口 = 一个权限点（scope）**，便于「逐接口勾选启用」后生成 token。
- 公开接口单独列出（不可勾选，仅用于前端展示全量接口）。
- 角色（role）只是权限点的预设组合；token 可以完全自定义 scopes 覆盖。
- 旧 token（tokens.json 中的字符串值）默认按 `user` 角色解释，保持历史行为：
  可上传/下载/建目录/改名/签名/建 bucket，但不能删除、管理 token、看审计。
"""

# 受保护接口目录（key = 权限点，唯一）
PERMISSION_CATALOG = [
    # ---- Bucket ----
    {"key": "bucket:list", "group": "Bucket", "label": "查看 bucket 列表",
     "method": "GET", "path": "/api/buckets", "write": False},
    {"key": "bucket:create", "group": "Bucket", "label": "创建 bucket",
     "method": "POST", "path": "/api/buckets", "write": True},

    # ---- 对象读 ----
    {"key": "object:list", "group": "对象读", "label": "列出对象 / 目录",
     "method": "GET", "path": "/api/objects/list", "write": False},
    {"key": "object:head", "group": "对象读", "label": "探测对象元信息",
     "method": "GET", "path": "/api/objects/head", "write": False},
    {"key": "object:download", "group": "对象读", "label": "下载对象",
     "method": "GET", "path": "/api/objects/download", "write": False},

    # ---- 对象写 ----
    {"key": "object:upload", "group": "对象写", "label": "上传对象（单请求）",
     "method": "POST", "path": "/api/objects", "write": True},
    {"key": "upload:initiate", "group": "对象写", "label": "分片上传-发起会话",
     "method": "POST", "path": "/api/objects/initiate", "write": True},
    {"key": "upload:chunk", "group": "对象写", "label": "分片上传-上传分片",
     "method": "POST", "path": "/api/objects/chunk", "write": True},
    {"key": "upload:complete", "group": "对象写", "label": "分片上传-合并完成",
     "method": "POST", "path": "/api/objects/complete", "write": True},
    {"key": "upload:abort", "group": "对象写", "label": "分片上传-取消",
     "method": "POST", "path": "/api/objects/abort", "write": True},
    {"key": "object:mkdir", "group": "对象写", "label": "新建目录",
     "method": "POST", "path": "/api/objects/mkdir", "write": True},
    {"key": "object:rename", "group": "对象写", "label": "重命名 / 移动目录或对象",
     "method": "POST", "path": "/api/objects/rename", "write": True},

    # ---- 删除 ----
    {"key": "object:delete", "group": "对象删除", "label": "删除对象 / 空目录",
     "method": "DELETE", "path": "/api/objects", "write": True},

    # ---- 签名链接 ----
    {"key": "link:create", "group": "签名链接", "label": "生成签名下载链接",
     "method": "POST", "path": "/api/objects/presign", "write": True},
    {"key": "link:list", "group": "签名链接", "label": "查看签名链接列表",
     "method": "GET", "path": "/api/objects/signed-links", "write": False},
    {"key": "link:revoke", "group": "签名链接", "label": "作废签名链接（本人/管理员）",
     "method": "POST", "path": "/api/objects/signed-links/{id}/revoke", "write": True},

    # ---- Token 管理 ----
    {"key": "token:read", "group": "Token 管理", "label": "查看 token 注册表",
     "method": "GET", "path": "/api/tokens", "write": False},
    {"key": "token:write", "group": "Token 管理", "label": "生成 / 登记 token",
     "method": "POST", "path": "/api/tokens", "write": True},
    {"key": "token:delete", "group": "Token 管理", "label": "移除 token",
     "method": "DELETE", "path": "/api/tokens", "write": True},
    {"key": "token:sync", "group": "Token 管理", "label": "从 DataHub 同步 token",
     "method": "POST", "path": "/api/tokens/sync", "write": True},

    # ---- 系统 / 审计 ----
    {"key": "audit:read", "group": "系统 / 审计", "label": "查询审计日志",
     "method": "GET", "path": "/api/audit", "write": False},
]

# 公开接口（无需 token；仅展示，不可勾选）
PUBLIC_CATALOG = [
    {"key": "", "group": "公开接口", "label": "健康检查", "method": "GET", "path": "/health", "write": False},
    {"key": "", "group": "公开接口", "label": "校验 token", "method": "GET", "path": "/api/auth/check", "write": False},
    {"key": "", "group": "公开接口", "label": "获取权限目录", "method": "GET", "path": "/api/auth/permissions", "write": False},
    {"key": "", "group": "公开接口", "label": "签名链接配置上下限", "method": "GET",
     "path": "/api/objects/signed-links/config", "write": False},
    {"key": "", "group": "公开接口", "label": "网页 UI", "method": "GET", "path": "/", "write": False},
]

ALL_SCOPES = [p["key"] for p in PERMISSION_CATALOG]
SCOPE_LABELS = {p["key"]: p["label"] for p in PERMISSION_CATALOG}

# 角色预设：名称 -> {label, scopes}
ROLE_PRESETS = {
    "viewer": {
        "label": "只读浏览（列 bucket / 列目录）",
        "scopes": ["bucket:list", "object:list"],
    },
    "downloader": {
        "label": "下载方（浏览 + 探测 + 下载）",
        "scopes": ["bucket:list", "object:list", "object:head", "object:download"],
    },
    "uploader": {
        "label": "上传方（浏览 + 上传 + 建目录，含分片）",
        "scopes": ["bucket:list", "object:list", "object:head",
                   "object:upload", "upload:initiate", "upload:chunk",
                   "upload:complete", "upload:abort", "object:mkdir"],
    },
    "publisher": {
        "label": "发布方（上传 + 改名移动 + 生成签名链接）",
        "scopes": ["bucket:list", "object:list", "object:head",
                   "object:upload", "upload:initiate", "upload:chunk",
                   "upload:complete", "upload:abort", "object:mkdir",
                   "object:rename", "link:create"],
    },
    "operator": {
        "label": "运营方（发布 + 删除 + 建 bucket + 签名链接管理）",
        "scopes": ["bucket:list", "bucket:create", "object:list", "object:head",
                   "object:download", "object:upload", "upload:initiate", "upload:chunk",
                   "upload:complete", "upload:abort", "object:mkdir", "object:rename",
                   "object:delete", "link:create", "link:list", "link:revoke"],
    },
    "auditor": {
        "label": "审计只读（浏览 + 审计 + 查看 token）",
        "scopes": ["bucket:list", "object:list", "audit:read", "token:read"],
    },
    "user": {
        "label": "历史用户（兼容旧 token：上传/下载/签名/建目录/建 bucket，无删除与管理）",
        "scopes": ["bucket:list", "bucket:create", "object:list", "object:head",
                   "object:download", "object:upload", "upload:initiate", "upload:chunk",
                   "upload:complete", "upload:abort", "object:mkdir", "object:rename",
                   "link:create", "link:list", "link:revoke"],
    },
    "custom": {
        "label": "自定义（完全按勾选的接口）",
        "scopes": [],
    },
    "admin": {
        "label": "管理员（全部接口，等价共享 access_token）",
        "scopes": list(ALL_SCOPES),
    },
}

DEFAULT_ROLE = "user"


def role_scopes(role: str):
    """返回角色预设的权限点列表（未知角色返回 None）"""
    preset = ROLE_PRESETS.get((role or "").strip())
    if not preset:
        return None
    return list(preset["scopes"])


def is_valid_scope(scope: str) -> bool:
    return scope in SCOPE_LABELS
