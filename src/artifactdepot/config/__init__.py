# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""配置加载（config 包）

目录结构（全部配置集中在本目录）：
    src/artifactdepot/config/
    ├── __init__.py              # 本文件：加载 / 校验 / 手动重载
    ├── config.json              # 非敏感配置（会进镜像）
    ├── config.production.json   # 生产模板
    └── .env                     # 敏感配置（字段名=值，如 access_token=xxx；不进 git/镜像）

加载优先级（低 → 高）：
    内置默认 DEFAULT
      < config.json（或 ARTIFACT_DEPOT_CONFIG 指定的文件）
        < .env（只认字段名，如 access_token=、max_upload_mb=、signed_links.count_max=）
          < 真实环境变量（ARTIFACT_DEPOT_* / WAREHOUSE_*）

手动重载：由 API `POST /api/config/reload`（仅管理员）触发 reload_config()；
host / port / depot_dir / meta_dir / cors_origins / allowed_hosts 改动需重启，其余即时生效。
"""
import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path

# 内置默认配置
DEFAULT = {
    "depot_dir": "./depot",                    # 对象存储根目录（相对路径按项目根解析；旧键 warehouse_dir 兼容读取）
    "meta_dir": "",                            # 状态文件目录（tokens.json/audit.log/signed_links.json）
                                               # 留空 = 放在 depot_dir 下；设了则单独放该目录（相对按 depot_dir 解析）
    "host": "0.0.0.0",                        # 监听地址
    "port": 8004,                             # 服务端口
    "access_token": "admin",                  # 管理员/工具共享令牌；内置默认 admin（开箱即用）
                                              # 生产/公网部署必须改成 >=16 位随机串（否则启动告警，
                                              # 设 ARTIFACT_DEPOT_STRICT_CONFIG=1 会直接拒绝启动）
    "max_upload_mb": 0,                       # 单文件上传上限（0 = 不限）
    "ui_enabled": True,                       # 是否启用网页 UI
    "metrics_enabled": True,                  # 是否暴露 /metrics（Prometheus 文本格式）
    "trusted_proxies": [],                    # 可信反向代理 IP/CIDR 列表；非空时才采信 X-Forwarded-For
    "audit_max_mb": 50,                       # 审计日志单文件上限（MB），超过自动轮转
    "audit_backups": 5,                       # 审计日志保留的历史文件数
    "cors_origins": [],                       # CORS 允许来源白名单；空=不加 CORS 中间件
    "allowed_hosts": ["*"],                   # TrustedHost 白名单；["*"]=不限制
    "require_read_token": False,              # 读接口（buckets/list）是否也要求 token（默认 false 保持内网公开）
    "ui": {                                   # 网页列表分页（改后刷新页面/重新加载即生效）
        "file_page_size": 200,                # 文件列表每页条数（10-1000）
        "audit_page_size": 500,               # 审计日志单次查询条数（10-5000）
    },
    "signed_links": {                         # 签名链接上下限（count=次数，expire=时效秒数）
        "count_min": 1,
        "count_max": 10,
        "expire_min_seconds": 60,             # 最短时效（1 分钟）
        "expire_max_seconds": 604800,         # 最长时效（7 天）
        "allow_directory": True,              # 是否允许为目录生成「打包签名链接」（下载时按需打包）
    },
    "package": {                              # 目录签名链接打包（包放在 <bucket>/<目录>/.package/）
        "enabled": True,                      # 目录打包总开关（关闭后不再创建目录链接/不启动新打包）
        "max_items": 100000,                  # 单次打包 files+dirs 上限（超限视为打包失败）
        "max_total_mb": 0,                    # 单次打包源文件总大小上限（MB；0=不限）
        "max_concurrent_jobs": 1,             # 同时进行的打包任务数（其余排队）
        "zip_compression": "stored",          # stored=仅存储（快；默认）/ deflate=压缩（CPU 高）
        "cleanup_interval_seconds": 600,      # 后台清理失效包的间隔（秒）
        "cleanup_grace_seconds": 60,          # 链接失效后延迟删包的宽限期（秒，避免与在途下载竞争）
        "delete_on_revoke": True,             # 作废链接时是否立即删包（否则等后台清理）
        "cleanup_on_startup": True,           # 启动时清理失效/残留包，并恢复未完成的打包任务
    },
    "upload": {                               # 上传分片/并发/超时（改后即时生效，无需重启）
        "chunk_size_mb": 8,                   # 每片大小（1-64MB；服务端返回给前端/CLI）
        "chunk_concurrency": 4,               # 浏览器每个文件同时上传几片（1-16）
        "file_concurrency": 3,                # 浏览器同时上传几个文件（1-10；网页下拉框默认值）
        "request_timeout_seconds": 86400,     # 单次分片/合并请求超时（默认 24h）
        "session_cleanup_interval_seconds": 600,  # 后台清理过期分片会话的间隔（默认 10 分钟）
        "max_chunk_mb": 64,                   # 单片硬上限（1-64MB，受代码常量兜底）
        "max_chunks": 100000,                 # 单文件最大分片数（上限 100000）
        "max_session_gb": 200,                # 会话总量上限（max_upload_mb=0 时生效，上限 200GB）
        "session_ttl_seconds": 86400,         # 分片会话绝对有效期（默认 24h）
        "session_idle_ttl_seconds": 86400,    # 空闲清理阈值（默认 24h，且不小于请求超时）
        "staging_dir": "",                    # 伴随文件/staging 根目录（分片临时文件不再写 /tmp）
                                              # 留空 = <depot_dir>/.staging；相对路径按 depot_dir 解析；
                                              # 可填绝对路径（容器可挂独立数据卷）
    },
    "download": {                             # 下载分块/并发/超时（改后即时生效，无需重启）
        "chunk_size_mb": 8,                   # 每片大小（1-64MB；客户端按此切 Range）
        "chunk_concurrency": 4,               # 同时下载几片（1-16）
        "request_timeout_seconds": 3600,      # 单次分片请求超时（默认 1h）
    },
    "token": {                                # 新建 token 的长度约束（覆盖 API 显式创建与服务端生成）
        "min_length": 5,                      # 允许的最小字符数（1-256）
        "max_length": 30,                     # 允许的最大字符数（1-256）；自动生成时取该长度
    },
}

# 明显过弱 / 示例性的 access_token（启动时告警）
WEAK_ACCESS_TOKENS = {
    "change-me", "changeme", "y", "yes", "admin", "administrator",
    "password", "passwd", "123456", "12345678", "token", "test", "secret",
}

# 改了必须重启才能生效的项：reload 时保留旧值并在结果里提示
RESTART_REQUIRED_KEYS = ("host", "port", "depot_dir", "meta_dir", "cors_origins", "allowed_hosts")

_CONFIG = None
_CONFIG_LOCK = threading.RLock()
_ENV_WARNINGS = []


class ConfigError(Exception):
    """配置文件语法/类型错误（手动重载时用于提示，不影响正在运行的旧配置）。"""


def _config_dir() -> Path:
    return Path(__file__).resolve().parent


def _default_config_path() -> Path:
    return _config_dir() / "config.json"


def _env_path() -> Path:
    """.env 路径；可用 ARTIFACT_DEPOT_ENV 覆盖（主要给测试用）。"""
    override = os.getenv("ARTIFACT_DEPOT_ENV", "").strip()
    return Path(override) if override else _config_dir() / ".env"


def _read_json(path: Path, strict: bool = False) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        if strict:
            raise ConfigError(f"配置文件解析失败 {path}: {e}") from e
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ConfigError(f"配置文件必须是 JSON 对象：{path}")
        return {}
    return data


def _read_env(path: Path) -> dict:
    """解析 .env：KEY=VALUE，忽略空行/注释，去掉首尾引号；只认配置字段名。"""
    out = {}
    if not path or not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val != "":
            # 空值视为「未设置」，避免模板 .env 里的 access_token= 覆盖 config.json
            out[key] = val
    return out


def _coerce(default_value, raw):
    """按内置默认值的类型，把 .env 里的字符串转成正确类型。"""
    if isinstance(default_value, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default_value, int):
        return int(str(raw).strip())
    if isinstance(default_value, float):
        return float(str(raw).strip())
    if isinstance(default_value, list):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else [str(parsed)]
        except Exception:
            return [x.strip() for x in str(raw).split(",") if x.strip()]
    if isinstance(default_value, dict):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("期望 JSON 对象")
        return parsed
    return str(raw)


def _apply_env_field(cfg: dict, key: str, raw, warnings: list, source: str) -> None:
    """把 .env 的「字段名=值」写进 cfg；支持点号嵌套（signed_links.count_max）。"""
    parts = [p for p in key.split(".") if p]
    if not parts or parts[0] not in DEFAULT:
        warnings.append(f"{source} 未知配置项：{key}（已忽略）")
        return
    cur = DEFAULT
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            cur = None
            break
    try:
        val = _coerce(cur, raw)
    except Exception as e:
        warnings.append(f"{source} 配置项 {key} 类型转换失败：{e}（已忽略）")
        return
    node = cfg
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = val


# 环境变量 → 配置字段（新前缀优先；旧 WAREHOUSE_* 仅在新前缀未设置时生效）
_ENV_MAP = {
    "ARTIFACT_DEPOT_DIR": "depot_dir",
    "ARTIFACT_DEPOT_META_DIR": "meta_dir",
    "ARTIFACT_DEPOT_ACCESS_TOKEN": "access_token",
    "ARTIFACT_DEPOT_PORT": "port",
    "ARTIFACT_DEPOT_MAX_UPLOAD_MB": "max_upload_mb",
    # 网页分页（支持 .env 点号写法与真实环境变量）
    "ARTIFACT_DEPOT_UI_FILE_PAGE_SIZE": "ui.file_page_size",
    "ARTIFACT_DEPOT_UI_AUDIT_PAGE_SIZE": "ui.audit_page_size",
    # 上传分片/并发/超时（支持 .env 点号写法与真实环境变量）
    "ARTIFACT_DEPOT_UPLOAD_CHUNK_SIZE_MB": "upload.chunk_size_mb",
    "ARTIFACT_DEPOT_UPLOAD_CHUNK_CONCURRENCY": "upload.chunk_concurrency",
    "ARTIFACT_DEPOT_UPLOAD_FILE_CONCURRENCY": "upload.file_concurrency",
    "ARTIFACT_DEPOT_UPLOAD_REQUEST_TIMEOUT_SECONDS": "upload.request_timeout_seconds",
    "ARTIFACT_DEPOT_UPLOAD_SESSION_CLEANUP_INTERVAL_SECONDS": "upload.session_cleanup_interval_seconds",
    "ARTIFACT_DEPOT_UPLOAD_MAX_CHUNK_MB": "upload.max_chunk_mb",
    "ARTIFACT_DEPOT_UPLOAD_MAX_CHUNKS": "upload.max_chunks",
    "ARTIFACT_DEPOT_UPLOAD_MAX_SESSION_GB": "upload.max_session_gb",
    "ARTIFACT_DEPOT_UPLOAD_SESSION_TTL_SECONDS": "upload.session_ttl_seconds",
    "ARTIFACT_DEPOT_UPLOAD_SESSION_IDLE_TTL_SECONDS": "upload.session_idle_ttl_seconds",
    "ARTIFACT_DEPOT_UPLOAD_STAGING_DIR": "upload.staging_dir",
    # 下载分块/并发/超时（支持 .env 点号写法与真实环境变量）
    "ARTIFACT_DEPOT_DOWNLOAD_CHUNK_SIZE_MB": "download.chunk_size_mb",
    "ARTIFACT_DEPOT_DOWNLOAD_CHUNK_CONCURRENCY": "download.chunk_concurrency",
    "ARTIFACT_DEPOT_DOWNLOAD_REQUEST_TIMEOUT_SECONDS": "download.request_timeout_seconds",
    # 新建 token 长度约束（支持 .env 点号写法与真实环境变量）
    "ARTIFACT_DEPOT_TOKEN_MIN_LENGTH": "token.min_length",
    "ARTIFACT_DEPOT_TOKEN_MAX_LENGTH": "token.max_length",
    # 目录签名链接打包（支持 .env 点号写法与真实环境变量）
    "ARTIFACT_DEPOT_SIGNED_LINKS_ALLOW_DIRECTORY": "signed_links.allow_directory",
    "ARTIFACT_DEPOT_PACKAGE_ENABLED": "package.enabled",
    "ARTIFACT_DEPOT_PACKAGE_MAX_ITEMS": "package.max_items",
    "ARTIFACT_DEPOT_PACKAGE_MAX_TOTAL_MB": "package.max_total_mb",
    "ARTIFACT_DEPOT_PACKAGE_MAX_CONCURRENT_JOBS": "package.max_concurrent_jobs",
    "ARTIFACT_DEPOT_PACKAGE_ZIP_COMPRESSION": "package.zip_compression",
    "ARTIFACT_DEPOT_PACKAGE_CLEANUP_INTERVAL_SECONDS": "package.cleanup_interval_seconds",
    "ARTIFACT_DEPOT_PACKAGE_CLEANUP_GRACE_SECONDS": "package.cleanup_grace_seconds",
    "ARTIFACT_DEPOT_PACKAGE_DELETE_ON_REVOKE": "package.delete_on_revoke",
    "ARTIFACT_DEPOT_PACKAGE_CLEANUP_ON_STARTUP": "package.cleanup_on_startup",
    # 兼容旧前缀
    "WAREHOUSE_DIR": "depot_dir",
    "WAREHOUSE_META_DIR": "meta_dir",
    "WAREHOUSE_ACCESS_TOKEN": "access_token",
    "WAREHOUSE_PORT": "port",
    "WAREHOUSE_MAX_UPLOAD_MB": "max_upload_mb",
}


def _load_config_full(strict: bool = False):
    """返回 (cfg, warnings)。strict=True 时语法/类型错误抛 ConfigError。"""
    global _ENV_WARNINGS
    warnings = []
    cfg = copy.deepcopy(DEFAULT)

    # 1) 配置文件：ARTIFACT_DEPOT_CONFIG 指定 > 包内默认 config.json
    path = (os.getenv("ARTIFACT_DEPOT_CONFIG", "") or os.getenv("WAREHOUSE_CONFIG", "")).strip()
    if path:
        cfg.update(_read_json(Path(path), strict=strict))
    else:
        cfg.update(_read_json(_default_config_path(), strict=strict))

    # 2) .env（字段名；优先级高于 config.json）
    for key, val in _read_env(_env_path()).items():
        _apply_env_field(cfg, key, val, warnings, ".env")

    # 3) 真实环境变量（最高优先级）
    for env, key in _ENV_MAP.items():
        if env.startswith("WAREHOUSE_") and os.getenv("ARTIFACT_DEPOT" + env[len("WAREHOUSE"):], "").strip():
            continue  # 新前缀已设置，跳过旧值
        val = os.getenv(env, "").strip()
        if not val:
            continue
        if "." in key:
            _apply_env_field(cfg, key, val, warnings, f"环境变量 {env}")
            continue
        try:
            cfg[key] = _coerce(DEFAULT.get(key), val)
        except (TypeError, ValueError) as e:
            if strict:
                raise ConfigError(f"环境变量 {env} 类型不合法：{val!r}（{e}）") from e
            warnings.append(f"环境变量 {env} 类型不合法：{val!r}（已忽略）")

    # 旧配置键 warehouse_dir 兼容映射（depot_dir 优先）
    if "warehouse_dir" in cfg:
        old = cfg.pop("warehouse_dir")
        if cfg.get("depot_dir") == DEFAULT.get("depot_dir"):
            cfg["depot_dir"] = old

    # 相对路径按项目根解析（容器部署填绝对路径 /data/depot）
    wh = str(cfg.get("depot_dir", "")).strip()
    if wh and not os.path.isabs(wh):
        proj_root = _config_dir().parents[2]  # …/ArtifactDepot/
        cfg["depot_dir"] = str(proj_root / wh)

    _ENV_WARNINGS = warnings
    return cfg, warnings


def load_config(strict: bool = False) -> dict:
    return _load_config_full(strict=strict)[0]


def get_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


# 上传配置的合法区间（越界自动夹紧，避免把服务端/浏览器调崩）
_UPLOAD_INT_LIMITS = {
    "chunk_size_mb": (1, 64),
    "chunk_concurrency": (1, 16),
    "file_concurrency": (1, 10),
    "request_timeout_seconds": (60, 7 * 24 * 3600),
    "session_cleanup_interval_seconds": (60, 86400),
    "max_chunk_mb": (1, 64),
    "max_chunks": (1, 100000),
    "max_session_gb": (1, 200),
    "session_ttl_seconds": (60, 7 * 24 * 3600),
    "session_idle_ttl_seconds": (60, 7 * 24 * 3600),
}


def _upload_int(raw: dict, key: str, default: int) -> int:
    """读取 upload.* 整数值；非法值回退默认值，并按 _UPLOAD_INT_LIMITS 夹紧。"""
    lo, hi = _UPLOAD_INT_LIMITS[key]
    try:
        val = int(raw.get(key, default))
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def get_upload_settings() -> dict:
    """返回归一化后的上传配置（config.json 的 upload.*，缺省用内置默认值）。

    每次调用都读当前配置，因此 `POST /api/config/reload` 后立即生效，无需重启。
    对外只暴露安全范围：分片 1–64MB、并发 1–16、超时 60s–7d、会话上限 ≤200GB。
    """
    raw = get_config().get("upload")
    if not isinstance(raw, dict):
        raw = {}
    d = DEFAULT["upload"]
    settings = {key: _upload_int(raw, key, int(d[key]))
                for key in _UPLOAD_INT_LIMITS}
    # 分片大小不能超过单片硬上限
    settings["chunk_size_mb"] = min(settings["chunk_size_mb"],
                                    settings["max_chunk_mb"])
    # 空闲清理不能早于请求超时，否则可能把“仍在传但暂时没写分片”的会话删掉
    settings["session_idle_ttl_seconds"] = max(
        settings["session_idle_ttl_seconds"],
        settings["request_timeout_seconds"],
    )
    return settings


def public_upload_settings() -> dict:
    """下发给浏览器/CLI 的上传配置（字节单位 + 秒），不含任何密钥。"""
    s = get_upload_settings()
    return {
        "chunk_size": s["chunk_size_mb"] * 1024 * 1024,
        "chunk_concurrency": s["chunk_concurrency"],
        "file_concurrency": s["file_concurrency"],
        "request_timeout_seconds": s["request_timeout_seconds"],
        "max_chunk_size": s["max_chunk_mb"] * 1024 * 1024,
        "max_chunks": s["max_chunks"],
        "max_session_gb": s["max_session_gb"],
        "max_upload_mb": int(get_config().get("max_upload_mb", 0) or 0),
    }


def get_staging_dir(cfg: dict | None = None) -> Path:
    """`upload.staging_dir` 解析后的绝对路径（伴随文件/staging 根目录）。

    - 留空 → `<depot_dir>/.staging`；
    - 相对路径 → 按 `depot_dir` 解析（与 meta_dir 一致）；
    - 绝对路径 → 原样使用（容器可挂独立数据卷）。

    用途：① 无 bucket/key 的旧客户端分片会话根目录（legacy）；
    ② 启动时把 `tempfile.tempdir` 指向该目录，让 Starlette multipart 的 spool 文件
    落在数据盘而不是 RAM `/tmp`（宿主 /tmp 常是小容量 tmpfs）。
    """
    cfg = cfg if cfg is not None else get_config()
    upload = cfg.get("upload")
    raw = ""
    if isinstance(upload, dict):
        raw = str(upload.get("staging_dir", "") or "").strip()
    depot = Path(str(cfg.get("depot_dir") or "."))
    p = Path(raw) if raw else Path(".staging")
    if not p.is_absolute():
        p = depot / p
    return p


# 下载配置的合法区间（越界自动夹紧，避免把客户端/服务端调崩）
_DOWNLOAD_INT_LIMITS = {
    "chunk_size_mb": (1, 64),
    "chunk_concurrency": (1, 16),
    "request_timeout_seconds": (60, 7 * 24 * 3600),
}


def _download_int(raw: dict, key: str, default: int) -> int:
    """读取 download.* 整数值；非法值回退默认值，并按 _DOWNLOAD_INT_LIMITS 夹紧。"""
    lo, hi = _DOWNLOAD_INT_LIMITS[key]
    try:
        val = int(raw.get(key, default))
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def get_download_settings() -> dict:
    """返回归一化后的下载配置（config.json 的 download.*，缺省用内置默认值）。

    每次调用都读当前配置，因此 `POST /api/config/reload` 后立即生效，无需重启。
    对外只暴露安全范围：分片 1–64MB、并发 1–16、超时 60s–7d。
    """
    raw = get_config().get("download")
    if not isinstance(raw, dict):
        raw = {}
    d = DEFAULT["download"]
    return {key: _download_int(raw, key, int(d[key]))
            for key in _DOWNLOAD_INT_LIMITS}


def public_download_settings() -> dict:
    """下发给浏览器/CLI 的下载配置（字节单位 + 秒），不含任何密钥。"""
    s = get_download_settings()
    return {
        "chunk_size": s["chunk_size_mb"] * 1024 * 1024,
        "chunk_concurrency": s["chunk_concurrency"],
        "request_timeout_seconds": s["request_timeout_seconds"],
    }


# ---------------------------------------------------------------------------
# 目录签名链接打包（config.package.*）
# ---------------------------------------------------------------------------

_PACKAGE_INT_LIMITS = {
    "max_items": (1, 1000000),
    "max_total_mb": (0, 1048576),
    "max_concurrent_jobs": (1, 8),
    "cleanup_interval_seconds": (60, 86400),
    "cleanup_grace_seconds": (0, 86400),
}

_PACKAGE_COMPRESSIONS = ("stored", "deflate")


def _package_bool(raw: dict, key: str, default: bool) -> bool:
    val = raw.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def get_package_settings() -> dict:
    """返回归一化后的目录打包配置（config.json 的 package.*）。

    每次调用都读当前配置，因此 `POST /api/config/reload` 后立即生效。
    - 整数项按 _PACKAGE_INT_LIMITS 夹紧，非法值回退默认；
    - zip_compression 只接受 stored / deflate，其它值回退 stored；
    - 布尔项兼容 "true"/"1" 等字符串写法（手改 config.json 时更宽容）。
    """
    raw = get_config().get("package")
    if not isinstance(raw, dict):
        raw = {}
    d = DEFAULT["package"]
    out: dict = {}
    for key, (lo, hi) in _PACKAGE_INT_LIMITS.items():
        try:
            val = int(raw.get(key, d[key]))
        except (TypeError, ValueError):
            val = int(d[key])
        out[key] = max(lo, min(hi, val))
    comp = str(raw.get("zip_compression", d["zip_compression"]) or "").strip().lower()
    out["zip_compression"] = comp if comp in _PACKAGE_COMPRESSIONS else d["zip_compression"]
    for key in ("enabled", "delete_on_revoke", "cleanup_on_startup"):
        out[key] = _package_bool(raw, key, bool(d[key]))
    return out


def public_package_settings() -> dict:
    """下发给网页/客户端的目录打包配置（不含任何密钥）。"""
    s = get_package_settings()
    signed_links = get_config().get("signed_links")
    if not isinstance(signed_links, dict):
        signed_links = {}
    return {
        "enabled": s["enabled"],
        "allow_directory": _package_bool(
            signed_links, "allow_directory",
            bool(DEFAULT["signed_links"]["allow_directory"])),
        "max_items": s["max_items"],
        "max_total_mb": s["max_total_mb"],
        "max_concurrent_jobs": s["max_concurrent_jobs"],
        "zip_compression": s["zip_compression"],
        "cleanup_interval_seconds": s["cleanup_interval_seconds"],
        "cleanup_grace_seconds": s["cleanup_grace_seconds"],
        "delete_on_revoke": s["delete_on_revoke"],
    }


# 网页分页合法区间（越界夹紧；只影响前端查询条数）
_UI_INT_LIMITS = {
    "file_page_size": (10, 1000),
    "audit_page_size": (10, 5000),
}


def get_ui_settings() -> dict:
    """返回归一化后的网页分页配置（config.json 的 ui.*）。"""
    raw = get_config().get("ui")
    if not isinstance(raw, dict):
        raw = {}
    d = DEFAULT["ui"]
    out = {}
    for key, (lo, hi) in _UI_INT_LIMITS.items():
        try:
            val = int(raw.get(key, d[key]))
        except (TypeError, ValueError):
            val = int(d[key])
        out[key] = max(lo, min(hi, val))
    return out


def public_ui_settings() -> dict:
    """下发给网页的分页配置。"""
    return get_ui_settings()


# 新建 token 长度合法区间（越界夹紧；跨字段 min<=max 在写入时校验）
_TOKEN_INT_LIMITS = {
    "min_length": (1, 256),
    "max_length": (1, 256),
}


def get_token_settings() -> dict:
    """返回归一化后的 token 长度配置（config.json 的 token.*）。

    - 每次调用都读当前配置，POST /api/config 热加载后立即生效；
    - 单项越界按区间夹紧；若 min_length > max_length（只能由手工改文件造成），
      回退到内置默认 5/30，避免生成/校验逻辑反转。
    """
    raw = get_config().get("token")
    if not isinstance(raw, dict):
        raw = {}
    d = DEFAULT["token"]
    out = {}
    for key, (lo, hi) in _TOKEN_INT_LIMITS.items():
        try:
            val = int(raw.get(key, d[key]))
        except (TypeError, ValueError):
            val = int(d[key])
        out[key] = max(lo, min(hi, val))
    if out["min_length"] > out["max_length"]:
        out = {"min_length": int(d["min_length"]), "max_length": int(d["max_length"])}
    return out


def public_token_settings() -> dict:
    """下发给网页/CLI 的新建 token 长度上下限。"""
    return get_token_settings()


def reload_config() -> dict:
    """手动重载配置：重新读 config.json / .env / 环境变量。

    返回：{ok, error, changed, needs_restart, warnings}
    - changed：已即时生效的字段
    - needs_restart：值变了但必须重启的字段（保留旧值）
    - 语法/类型错误：ok=False，保留旧配置不动
    """
    global _CONFIG
    old = get_config()
    try:
        new, warnings = _load_config_full(strict=True)
    except ConfigError as e:
        return {"ok": False, "error": str(e), "changed": [],
                "needs_restart": [], "warnings": []}
    changed, needs_restart = [], []
    merged = copy.deepcopy(old)
    for key, new_val in new.items():
        if key.startswith("_"):
            continue
        if old.get(key) != new_val:
            if key in RESTART_REQUIRED_KEYS:
                needs_restart.append(key)
            else:
                merged[key] = new_val
                changed.append(key)
    # staging_dir 只在启动时设置 tempfile.tempdir；热加载改它需要重启才完整生效
    # （新会话已按新值落盘，但框架级 spool 仍指向旧目录），这里只提示不强制。
    def _staging_of(c: dict) -> str:
        up = c.get("upload")
        return str(up.get("staging_dir", "") or "").strip() if isinstance(up, dict) else ""
    if _staging_of(old) != _staging_of(new):
        needs_restart.append("upload.staging_dir")
    _CONFIG = merged
    return {"ok": True, "error": "", "changed": sorted(changed),
            "needs_restart": sorted(needs_restart), "warnings": warnings}


def config_warnings(cfg: dict | None = None) -> list:
    """启动时配置体检：返回需要提醒的问题列表（不抛异常）。"""
    cfg = cfg if cfg is not None else get_config()
    out = list(_ENV_WARNINGS)
    tok = str(cfg.get("access_token", "") or "").strip()
    if not tok:
        out.append("access_token 为空：管理/写接口将返回 401；"
                   "请在 config/.env 里写 access_token=...（或设置环境变量 ARTIFACT_DEPOT_ACCESS_TOKEN）")
    elif tok.lower() in WEAK_ACCESS_TOKENS or len(tok) < 12:
        out.append(f"access_token 过弱（长度 {len(tok)}）：生产环境请改用 >=16 位随机串")
    try:
        sl = cfg.get("signed_links") or {}
        cmin = int(sl.get("count_min", 1) or 1)
        cmax = int(sl.get("count_max", 10) or 10)
        emin = int(sl.get("expire_min_seconds", 60) or 60)
        emax = int(sl.get("expire_max_seconds", 604800) or 604800)
        if cmin < 1 or cmax < cmin:
            out.append("signed_links.count_min/count_max 配置不合法")
        if emin < 1 or emax < emin:
            out.append("signed_links.expire_min_seconds/expire_max_seconds 配置不合法")
    except (TypeError, ValueError):
        out.append("signed_links 配置项不是整数")
    # 目录打包配置：压缩方式必须可识别（非法值会回退 stored，但启动时提醒）
    pkg_cfg = cfg.get("package")
    if isinstance(pkg_cfg, dict):
        comp = str(pkg_cfg.get("zip_compression", "stored") or "").strip().lower()
        if comp not in _PACKAGE_COMPRESSIONS:
            out.append("package.zip_compression 只能是 stored 或 deflate（当前值已回退 stored）")
    # staging 目录（伴随文件落盘位置）不可写 → 分片会话/框架 spool 都会失败，启动前提示
    try:
        staging = get_staging_dir(cfg)
        probe = staging if staging.exists() else staging.parent
        if probe.exists() and not os.access(probe, os.W_OK):
            out.append(f"upload.staging_dir 不可写：{staging}（分片临时文件将无法落盘，请检查目录权限）")
    except (OSError, ValueError) as e:
        out.append(f"upload.staging_dir 无法访问：{e}")
    # 新建 token 长度配置（min<=max 才有意义）
    try:
        tk = cfg.get("token") or {}
        tmin = int(tk.get("min_length", 5))
        tmax = int(tk.get("max_length", 30))
        if tmin < 1 or tmax < 1 or tmin > tmax:
            out.append("token.min_length/token.max_length 配置不合法（需 1 <= min <= max）")
    except (TypeError, ValueError):
        out.append("token 配置项不是整数")
    return out


# ---------------------------------------------------------------------------
# 网页「配置」标签页：配置项元数据 / 快照 / 写回 config.json + 热加载
# ---------------------------------------------------------------------------

CONFIG_GROUPS = ("基础", "网络与安全", "界面", "上传", "下载", "签名链接",
                 "目录打包", "系统/审计", "令牌")

_MASKED_SECRET = "********"

# 每个配置项的中文名 / 分组 / 控件类型 / 说明；DEFAULT 是字段范围与默认值的唯一真源。
# type: string | path | int | bool | list | secret
_CONFIG_FIELD_META = {
    "depot_dir": {"label": "仓库根目录", "group": "基础", "type": "path", "restart": True,
                  "desc": "对象存储根目录；相对路径按项目根解析"},
    "meta_dir": {"label": "状态文件目录", "group": "基础", "type": "path", "restart": True,
                 "desc": "tokens.json / audit.log / signed_links.json 目录；留空=放在 depot_dir 下"},
    "host": {"label": "监听地址", "group": "网络与安全", "type": "string", "restart": True},
    "port": {"label": "监听端口", "group": "网络与安全", "type": "int", "restart": True,
             "min": 1, "max": 65535},
    "access_token": {"label": "管理员共享令牌", "group": "网络与安全", "type": "secret",
                     "desc": "留空表示保持现有值；修改后请用新令牌继续操作"},
    "max_upload_mb": {"label": "单文件上传上限（MB）", "group": "上传", "type": "int",
                      "min": 0, "max": 1048576, "desc": "0=不限；分片会话总量也受此上限约束"},
    "trusted_proxies": {"label": "可信代理 IP/CIDR", "group": "网络与安全", "type": "list",
                        "desc": "仅这些代理的 X-Forwarded-For 会被采信；逗号分隔"},
    "cors_origins": {"label": "CORS 允许来源", "group": "网络与安全", "type": "list", "restart": True,
                     "desc": "空=不启用 CORS 中间件；逗号分隔"},
    "allowed_hosts": {"label": "TrustedHost 白名单", "group": "网络与安全", "type": "list",
                      "restart": True, "desc": "[\"*\"]=不限制；逗号分隔"},
    "require_read_token": {"label": "读接口也要求令牌", "group": "网络与安全", "type": "bool",
                           "desc": "开启后 buckets/list 也需要有效 token"},
    "ui_enabled": {"label": "启用网页 UI", "group": "界面", "type": "bool"},
    "ui.file_page_size": {"label": "文件列表每页条数", "group": "界面", "type": "int",
                          "min": 10, "max": 1000, "desc": "网页「加载更多」每次请求条数"},
    "ui.audit_page_size": {"label": "审计单次查询条数", "group": "界面", "type": "int",
                           "min": 10, "max": 5000},
    "token.min_length": {"label": "Token 最小长度", "group": "令牌", "type": "int",
                          "min": 1, "max": 256,
                          "desc": "新建 token 允许的最小字符数（默认 5）"},
    "token.max_length": {"label": "Token 最大长度", "group": "令牌", "type": "int",
                          "min": 1, "max": 256,
                          "desc": "新建 token 允许的最大字符数（默认 30）；服务端自动生成时取该长度"},
    "signed_links.count_min": {"label": "签名链接最少次数", "group": "签名链接", "type": "int",
                               "min": 1, "max": 100000},
    "signed_links.count_max": {"label": "签名链接最多次数", "group": "签名链接", "type": "int",
                               "min": 1, "max": 100000},
    "signed_links.expire_min_seconds": {"label": "签名链接最短时效（秒）", "group": "签名链接",
                                        "type": "int", "min": 1, "max": 31536000},
    "signed_links.expire_max_seconds": {"label": "签名链接最长时效（秒）", "group": "签名链接",
                                        "type": "int", "min": 1, "max": 31536000},
    "signed_links.allow_directory": {"label": "允许目录打包链接", "group": "签名链接",
                                     "type": "bool",
                                     "desc": "开启后可对目录生成签名链接；关闭后目录链接返回 403"},
    "package.enabled": {"label": "启用目录打包", "group": "目录打包", "type": "bool",
                        "desc": "总开关；关闭后不再创建目录链接、不启动新的打包任务"},
    "package.max_items": {"label": "单次打包最大项数", "group": "目录打包", "type": "int",
                          "desc": "目录内 files + dirs 超过该值视为打包失败（默认 100000）"},
    "package.max_total_mb": {"label": "单次打包源文件上限（MB）", "group": "目录打包",
                             "type": "int",
                             "desc": "0=不限；打包会在源目录内复制一份，建议按磁盘余量设置"},
    "package.max_concurrent_jobs": {"label": "同时打包任务数", "group": "目录打包",
                                    "type": "int", "desc": "超出的任务排队等待"},
    "package.zip_compression": {"label": "ZIP 压缩方式", "group": "目录打包", "type": "string",
                                "desc": "stored=仅存储（快，默认）/ deflate=压缩（更小但更耗 CPU）"},
    "package.cleanup_interval_seconds": {"label": "失效包清理间隔（秒）", "group": "目录打包",
                                         "type": "int"},
    "package.cleanup_grace_seconds": {"label": "失效后删包宽限期（秒）", "group": "目录打包",
                                      "type": "int",
                                      "desc": "链接失效后再等待这么久才删包，避免与在途下载竞争"},
    "package.delete_on_revoke": {"label": "作废时立即删包", "group": "目录打包", "type": "bool",
                                 "desc": "关闭则等到后台清理周期再删"},
    "package.cleanup_on_startup": {"label": "启动时清理/恢复", "group": "目录打包", "type": "bool",
                                   "desc": "启动时清理失效包与残留半成品，并恢复未完成的打包任务"},
    "upload.chunk_size_mb": {"label": "分片大小（MB）", "group": "上传", "type": "int"},
    "upload.chunk_concurrency": {"label": "每文件分片并发", "group": "上传", "type": "int"},
    "upload.file_concurrency": {"label": "同时上传文件数", "group": "上传", "type": "int"},
    "upload.request_timeout_seconds": {"label": "分片请求超时（秒）", "group": "上传", "type": "int"},
    "upload.session_cleanup_interval_seconds": {"label": "分片会话清理间隔（秒）", "group": "上传",
                                                "type": "int"},
    "upload.max_chunk_mb": {"label": "单片硬上限（MB）", "group": "上传", "type": "int"},
    "upload.max_chunks": {"label": "单文件最大分片数", "group": "上传", "type": "int"},
    "upload.max_session_gb": {"label": "会话总量上限（GB）", "group": "上传", "type": "int"},
    "upload.session_ttl_seconds": {"label": "会话绝对有效期（秒）", "group": "上传", "type": "int"},
    "upload.session_idle_ttl_seconds": {"label": "会话空闲清理（秒）", "group": "上传", "type": "int"},
    "upload.staging_dir": {"label": "staging 目录", "group": "上传", "type": "path", "restart": True,
                            "desc": "留空=<depot_dir>/.staging；改后需重启才完整生效"},
    "download.chunk_size_mb": {"label": "下载分片大小（MB）", "group": "下载", "type": "int"},
    "download.chunk_concurrency": {"label": "下载分片并发", "group": "下载", "type": "int"},
    "download.request_timeout_seconds": {"label": "下载请求超时（秒）", "group": "下载", "type": "int"},
    "metrics_enabled": {"label": "启用 /metrics", "group": "系统/审计", "type": "bool"},
    "audit_max_mb": {"label": "审计单文件上限（MB）", "group": "系统/审计", "type": "int",
                     "min": 0, "max": 10240},
    "audit_backups": {"label": "审计历史文件保留数", "group": "系统/审计", "type": "int",
                      "min": 1, "max": 100},
}


def _infer_field_type(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, list):
        return "list"
    return "string"


def _field_limits(path: str, meta: dict):
    """返回整数字段的 (min, max)；优先用 upload/download/ui 的夹紧区间。"""
    for prefix, table in (("upload.", _UPLOAD_INT_LIMITS),
                          ("download.", _DOWNLOAD_INT_LIMITS),
                          ("package.", _PACKAGE_INT_LIMITS),
                          ("ui.", _UI_INT_LIMITS)):
        if path.startswith(prefix):
            key = path[len(prefix):]
            if key in table:
                return table[key]
    return meta.get("min"), meta.get("max")


def config_schema() -> list[dict]:
    """返回全部可编辑配置项（顺序与 DEFAULT 一致，供网页「配置」标签页渲染）。"""
    fields: list[dict] = []

    def walk(prefix: str, node: dict) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(path, value)
                continue
            meta = _CONFIG_FIELD_META.get(path, {})
            ftype = meta.get("type") or _infer_field_type(value)
            item = {
                "key": path,
                "label": meta.get("label", path),
                "group": meta.get("group") or _group_for_path(path),
                "type": ftype,
                "default": "" if ftype == "secret" else value,
                "restart": bool(meta.get("restart", path in RESTART_REQUIRED_KEYS)),
                "description": meta.get("desc", ""),
            }
            if ftype == "int":
                lo, hi = _field_limits(path, meta)
                item["min"], item["max"] = lo, hi
            fields.append(item)

    walk("", DEFAULT)
    return fields


def _group_for_path(path: str) -> str:
    if path.startswith("upload."):
        return "上传"
    if path.startswith("download."):
        return "下载"
    if path.startswith("signed_links."):
        return "签名链接"
    if path.startswith("package."):
        return "目录打包"
    if path.startswith("ui."):
        return "界面"
    return "基础"


def get_config_file_path() -> Path:
    """当前生效的 config.json 路径（ARTIFACT_DEPOT_CONFIG 优先）。"""
    override = (os.getenv("ARTIFACT_DEPOT_CONFIG", "")
                or os.getenv("WAREHOUSE_CONFIG", "")).strip()
    return Path(override) if override else _default_config_path()


def _flatten_config(node, prefix: str = "") -> dict:
    out = {}
    if not isinstance(node, dict):
        return out
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten_config(value, path))
        else:
            out[path] = value
    return out


def _set_config_path(node: dict, path: str, value) -> None:
    parts = [p for p in path.split(".") if p]
    cur = node
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _is_secret_key(key: str) -> bool:
    low = str(key).lower()
    return any(word in low for word in ("token", "secret", "password", "passwd"))


def _mask_config(node):
    """递归脱敏 access_token 等密钥字段（返回值可安全下发给已认证管理员页面）。"""
    if isinstance(node, dict):
        return {k: (_MASKED_SECRET if isinstance(v, str) and v and _is_secret_key(k)
                    else _mask_config(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_config(v) for v in node]
    return node


def _env_override_keys() -> set:
    """真实环境变量实际覆盖的配置项集合（新前缀优先于旧 WAREHOUSE_*）。"""
    out = set()
    for env, key in _ENV_MAP.items():
        if env.startswith("WAREHOUSE_"):
            new_env = "ARTIFACT_DEPOT" + env[len("WAREHOUSE"):]
            if os.getenv(new_env, "").strip():
                continue
        if os.getenv(env, "").strip():
            out.add(key)
    return out


def _dotenv_keys() -> set:
    return {str(k).strip() for k in _read_env(_env_path()) if str(k).strip()}


def _config_sources(file_flat: dict | None = None) -> dict:
    """每个 schema 字段的取值来源：environment / .env / config.json / default。"""
    if file_flat is None:
        try:
            file_flat = _flatten_config(_read_json(get_config_file_path()))
        except ConfigError:
            file_flat = {}
    env_keys = _env_override_keys()
    dotenv_keys = _dotenv_keys()
    sources = {}
    for field in config_schema():
        key = field["key"]
        if key in env_keys:
            sources[key] = "environment"
        elif key in dotenv_keys:
            sources[key] = ".env"
        elif key in file_flat:
            sources[key] = "config.json"
        else:
            sources[key] = "default"
    return sources


def config_snapshot() -> dict:
    """给网页「配置」标签页的快照：文件值、生效值、来源、需重启项、告警。

    - `values` 是 config.json 里的值（编辑基准，密钥脱敏）；
    - `effective` 是当前进程实际生效的值（含 .env / 环境变量覆盖，密钥脱敏）；
    - `sources` 标明每个字段最终来自哪里，被 .env/环境变量覆盖的字段改 config.json 不会生效。
    """
    path = get_config_file_path()
    error = ""
    try:
        file_cfg = _read_json(path, strict=True)
    except ConfigError as e:
        file_cfg = {}
        error = str(e)
    if not isinstance(file_cfg, dict):
        file_cfg = {}
    schema = config_schema()
    file_flat = _flatten_config(file_cfg)
    sources = _config_sources(file_flat)
    overridden = sorted(k for k, v in sources.items() if v in (".env", "environment"))
    restart_required = sorted(f["key"] for f in schema if f.get("restart"))
    return {
        "path": str(path),
        "exists": path.is_file(),
        "error": error,
        "groups": list(CONFIG_GROUPS),
        "schema": schema,
        "values": _mask_config(file_cfg),
        "effective": _mask_config(get_config()),
        "sources": sources,
        "overridden": overridden,
        "restart_required": restart_required,
        "warnings": config_warnings(),
    }


def _coerce_config_value(field: dict, raw):
    key, typ = field["key"], field["type"]
    label = field.get("label") or key
    if raw is None:
        raise ConfigError(f"{label} 不能为 null")
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise ConfigError(f"{label} 必须是布尔值（true/false）")
    if typ == "int":
        if isinstance(raw, bool):
            raise ConfigError(f"{label} 必须是整数")
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{label} 必须是整数：{raw!r}") from e
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and val < lo:
            raise ConfigError(f"{label} 不能小于 {lo}")
        if hi is not None and val > hi:
            raise ConfigError(f"{label} 不能大于 {hi}")
        return val
    if typ == "list":
        if isinstance(raw, (dict, bool)):
            raise ConfigError(f"{label} 必须是字符串列表")
        if isinstance(raw, list):
            if any(isinstance(x, (dict, list)) for x in raw):
                raise ConfigError(f"{label} 的列表项必须是字符串")
            items = [str(x).strip() for x in raw]
        else:
            items = [x.strip() for x in re.split(r"[,，\n]", str(raw))]
        return [x for x in items if x]
    if typ in ("string", "path"):
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ConfigError(f"{label} 必须是字符串")
        val = str(raw).strip()
        if key == "depot_dir" and not val:
            raise ConfigError("仓库根目录不能为空")
        return val
    if typ == "secret":
        return str(raw).strip()
    raise ConfigError(f"{label} 的类型不支持在线编辑：{typ}")


def _validate_merged_config(cfg: dict) -> None:
    """跨字段校验（单字段范围已由 schema 校验）。"""
    sl = cfg.get("signed_links")
    if isinstance(sl, dict):
        try:
            cmin = int(sl.get("count_min", 1))
            cmax = int(sl.get("count_max", 10))
            emin = int(sl.get("expire_min_seconds", 60))
            emax = int(sl.get("expire_max_seconds", 604800))
        except (TypeError, ValueError) as e:
            raise ConfigError("signed_links 配置项必须是整数") from e
        if cmax < cmin:
            raise ConfigError("签名链接最多次数不能小于最少次数")
        if emax < emin:
            raise ConfigError("签名链接最长时效不能小于最短时效")
    pkg = cfg.get("package")
    if isinstance(pkg, dict):
        comp = str(pkg.get("zip_compression", "stored") or "").strip().lower()
        if comp not in _PACKAGE_COMPRESSIONS:
            raise ConfigError("package.zip_compression 只能是 stored 或 deflate")
    up = cfg.get("upload")
    if isinstance(up, dict):
        try:
            chunk = int(up.get("chunk_size_mb", 8))
            max_chunk = int(up.get("max_chunk_mb", 64))
        except (TypeError, ValueError) as e:
            raise ConfigError("upload.chunk_size_mb / max_chunk_mb 必须是整数") from e
        if chunk > max_chunk:
            raise ConfigError("分片大小不能大于单片硬上限（upload.max_chunk_mb）")
    tk = cfg.get("token")
    if isinstance(tk, dict):
        try:
            tmin = int(tk.get("min_length", 5))
            tmax = int(tk.get("max_length", 30))
        except (TypeError, ValueError) as e:
            raise ConfigError("token.min_length / token.max_length 必须是整数") from e
        if tmin < 1 or tmax < 1:
            raise ConfigError("Token 长度必须大于 0")
        if tmin > tmax:
            raise ConfigError("Token 最小长度不能大于最大长度")


def _write_config_text(path: Path, text: str) -> None:
    """原子写 config.json（0600；含密钥，不能留下半截文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def update_config_file(values: dict) -> dict:
    """把网页提交的值写回 config.json，并调用 reload_config() 热加载。

    - 只接受 schema 中存在的字段（未知字段拒绝，避免拼错键静默无效）；
    - `access_token` 留空 / 提交脱敏占位符 = 保持原值；
    - 写盘失败、重载失败会回滚到原文件并抛 ConfigError；
    - 返回 {ok, changed, needs_restart, warnings, overridden, path}。

    并发安全：进程内用 `_CONFIG_LOCK` 串行化「读文件-合并-写文件-重载」，
    避免两个并发保存各自基于旧文件合并、后写覆盖先写（丢失更新）。
    单进程部署假设与上传会话一致；多进程部署需外部锁/单实例。
    """
    with _CONFIG_LOCK:
        return _update_config_file_locked(values)


def _update_config_file_locked(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ConfigError("values 必须是对象")
    path = get_config_file_path()
    schema = {f["key"]: f for f in config_schema()}
    try:
        file_cfg = _read_json(path, strict=True)
    except ConfigError as e:
        raise ConfigError(f"当前配置文件无法解析，拒绝写入：{e}") from e
    if not isinstance(file_cfg, dict):
        file_cfg = {}
    if not path.is_file():
        # 配置文件缺失时以“当前生效配置”为基线，避免只提交一个字段就把管理员令牌等
        # 其它配置重置成默认值（尤其是 access_token 会导致页面立刻失去管理权限）。
        file_cfg = copy.deepcopy(get_config())

    merged = copy.deepcopy(file_cfg)
    written = []
    for key, raw in values.items():
        field = schema.get(key)
        if field is None:
            raise ConfigError(f"未知配置项：{key}")
        if field["type"] == "secret":
            if raw is None or str(raw).strip() in ("", _MASKED_SECRET):
                continue
            val = _coerce_config_value(field, raw)
        else:
            val = _coerce_config_value(field, raw)
        _set_config_path(merged, key, val)
        written.append(key)

    _validate_merged_config(merged)
    old_text = path.read_text(encoding="utf-8") if path.is_file() else None
    new_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    _write_config_text(path, new_text)
    result = reload_config()
    if not result.get("ok"):
        # 写盘成功但重载失败：回滚旧文件，避免下次重启才炸
        if old_text is None:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            _write_config_text(path, old_text)
        reload_config()
        raise ConfigError("配置已回滚（新配置无法加载）：" + (result.get("error") or "未知错误"))

    sources = _config_sources(_flatten_config(merged))
    overridden = sorted(k for k in written
                        if sources.get(k) in (".env", "environment"))
    return {
        "ok": True,
        "changed": result.get("changed", []),
        "needs_restart": result.get("needs_restart", []),
        "warnings": result.get("warnings", []),
        "overridden": overridden,
        "path": str(path),
    }
