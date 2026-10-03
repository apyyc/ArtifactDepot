# ArtifactDepot — 对象存储仓库站点

> 版本 0.7.1，端口 8004，独立服务

**一句话**：按行业标准（对象存储 / S3 模型）设计的文件仓库站点，收存处理产物（视频 / 文件），提供上传、列表、Range 下载、删除、新建目录、签名链接与审计，并带网页界面。业务 JSON 数据在 DataHub，**本服务不承担业务逻辑**。

---

## 目录

- [核心特性](#核心特性)
- [架构与工作流](#架构与工作流)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [目录结构](#目录结构)
- [核心模块](#核心模块)
- [API 参考](#api-参考)
- [模板与整洁架构规范](#模板与整洁架构规范)
- [常见问题](#常见问题)
- [许可证与支持](#许可证与支持)

---

## 核心特性

| 类别 | 能力 |
|---|---|
| 存储模型 | S3 风格：`bucket`（= 项目）+ `key`（= 任务/文件路径）扁平命名，文件系统为唯一事实源 |
| 上传 | multipart 流式落盘 + SHA-256 + `max_upload_mb` 上限；**整页拖拽上传**（多文件排队、上传前先校验 token、并发数可调）；文件列表新增**上传者**列（按 token 识别）；**大文件自动多线程分片上传**（>8MB 分片并行合并） |
| 下载 | Starlette `FileResponse` 原生 **Range(206)**（视频可拖动、断点续传）；**需有效 token**（签名链接免 token） |
| 目录管理 | **新建目录**（`key` 含 `/` 自动多级）；**删除仅允许空目录**（含内容拒绝，防误删） |
| 目录信息 | 目录项显示**递归大小**（本目录 + 全部嵌套子目录的文件总字节，`fmtSize`）与**最新修改时间**（所有文件里最新的 mtime，`fmtTime`）；跳过 `.keep` 等隐藏占位文件 |
| 签名链接 | 注册表驱动：**按次数(1-10) / 按时效 / 永久** 三种时效；管理员可作废全部、创建者可作废自己的；上下限可配置 |
| 身份识别 | 按令牌识别「谁」：共享 token 识别为「系统/工具」；用户 token 识别为用户名；**管理员 token 为全集**；删除为权限点 `object:delete`（默认仅管理员有） |
| 审计日志 | 每次上传/下载/删除/建目录/签名/作废追加 `audit.log`（JSONL），**双 IP**（局域网 + 公网）、**UTC 时间** |
| Token 同步 | 从 DataHub `users.json` 拉取用户 token（collab 权威源，本地副本，只增不删；DataHub 不可达时明确提示） |
| 状态文件可迁移 | `meta_dir` 配置项把 `tokens.json` / `signed_links.json` / `audit.log` 移出仓库根，与业务文件隔离、便于备份 |
| 生产部署 | 脚本**构建完自动导出 tar**（`--no-save` 跳过）；部署默认 **host 网络**（容器内 `127.0.0.1` = 宿主机，同机连 DataHub 最稳）；Podman 走 systemd user 自启，Docker 走 `--restart=always`；`--config` / `VOLUME_MAPS` 可挂载外部 config.json |
| 网页 UI | bucket 点击选中 + 高亮、整页拖拽、新建目录、签名链接管理、Token 管理、审计、**API 文档**（页内渲染 `docs/*.md`，含 Markdown 原文/ Swagger / ReDoc 入口）；自定义确认框（不依赖浏览器原生弹窗） |
| 跳转免填 | 支持 URL 携带 token/bucket 直接进入：`/?token=<api_token>[&bucket=<bucket>]` 自动填充令牌并进入目标 bucket，落地即清除地址栏 token（协作平台「任务数据」跳转自动认证） |

---

## 架构与工作流

### 数据流

- 处理端（carryvideo server.py，跑在 8 或本机）处理完成后把产物推送进来（`POST /api/objects`，token 认证）
- ArtifactDepot（FastAPI :8004）由 `storage.py` 负责 bucket/key 落盘 + 元数据清单 + 签名链接注册表 + 审计：
  - `POST /api/objects` 上传（token 认证，可先 `/api/auth/check` 校验）
  - `POST /api/objects/mkdir` 新建目录
  - `GET /api/objects/list` 列文件
  - `GET /api/objects/download` 下载（需 token；签名链接免）
  - `POST /api/objects/presign` 生成签名链接
  - `GET /` 网页浏览
- 消费方：用户浏览器 / ProjectCollab「任务数据」模块

### 身份与审计工作流

1. 用户在协作平台成员列表「查看我的 Token」输密码获得 api_token（平台把 token 与用户的映射登记到 ArtifactDepot）
2. 用户用 api_token 上传/下载，ArtifactDepot 查本地 `tokens.json`（未命中先拉 DataHub）
3. 审计日志记录谁在何时上传/下载/删除/建目录/签名了哪个文件（actor + 局域网 IP + 公网 IP + UTC 时间）

**权限口径**：
- **管理员 token 是全集**：上传 / 下载 / 签名 / 建目录 / 删除 / 作废 / 管理 全部操作
- 上传 / 下载 / 签名 / 建目录：任一有效 token（管理员或用户）
- 下载：任一有效 token；签名链接（`link`+`tk` / 旧式 `expires`+`sig`）免 token
- 删除：权限点 `object:delete`（默认仅管理员共享 token 有）；且**目录仅空可删**（含文件/子目录拒绝）
- 签名链接作废：管理员可作废全部，创建者可作废自己的

### 生产部署与升级流程

**build、save、拷贝、load、deploy**（版本目录随版本走，数据卷独立保留）：

1. 开发机构建并自动导出

```bash
cd ArtifactDepot
./build_image.sh                     # 构建 localhost/artifactdepot:0.7.1 + 自动导出 artifactdepot-0.7.1.tar
```

2. 把 tar 拷到生产机

```bash
scp artifactdepot-0.7.1.tar apyyc@<生产机>:~/SERVER/artifactdepot/
```

3. 生产机导入并部署

```bash
cd ~/SERVER/artifactdepot
podman load -i artifactdepot-0.7.1.tar
cd ArtifactDepot
./deploy_container.sh --token y      # 默认 host 网络；datahub_url 用挂载的 config.json
```

> 升级到新版本时数据卷默认指到 `~/SERVER/artifactdepot/artifactdepot_0.7.1/warehouse`（新目录，空）。
> 若想沿用上一版数据，用 `--data-dir ~/SERVER/artifactdepot/<上一版目录>/warehouse` 指过去，
> 或把旧数据目录内容拷贝/移动到新目录（`tokens.json` 可让同步从 DataHub 重新生成）。
> 若 `~/SERVER` 不可写，脚本会自动回退到 `${XDG_DATA_HOME:-~/.local/share}/artifactdepot/artifactdepot_0.7.1/warehouse`。

---

## 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.10+（含 `fastapi`、`uvicorn`、`httpx`、`python-multipart`） |
| 容器（部署） | Podman 或 Docker |
| 网络 | 局域网可访问；DataHub（8002）与本服务可互通（**同机部署推荐 host 网络**，容器内 `127.0.0.1:8002` 直连） |

---

## 安装

### 本地安装依赖

```bash
pip install fastapi uvicorn httpx python-multipart
```

### 构建镜像（自动导出 tar）

```bash
cd ArtifactDepot
./build_image.sh                      # 构建 + 自动导出 artifactdepot-0.7.1.tar
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--no-save` | 只构建，不导出 tar |
| `--save /path/to/out.tar` | 指定导出路径（默认同目录 `artifactdepot-<tag>.tar`） |
| `--no-cache` | 不用构建缓存（依赖层有改动时用） |
| `--tag <版本>` | 自定义版本标签（默认 0.7.1） |
| `--load /path/to/in.tar` | 不构建，直接导入已有 tar |
| `--list` | 只列出本地镜像 |

> 脚本内部自动 `env -u HTTP_PROXY ... <podman|docker> build --network=host`：绕本机代理对 docker.io 的拦截 + 绕容器网桥 DNS 失效。使用 Podman 时还会加 `--format docker`，避免 `HEALTHCHECK` 被 OCI 格式忽略。

### 导入 / 保存镜像

```bash
# 导出（等价于 build_image.sh 的自动步骤）
podman save -o artifactdepot-0.7.1.tar localhost/artifactdepot:0.7.1
# 目标机导入
podman load -i artifactdepot-0.7.1.tar
```

---

## 快速开始

### 本地开发

```bash
cd ArtifactDepot
./dev_start_headless.sh start       # 后台启动 + 日志
./dev_start_headless.sh status      # 查看状态
./dev_start_headless.sh tail        # 跟踪日志
./dev_start_headless.sh stop        # 停止
```

或 `./scripts/start.sh`（自动找带 uvicorn 的 python；`--port` / `--config` 可选）。

端口默认 8004，可用环境变量 `AD_PORT` 覆盖（`AD_PORT=8090 ./dev_start_headless.sh start`，`scripts/start.sh` 同样透传 `--port`）。

### 容器部署（带自启）

```bash
./deploy_container.sh --token y
```

- **默认 host 网络**：容器共享宿主网络，不再映射端口；容器内 `127.0.0.1` 就是宿主机，同机连 DataHub（`http://127.0.0.1:8002/api/data`）最稳。用 `--port-map` 才改为端口映射（rootless 桥接下容器连宿主发布端口常不通）
- **自启**：Podman 默认生成 `systemd --user` 服务；Docker 没有 `podman generate systemd`，脚本自动改用 `--restart=always`
- `--token y` / 环境变量 `ARTIFACT_DEPOT_ACCESS_TOKEN`：注入共享令牌（不传且 config.json 也无 access_token 时写入/管理接口 401）
- `--datahub-url <地址>` / 环境变量 `ARTIFACT_DEPOT_DATAHUB_URL`：**显式**注入 DataHub 地址；**不传则不注入**，以容器内配置文件的 `datahub_url` 为准
- 数据卷默认 `~/SERVER/artifactdepot/artifactdepot_0.7.1/warehouse` 挂到容器 `/data/depot`（`--data-dir` 指定；默认目录不可写时自动回退到 `~/.local/share/artifactdepot/...`）
- **配置挂载**：脚本自动探测 `~/SERVER/artifactdepot/config/config.json` 或 `~/SERVER/artifactdepot/artifactdepot_0.7.1/config/config.json`；也可用 `--config <宿主机路径>` 显式挂载
- **meta_dir**：默认不注入 `ARTIFACT_DEPOT_META_DIR`，完全以 config.json 的 `meta_dir` 为准；需要显式覆盖时用 `--meta-dir <容器内路径>`

### 首次使用流程

1. 打开 `http://<主机>:8004/`，页面自动列出 bucket，点击即选中进入
2. 顶部**令牌框**填入你的 api_token 或管理员 access_token（上传/下载/签名/删除/建目录通用；管理员可做全部）
3. 选文件或**拖拽到页面任意位置**，松手自动上传（先校验 token）
4. 点「新建目录」输入目录名（可含 `/` 多级）组织文件；目录「删除」仅空目录可删
5. 需要分享时点文件「签名链接」，弹窗选按时效/按次数/永久，生成后内嵌显示、可复制
6. 管理员在「签名链接」页可看全部链接（含每行复制）、作废；「Token 管理」页同步/登记
7. 需要查接口说明时点顶部「API 文档」标签（页内读 `docs/api_rules.md`），或点标签栏右侧 Swagger / ReDoc 在线调试

---

## 配置说明

配置文件：`src/artifactdepot/resources/config.json`（可用环境变量 `ARTIFACT_DEPOT_CONFIG` 指定外部路径）。

### config.json 字段详解

```json
{
  "depot_dir": "./warehouse",
  "meta_dir": "",
  "datahub_url": "http://127.0.0.1:8002/api/data",
  "host": "0.0.0.0",
  "port": 8004,
  "access_token": "y",
  "max_upload_mb": 0,
  "ui_enabled": true,
  "signed_links": {
    "count_min": 1,
    "count_max": 10,
    "expire_min_seconds": 60,
    "expire_max_seconds": 604800
  }
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `depot_dir` | `./warehouse` | 对象存储根目录。相对路径按**项目根**解析（本地免 sudo）；**容器部署填 `/data/depot` 卷路径** |
| `meta_dir` | `""` | **状态文件目录**（`tokens.json` / `signed_links.json` / `audit.log`）。留空 = 放在 `depot_dir` 根下；设了则独立存放（相对路径按 `depot_dir` 解析）。生产建议 `"/data/depot/state"`，把可再生的状态文件与业务文件隔离、备份更清晰（0.5.0 新增） |
| `datahub_url` | `http://127.0.0.1:8002/api/data` | 从哪个 DataHub 拉取用户 token（collab 权威源）。**同机 host 网络**部署就用 `127.0.0.1:8002`；跨机用 `http://<IP>:8002/api/data` |
| `host` | `0.0.0.0` | 监听地址 |
| `port` | `8004` | 服务端口 |
| `access_token` | `change-me` | **管理员/工具共享令牌**：识别为「系统/工具」，可执行全部操作；管理接口（Token/审计）也用它。生产必改 |
| `max_upload_mb` | `0` | 单文件上传上限（MB）。`0` = 不限 |
| `ui_enabled` | `true` | 是否启用网页 UI（`false` 时 `GET /` 返回 403，API 照常） |
| `require_read_token` | `false` | 读接口（`/api/buckets`、`/api/objects/list`）是否要求 `bucket:list` / `object:list` 权限；默认 false 保持内网公开 |
| `signed_links.count_min` | `1` | 签名链接**次数**下限（按次数类型） |
| `signed_links.count_max` | `10` | 签名链接**次数**上限 |
| `signed_links.expire_min_seconds` | `60` | 签名链接**时效**下限（秒） |
| `signed_links.expire_max_seconds` | `604800` | 签名链接**时效**上限（秒，604800 = 7 天） |

> **次数与时效互斥**：签名链接类型为「按时效 / 按次数 / 永久」三选一。选按时效时次数不生效，选按次数时效不生效。越界返回 400；前端按配置显示 min/max 提示并拦截。

### 环境变量覆盖

| 环境变量 | 覆盖字段 |
|---|---|
| `ARTIFACT_DEPOT_CONFIG` | 指定整个配置文件路径（优先级最高） |
| `ARTIFACT_DEPOT_DIR` | `depot_dir` |
| `ARTIFACT_DEPOT_META_DIR` | `meta_dir`（0.5.0 新增） |
| `ARTIFACT_DEPOT_DATAHUB_URL` | `datahub_url` |
| `ARTIFACT_DEPOT_ACCESS_TOKEN` | `access_token` |
| `ARTIFACT_DEPOT_PORT` | `port` |
| `ARTIFACT_DEPOT_MAX_UPLOAD_MB` | `max_upload_mb` |

> 优先级：**环境变量 > 配置文件 > 内置默认**。容器部署用 `-e` 传环境变量即可，无需改代码/文件。

### 配置优先级与生产模板

**镜像内不要烤死 `datahub_url` 默认值**：`config.py` 里环境变量优先级高于配置文件，若 Dockerfile 用 `ENV` 预设 `ARTIFACT_DEPOT_DATAHUB_URL=127.0.0.1`，即使挂载了写好的 config.json 也会被盖掉（0.5.0 已从 Dockerfile 移除该 ENV）。`datahub_url` 以**容器内配置文件的字段**为准，生产用 `--config` 或 `VOLUME_MAPS` 挂载宿主机 config.json 显式填写。

根目录附有 **`config.production.json`** 生产模板，可整份复制到生产机：

```bash
# 生产机（假定文件放 ~/SERVER/artifactdepot/config/config.json）
mkdir -p ~/SERVER/artifactdepot/config
cp config.production.json ~/SERVER/artifactdepot/config/config.json
# 按需修改 access_token / datahub_url / meta_dir，然后部署时显式挂载：
./deploy_container.sh --config ~/SERVER/artifactdepot/config/config.json --token <你的token>
# 也可只挂配置、token 从 config.json 读取：
# ./deploy_container.sh --config ~/SERVER/artifactdepot/config/config.json
```

```json
{
  "depot_dir": "/data/depot",
  "meta_dir": "/data/depot/state",
  "datahub_url": "http://127.0.0.1:8002/api/data",
  "host": "0.0.0.0",
  "port": 8004,
  "access_token": "change-me",
  "max_upload_mb": 0,
  "ui_enabled": true,
  "signed_links": {
    "count_min": 1,
    "count_max": 10,
    "expire_min_seconds": 60,
    "expire_max_seconds": 604800
  }
}
```

> 生产模板用 host 网络时 `datahub_url` 填 `127.0.0.1:8002`（容器内即宿主机）；跨机部署改为目标 DataHub 的 IP。

---

## 目录结构

```
ArtifactDepot/
- README.md / CHANGELOG.md / docs/architecture.md
- config.production.json          # 生产可直接复制的配置模板
- check_ad_sync.sh                # 生产诊断脚本（python3-only，不依赖 curl/jq）
- .dockerignore                   # 构建上下文排除 tar / warehouse / logs / __pycache__ 等
- Dockerfile                      # 单阶段镜像（python:3.12-alpine + supervisor + tzdata）
- docker/supervisord.conf         # supervisor 托管 uvicorn（8004）
- build_image.sh                  # 构建脚本：构建 + 自动导出 tar（默认 --save）
- deploy_container.sh             # 部署脚本：建容器 + Podman systemd / Docker restart（--config/--meta-dir/--token/--datahub-url/--data-dir/--port-map）
- dev_start_headless.sh           # 本地后台启动脚本
- scripts/start.sh                # 本地启动脚本（自动找带 uvicorn 的 python）
- src/artifactdepot/
  - main.py                       # FastAPI 入口 + lifespan（建目录 + 启动同步）
  - config.py                     # 配置加载（默认 < 文件 < 环境变量；meta_dir）
  - storage.py                    # 核心引擎：路径安全 / bucket-key / manifest / 签名链接注册表 / token 注册表 / 审计 / DataHub 同步 / mkdir / meta_dir
  - auth.py                       # 认证：管理员 token（UTF-8 比较）、用户 token、惰性同步
  - api/
    - objects.py                  # 对象 API（上传/列表/下载/删除/mkdir/签名/作废）
    - system.py                   # /health /api/buckets /api/tokens /api/audit /api/auth/check
    - docs.py                     # /api/docs 文档清单与内容（前端「API 文档」页数据源）
  - permissions.py                # 权限点目录与角色预设
- tests/                          # pytest 回归测试（权限点 / 文档接口 / 资源范围）
  - web/
    - ui.py                       # 网页 UI（读 resources/index.html）
  - resources/
    - config.json                 # 运行配置（含 signed_links 上下限、meta_dir）
    - index.html                  # 单页界面（拖拽/新建目录/签名/Token/审计/API 文档）
```

> 文档相关：`docs/api_rules.md` 是对外接口文档；`docs/architecture.md` 是架构说明。
> 两者（含 README / CHANGELOG）都可通过网页 UI 的「API 文档」标签页查看，
> 数据由公开只读接口 `GET /api/docs` 提供；也可直接打开 FastAPI 的 `/docs`（Swagger）/ `/redoc`。

### 磁盘存储布局

```
<depot_dir>/                  # 如 ./warehouse 或 /data/depot
- <bucket>/                       # bucket = 项目（如 音视门carryVdeio）
  - 任务1/交付物/a.mp4             # key = 路径，含 / 即子目录
  - 任务1/交付物/.keep             # 新建目录的隐藏占位（列表跳过）
  - .warehouse.json               # 该 bucket 元数据清单（隐藏）
- ...（bucket 们）

<meta_dir>/（可选，默认在 depot_dir 根下；可用 meta_dir 单独放）
- tokens.json                     # token 与用户名映射注册表（DataHub 同步副本）
- signed_links.json               # 签名链接注册表（时效/次数/作废）
- audit.log                       # 审计日志（JSONL 追加）
```

**key 与目录**：`key` 是文件在 bucket 里的完整路径，含 `/` 自动建子目录（如 `key=任务1/交付物/a.mp4`）。网页点「新建目录」输入名（可含 `/` 多级）创建空目录，或直接以 `key=二级目录/文件名` 上传自动建目录。

**meta_dir**：默认状态文件与 bucket 同根；设 `meta_dir` 后 `tokens.json` / `signed_links.json` / `audit.log` 移到指定目录（相对路径按 `depot_dir` 解析），与上传的业务文件隔离。

---

## 核心模块

| 模块 | 职责 |
|---|---|
| `config.py` | 合并 默认 < 配置文件 < 环境变量；相对路径按项目根解析；支持 `meta_dir` / `ARTIFACT_DEPOT_META_DIR` |
| `storage.py` | **核心引擎**：bucket/key 落盘、路径穿越防护、SHA-256、`.warehouse.json` 清单、签名链接注册表（创建/校验/次数递减/作废）、token 注册表、审计日志（双 IP + UTC）、DataHub 同步、mkdir、空目录删除策略、`_dir_recursive_info()` 目录递归统计（大小/最新 mtime）、`_meta_path` 状态文件定位（meta_dir） |
| `auth.py` | `check_admin_token`（UTF-8 字节常量时间比较，非 ASCII 安全）、`resolve_actor`、`resolve_actor_with_sync`（惰性同步）、`require_write_token` / `require_admin` |
| `api/objects.py` | 对象 API：上传（multipart）、列表、下载（token 鉴权 + Range）、删除（`object:delete` + 空目录）、mkdir、签名链接（presign / config / list / revoke） |
| `api/system.py` | 健康检查、bucket 列表、Token 注册表 CRUD、`/api/tokens/sync`（含 ok/error）、`/api/auth/check`、审计查询 |
| `web/ui.py` | 网页：bucket 点击选中高亮、整页拖拽、新建目录、签名弹窗、Token 管理、审计双 IP 列 |
| `main.py` | 应用组装 + lifespan（确保根目录存在、启动即拉取用户 token） |
| `check_ad_sync.sh` | 生产诊断：容器配置 / 应用同步 / 容器内访问 DataHub / 宿主对照 / 端口监听 / 挂载检查（仅 python3） |

---

## API 参考

> 全部接口前缀为 `http://<IP>:8004`。返回统一结构：`{"code":0,"message":"success","data":...}`；出错时 HTTP 4xx/5xx + `{"detail":"..."}`。

### 鉴权约定

| 等级 | 令牌 | 权限 |
|---|---|---|
| 管理员共享 token | `config.json` 的 `access_token` | 全部操作（含 Token/审计管理、删除） |
| 用户 token | 协作平台「查看我的 Token」或管理员生成 | 由 token 携带的权限点（scope）决定；旧 token 默认相当于历史全量写权限 |
| 签名链接 | `link` + `tk` 参数 | 仅该链接指向对象的下载（免 token） |

> 0.7.0 起支持**逐接口勾选生成自定义 Token**：管理员在「Token 管理」页选择角色/勾选接口/限定 bucket 与路径前缀/设置过期时间，点击「生成本轮 Token」。接口权限目录见 `GET /api/auth/permissions`；详见 `docs/api_rules.md` 第十二章。
>
> 网页 UI 顶部「API 文档」标签页可**页内查看接口文档**（读 `GET /api/docs`；含 `docs/api_rules.md`、`docs/architecture.md`、README、CHANGELOG），标签栏右侧还有 Swagger / ReDoc 入口可直接在线调试。

传递方式：`?token=` 查询参数，或 `Authorization: Bearer <token>`，或 multipart 表单 `token` 字段。

### 对象接口（S3 风格）

#### 上传文件 — `POST /api/objects`（PutObject）

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `object:upload`（管理员共享 token 放行全部；按 token 的 bucket/前缀范围校验） |
| 请求 | multipart/form-data：`file`(必)、`bucket`(必，=项目)、`key`(必，含 `/` 自动建目录)、`token`、`source_url`、`overwrite`(默认 `true`)、`public_ip` |
| 成功 | `data:{bucket, key, size, sha256}`；`.warehouse.json` 同时记录 `uploader`（按 token 识别为「系统/工具」或用户名） |
| 错误 | `401` 无效 token；`413` 超过 `max_upload_mb` 上限；`409` `overwrite=false` 且对象已存在 |
| 说明 | 流式落盘到 `<depot_dir>/<bucket>/<key>`，写审计（双 IP、UTC）；key 父目录自动创建；小文件/常规上传直接走本接口；大文件建议使用下面的分片接口 |

#### 分片上传（多线程）— `POST /api/objects/initiate|chunk|complete|abort`

用于**单个大文件多线程并行上传**：文件拆成 8MB 分片，多路并发传至服务端，最后合并落盘。

| 接口 | 方法 | 说明 |
|---|---|---|
| `initiate` | `POST /api/objects/initiate` | 创建分片会话，返回 `data:{upload_id, chunk_size}` |
| `chunk` | `POST /api/objects/chunk` | multipart：`upload_id`、`index`、`chunk`（分片文件） |
| `complete` | `POST /api/objects/complete` | multipart/form-data：`upload_id`、`bucket`、`key`、`total_chunks`、`source_url`、`overwrite`、`public_ip`；合并并落盘 |
| `abort` | `POST /api/objects/abort` | `upload_id`；放弃并清理临时分片 |

> 说明：`complete` 会校验所有分片齐全、合并后复用 `put_object` 写 `.warehouse.json` 与审计；
> 前端对 `>8MB` 文件自动走分片流程（并发 4 路），`<=8MB` 走普通 `POST /api/objects`。

#### 新建目录 — `POST /api/objects/mkdir`

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `object:mkdir` |
| 请求 | JSON：`{"bucket": "...", "key": "任务1/交付物"}`（key 可多级，自动逐级创建） |
| 成功 | `data:{bucket, key: "任务1/交付物/"}`（key 尾带 `/`） |
| 说明 | 目录树内放隐藏 `.keep` 占位文件（列表会跳过隐藏文件） |

#### 列对象 — `GET /api/objects/list`（ListObjects）

| 项 | 说明 |
|---|---|
| 鉴权 | 默认内网开放；`require_read_token=true` 时需权限点 `object:list`（资源范围外返回 403） |
| 请求 | Query：`bucket`(必)、`prefix`(空=根目录；指向目录时列该目录直接子项) |
| 成功 | `data:{bucket, prefix, items:[...]}`；文件项含 `name/key/is_dir/size/mtime(UTC Z)/sha256/source_url/uploader`；目录项 `key` 以 `/` 结尾，并含 `size`（**递归总大小**）与 `mtime`（**最新修改时间**） |
| 错误 | `400` prefix 越界（越出 bucket 根） |

#### 下载文件 — `GET /api/objects/download`（GetObject）

| 项 | 说明 |
|---|---|
| 鉴权 | `object:download` 权限点，或签名链接 `link`+`tk`，或旧式签名 `expires`+`sig`（三选一） |
| 请求 | Query：`bucket`、`key`、`token` 或 `link`+`tk` 或 `expires`+`sig` |
| 成功 | 文件流；Starlette `FileResponse` 原生支持 **Range(206)**（视频拖动、断点续传） |
| 错误 | `401` 无有效凭证；`404` 对象不存在 |
| 说明 | 每次下载写审计 |

#### 删除对象/目录 — `DELETE /api/objects`（DeleteObject）

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `object:delete`（管理员共享 token 放行全部） |
| 请求 | Query：`bucket`、`key` |
| 成功 | `data:{...}` |
| 错误 | `400` 目录非空（需先清空内容才能删）；`404` 不存在 |
| 说明 | 安全策略：**目录仅允许删除空目录**（隐藏 `.keep` 不算内容，自动一并移除），防误删 |

#### 生成签名链接 — `POST /api/objects/presign`

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `link:create` |
| 请求 | JSON：`{"bucket", "key", "mode": "count|time|permanent", "count": 1-10, "expires": 3600}` |
| 成功 | `data:{url, id, mode, max_uses, remaining, expires}` |
| 说明 | `mode=count` 按次数(1-10)；`time` 按时效秒（默认 1 小时）；`permanent` 永久；次数/时效上下限见 `config.signed_links` |

#### 签名链接辅助

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/objects/signed-links/config` | 返回次数/时效上下限（供前端渲染校验） |
| GET | `/api/objects/signed-links` | 列签名链接（需 `link:list`；带范围 token 只见范围内条目；**完整 URL 仅管理员/创建者可见**；密钥不回传） |
| POST | `/api/objects/signed-links/{id}/revoke` | 作废（需 `link:revoke`；管理员可作废任意，用户只能作废自己创建的） |

### 系统 / 管理接口

#### Token 校验 — `GET /api/auth/check`
`?token=` → `data:{valid, actor, role, scopes, allow_buckets, allow_prefixes, expires_at, expired, enabled}`；上传/下载前先审核。

#### 健康检查 — `GET /health`
`{"status":"ok","depot_dir":"...","exists":true}`

#### 列 bucket — `GET /api/buckets`
返回 `data:[...]` bucket（项目）列表。

#### Token 注册表管理（权限点 `token:*`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tokens` | 查看 token→用户名 映射 |
| POST | `/api/tokens` | 登记/更新：JSON `{"token": "...", "user": "..."}` |
| DELETE | `/api/tokens?value=<token>` | 移除某个 token（用户重置/注销时） |
| POST | `/api/tokens/sync` | 从 DataHub `users.json` 拉取并合并用户 token（collab 权威源、本地副本只增不删）；返回 `data.ok` 反映 DataHub 可达性 |

#### 审计查询 — `GET /api/audit`（权限点 `audit:read`）
Query：`bucket`/`key`/`actor`/`since`(YYYY-MM-DD 起)/`limit`(默认 500)。返回审计记录（操作类型 / 操作者 / **双 IP** / UTC 时间 / size / sha256）。

### 文档接口（0.7.1）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/docs` | 文档清单（公开）：`[{name,title,available}]`，name ∈ api_rules/architecture/readme/changelog |
| GET | `/api/docs/{name}` | 文档内容（公开）：`data:{name,title,content}` |
| GET | `/api/docs/{name}?raw=1` | 直接返回 `text/plain` 原文 |

### 网页 UI

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 网页控制台（bucket 选择、整页拖拽上传、新建目录、签名链接/Token/审计管理、API 文档页；标签栏含 Swagger/ReDoc 入口） |

### 调用示例

```bash
BASE=http://192.168.18.43:8004
ADMIN=y                                    # 管理员 token（config.json 的 access_token）
USER=<你的 api_token>                       # 协作平台「查看我的 Token」获取

# 校验 token
curl "$BASE/api/auth/check?token=$USER"

# 上传
curl -F "bucket=音视门carryVdeio" -F "key=任务1/video.mp4" -F "token=$USER" -F "file=@/本地/video.mp4" "$BASE/api/objects"

# 新建目录（可多级）
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $USER" \
  -d '{"bucket":"音视门carryVdeio","key":"任务1/交付物"}' "$BASE/api/objects/mkdir"

# 列表（中文 bucket 用 -G --data-urlencode）
curl -G "$BASE/api/objects/list" --data-urlencode "bucket=音视门carryVdeio"

# 下载（需 token）
curl -o out.mp4 "$BASE/api/objects/download?bucket=音视门carryVdeio&key=任务1/video.mp4&token=$USER"

# 签名链接 —— 按时效
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $USER" \
  -d '{"bucket":"音视门carryVdeio","key":"artifactdepot_0.7.1.tar","mode":"time","expires":3600}' \
  "$BASE/api/objects/presign"

# 签名链接 —— 按次数（3 次）
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $USER" \
  -d '{"bucket":"音视门carryVdeio","key":"artifactdepot_0.7.1.tar","mode":"count","count":3}' \
  "$BASE/api/objects/presign"

# 签名链接 —— 永久
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $USER" \
  -d '{"bucket":"音视门carryVdeio","key":"artifactdepot_0.7.1.tar","mode":"permanent"}' \
  "$BASE/api/objects/presign"

# 删除（需 object:delete；目录仅空可删）
curl -X DELETE "$BASE/api/objects?bucket=音视门carryVdeio&key=任务1/video.mp4&token=$ADMIN"

# 审计（管理员）
curl -G "$BASE/api/audit" --data-urlencode "bucket=音视门carryVdeio" --data-urlencode "token=$ADMIN"

# 从 DataHub 同步用户 token（管理员）
curl -X POST -H "Authorization: Bearer $ADMIN" "$BASE/api/tokens/sync"
```

---

## 模板与整洁架构规范

本项目按**简洁分层**组织（规模小、不引入过度抽象）：

- **配置驱动**：所有环境差异（token、DataHub 地址、端口、路径、状态文件位置、签名链接上下限）走 `config.json` / 环境变量，代码不写死结构；**环境变量优先级高于配置文件**，镜像内不预设会被挂载配置盖掉的默认值
- **单一事实源**：文件系统为存储事实源；`tokens.json` / `signed_links.json` 是可管理副本；`audit.log` 只增不改；`meta_dir` 让「可再生状态」与「业务数据」分层
- **分层**：`api/`（HTTP 契约）、`storage.py`（存储引擎）、`config.py`（配置）；`auth.py` 只做认证、不碰业务
- **安全默认**：写操作必带令牌（管理员/用户）；下载需 token（签名链接除外）；删除需 `object:delete` + 目录仅空可删；路径穿越防护；令牌比较 UTF-8 常量时间；签名链接限时/限次/可作废
- **命名**：S3 语义命名（bucket/key/object/mkdir），对外 API 与 S3 一一对应，便于未来迁移 MinIO/OSS
- **可部署性**：脚本约定统一（`build_image.sh` 自动导出、`deploy_container.sh` host 网络默认 + `--config`/`VOLUME_MAPS`），生产问题可用 `check_ad_sync.sh` 定位

新增代码请遵循：新端点放对应 `api/*.py` 并在 `main.py` 注册；文件读写统一走 `storage.py`；不得在路由里直接碰文件系统。

---

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| 上传/下载提示「无效的访问令牌」 | token 未登记（先 `POST /api/tokens` 或从 DataHub 同步）或 `access_token` 未设；`/api/auth/check` 可先自测 |
| 下载提示「无效的访问令牌」 | 0.4.0 起下载需有效 token；直链需带 `&token=`；分享请走签名链接 |
| 删除提示「权限不足：需要 object:delete」 | 0.7.0 起删除是权限点 `object:delete`：管理员共享 `access_token` 始终可删；普通用户 token 需由管理员在「Token 管理」页勾选该权限点。缺权限返回 403，无效 token 返回 401。 |
| 删除目录提示「目录非空」 | 0.4.0 起目录仅空可删（防误删）；先进去删文件/子目录，清空后再删目录 |
| 签名链接提示「次数已用完」/「链接已作废」 | 次数耗尽或已被作废；重新生成或换时效/永久 |
| 复制链接没反应 | `navigator.clipboard` 在 HTTP 下不可用时自动降级 `execCommand`；仍不行就手动选中链接复制 |
| 点生成/作废/删除没弹窗 | 浏览器拦截原生 `confirm/prompt`；已全部改用**页面内自定义弹窗**，不依赖浏览器原生 |
| 令牌含中文/特殊符号报 500 | 已修：`compare_digest` 改 UTF-8 字节比较，非 ASCII 令牌返回 401 而非崩溃 |
| 审计时间不准 / 显示 UTC | 后端统一记 UTC（`...Z`），前端自动转**浏览器本地时间**；容器已设 `TZ=Asia/Shanghai` 兜底 |
| 审计「公网 IP」为空 | 公网 IP 由浏览器调 ipify 尽力上报；外网不可达或签名链接外站直连时为空（局域网 IP 仍记录） |
| 同步不到新用户 token | 两处 users.json 不一致（指向不同 DataHub）、JSON 重复键、该用户 `api_token` 为空；按钮会提示 DataHub 是否可达 |
| **生产同步报 "All connection attempts failed"** | 三层根因（0.5.0 已修前两层）：1. 旧脚本默认注入 `ARTIFACT_DEPOT_DATAHUB_URL=127.0.0.1` 指向自身，0.5.0 起不传 `--datahub-url` 就不注入，以 config.json 为准；2. 旧镜像 Dockerfile 烤死 `ARTIFACT_DEPOT_DATAHUB_URL` 盖掉挂载 config，0.5.0 已移除该 ENV，**需重建镜像**；3. collab 的 DataHub 只绑 `127.0.0.1:8002` 宿主访问不通，supervisord.conf 改 `0.0.0.0`。用 `bash check_ad_sync.sh` 一键定位 |
| **容器访问不到宿主 8002** | rootless pasta 网桥下容器连宿主发布端口常不通；`deploy_container.sh` 默认 host 网络即解决，容器内用 `127.0.0.1:8002` 直连 |
| **tokens.json 生成位置不对** | 0.5.0 起可用 `meta_dir` 指定状态文件目录；改 `config.py`/`storage.py` 后**需重建镜像**再部署（运行中镜像不带新代码，只改配置不生效） |
| 访问 `:8002` 连接被重置 | collab 容器默认只映射 8003；用多端口部署把 8002 也 `-p` 出来，并确认 DataHub 绑 `0.0.0.0` |
| 点「API 文档」看不到内容 | 先确认 `GET /api/docs` 有返回（可用 curl 自测）；容器部署需镜像内带 `docs/`（0.7.1 起 `Dockerfile` 已 COPY，旧镜像重建即可）；Swagger `/docs` 依赖外网 CDN，内网以页内文档为准 |
| 大文件上传「没反应」 | 看进度条；uvicorn 访问日志在**请求完成后**才打印，传输期间终端安静是正常的 |
| 启动报 `No module named uvicorn` | `python3` 解析到无 uvicorn 的 `/usr/bin/python3`；`scripts/start.sh` 已自动探测 |
| 容器里服务反复退出 | 缺 `python-multipart`（上传必需）；确认镜像构建时已安装 |

---

## 许可证与支持

- **许可证**：本项目未单独声明许可证，随 `ass-Computational` 系统使用。
- **支持**：问题与建议请联系系统维护者；版本记录见 [CHANGELOG.md](CHANGELOG.md)。
- **相关**：依赖 DataHub（用户/token 权威源）与协作平台（「查看我的 Token」）；处理端对接见 [docs/architecture.md](docs/architecture.md)。
