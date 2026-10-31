# ArtifactDepot

ArtifactDepot 是一个基于 FastAPI 的对象存储式工件仓库（S3 风格 bucket/key 模型），用于收存处理产物（视频、文件等）。它提供上传（含大文件分片与断点续传）、目录管理、Range 分块下载、一键下载目录、签名链接、审计与可观测能力，并自带网页控制台和桌面客户端。本服务只负责文件存储，不承担业务逻辑。

版本号以 [VERSION](VERSION) 为唯一来源（当前 0.11.0）。

## 目录

- [项目简介](#项目简介)
- [核心功能](#核心功能)
- [技术栈](#技术栈)
- [架构说明](#架构说明)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [使用说明](#使用说明)
- [API 概览](#api-概览)
- [测试与开发](#测试与开发)
- [部署与运维](#部署与运维)
- [安全说明](#安全说明)
- [常见问题](#常见问题)
- [许可证与支持](#许可证与支持)
- [相关文档](#相关文档)

## 项目简介

- **定位**：单实例文件仓库服务，按 S3 语义组织数据。`bucket` 对应项目，`key` 是文件在 bucket 内的完整路径，包含 `/` 时自动形成多级目录；文件系统是唯一事实源。
- **典型使用场景**：
  - 处理端（如 carryvideo）处理完成后通过 HTTP 接口推送产物。
  - 协作平台与用户在网页或桌面客户端中浏览、上传、下载产物。
  - 需要临时分享单个文件时，生成按时效、按次数或永久的签名链接。
- **不负责**：业务 JSON 数据与账号体系。用户 token 由本服务在本地维护注册表；服务不承担业务逻辑，也不是分布式对象存储。

## 核心功能

| 类别 | 能力 |
|---|---|
| 存储模型 | S3 风格 `bucket` / `key` 扁平命名；`key` 含 `/` 时自动创建多级目录；`.warehouse.json` 保存对象元数据清单 |
| 上传 | multipart 流式落盘，计算 SHA-256，受 `max_upload_mb` 限制；超过分片阈值的文件自动走分片上传（默认 8 MiB × 4 路并发） |
| 断点续传 | 分片会话保存在目标目录 `.artifactdepot-uploads/<upload_id>/`，支持暂停、继续、失败重试与服务重启后续传，并按 TTL 自动清理 |
| 下载 | 基于 `FileResponse` 的 Range 206；`HEAD` 探测元信息；ETag 优先取清单中的 SHA-256，无清单记录时回退为 `size-mtime`，配合 `If-None-Match` 返回 304；网页与客户端支持分块并发下载、进度、取消与完整性校验 |
| 目录管理 | 新建目录、重命名与移动、删除（仅允许空目录）、批量删除目录内容（`files` / `all`，执行时需输入目录名确认） |
| 一键下载目录 | 目录内只有 1 个可见文件且没有可见子目录时直接下载；其余非空情况（多文件或含子目录）流式打包 ZIP，不写临时文件 |
| 签名链接 | 按时效（秒）、按次数（1 至上限）或永久；可作废；上下限由配置控制 |
| 目录签名链接 | 目录行生成分享链接；后台打包到 `<目录>/.package/<link_id>/`（唯一文件直下 / 其他 ZIP），生成后立即返回链接并显示进度；链接失效自动删包，目录改名链接跟随、删除/清空目录自动作废 |
| 身份与权限 | 管理员共享 token 拥有全部权限；用户 token 可从 22 个权限点与 9 种角色预设中组合，并可限定 bucket、路径前缀与过期时间 |
| 审计与可观测 | 上传、下载、删除、建目录、改名、签名、Token 变更等操作写入 JSONL 审计（客户端 IP、UTC，配置可信代理时另记解析出的公网 IP）；提供 `/metrics`、`/health/live`、`/health/ready` 与 `X-Request-ID` |
| 网页控制台 | 浏览、签名链接、Token 管理、审计、配置五个标签页；拖拽上传、上传队列暂停与继续、分块并发下载、批量删除、在线修改配置 |
| 桌面客户端 | tkinter 桌面客户端（`artifactdepot_gui.py` 与 `artifactdepot_upload.py`），覆盖上传（分片、暂停、续传）与下载（分块并发、暂停、续传）及管理工作流；另提供仅上传的 CLI 模式 |
| Token 管理 | 本地维护 token 注册表，按权限点生成、更新与移除 |
| 部署 | 单镜像（Podman / Docker）与三卷分离（config、data、state）；构建脚本自动导出 tar，部署脚本配置自启 |

## 技术栈

| 层次 | 技术 |
|---|---|
| 服务端 | Python 3.10+；FastAPI 0.139.0、Uvicorn 0.51.0、Starlette、httpx 0.28.1、python-multipart 0.0.32（版本精确锁定于 [requirements.txt](requirements.txt)） |
| 存储 | 本地文件系统（bucket/key 目录映射）、JSON 元数据与注册表、JSONL 审计日志 |
| 前端 | 原生 HTML / CSS / JavaScript 单页应用，事件委托与严格 CSP；Swagger UI 与 ReDoc 静态资源自托管 |
| 桌面客户端 | Python 标准库与 tkinter，无必需的第三方运行时依赖（[client/](client)） |
| 部署 | Podman 或 Docker；镜像内由 supervisor 托管 uvicorn；Podman 使用 systemd user 服务，Docker 使用重启策略 |
| 测试 | pytest |

## 架构说明

服务端按职责分为四层：

- **接入层** `src/artifactdepot/api/`：`objects.py` 提供对象与分片上传接口，`system.py` 提供健康检查、bucket、Token、审计与配置接口，`docs.py` 提供文档读取接口；网页路由位于 `web/ui.py`。
- **存储引擎** `src/artifactdepot/storage.py`：负责 bucket/key 落盘、路径校验、元数据清单、签名链接与 token 注册表、审计、分片会话与过期清理；流式 ZIP 由 `zipstream.py` 实现。
- **配置与认证**：`config/` 负责多层配置合并、校验与热加载；`auth.py` 负责令牌解析与校验；`permissions.py` 定义权限点与角色预设。
- **可观测性**：`observability.py` 提供请求 ID、访问日志与指标；健康检查接口位于 `api/system.py`，日志脱敏由 `logging_utils.py` 负责。

主要数据流：

1. 处理端通过 `POST /api/objects` 或分片接口（`initiate`、`chunk`、`complete`）推送产物，请求携带 token。
2. 文件写入 `<depot_dir>/<bucket>/<key>`，对象元数据写入同 bucket 的 `.warehouse.json`，审计记录追加到 `<meta_dir>/audit.log`。
3. 浏览器或桌面客户端通过网页与 API 浏览、下载；下载前可用 `HEAD` 探测，随后按 Range 分块并发获取。
4. 用户 token 由本地注册表 `<meta_dir>/tokens.json` 维护，用于操作者识别与权限判定。

存储布局：

```text
<depot_dir>/
├── <bucket>/                        # bucket 即项目
│   ├── <key 路径>/<文件>            # key 含 / 即多级目录
│   ├── <key 路径>/.artifactdepot-uploads/<upload_id>/
│   │                                # 分片上传会话（完成或过期后自动清理）
│   ├── <key 路径>/.package/<link_id>/
│   │                                # 目录签名链接成品（<目录名>.zip 或唯一文件 + .ready.json）
│   └── .warehouse.json              # 该 bucket 的对象元数据清单（隐藏）
└── .staging/                        # 默认 staging（upload.staging_dir 留空时）
                                     # 旧版会话与 multipart spool 临时文件

<meta_dir>/                          # 留空时位于 depot_dir 根下
├── tokens.json                      # token 与用户名映射（本地注册表）
├── signed_links.json                # 签名链接注册表
└── audit.log                        # 审计日志（JSONL，自动轮转）
```

更完整的拓扑、可靠性要点与部署说明见 [docs/architecture.md](docs/architecture.md)。

## 项目结构

```text
ArtifactDepot/
├── README.md / CHANGELOG.md / VERSION      # VERSION 为版本号唯一来源
├── LICENSE / LICENSE.zh-CN.md              # Apache-2.0（英文 / 中文）
├── Dockerfile / docker/supervisord.conf    # 镜像定义与进程托管
├── requirements.txt                        # 运行依赖（精确锁定）
├── build_image.sh / build_image.conf       # 构建镜像并导出 tar
├── deploy_container.sh                     # 部署容器并配置自启
├── dev_start_headless.sh                   # 本地后台启动（开发调试）
├── check_ad_sync.sh                        # 生产环境诊断
├── sync_artifacts.sh / sync_artifacts.conf # 通用 rsync 产物同步工具
├── docs/
│   ├── api_rules.md                        # 对外 API 文档（也可经 /api/docs 读取）
│   └── architecture.md                     # 架构说明
├── scripts/add_spdx_headers.py             # SPDX 署名头补写与校验
├── src/artifactdepot/
│   ├── main.py                             # 应用组装与 lifespan
│   ├── config/                             # 配置包与配置文件
│   ├── storage.py / zipstream.py           # 存储引擎与流式 ZIP
│   ├── auth.py / permissions.py            # 令牌与权限点
│   ├── observability.py / logging_utils.py / netutil.py
│   ├── api/                                # objects.py / system.py / docs.py
│   ├── web/ui.py                           # 网页路由
│   └── resources/                          # index.html、static/ 前端资源
├── client/                                 # 桌面客户端与 CLI（client/README.md）
└── tests/                                  # pytest 回归与验收测试
```

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Linux（服务端与管理脚本为 bash；自启依赖 systemd user 服务或 Docker 重启策略）；桌面客户端脚本也支持 macOS |
| Python | 3.10 及以上（代码使用 PEP 604 联合类型注解）；容器镜像固定 `python:3.12-alpine` |
| 运行依赖 | 见 [requirements.txt](requirements.txt)，与镜像内安装版本一致 |
| 容器运行时 | Podman 或 Docker（仅构建与容器部署需要） |
| 端口与网络 | 默认监听 `0.0.0.0:8004`；容器部署默认使用 host 网络 |
| 磁盘 | `depot_dir` 与 `meta_dir` 所在卷需容纳全部产物；分片上传的临时会话与业务文件同卷 |

## 安装

### 本地环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

- 若系统缺少 `pip` 或 `ensurepip`（Debian/Ubuntu 可安装 `python3-venv`、`python3-pip`），也可改用 uv（需另行安装）：先执行 `uv venv .venv`，再执行 `uv pip install -r requirements.txt`。
- [requirements.txt](requirements.txt) 只包含运行依赖；运行测试需要另行安装 pytest。
- [dev_start_headless.sh](dev_start_headless.sh) 按 `AD_PYTHON`、项目 `.venv`、上级与上上级目录的 `.venv`、`python3` 的顺序选择解释器，系统环境已具备依赖时可直接启动。

### 构建镜像

```bash
./build_image.sh
```

默认按 [VERSION](VERSION) 生成 `localhost/artifactdepot:<版本>` 镜像，并自动导出到 `../podman/artifactdepot-<版本>.tar`（导出行为由 [build_image.conf](build_image.conf) 的 `SAVE_DEFAULT`、`SAVE_DIR` 控制）。

| 参数 | 说明 |
|---|---|
| `--tag <版本>` | 覆盖镜像标签 |
| `--no-cache` | 禁用构建缓存 |
| `--no-save` | 只构建，不导出 tar |
| `--save <目录>` | 指定导出目录（不是单个 tar 文件路径） |
| `--image <id>` | 只构建指定镜像（配置中的镜像 id 为 `app`） |
| `--images` | 列出配置中的镜像 |
| `-h` / `--help` | 查看全部用法 |

构建代理、镜像仓库与导出规则在 [build_image.conf](build_image.conf) 中配置，`PROXY_MODE` 支持 `keep`（继承当前代理）、`strip`（不使用代理）与 `set`（使用 `PROXY_URL`）。

### 导入镜像

```bash
podman load -i ../podman/artifactdepot-<版本>.tar
# 使用 Docker 时
docker load -i ../podman/artifactdepot-<版本>.tar
```

## 快速开始

### 本地启动

```bash
./dev_start_headless.sh check    # 检查解释器与运行依赖
./dev_start_headless.sh start    # 后台启动，日志写入 logs/artifactdepot.log
./dev_start_headless.sh status   # 查看运行状态
./dev_start_headless.sh tail     # 跟踪日志
./dev_start_headless.sh stop     # 停止服务
```

- 端口默认 8004，可用 `AD_PORT` 覆盖；解释器可用 `AD_PYTHON` 指定。
- 等价的前台命令：

```bash
PYTHONPATH=src .venv/bin/python -m uvicorn artifactdepot.main:app --host 0.0.0.0 --port 8004
```

启动后访问 `http://<主机>:8004/`。

### 容器部署

```bash
./deploy_container.sh
```

部署前需已构建或导入镜像。脚本默认创建容器 `artifactdepot`，镜像标签取自 [VERSION](VERSION)。

| 宿主机目录 | 容器内路径 | 内容 |
|---|---|---|
| `<部署根>/config/` | `/data/config` | `config.json` 与 `.env`；首次部署时用 `config.production.json` 播种 |
| `<部署根>/data/` | `/data/depot` | bucket 与对象文件 |
| `<部署根>/state/` | `/data/state` | `tokens.json`、`signed_links.json`、`audit.log` |

- 默认部署根为 `~/SERVER/artifactdepot`；默认目录不可写时，`config`、`data`、`state` 会分别回退到 `${XDG_DATA_HOME:-~/.local/share}/artifactdepot/{config,data,state}`。可用 `--deploy-root` 或环境变量 `DEPLOY_ROOT` 修改；显式指定的目录不可写时脚本直接报错。
- 默认使用 host 网络，容器内 `127.0.0.1` 即宿主机；`--port-map` 改为端口映射，`--network-host` 显式指定 host 网络。
- 自启方式：Podman 生成 systemd user 服务（`--no-systemd` 跳过），Docker 使用 `--restart=always`。
- 管理员令牌：编辑 `<部署根>/config/config.json`（首次部署已用生产模板播种）或同目录 `.env` 中的 `access_token`，然后重启容器（`podman restart artifactdepot` 或 `docker restart artifactdepot`）。脚本接受的 `--token` 参数当前不会传入容器，见「已知问题」。
- 其他参数：`--data-dir`、`--state-dir`、`--config-dir`、`--config <文件>`、`--meta-dir`、`--no-volume`、`--stop`、`--rm`。

### 首次使用

1. 打开 `http://<主机>:8004/`，页面列出 bucket，点击进入。
2. 在顶部令牌框填入用户 token 或管理员共享 token。令牌只保存在页面内存；也可通过 `/?token=<token>&bucket=<bucket>` 免填进入，进入后地址栏中的 token 会被清除。
3. 上传：选择文件或把文件拖拽到页面任意位置；大文件自动分片，可暂停、继续或在失败后续传。
4. 管理：新建目录；删除文件或空目录；需要清空目录时使用「清空本目录文件」或「删除本目录全部内容」，先预览再输入目录名确认（bucket 根目录输入 bucket 名）。
5. 下载：单个文件自动分块并发下载，可取消；「下载本目录」在仅有 1 个可见文件且没有子目录时直接下载，否则打包 ZIP。
6. 分享：为文件生成按时效、按次数或永久的签名链接；目录行也可生成目录打包链接（链接立即生成，后台打包完成后可下载，唯一文件直下 / 其他 ZIP）。
7. 管理员：在「Token 管理」中生成、更新、移除 token，在「审计」中查询记录，在「配置」中在线修改配置。
8. 接口调试：`/docs`（Swagger UI）与 `/redoc`（ReDoc），文档原文可从 `/api/docs` 获取。
9. 桌面客户端：`cd client && ./run.sh`，用法见 [client/README.md](client/README.md)。

## 配置说明

### 加载顺序

配置按以下顺序叠加，后者覆盖前者：

| 优先级 | 来源 |
|---|---|
| 1（最低） | 内置默认值（[src/artifactdepot/config/__init__.py](src/artifactdepot/config/__init__.py) 中的 `DEFAULT`） |
| 2 | 配置文件：默认 `src/artifactdepot/config/config.json`，可用 `ARTIFACT_DEPOT_CONFIG` 指定其他文件 |
| 3 | `.env`：默认 `src/artifactdepot/config/.env`，可用 `ARTIFACT_DEPOT_ENV` 指定；写法为 `字段名=值`，支持点号嵌套 |
| 4（最高） | 环境变量 `ARTIFACT_DEPOT_*`（旧前缀 `WAREHOUSE_*` 仅在新前缀未设置时生效） |

需要注意的规则：

- `.env` 中的空值视为未设置，不会覆盖 `config.json` 中的同名项。
- `.env` 支持点号写法，例如 `upload.chunk_size_mb=8`、`signed_links.count_max=20`。
- 未知配置项：`.env` 与环境变量中的未知键会在启动时告警并忽略，`config.json` 中的未知键不告警。
- 相对路径：`depot_dir` 按项目根解析；`meta_dir` 与 `upload.staging_dir` 按 `depot_dir` 解析。容器部署应使用绝对路径，例如 `/data/depot`、`/data/state`。
- 敏感项放在 `.env`，该文件不进 git 与镜像；字段名可参考 [.env.example](src/artifactdepot/config/.env.example)。
- 仓库内的 `config.json` 会进入镜像，作为无挂载时的默认配置；生产配置应使用部署根下的 `config/config.json`。

### 常用配置项

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `depot_dir` | `./depot` | 对象存储根目录；容器部署使用 `/data/depot` |
| `meta_dir` | `""` | 状态文件目录；留空表示放在 `depot_dir` 根下，生产建议 `/data/state` |
| `host` / `port` | `0.0.0.0` / `8004` | 监听地址与端口 |
| `access_token` | `admin` | 管理员共享令牌；生产环境必须替换为至少 16 位随机串 |
| `max_upload_mb` | `0` | 单文件上传上限（MB），`0` 表示不限 |
| `ui_enabled` / `metrics_enabled` | `true` / `true` | 是否启用网页界面与 `/metrics` |
| `require_read_token` | `false` | 读接口（`/api/buckets`、`/api/objects/list`）是否也要求 token |
| `audit_max_mb` / `audit_backups` | `50` / `5` | 审计日志轮转大小（MB，`0` 表示不轮转）与保留份数 |
| `trusted_proxies` | `[]` | 可信反向代理地址；命中时才从 `X-Forwarded-For` 解析审计公网 IP |
| `cors_origins` / `allowed_hosts` | `[]` / `["*"]` | CORS 白名单与 TrustedHost 白名单 |
| `ui.file_page_size` / `ui.audit_page_size` | `200` / `500` | 网页文件列表分页条数与审计查询条数 |
| `signed_links.count_min` / `count_max` | `1` / `10` | 签名链接次数范围 |
| `signed_links.expire_min_seconds` / `expire_max_seconds` | `60` / `604800` | 签名链接时效范围（秒） |
| `signed_links.allow_directory` | `true` | 是否允许生成目录打包签名链接 |
| `package.enabled` | `true` | 目录打包总开关 |
| `package.max_items` / `max_total_mb` | `100000` / `0` | 单次打包项数上限与源文件总大小上限（MB，`0` 不限） |
| `package.max_concurrent_jobs` / `zip_compression` | `1` / `stored` | 同时打包任务数与 ZIP 方式（`stored` / `deflate`） |
| `package.cleanup_interval_seconds` / `cleanup_grace_seconds` | `60` / `60` | 失效包清理间隔与失效后删包宽限期（秒） |
| `package.delete_on_revoke` / `cleanup_on_startup` | `true` / `true` | 作废时立即删包 / 启动时清理并恢复未完成打包 |
| `upload.chunk_size_mb` / `chunk_concurrency` | `8` / `4` | 分片大小（MB）与单文件分片并发数 |
| `upload.file_concurrency` | `3` | 网页端同时上传的文件数 |
| `upload.request_timeout_seconds` | `86400` | 分片与合并请求超时（秒） |
| `upload.session_ttl_seconds` / `session_idle_ttl_seconds` | `86400` / `86400` | 分片会话的绝对有效期与空闲清理阈值 |
| `download.chunk_size_mb` / `chunk_concurrency` | `8` / `4` | 下载分片大小（MB）与并发数 |
| `download.request_timeout_seconds` | `3600` | 单次分片下载请求超时（秒） |
| `token.min_length` / `max_length` | `5` / `30` | 新建 token 的长度范围，自动生成时取最大长度 |

完整字段与取值范围见 [docs/api_rules.md](docs/api_rules.md) 的配置项章节；具备 `config:write` 权限的令牌也可通过 `GET /api/config` 获取配置快照与字段来源。

### 常用环境变量

| 环境变量 | 覆盖项 |
|---|---|
| `ARTIFACT_DEPOT_CONFIG` / `ARTIFACT_DEPOT_ENV` | 配置文件与 `.env` 文件路径 |
| `ARTIFACT_DEPOT_DIR` / `ARTIFACT_DEPOT_META_DIR` | `depot_dir` / `meta_dir` |
| `ARTIFACT_DEPOT_ACCESS_TOKEN` / `ARTIFACT_DEPOT_PORT` / `ARTIFACT_DEPOT_MAX_UPLOAD_MB` | 共享令牌、端口与上传上限 |
| `ARTIFACT_DEPOT_SIGNED_LINKS_ALLOW_DIRECTORY` | `signed_links.allow_directory` |
| `ARTIFACT_DEPOT_PACKAGE_*` | `package.enabled/max_items/max_total_mb/max_concurrent_jobs/zip_compression/cleanup_*` 等同名配置 |
| `ARTIFACT_DEPOT_UPLOAD_*` | `upload.*` 各项，例如 `ARTIFACT_DEPOT_UPLOAD_CHUNK_SIZE_MB` |
| `ARTIFACT_DEPOT_DOWNLOAD_*` | `download.*` 各项，例如 `ARTIFACT_DEPOT_DOWNLOAD_CHUNK_CONCURRENCY` |
| `ARTIFACT_DEPOT_UI_FILE_PAGE_SIZE` / `ARTIFACT_DEPOT_UI_AUDIT_PAGE_SIZE` | 网页分页条数 |
| `ARTIFACT_DEPOT_TOKEN_MIN_LENGTH` / `ARTIFACT_DEPOT_TOKEN_MAX_LENGTH` | token 长度限制 |
| `ARTIFACT_DEPOT_STRICT_CONFIG` | 设为 `1` 时，配置告警会阻止服务启动 |

### 热加载与生产模板

- 网页「配置」标签页与 `POST /api/config` 可在线修改 `config.json` 并热加载；`POST /api/config/reload` 重新读取磁盘配置，返回 `changed` 与 `needs_restart`。
- `host`、`port`、`depot_dir`、`meta_dir`、`cors_origins`、`allowed_hosts`、`upload.staging_dir` 修改后需要重启。
- 配置写回使用原子写与 600 权限，密钥字段脱敏；`access_token` 留空表示保持原值，且具备 `config:write` 的用户 token 不能轮换管理员令牌。
- [config.production.json](src/artifactdepot/config/config.production.json) 是生产模板，运行时不会读取。首次部署时由 `deploy_container.sh` 复制到 `<部署根>/config/config.json`，之后直接编辑该文件；不要把它改名覆盖仓库内的 `config.json`。

## 使用说明

### 网页控制台

| 标签页 | 功能 |
|---|---|
| 浏览 | bucket 选择与目录导航、分页加载、拖拽上传、上传队列（暂停、继续、取消、断点续传）、单文件下载（分块并发、进度与速度、取消）、目录行下载、新建目录、删除与批量删除、文件/目录签名链接入口（目录打包显示进度） |
| 签名链接 | 查看全部链接、复制、作废；目录链接显示「目录 ZIP」与打包状态（就绪/打包中/失败/失效） |
| Token 管理 | 查看注册表、按权限点生成 token、更新与移除、重新加载配置 |
| 审计 | 按 bucket、key、操作者、起始时间查询 |
| 配置 | 展示配置项分组与生效来源，在线修改并热加载 |

页面通过 `?token=` 可自动填充令牌；令牌仅保存在内存，不写入 `localStorage` 或 `sessionStorage`。前端使用外置脚本与严格 CSP，无内联脚本。

### 桌面客户端与 CLI

```bash
cd client
./run.sh                 # 图形界面
./run.sh --help          # 查看全部参数
```

- 客户端为 tkinter 桌面应用（`artifactdepot_gui.py` 与 `artifactdepot_upload.py`），无必需的第三方运行时依赖；`run.sh` 会检测 Python 3.9+，并按需准备虚拟环境、Tk 运行库与可选的拖拽支持。
- 上传断点写在源文件同目录的 `.<文件名>.artifactdepot-upload.json`，源目录不可写时回退到 `~/.artifactdepot/upload-state/`；下载使用 `.part` 与 `.<目标名>.artifactdepot-download.json`。
- CLI 仅保留上传：`./run.sh --server http://<主机>:8004 --token <token> --bucket <bucket> --path <前缀> --file <本地文件>`；也可用环境变量 `ARTIFACTDEPOT_SERVER`、`ARTIFACTDEPOT_TOKEN` 代替命令行参数。
- 完整功能、断点续传机制与测试方式见 [client/README.md](client/README.md)。

## API 概览

- 基础地址：`http://<主机>:8004`。业务接口成功响应为 `{"code": 0, "message": "success", "data": ...}`；例外包括：`/health`、`/health/live`、`/health/ready` 返回 `{"status": ...}`，`/metrics` 返回文本，下载返回文件流或 304，`/api/docs/{name}?raw=1` 返回纯文本。错误响应为 `{"detail": ...}`（参数校验失败时 `detail` 为数组）并带对应的 4xx / 5xx 状态码。
- 认证：推荐 `Authorization: Bearer <token>`，也兼容查询参数 `token` 与上传表单字段 `token`；签名链接使用 `link` 与 `tk` 参数。
- 权限：管理员共享 token 拥有全部权限；用户 token 按权限点与资源范围（bucket、路径前缀、过期时间）判定。`/api/buckets` 与 `/api/objects/list` 在 `require_read_token=false` 时允许匿名读取，`/api/objects/head` 与 `/api/objects/download` 始终需要 token 或签名链接；携带 token 时一律校验权限与范围。

主要接口分组：

| 分组 | 接口与权限点 |
|---|---|
| 对象 | `POST /api/objects`（`object:upload`）、`GET /api/objects/list`（`object:list`）、`GET /api/objects/download`、`HEAD /api/objects/download`（`object:download` 或签名链接）、`GET /api/objects/head`（`object:head`）、`GET /api/objects/download-bulk`（`object:download`）、`DELETE /api/objects`（`object:delete`）、`POST /api/objects/mkdir`（`object:mkdir`）、`POST /api/objects/rename`（`object:rename`）、`POST /api/objects/bulk-delete`（`object:delete_bulk`） |
| 分片上传 | `POST /api/objects/initiate`、`chunk`、`complete`、`abort`（`upload:initiate`、`upload:chunk`、`upload:complete`、`upload:abort`）；`GET /api/objects/uploads` 为网页使用的内部接口，不进入 OpenAPI |
| 签名链接 | `POST /api/objects/presign`（`link:create`；`kind=file/dir`）、`GET /api/objects/signed-links`（`link:list`）、`POST /api/objects/signed-links/{link_id}/revoke`（`link:revoke`）、`GET /api/objects/signed-links/config`（公开）；目录打包进度/取消为网页内部接口（`GET /api/objects/signed-links/{link_id}/package`、`POST .../package/cancel`，`include_in_schema=false`） |
| Bucket | `GET /api/buckets`（公开或 `bucket:list`）、`POST /api/buckets`（`bucket:create`） |
| Token | `/api/tokens`（`GET` 需 `token:read`，`POST` 与 `PUT /api/tokens/{value}` 需 `token:write`，`DELETE` 需 `token:delete`） |
| 系统与配置 | `/health`、`/health/live`、`/health/ready`、`/metrics`、`GET /api/auth/check`、`GET /api/auth/permissions`、`GET /api/audit`（`audit:read`）、`GET`/`POST /api/config`、`POST /api/config/reload`（`config:write`） |
| 文档与页面 | `GET /api/docs`、`GET /api/docs/{name}`（`?raw=1` 返回纯文本）；`/`（网页控制台）、`/docs`、`/redoc`、`/openapi.json` |

完整参数、状态码与请求示例见 [docs/api_rules.md](docs/api_rules.md)；服务运行后也可通过 `/api/docs` 阅读，或在 `/docs` 中直接调试。

### 调用示例

```bash
BASE=http://127.0.0.1:8004
TOKEN=admin

# 校验令牌
curl -s "$BASE/api/auth/check?token=$TOKEN"

# 上传文件（multipart 表单，bucket/key 可直接使用中文）
curl -s -F "bucket=demo" -F "key=task1/video.mp4" -F "token=$TOKEN" \
     -F "file=@video.mp4" "$BASE/api/objects"

# 列出目录
curl -sG -H "Authorization: Bearer $TOKEN" \
     --data-urlencode "bucket=demo" --data-urlencode "prefix=task1" \
     "$BASE/api/objects/list"

# 下载（推荐使用 Authorization 头）
curl -sG -H "Authorization: Bearer $TOKEN" \
     --data-urlencode "bucket=demo" --data-urlencode "key=task1/video.mp4" \
     -o video.mp4 "$BASE/api/objects/download"

# 生成 1 小时有效的签名链接
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"bucket":"demo","key":"task1/video.mp4","mode":"time","expires":3600}' \
     "$BASE/api/objects/presign"
```

> `bucket` 或 `key` 含中文等非 ASCII 字符时，GET 请求需用 `--data-urlencode`（或自行百分号编码）传递查询参数；直接把中文写进 URL 会被 HTTP 服务器以 400 拒绝。multipart 表单上传不受此限制。

## 测试与开发

运行测试（需先安装 pytest，未包含在 `requirements.txt` 中）：

```bash
ARTIFACT_DEPOT_CONFIG=$(mktemp) PYTHONPATH=src .venv/bin/python -m pytest tests -q
```

- 上述命令使用「安装」章节创建的 `.venv`；若依赖装在别的解释器里，请替换为对应 Python 路径。
- [tests/conftest.py](tests/conftest.py) 会把 `ARTIFACT_DEPOT_ENV` 指向不存在的临时文件，避免读取真实的 `config/.env`；各用例使用独立的临时 depot。
- 客户端测试会启动真实的 uvicorn 子进程并发起真实 HTTP 请求：

```bash
PYTHONPATH=src .venv/bin/python -m pytest client/test_upload_resume.py client/test_parallel_download.py -q
```

测试覆盖的主要方面：

| 范围 | 说明 |
|---|---|
| 接口契约与权限 | 各接口的返回结构、状态码、权限点与资源范围（含 401 / 403 语义、空 `scopes`、越界拒绝） |
| 配置 | 多层加载顺序、`.env` 点号写法、热加载、非法配置回滚、配置页接口 |
| 上传 | 分片会话、暂停与续传、过期清理、临时文件清理、参数上下限 |
| 下载 | Range 与 206 / 416、HEAD 探测、ETag 与 304、分块并发、一键下载目录 |
| 目录与删除 | 空目录删除、批量删除的预览与确认、隐藏目录保护 |
| 可观测性与安全 | 健康检查、指标、请求 ID、CSP、路径穿越、令牌比较、审计轮转 |
| 文档与界面 | 文档接口可用性、文档与界面不含 emoji |

开发约定：

- 新接口放在 `src/artifactdepot/api/` 并在 `main.py` 注册；对象的读写统一经过 `storage.py`，路由不直接拼接存储路径。
- 新增配置项需同步更新 `config/__init__.py` 中的默认值、schema 与脱敏规则；需要重启才能生效的项加入 `RESTART_REQUIRED_KEYS`。
- 命名沿用 S3 语义（bucket、key、mkdir、presign），便于后续对接 MinIO 或 OSS。
- 第一方源文件需带 SPDX 双行署名头；文档与界面不允许出现 emoji（由 `tests/test_no_emoji.py` 校验）。

## 部署与运维

### 构建与升级流程

1. 开发机构建并导出镜像：

```bash
./build_image.sh
```

2. 把 tar 复制到生产机（示例放在部署根）：

```bash
scp ../podman/artifactdepot-<版本>.tar <用户>@<生产机>:~/SERVER/artifactdepot/
```

3. 在生产机导入镜像（按 tar 的实际路径）：

```bash
podman load -i ~/SERVER/artifactdepot/artifactdepot-<版本>.tar
```

4. 在仓库目录执行部署脚本（脚本需要读取仓库内的 `VERSION` 与生产配置模板）：

```bash
cd <ArtifactDepot 仓库目录>
./deploy_container.sh
```

5. 首次部署后编辑 `<部署根>/config/config.json`，设置 `access_token`，然后重启容器。

升级时挂载目录不变，重新导入镜像并再次执行 `deploy_container.sh` 即可原地沿用配置与数据。部署脚本不构建镜像；镜像标签取自 [VERSION](VERSION)，也可用环境变量 `IMAGE_TAG` 覆盖。

### 运行状态与日志

| 项目 | 位置或方式 |
|---|---|
| 存活检查 | `GET /health/live` |
| 就绪检查 | `GET /health/ready`，`depot_dir` 不存在或不可写时返回 503 |
| 指标 | `GET /metrics`（Prometheus 文本格式，`metrics_enabled=false` 时返回 404） |
| 审计日志 | `<meta_dir>/audit.log`，JSONL，权限 600，按 `audit_max_mb` 轮转 |
| 应用日志 | 本地运行写入 `logs/artifactdepot.log`；容器内由 supervisor 写入 `/tmp/artifactdepot.out.log`，可用 `podman exec artifactdepot cat /tmp/artifactdepot.out.log` 查看 |
| 生产诊断 | `bash check_ad_sync.sh [容器名]`，检查容器状态、挂载、环境变量、配置、健康检查与端口连通性 |

### 备份与迁移

- 需要备份的目录：`<部署根>/data`（业务文件）、`<部署根>/state`（状态文件）与 `<部署根>/config`（配置）。
- 迁移时停止容器，把三个目录复制到新主机的同一部署根，导入镜像后重新执行 `deploy_container.sh`。

### 运行约束

- 分片上传的会话注册表保存在进程内，启动时从数据盘扫描重建，因此服务以单进程运行（镜像内 supervisor 启动单个 uvicorn，未启用 `--workers`），也不要让多个实例共用同一 `depot_dir`。
- [sync_artifacts.sh](sync_artifacts.sh) 是通用的 rsync 产物同步脚本，目标地址需在 [sync_artifacts.conf](sync_artifacts.conf) 中按实际环境填写。

### 已知问题

- [deploy_container.sh](deploy_container.sh) 接受 `--token` 参数以及 `ARTIFACT_DEPOT_ACCESS_TOKEN` 环境变量，但当前版本只解析、未注入容器（脚本帮助文本仍描述为经临时 env-file 注入）。请改用 `<部署根>/config/config.json` 或同目录 `.env` 配置后再重启容器。

## 安全说明

- **令牌**：默认共享令牌 `admin` 仅用于内网开箱即用，生产环境必须替换为至少 16 位随机串；弱令牌会在启动时告警，设置 `ARTIFACT_DEPOT_STRICT_CONFIG=1` 时会拒绝启动。
- **传输**：优先使用 `Authorization: Bearer` 传递令牌，访问日志会将 `token`、`tk`、`sig` 等参数脱敏。服务本身不内置 TLS 终止，跨网或公网访问应置于反向代理或网关之后。
- **权限**：22 个权限点与 9 种角色预设；写操作需要对应的权限点与资源范围；目录仅允许为空时删除；批量删除必须输入目录名确认；`config:write` 不能轮换 `access_token`。
- **读接口**：`/api/buckets` 与 `/api/objects/list` 默认匿名可读，适用于内网部署；需要限制读取时设置 `require_read_token=true`，并通过 token 授予 `bucket:list` 与 `object:list`。`/api/objects/head` 与 `/api/objects/download` 始终需要 token 或签名链接。
- **审计**：上传、下载、删除、建目录、改名、签名、Token 变更等操作均写入审计，记录操作者、客户端 IP（配置可信代理时另记解析出的公网 IP）、UTC 时间、大小与 SHA-256。
- **数据保护**：状态文件原子写且权限为 600；路径穿越与隐藏路径有防护；`.artifactdepot-uploads` 不会被列表、打包或批量删除破坏。
- **密钥管理**：敏感项放在 `.env`（不进 git 与镜像），生产配置由部署根下的配置文件经宿主机卷管理；不要把真实令牌写入文档、示例或提交记录。
- **已知限制**：服务按单实例设计；签名链接按次数计数时，分块并发下载会为每个 Range 请求消耗一次次数，大文件下载建议使用 token；重命名操作非事务，底层 I/O 异常可能留下部分移动。目录打包会在源目录 `.package` 内额外占用一份成品空间（唯一文件复制、其他 ZIP），大目录请用 `package.max_total_mb` / `max_items` 限制；打包任务在后台线程执行，进程重启后未完成的打包会由启动清理重新排队（已在 `.ready.json` 标记的成品不受影响）；永久目录链接的包会一直保留到作废或源目录删除。

安全摘要与逐项说明见 [docs/api_rules.md](docs/api_rules.md)。

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| 上传或下载提示「无效的访问令牌」 | token 未登记、已过期或已禁用；先用 `GET /api/auth/check` 自测，必要时在「Token 管理」中重建 |
| 默认 `admin` 令牌能否直接使用 | 仅适用于内网联调；生产环境必须替换为强随机串 |
| 部署后如何设置管理员令牌 | 编辑 `<部署根>/config/config.json`（或同目录 `.env`）中的 `access_token`，再执行 `podman restart artifactdepot` 或 `docker restart artifactdepot` |
| 一次下载出现多条审计 | 分块并发下载对每个 Range 请求各写一条 `download` 审计（`partial=true`，`size` 为区间字节数）；`HEAD` 记为 `head=true` 且 `size=0`；完整 GET 只有一条 |
| 下载返回 416 | 请求区间越界，或下载期间对象被替换导致大小变化；重新 `HEAD` 探测后再下载 |
| 打开目录签名链接返回 409「目录打包中」 | 后台还在扫描/打包；等进度完成后再打开。若打包失败，链接会自动作废，需重新生成（可在「签名链接」页查看状态） |
| `.package` 是什么、能否删除 | 目录签名链接的成品目录（隐藏），由服务端管理；链接失效后自动清理，不要手工修改或依赖其内容 |
| 网页下载没有分块并发 | 浏览器不支持 File System Access 且文件超过 1 GiB、服务端未返回 `Accept-Ranges` 或忽略 Range 时，会回退为原生单流下载，属于预期行为 |
| 上传失败或断网后是否需要重传整个文件 | 不需要。分片会话与已上传分片保留在服务端，再次上传同一文件时只补缺失分片；超过 `upload.session_ttl_seconds` 后会话被清理 |
| 网页刷新后如何续传 | 浏览器无法恢复文件对象；重新选择同一文件，或在「未完成的上传」中点击「继续上传」，按 `bucket/key/size` 找回会话 |
| 刷新后未完成的上传为什么没有暂停按钮 | 那是服务端保留的断点会话，本页没有进行中的传输，因此只提供「继续上传」与「取消上传」 |
| `.artifactdepot-uploads` 可以删吗 | 不要手动删除；其中是正在上传或可续传的分片，完成或超过 TTL 后服务端会自动清理 |
| 服务重启后还能续传吗 | 能。会话保存在数据盘，启动时扫描并重建注册表；前提是保持单进程运行 |
| 客户端续传状态文件在何处 | 上传为源文件同目录的 `.<文件名>.artifactdepot-upload.json`；下载为 `.part` 与 `.<目标名>.artifactdepot-download.json`；源目录不可写时上传状态回退到 `~/.artifactdepot/upload-state/` |
| 删除提示「目录非空」 | 目录仅允许为空时删除；先清空内容，或使用批量删除（保留目录本身） |
| 删除或批量删除提示权限不足 | 缺少 `object:delete` 或 `object:delete_bulk` 权限点，且资源范围需覆盖目标 bucket 与前缀；由管理员在「Token 管理」中勾选 |
| 审计时间为 UTC 或公网 IP 为空 | 审计统一记录 UTC，前端会转换为浏览器本地时间；公网 IP 仅在直连方命中 `trusted_proxies` 时从 `X-Forwarded-For` 解析，未配置或无代理时为空 |
| 启动报 `No module named uvicorn` | 当前解释器缺少运行依赖；按「安装」创建虚拟环境并安装 `requirements.txt` |

## 许可证与支持

- **许可证**：Apache License 2.0（SPDX 标识 `Apache-2.0`）。英文版见 [LICENSE](LICENSE)，中文版见 [LICENSE.zh-CN.md](LICENSE.zh-CN.md)。
- **版权**：Copyright 2026 apyyc。
- **源文件署名**：第一方源文件带 `# Copyright 2026 apyyc` 与 `# SPDX-License-Identifier: Apache-2.0` 双行头。运行 `python3 scripts/add_spdx_headers.py` 补写，`python3 scripts/add_spdx_headers.py --check` 只检查（缺头时退出码为 1，可用于 CI）。第三方 vendored 静态资源保留原作者版权，不加本声明。
- **支持**：2479511984@qq.com；版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 相关文档

| 文档 | 内容 |
|---|---|
| [docs/api_rules.md](docs/api_rules.md) | 完整 HTTP 接口、参数、状态码、权限点与角色、配置项、安全摘要 |
| [docs/architecture.md](docs/architecture.md) | 系统拓扑、存储布局、可靠性要点、可观测性与部署说明 |
| [client/README.md](client/README.md) | 桌面客户端与 CLI 的启动方式、功能、断点续传与测试 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [LICENSE](LICENSE) / [LICENSE.zh-CN.md](LICENSE.zh-CN.md) | Apache-2.0 许可证（英文 / 中文） |
