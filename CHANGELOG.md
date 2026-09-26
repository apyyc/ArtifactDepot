# Changelog

DataWarehouse v0.5.5 — 所有对本项目的重要更改都将记录在此文件中。

本日志格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本规范](https://semver.org/lang/zh-CN/)。

---

## [0.5.5] - 2026-09-26

### Changed

- 版本号统一为 `0.5.5`（镜像标签、应用版本、文档与部署脚本）。

### Fixed

- **Podman 构建保留 HEALTHCHECK**：`build_image.sh` 在 Podman 下自动追加 `--format docker`，避免默认 OCI 格式忽略 `HEALTHCHECK`。
- **`deploy_container.sh` 真正兼容 Docker**：镜像检查改用 Podman/Docker 都支持的 `image inspect`；Docker 下不使用 `podman generate systemd`，自动改用 `--restart=always`。
- **`meta_dir` 语义修复**：部署时默认不再注入 `WAREHOUSE_META_DIR`，以容器内 `config.json` 为准；新增 `--meta-dir` 供显式覆盖。
- **默认数据目录可回退**：`~/SERVER` 不可写时自动回退到 `${XDG_DATA_HOME:-~/.local/share}/datawarehouse/...`，避免开箱即用失败。
- **默认配置路径统一**：`deploy_container.sh` 自动探测 `~/SERVER/datawarehouse/config/config.json`，新增 `--config` 显式挂载；修正数据卷/配置路径文档。
- `supervisord` 配置消除 root 与 `redirect_stderr` 警告。
- 新增 `.dockerignore`，构建上下文排除 `*.tar`、`warehouse/`、`logs/`、`__pycache__` 等无关大文件。
- 补齐 `config.production.json` 与生产诊断脚本 `check_dw_sync.sh`。

## [0.5.4] - 2026-09-26

### Added

- API 文档补充统一响应例外、422 校验错误、`public_ip` 可选参数、DataHub 同步响应字段与列表边界行为。
- 下载接口与 `/api/auth/check` 支持 `Authorization: Bearer <token>`。

### Changed

- 管理接口权限语义修正：无 token / 无效 token 返回 401；已认证普通用户调用管理接口返回 403。
- 对象 key 的隐藏路径段规则收紧：任意路径段以 `.` 开头均拒绝上传，与 `.warehouse.json` / `.keep` 隐藏约定保持一致。
- 文档与构建/部署脚本版本号统一为 0.5.4。

### Fixed

- 签名链接下载先检查对象是否存在，再扣减次数，避免对象不存在时仍消费链接次数。
- 手工放入、无 `.warehouse.json` 记录的文件列表 `mtime` 回退为文件系统 mtime。
- 空目录列表 `mtime` 改为目录自身 mtime，不再返回当前时间。
- 对象操作不再接受空 key：防止 `DELETE /api/objects?key=` 误删整个空 bucket，同时拒绝空 key 的 presign/download/上传落盘。
- 审计查询 `limit=0` 返回空列表，负数返回 422，避免负数导致返回数量异常。

## [0.5.3] - 2026-08-15

### Added

- **目录列表显示递归大小与最新修改时间**：目录项不再显示「-」，改为显示其全部文件（含嵌套子目录 + 当前目录）的总字节数（`fmtSize`）与所有文件里最新的 mtime（`fmtTime`）；计算跳过隐藏占位文件（`.keep` 等）。后端 `list_objects` 对目录调用新增的 `_dir_recursive_info()` 递归统计。

## [0.5.2] - 2026-08-14

### Changed

- **本地启动端口可配**：`dev_start_headless.sh` 支持 `DW_PORT` 环境变量覆盖默认端口 8004（并把 `--port` 透传给 `scripts/start.sh`），`start`/`restart`/`status` 均按变量端口判断

## [0.5.1] - 2026-08-11

> 本次为**协作平台集成**发布：Web UI 支持从 URL 携带 token/bucket 进入（协作平台「任务数据」跳转自动认证）。

### Added

- **Web UI 支持 URL 携带 token/bucket 进入**：访问 `/?token=<api_token>[&bucket=<bucket>]` 时，自动把 token 填入令牌输入框（`#upToken` / `#lkToken`）、自动进入指定 bucket；随后 `history.replaceState` 立即清除地址栏里的 token，避免 token 进入浏览器历史 / Referer / 日志
- 自动填充后调用 `/api/auth/check` 校验一次，若有效则在页面右上角提示「已识别身份：<用户名>」

### Fixed

- **旧数据时间缺 `Z` 后缀导致前端差 8 小时**：时间修复前的 mtime / 审计 / 签名链接旧数据存 UTC 但未加 `Z`，前端 `fmtTime` 只转换带 `Z` 的时间，无 `Z` 的原样显示，恰好差 8 小时。新增 `_ensure_utc_z()`：在返回层对无 `Z` 的标准 ISO 时间补 `Z`（空值 / 已带 Z / 非标准格式原样），覆盖文件列表 mtime、审计日志 time、签名链接 created_at；数据文件不改（保持只读）

### Notes

- 配合 ProjectCollaborationPlatform 0.5.1「任务数据」子模块使用：从协作平台点击「进入数据仓库」即携带当前登录用户 token 跳转，无需手动填写令牌

## [0.5.0] - 2026-08-08

> 本次为**配置与部署**发布：新增 `meta_dir` 状态文件目录；修复生产环境 token 同步失败的全链路（Dockerfile 烤死环境变量 + 网络默认）；构建脚本**自动导出 tar**；新增生产配置模板与一键诊断脚本。

### Added

- **`meta_dir` 配置项**：把状态文件（`tokens.json` / `signed_links.json` / `audit.log`）从仓库根移出到独立目录（留空 = 原行为放 `warehouse_dir` 根下；相对路径按 `warehouse_dir` 解析）。新增环境变量 `WAREHOUSE_META_DIR` 覆盖
- **`build_image.sh` 构建完自动导出 tar**：默认 `./build_image.sh` 即构建 + 导出 `datawarehouse-0.5.0.tar`；`--no-save` 跳过、`--save [路径]` 指定、`--tag` 覆盖版本标签、`--load` / `--list` 保留
- **`config.production.json`**：生产可整份复制的配置模板（`warehouse_dir=/data/warehouse`、`meta_dir=/data/warehouse/state`、host 网络 `datahub_url=127.0.0.1:8002`）
- **`check_dw_sync.sh`**：生产连通性诊断脚本（只依赖 python3，无需 curl/wget/jq）—— 校验容器内配置 / 应用实际同步 / 容器内访问 DataHub / 宿主对照 / 端口监听 / 挂载列表，逐段定位 "All connection attempts failed"

### Changed

- **`deploy_container.sh` 默认 host 网络**：`DO_HOST_NETWORK=true`，容器共享宿主网络不再映射端口（同机连 DataHub `127.0.0.1:8002` 最稳，规避 rootless pasta 网桥连不通宿主发布端口）；`--port-map` 才改用端口映射
- **`deploy_container.sh` 不再默认注入 `datahub_url`**：只有显式 `--datahub-url` / 环境变量 `WAREHOUSE_DATAHUB_URL` 才注入，否则以容器内配置文件的 `datahub_url` 为准（修复：旧脚本默认把 127.0.0.1 注入容器，导致同步指向自身）
- **`Dockerfile` 移除烤死的 `WAREHOUSE_DATAHUB_URL=127.0.0.1` ENV**：`config.py` 里环境变量优先级高于配置文件，镜像内预设默认值会把挂载 config.json 的 `datahub_url` 盖掉（正是生产 "All connection attempts failed" 的根因之一）。`datahub_url` 以容器内配置文件字段为准
- **`deploy_container.sh` 新增 `VOLUME_MAPS`**：脚本顶部列表，每项 `宿主机:容器内` 追加挂载；典型用途把改好的 config.json 挂进容器（配置显式可改、无需重建镜像）
- 默认数据目录随版本：`~/SERVER/datawarehouse/datawarehouse_0.5.0`

### Fixed

- **生产 token 同步失败（"All connection attempts failed"）全链路**：
  1. 部署脚本默认注入 `WAREHOUSE_DATAHUB_URL=127.0.0.1`（指向容器自身），改为默认不再注入
  2. Dockerfile `ENV` 烤死 `WAREHOUSE_DATAHUB_URL` 盖掉挂载配置，移除该 ENV（需重建镜像生效）
  3. collab 容器 DataHub 只绑 `127.0.0.1:8002`，宿主访问被拒，supervisord.conf 改绑 `0.0.0.0`
  4. rootless pasta 桥接下容器连宿主发布端口不通，DW 容器改默认 host 网络
  （第 1、3、4 项为配置/脚本/容器改动，第 2 项为镜像构建改动）

---

## [0.4.0] - 2026-08-08

> 本次为**功能 + 安全策略**发布：新增拖拽上传 / 签名链接注册表 / 新建目录 / 双 IP / 时间修复；收紧下载与删除权限；多项体验与健壮性修复。

### Fixed

- **从 DataHub 同步**：`/api/tokens/sync` 返回 `ok / error`，DataHub 不可达时前端明确提示（不再误报「已同步」）；同步保持**只增不删**
- **非 ASCII 令牌不再 500**：`check_admin_token` / `verify_signature` 的 `hmac.compare_digest` 改为 UTF-8 字节比较，令牌含中文/特殊符号时返回 401 而非崩溃
- **复制链接兼容 HTTP**：`navigator.clipboard` 仅在 HTTPS/localhost 可用，局域网 HTTP 下自动降级 `document.execCommand('copy')`
- **原生弹窗失效**：浏览器拦截 `confirm/prompt`，全部改为**页面内自定义确认框 / 弹窗**

### Added

- **`GET /api/auth/check`**：轻量校验 token（返回 `valid + actor`），拖拽上传前先审核
- **拖拽上传**：整页拖放区 + 高亮遮罩，松手自动上传；支持**多文件**排队上传（各自进度条）
- **签名链接注册表** `signed_links.json`：三种时效 **count(按次数) / time(按时效) / permanent(永久)**
- **签名链接管理**：任一有效 token 可见全部；完整链接 URL 仅管理员/创建者可看（含每行**复制链接**）；管理员可作废全部、创建者可作废自己的
- **新建目录**：`POST /api/objects/mkdir` + 网页「新建目录」按钮（`key` 可含 `/` 多级，`.keep` 占位隐身）
- **签名链接上下限配置**：`config.signed_links`（`count_min/max`、`expire_min/max` 秒）+ `GET /api/objects/signed-links/config`
- 网页文件列表新增**删除**按钮（文件 + 目录，仅管理员令牌）

### Changed

- **下载需 token**：非签名链接必须带有效 token（管理员或用户），`actor` **从 token 推导**（去掉客户端可伪造的 `actor` 参数）
- **删除仅管理员 + 目录仅空可删**：`DELETE /api/objects` 收严为 `require_admin`，用户 token 不再可删；**非空目录删除返回 400**（防误删，需先清空）；管理员 token 为全集
- **网页令牌框统一**：浏览页合并为单个「令牌」输入框，用户或管理员 token 通用
- **审计双 IP**：新增 `public_ip`（前端调 ipify 上报公网 IP），与 `ip`（局域网）**两列并存**（审计页显示两列）
- **时间修复**：后端统一记录 **UTC**（ISO-8601 + `Z`）；前端浏览/审计/签名链接页**转浏览器本地时间**显示；Dockerfile 设 `TZ=Asia/Shanghai` 兜底
- **签名链接弹窗重构**：类型单选（按时效/按次数/永久）**互斥**——选按时效不显示次数（消除歧义）；生成的链接**内嵌显示 + 一键复制**；签名链接列表**按时间降序**、次数用完显示「已用完」

---

## [0.3.4] - 2026-08-07

### Added

- **容器化部署**
  - 新增 `Dockerfile`（python:3.12-alpine + supervisor + 非 root + `VOLUME /data/warehouse` + 健康检查）
  - 新增 `docker/supervisord.conf`（supervisor 托管 uvicorn 8004）
  - 新增 `deploy_container.sh`（参照 collab：建容器 + systemd 开机自启；支持 `--token` / `--datahub-url` / `--data-dir` / `--no-systemd` / `--stop` / `--rm`）
  - 新增 `dev_start_headless.sh`（本地后台启动 + status/tail/logs/stop）
  - `config.py` 支持**环境变量覆盖**：`WAREHOUSE_DIR` / `WAREHOUSE_DATAHUB_URL` / `WAREHOUSE_ACCESS_TOKEN` / `WAREHOUSE_PORT` / `WAREHOUSE_MAX_UPLOAD_MB`
  - Dockerfile 安装 `python-multipart`（上传必需）

- **网页 UI 增强**
  - bucket 列表**可点击选中**（蓝色胶囊 + 选中高亮），页面加载自动列出
  - API Token 输入框移到顶部 bucket 区域（上传 / 签名链接共用）
  - 文件列表卡片移到「上传文件」下方

- **文档**
  - README 重写为完整规范文档（核心特性 / 架构与工作流 / 环境要求 / 安装 / 快速开始 / 配置详解 / 目录结构 / 核心模块 / API 参考 / 规范 / 常见问题 / 许可）

### Changed

- 构建命令统一为 `env -u HTTP_PROXY ... podman build --network=host -t localhost/datawarehouse:0.4.0 .`

## [0.3.0] - 2026-08-07

### Added

- **从 DataHub 拉取用户 token（collab 为权威源，本地 tokens.json 为缓存副本）**
  - 新增 `config.datahub_url`（默认 `http://127.0.0.1:8002/api/data`）
  - `sync_tokens_from_datahub()`：拉取 DataHub `users.json`，构建 `{api_token: user_name}`（跳过空 token），合并写回本地 `tokens.json`
  - 同步触发：
    - **启动时**：lifespan 自动拉取一次
    - **惰性**：写操作 token 未命中本地时自动拉一次再判断（新用户 token 即时生效，60s 防抖防打爆 DataHub）
    - **手动**：新增 `POST /api/tokens/sync`（管理员 token）+ 网页「Token 管理」页的「从 DataHub 同步」按钮
  - 合并语义：DataHub 覆盖本地同名；本地独有条目保留；DataHub 不可用时不阻断、沿用本地缓存

## [0.2.0] - 2026-08-06

### Added

- **Token 注册表（token 对应用户名）**
  - 新增 `<warehouse_dir>/tokens.json`，记录每个用户 api_token 对应的用户名
  - 管理接口（需管理员共享 token）：`GET/POST/DELETE /api/tokens`
  - 平台在注册/查 Token 时自动推送登记；网页「Token 管理」标签可查看/添加/移除

- **写操作操作者识别（actor）**
  - 上传/删除/预签名时解析令牌：共享 token 识别为「系统/工具」；注册表命中识别为用户名；否则 401 拒绝
  - 修复此前只校验共享 token、无法区分「谁上传」的问题

- **审计日志（谁在何时做了什么）**
  - 追加写 `<warehouse_dir>/audit.log`（JSONL，一行一条，只增不改）
  - 记录：time / action(upload/delete/download/presign) / bucket / key / actor / ip / size / sha256
  - 下载记 actor（平台模块带身份）或 anonymous + 来源 IP；预签名记生成者
  - `GET /api/audit`（需管理员 token）：按 bucket/key/actor/since 过滤查询
  - 网页「审计」标签可筛选查看

### Changed

- 上传接口 token 校验从「仅管理员共享 token」改为「管理员 token 或已登记用户 token」

## [0.1.0] - 2026-08-06

### Added

- **初始版本：对象存储仓库站点（S3 模型）**
  - 项目骨架：`src/datawarehouse/`（FastAPI），配置、存储引擎、认证、API、网页 UI 分层
  - 存储布局：`<warehouse_dir>/<bucket>/<key>`，bucket=项目，key=任务/文件；每 bucket 一个隐藏元数据清单 `.warehouse.json`（size/sha256/mtime/source_url）
  - 文件系统为唯一事实源，列表以目录扫描为准；手工放入的文件也能列出
  - 路径穿越防护：bucket 名校验 + key 拒绝绝对路径/`..`/空段/反斜杠/点前缀

- **S3-like API**
  - `POST /api/objects` — multipart 上传（流式落盘，SHA-256 校验，`max_upload_mb` 上限）
  - `GET /api/objects/list` — 列对象（bucket/prefix，返回 items + 目录）
  - `GET /api/objects/download` — 下载，Starlette FileResponse 原生 Range(206)，视频可拖动
  - `DELETE /api/objects` — 删除对象/目录（含清单子项）
  - `POST /api/objects/presign` — 限时签名下载 URL（HMAC-SHA256）
  - `GET /api/buckets`、`GET /health`

- **网页 UI**
  - `GET /` 单页浏览：选 bucket、目录导航、文件行（大小/时间/来源/下载/签名链接）+ 上传表单

- **安全**
  - 写操作 `access_token` 认证（query / Bearer，常量时间比较）
  - 读操作默认内网开放，预签名 URL 限时访问

- **文档**：README（API/存储布局/集成指引）、CHANGELOG、docs/architecture.md、scripts/start.sh
