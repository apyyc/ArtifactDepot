"""配置加载

优先级：环境变量 ARTIFACT_DEPOT_CONFIG（兼容旧 WAREHOUSE_CONFIG）指向的 JSON 文件 > resources/config.json > 内置默认值。
配置项缺省全部有默认值，服务可开箱即用。
"""
import json
import os
from pathlib import Path

# 内置默认配置
DEFAULT = {
    "depot_dir": "./depot",                    # 对象存储根目录（相对路径按项目根解析；旧键 warehouse_dir 兼容读取）
    "meta_dir": "",                            # 状态文件目录（tokens.json/audit.log/signed_links.json）
                                               # 留空 = 放在 depot_dir 下；设了则单独放该目录（相对按 depot_dir 解析）
    "datahub_url": "http://127.0.0.1:8002/api/data",  # 从 DataHub users.json 拉取用户 token
    "host": "0.0.0.0",                        # 监听地址
    "port": 8004,                             # 服务端口
    "access_token": "change-me",              # 写操作访问令牌（管理员/工具；生产必须修改）
    "max_upload_mb": 0,                       # 单文件上传上限（0 = 不限）
    "ui_enabled": True,                       # 是否启用网页 UI
    "require_read_token": False,              # 读接口（buckets/list）是否也要求 token（默认 false 保持内网公开）
    "signed_links": {                         # 签名链接上下限（count=次数，expire=时效秒数）
        "count_min": 1,
        "count_max": 10,
        "expire_min_seconds": 60,             # 最短时效（1 分钟）
        "expire_max_seconds": 604800,         # 最长时效（7 天）
    },
}

_CONFIG = None


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "config.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_config() -> dict:
    """合并配置：内置默认 < 配置文件 < 环境变量指定文件
    depot_dir 为相对路径时，按项目根（ArtifactDepot/）解析。旧键 warehouse_dir / 旧前缀 WAREHOUSE_* 兼容读取。"""
    cfg = dict(DEFAULT)
    path = os.getenv("ARTIFACT_DEPOT_CONFIG", "") or os.getenv("WAREHOUSE_CONFIG", "")
    path = path.strip()
    if path:
        cfg.update(_read_json(Path(path)))
    else:
        cfg.update(_read_json(_default_config_path()))
    # 环境变量覆盖（容器部署优先；不设则用文件/默认）
    # 新前缀 ARTIFACT_DEPOT_* 优先；旧前缀 WAREHOUSE_* 作兼容别名（新值优先）
    env_map = {
        "ARTIFACT_DEPOT_DIR": "depot_dir",
        "ARTIFACT_DEPOT_META_DIR": "meta_dir",
        "ARTIFACT_DEPOT_DATAHUB_URL": "datahub_url",
        "ARTIFACT_DEPOT_ACCESS_TOKEN": "access_token",
        "ARTIFACT_DEPOT_PORT": "port",
        "ARTIFACT_DEPOT_MAX_UPLOAD_MB": "max_upload_mb",
        # 兼容旧前缀（仅在对应新前缀未设置时生效）
        "WAREHOUSE_DIR": "depot_dir",
        "WAREHOUSE_META_DIR": "meta_dir",
        "WAREHOUSE_DATAHUB_URL": "datahub_url",
        "WAREHOUSE_ACCESS_TOKEN": "access_token",
        "WAREHOUSE_PORT": "port",
        "WAREHOUSE_MAX_UPLOAD_MB": "max_upload_mb",
    }
    for env, key in env_map.items():
        if env.startswith("WAREHOUSE_") and os.getenv("ARTIFACT_DEPOT" + env[len("WAREHOUSE"):], "").strip():
            continue  # 新前缀已设置，跳过旧值
        val = os.getenv(env, "").strip()
        if val:
            try:
                cfg[key] = int(val) if key in ("port", "max_upload_mb") else val
            except ValueError:
                pass
    # 旧配置键 warehouse_dir 兼容映射（depot_dir 优先）
    if "warehouse_dir" in cfg:
        old = cfg.pop("warehouse_dir")
        # 仅当 depot_dir 仍为内置默认值（即新键未在文件/环境中设置）时才采用旧值
        if cfg.get("depot_dir") == DEFAULT.get("depot_dir"):
            cfg["depot_dir"] = old

    # 相对路径按项目根解析（普通用户免 sudo 即可用；容器部署填绝对路径 /data/depot）
    wh = str(cfg.get("depot_dir", "")).strip()
    if wh and not os.path.isabs(wh):
        proj_root = Path(__file__).resolve().parents[2]  # …/ArtifactDepot/
        cfg["depot_dir"] = str(proj_root / wh)
    return cfg


def get_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
