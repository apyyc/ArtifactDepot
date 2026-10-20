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
import threading
from pathlib import Path

# 内置默认配置
DEFAULT = {
    "depot_dir": "./depot",                    # 对象存储根目录（相对路径按项目根解析；旧键 warehouse_dir 兼容读取）
    "meta_dir": "",                            # 状态文件目录（tokens.json/audit.log/signed_links.json）
                                               # 留空 = 放在 depot_dir 下；设了则单独放该目录（相对按 depot_dir 解析）
    "datahub_url": "http://127.0.0.1:8002/api/data",  # 从 DataHub users.json 拉取用户 token
    "host": "0.0.0.0",                        # 监听地址
    "port": 8004,                             # 服务端口
    "access_token": "",                       # 管理员/工具共享令牌；为空=无管理员（生产必须显式配置）
    "max_upload_mb": 0,                       # 单文件上传上限（0 = 不限）
    "ui_enabled": True,                       # 是否启用网页 UI
    "metrics_enabled": True,                  # 是否暴露 /metrics（Prometheus 文本格式）
    "trusted_proxies": [],                    # 可信反向代理 IP/CIDR 列表；非空时才采信 X-Forwarded-For
    "audit_max_mb": 50,                       # 审计日志单文件上限（MB），超过自动轮转
    "audit_backups": 5,                       # 审计日志保留的历史文件数
    "cors_origins": [],                       # CORS 允许来源白名单；空=不加 CORS 中间件
    "allowed_hosts": ["*"],                   # TrustedHost 白名单；["*"]=不限制
    "require_read_token": False,              # 读接口（buckets/list）是否也要求 token（默认 false 保持内网公开）
    "signed_links": {                         # 签名链接上下限（count=次数，expire=时效秒数）
        "count_min": 1,
        "count_max": 10,
        "expire_min_seconds": 60,             # 最短时效（1 分钟）
        "expire_max_seconds": 604800,         # 最长时效（7 天）
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
    "ARTIFACT_DEPOT_DATAHUB_URL": "datahub_url",
    "ARTIFACT_DEPOT_ACCESS_TOKEN": "access_token",
    "ARTIFACT_DEPOT_PORT": "port",
    "ARTIFACT_DEPOT_MAX_UPLOAD_MB": "max_upload_mb",
    # 兼容旧前缀
    "WAREHOUSE_DIR": "depot_dir",
    "WAREHOUSE_META_DIR": "meta_dir",
    "WAREHOUSE_DATAHUB_URL": "datahub_url",
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
        try:
            cfg[key] = int(val) if key in ("port", "max_upload_mb") else val
        except ValueError:
            if strict:
                raise ConfigError(f"环境变量 {env} 不是合法整数：{val!r}")

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
    return out
