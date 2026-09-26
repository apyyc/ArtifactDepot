"""配置加载

优先级：环境变量 WAREHOUSE_CONFIG 指向的 JSON 文件 > resources/config.json > 内置默认值。
配置项缺省全部有默认值，服务可开箱即用。
"""
import json
import os
from pathlib import Path

# 内置默认配置
DEFAULT = {
    "warehouse_dir": "./warehouse",           # 对象存储根目录（相对路径按项目根解析）
    "meta_dir": "",                            # 状态文件目录（tokens.json/audit.log/signed_links.json）
                                               # 留空 = 放在 warehouse_dir 下；设了则单独放该目录（相对按 warehouse_dir 解析）
    "datahub_url": "http://127.0.0.1:8002/api/data",  # 从 DataHub users.json 拉取用户 token
    "host": "0.0.0.0",                        # 监听地址
    "port": 8004,                             # 服务端口
    "access_token": "change-me",              # 写操作访问令牌（管理员/工具；生产必须修改）
    "max_upload_mb": 0,                       # 单文件上传上限（0 = 不限）
    "ui_enabled": True,                       # 是否启用网页 UI
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
    warehouse_dir 为相对路径时，按项目根（DataWarehouse/）解析。"""
    cfg = dict(DEFAULT)
    path = os.getenv("WAREHOUSE_CONFIG", "").strip()
    if path:
        cfg.update(_read_json(Path(path)))
    else:
        cfg.update(_read_json(_default_config_path()))
    # 环境变量覆盖（容器部署优先；不设则用文件/默认）
    env_map = {
        "WAREHOUSE_DIR": "warehouse_dir",
        "WAREHOUSE_META_DIR": "meta_dir",
        "WAREHOUSE_DATAHUB_URL": "datahub_url",
        "WAREHOUSE_ACCESS_TOKEN": "access_token",
        "WAREHOUSE_PORT": "port",
        "WAREHOUSE_MAX_UPLOAD_MB": "max_upload_mb",
    }
    for env, key in env_map.items():
        val = os.getenv(env, "").strip()
        if val:
            try:
                cfg[key] = int(val) if key in ("port", "max_upload_mb") else val
            except ValueError:
                pass
    # 相对路径按项目根解析（普通用户免 sudo 即可用；容器部署填绝对路径 /data/warehouse）
    wh = str(cfg.get("warehouse_dir", "")).strip()
    if wh and not os.path.isabs(wh):
        proj_root = Path(__file__).resolve().parents[2]  # …/DataWarehouse/
        cfg["warehouse_dir"] = str(proj_root / wh)
    return cfg


def get_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
