# ArtifactDepot 架构说明

> 版本 **0.5.5**

## 定位

ArtifactDepot 是一个**对象存储 / 工件仓库**站点（不是传统意义的"数据仓库/OLAP"）。
它负责收存处理产物（视频、文件等），按 **S3 模型**提供标准接口：bucket/key 命名、
上传、列表、下载（Range）、删除、预签名 URL，并提供网页浏览。

它不承担业务逻辑，也不存协作平台的 JSON 数据（那是 DataHub 的事）。

## 拓扑

```
处理端（carryVideo server.py，跑在 8 或本地）
    │  处理完成后 POST /api/objects 推送（token 认证）
    ▼
ArtifactDepot（独立 FastAPI 服务，端口 8004）
    │  文件落盘 <depot_dir>/<bucket>/<key>
    ├─ GET  /api/objects/list      ← ProjectCollab"任务数据"模块 / 网页 UI
    ├─ GET  /api/objects/download  ← 下载（Range 206，视频可拖动）
    ├─ GET  /                      网页浏览
    ▼
用户浏览器
```

### Token 同步链路（DataHub 为权威源）

```
DataHub（collab 容器 :8002，users.json）── 0.5.0 起必须绑 0.0.0.0
    │  GET {datahub_url}/users.json（触发：启动 / 写操作惰性 60s / 手动 /api/tokens/sync）
    ▼
ArtifactDepot 合并写回 <meta_dir>/tokens.json（只增不删；DataHub 不可达沿用本地缓存）
    │  token → 用户名 解析（auth.py）
    ▼
审计 actor / 权限判定
```

> **生产可达性关键**：DW 容器用 **host 网络**（容器内 `127.0.0.1` = 宿主机），
> `datahub_url` 写 `http://127.0.0.1:8002/api/data` 即可直连同机 collab 的 DataHub。
> 桥接网络（rootless pasta）下容器连宿主发布端口常不通，是本服务跨机部署失败的主因。

## 目录结构

```
ArtifactDepot/
├── README.md / CHANGELOG.md
├── config.production.json        # 生产可复制的配置模板
├── check_dw_sync.sh              # 生产诊断（仅 python3）
├── docs/architecture.md          # 本文档
├── scripts/start.sh              # 本地启动脚本
├── build_image.sh                # 构建 + 自动导出 tar
├── deploy_container.sh           # 部署（host 网络默认 + --config/VOLUME_MAPS）
└── src/artifactdepot/
    ├── main.py                 # FastAPI 入口 + lifespan（确保仓库根存在）
    ├── config.py               # 配置加载（ARTIFACT_DEPOT_CONFIG env > resources/config.json > 默认；meta_dir）
    ├── storage.py              # 对象存储引擎：路径安全 / put/list/delete / manifest / 预签名 / 签名链接注册表 / token 注册表 / 审计 / DataHub 同步 / meta_dir
    ├── auth.py                 # 写操作 token 校验（query / Bearer）
    ├── api/
    │   ├── objects.py          # S3-like 对象 API
    │   └── system.py           # /health /api/buckets /api/tokens /api/audit
    ├── web/ui.py               # 网页 UI
    └── resources/
        ├── config.json         # 运行配置
        └── index.html          # 单页浏览界面
```

## 存储布局

```
<depot_dir>/                  # 默认 ./warehouse（项目内）；容器部署用 /data/depot 卷
├── <bucket>/                     # bucket = 项目（如 project_1）
│   ├── task_5/xxx.mp4            # key = 任务/文件名（扁平路径式命名）
│   └── .depot.json           # 每 bucket 元数据清单（隐藏，列表跳过）
└── …

<meta_dir>/（可选；默认放 depot_dir 根下，0.5.0 起可用 meta_dir 单独放）
├── tokens.json                   # token→用户名 注册表（DataHub 同步副本）
├── signed_links.json             # 签名链接注册表
└── audit.log                     # 审计日志（JSONL 追加）
```

- **文件系统是唯一事实源**：列表以目录扫描为准，手工放入的文件也能列出；
  `.depot.json` 清单只做元数据补充（size/sha256/mtime/source_url）。
- 隐藏点文件（`.` 开头）不在列表展示、不可作为上传 key，防止覆盖清单。
- **`meta_dir`**：`tokens.json` / `signed_links.json` / `audit.log` 是**可再生状态**，
  与业务文件（bucket）分层存放，便于隔离与备份；留空时保持旧行为在仓库根下。

## 配置优先级

```
内置默认 < resources/config.json（或 ARTIFACT_DEPOT_CONFIG 指定文件）< 环境变量
```

- 支持环境变量：`ARTIFACT_DEPOT_DIR` / `ARTIFACT_DEPOT_META_DIR` / `ARTIFACT_DEPOT_DATAHUB_URL` /
  `ARTIFACT_DEPOT_ACCESS_TOKEN` / `ARTIFACT_DEPOT_PORT` / `ARTIFACT_DEPOT_MAX_UPLOAD_MB`
- **镜像内不得烤死 `ARTIFACT_DEPOT_DATAHUB_URL` 默认值**：环境变量优先级高于配置文件，
  烤死的 127.0.0.1 会盖掉挂载 config.json 的 `datahub_url`（0.5.0 已从 Dockerfile 移除）。

## API 契约（S3 对应）

| 方法 | 路径 | 说明 | 对应 S3 |
|---|---|---|---|
| POST | `/api/objects` | multipart 上传（bucket/key/token/source_url/overwrite） | PutObject |
| POST | `/api/objects/mkdir` | 新建目录（key 可含 `/` 多级） | - |
| GET | `/api/objects/list` | 列对象（bucket/prefix） | ListObjects |
| GET | `/api/objects/download` | 下载，Range 206，需 token（签名链接免） | GetObject |
| DELETE | `/api/objects` | 删除对象/目录（仅管理员；目录仅空可删） | DeleteObject |
| POST | `/api/objects/presign` | 生成签名链接（count/time/permanent） | Presigned URL |
| GET/POST/DELETE | `/api/tokens` | Token 注册表管理（管理员） | - |
| POST | `/api/tokens/sync` | 从 DataHub 拉取并合并用户 token（返回 ok/error） | - |
| GET | `/api/buckets` | 列 bucket | ListBuckets |
| GET | `/api/audit` | 审计查询（管理员） | - |
| GET | `/health` | 健康检查 | - |
| GET | `/` | 网页 UI | - |

写操作（上传/删除/预签名/mkdir）需有效 token（`?token=` 或 `Authorization: Bearer`）。
读操作（列表/下载）默认内网开放、下载需 token；也可用签名链接限时/限次访问。

## 安全要点

- 路径穿越防护：bucket 名校验 + key 拒绝绝对路径/`..`/空段/反斜杠/点前缀，解析后校验落在仓库根内。
- 上传上限：`max_upload_mb`（0 不限）。
- 写令牌：UTF-8 字节常量时间比较（hmac.compare_digest，非 ASCII 安全）。
- 预签名：HMAC-SHA256(secret=access_token, bucket|key|expires)，过期即拒。
- 删除：仅管理员共享 token；目录仅空可删（防误删）。

## 部署

- **build → save → 拷贝 → load → deploy**：
  - 开发机 `./build_image.sh`（构建 + 自动导出 `artifactdepot-0.5.5.tar`）
  - 拷贝 tar 到生产机 → `podman load -i` → `./deploy_container.sh --token y`
- **host 网络默认**：容器共享宿主网络，`datahub_url` 用 `127.0.0.1:8002` 直连同机 DataHub；
  `--port-map` 可改回端口映射（不推荐，rootless 桥接常不通）。
- **config.json 挂载**：`deploy_container.sh` 自动探测 `~/SERVER/artifactdepot/config/config.json`，
  也可用 `--config <宿主机路径>` 显式挂到容器内 `resources/config.json`，生产配置显式可改、无需重建镜像。
- 数据卷默认 `~/SERVER/artifactdepot/artifactdepot_0.6.1/warehouse` → 容器 `/data/depot`；
  若该路径不可写，脚本自动回退到 `~/.local/share/artifactdepot/...`。
- **自启**：Podman 走 `systemd --user`；Docker 走 `--restart=always`。
- 排障：`bash check_dw_sync.sh` 逐段定位 token 同步链路。
- 可选 Nginx `location /depot/` 反代到 8004。
- carryVideo `server.py` 处理完推送 + ProjectCollab"任务数据"子模块对接，见 README 集成指引。

