# ArtifactDepot — 对象存储仓库站点

> 版本 0.10.0，端口 8004，独立服务

对象存储 / S3模型设计的文件仓库站点，收存处理产物（视频 / 文件），提供上传（分片并发、暂停/继续、断点续传）、列表、Range 分块并发下载（断点续传）、一键下载目录、删除、新建目录、签名链接与审计，并带网页界面。业务 JSON 数据在 DataHub，**本服务不承担业务逻辑**。

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
| 上传 | multipart 流式落盘 + SHA-256 + `max_upload_mb` 上限；**整页拖拽上传**（多文件排队、上传前先校验 token、并发数可调）；文件列表新增**上传者**列（按 token 识别）；**大文件自动多线程分片上传**（按 `upload.chunk_size_mb`，默认 8MB；分片并发/请求超时由 `upload.*` 配置，超时默认 24h）；**分片会话与临时文件随文件落盘**（目标父目录 `.artifactdepot-uploads/<upload_id>/`，不再写 `/tmp`）；**支持暂停/继续与失败后断点续传**（`initiate(resume=true)` 返回磁盘真实已传分片，只补缺失分片）；校验类失败保留会话，启动扫描 + 后台 10 分钟回收默认 24h 无写入会话；网页可按当前 token 列出未完成上传并手动取消（只清理自己的会话） |
| 下载 | Starlette `FileResponse` 原生 **Range(206)**（视频可拖动、断点续传）；**需有效 token**（签名链接免 token）；**大文件分块并发下载**（默认 8MB × 4 路，`download.*` 可配）：网页与桌面客户端先 `HEAD` 探测，再并行 `Range` 下载，支持失败重试、进度/速度、取消、SHA-256 完整性校验，并按环境回退「边下边写盘 / 内存 Blob / 原生单流」；**一键下载目录**（`GET /api/objects/download-bulk`，复用 `object:download`）：单文件直下，多文件/含子目录**流式打包 ZIP**（顶层保留目录结构，跳过隐藏项与符号链接；ZIP 无 `Content-Length`，保持单流不做分块） |
| 伴随文件本地化 | 上传/下载自动创建的临时文件与断点文件都放**「文件所在目录」**：服务端分片会话 `<目标父目录>/.artifactdepot-uploads/<upload_id>/`（`.session.json` / `chunk_<i>.part` / `.incoming` / `.merged`）、直传临时 `.<name>.artifactdepot-incoming-*.tmp`、框架 multipart spool（`upload.staging_dir`，默认 `<depot_dir>/.staging`）；客户端上传断点 `.<源文件名>.artifactdepot-upload.json`、下载 `.part` 与 `.<目标名>.artifactdepot-download.json`；源目录不可写时上传断点回退 `~/.artifactdepot/upload-state/` |
| 暂停/续传 | 网页按**每个上传任务行**独立暂停/继续（按钮只在对应任务进度条右侧，分片边界生效、已传分片不重传），并可查看/取消当前 token 的未完成会话；桌面客户端支持**暂停/继续上传**（分片边界生效、已传分片不重传），并发分片下载另支持**暂停/继续下载**。上传失败/暂停/取消**不丢弃会话**，再次开始（或刷新后重新选同一文件）即从断点续传；下载失败/暂停保留 `.part` + 状态（桌面），校验 `size`/ETag 后只补缺失分片；**服务重启后仍可续传**（会话在数据盘，启动扫描重建内存 registry） |
| 缓存/条件请求 | 下载/探测返回 **ETag**（基于 SHA-256），支持 `If-None-Match` → **304**，重复访问省流量 |
| 目录管理 | **新建目录**（`key` 含 `/` 自动多级）；**删除仅允许空目录**（含内容拒绝，防误删）；**批量删除目录内容**（`object:delete_bulk`）：`files` 只删当前目录的直接文件、`all` 递归清空文件与子目录，保留目录本身、保护 `.warehouse.json`，执行前必须输入目录名确认 |
| 目录信息 | 目录项显示**递归大小**（本目录 + 全部嵌套子目录的文件总字节，`fmtSize`）与**最新修改时间**（所有文件里最新的 mtime，`fmtTime`）；跳过 `.keep` 等隐藏占位文件 |
| 签名链接 | 注册表驱动：**按次数(1-10) / 按时效 / 永久** 三种时效；管理员可作废全部、创建者可作废自己的；上下限可配置 |
| 身份识别 | 按令牌识别「谁」：共享 token 识别为「系统/工具」；用户 token 识别为用户名；**管理员 token 为全集**；删除为权限点 `object:delete`（普通 `user` 预设不含；`operator` / `admin` 含）；批量删除为独立权限点 `object:delete_bulk`（`operator` 预设包含） |
| 审计日志 | 每次上传/下载/删除/建目录/签名/作废追加 `audit.log`（JSONL），**双 IP**（局域网 + 公网）、**UTC 时间**；按 `audit_max_mb` 自动轮转 |
| 可观测性 | `X-Request-ID` 透传 + JSON 结构化访问日志；`/metrics`（Prometheus 文本）；`/health/live`（存活）/ `/health/ready`（可写就绪） |
| Token 同步 | 从 DataHub `users.json` 拉取用户 token（collab 权威源，本地副本，只增不删；DataHub 不可达时明确提示） |
| 状态文件可迁移 | `meta_dir` 配置项把 `tokens.json` / `signed_links.json` / `audit.log` 移出仓库根，与业务文件隔离、便于备份 |
| 生产部署 | 脚本**构建完自动导出 tar**（`--no-save` 跳过）；部署默认 **host 网络**（容器内 `127.0.0.1` = 宿主机，同机连 DataHub 最稳）；Podman 走 systemd user 自启，Docker 走 `--restart=always`；`--config` / `VOLUME_MAPS` 可挂载外部 config.json |
| 网页 UI | bucket 点击选中 + 高亮、整页拖拽、新建目录、**每个上传任务进度条右侧的「暂停上传 / 继续上传」，失败行「失败（可续传）」+「继续上传」只补缺失分片；按当前令牌列出未完成上传并可手动取消；必须先填令牌才能选择文件；令牌输入框跨标签页自动同步**、**单文件分块并发下载（进度/速度/取消，支持边下边写盘与原生回退）**、**一键下载目录（单文件直下，多文件/含子目录流式 ZIP，无确认）**、**批量删除（两个危险按钮 + 预览 + 输入目录名确认）**、签名链接管理、Token 管理、审计、**「配置」标签页（管理员在线修改 config.json 并热加载）**、文件列表 **「加载更多」分页**；标签栏右侧提供 **Swagger / ReDoc** 入口（静态资源自托管，内网/离线可用）；前端脚本/样式外置 + 严格 **CSP**（`script-src 'self'`）；自定义确认框 |
| 跳转免填 | 支持 URL 携带 token/bucket 直接进入：`/?token=<api_token>[&bucket=<bucket>]` 自动填充令牌（五个标签页一起填）并进入目标 bucket，落地即清除地址栏 token（协作平台「任务数据」跳转自动认证） |

---

## 架构与工作流

### 数据流

- 处理端（carryvideo server.py，跑在 8 或本机）处理完成后把产物推送进来（`POST /api/objects`，token 认证）
- ArtifactDepot（FastAPI :8004）由 `storage.py` 负责 bucket/key 落盘 + 元数据清单 + 签名链接注册表 + 审计：
  - `POST /api/objects` 上传（token 认证，可先 `/api/auth/check` 校验）
  - `POST /api/objects/initiate|chunk|complete|abort` 分片上传（会话落目标父目录 `.artifactdepot-uploads`，支持暂停/续传；直传与合并走 `put_object(move=True)` 原子落盘）
  - `POST /api/objects/mkdir` 新建目录
  - `POST /api/objects/bulk-delete` 批量删除目录内容（保留目录本身，需输入名称确认）
  - `GET /api/objects/list` 列文件
  - `GET /api/objects/download` 下载（需 token；签名链接免；支持 `HEAD` 探测 + `Range` 206 分块并发；每个 Range 单独写审计）
  - `GET /api/objects/download-bulk` 一键下载目录（单文件直下 / 多文件或含子目录流式打包 ZIP）
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
- 分片上传续传：复用 `upload:initiate`（**不新增权限点**，旧 token 无需变更）；`initiate(resume=true)` 返回服务端磁盘上真实已传分片，`uploaded` 为唯一事实源；会话绑定 owner（admin 例外）
- 下载：任一有效 token；签名链接（`link`+`tk` / 旧式 `expires`+`sig`）免 token；大文件由网页 / 桌面客户端按 `download.*`（默认 8MB × 4 路）`HEAD` 探测后并行 `Range` 下载，**每个 Range 请求一条 `download` 审计**（`partial=true`、`size` 为区间字节数；HEAD 记 `head=true`、`size=0` 且不计下载指标）
- 一键下载目录：复用 `object:download`（**不新增权限点**，旧 token 立即生效）；仅支持 token（不接签名链接）；范围按 bucket/prefix 校验
- 删除：权限点 `object:delete`（普通 `user` 预设不含；`operator` / `admin` 含）；且**目录仅空可删**（含文件/子目录拒绝）
- 批量删除：权限点 `object:delete_bulk`（`operator` 预设包含）；执行必须输入目录名（bucket 根目录输入 bucket 名）并由服务端二次校验；只清内容不删目录本身，bucket 根 `.warehouse.json` 受保护
- 签名链接作废：管理员可作废全部，创建者可作废自己的

### 生产部署与升级流程

**build、save、拷贝、load、deploy**（版本目录随版本走，数据卷独立保留）：

1. 开发机构建并自动导出

```bash
cd ArtifactDepot
./build_image.sh                     # 构建 localhost/artifactdepot:0.10.0 + 自动导出 artifactdepot-0.10.0.tar
```

2. 把 tar 拷到生产机

```bash
scp artifactdepot-0.10.0.tar apyyc@<生产机>:~/SERVER/artifactdepot/
```

3. 生产机导入并部署

```bash
cd ~/SERVER/artifactdepot
podman load -i artifactdepot-0.10.0.tar
cd ArtifactDepot
./deploy_container.sh --token <你的管理员token>   # 默认 host 网络；datahub_url 用挂载的 config.json
```

> 升级到新版本时挂载目录**不再变化**（三卷固定为 `<部署根>/{config,data,state}`），
> 直接部署即可原地沿用数据与配置。
> 若 `~/SERVER` 不可写，脚本会自动回退到 `${XDG_DATA_HOME:-~/.local/share}/artifactdepot/{config,data,state}`。

---

## 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.10+（含 `fastapi`、`uvicorn`、`httpx`、`python-multipart`） |
| 容器（部署） | Podman 或 Docker |
| 网络 | 局域网可访问；DataHub（8002）与本服务可互通（**同机部署推荐 host 网络**，容器内 `127.0.0.1:8002` 直连） |

---

## 安装

### 本地安装依赖（推荐 uv 本地虚拟环境）

```bash
# 在 ArtifactDepot 仓库或其上级工作区创建 .venv 均可（启动脚本会自动识别）
uv venv .venv --python /usr/bin/python3.13
uv pip install --python .venv/bin/python -r requirements.txt
```

> 本工作区已自带 uv：`/home/apyyc/work/ArtifactDepot/.tools/uv`。
> 若系统 Python 已装依赖，也可继续用 `pip install -r requirements.txt`；但不建议依赖系统环境。

### 构建镜像（自动导出 tar）

```bash
cd ArtifactDepot
./build_image.sh                      # 构建 + 自动导出 artifactdepot-0.10.0.tar
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--no-save` | 只构建，不导出 tar |
| `--save /path/to/out.tar` | 指定导出路径（默认同目录 `artifactdepot-<tag>.tar`） |
| `--no-cache` | 不用构建缓存（依赖层有改动时用） |
| `--tag <版本>` | 自定义版本标签（默认 0.10.0） |
| `--load /path/to/in.tar` | 不构建，直接导入已有 tar |
| `--list` | 只列出本地镜像 |

> 脚本内部自动 `env -u HTTP_PROXY ... <podman|docker> build --network=host`：绕本机代理对 docker.io 的拦截 + 绕容器网桥 DNS 失效。使用 Podman 时还会加 `--format docker`，避免 `HEALTHCHECK` 被 OCI 格式忽略。

### 导入 / 保存镜像

```bash
# 导出（等价于 build_image.sh 的自动步骤）
podman save -o artifactdepot-0.10.0.tar localhost/artifactdepot:0.10.0
# 目标机导入
podman load -i artifactdepot-0.10.0.tar
```

---

## 快速开始

### 本地开发

```bash
cd ArtifactDepot
./dev_start_headless.sh check       # 确认使用本地 venv（打印 sys.executable）
./dev_start_headless.sh start       # 后台启动 + 日志
./dev_start_headless.sh status      # 查看状态
./dev_start_headless.sh tail        # 跟踪日志
./dev_start_headless.sh stop        # 停止
```

端口默认 8004，可用环境变量 `AD_PORT` 覆盖（`AD_PORT=8090 ./dev_start_headless.sh start`）。
启动脚本会自动选择本地 venv（`$AD_PYTHON` > 项目 `.venv` > 工作区根 `../.venv` > `python3`），也可用 `AD_PYTHON=/path/to/python` 显式覆盖。等价于（以 `check` 输出为准；venv 在工作区根时把 `.venv` 换成 `../.venv`）：

```bash
PYTHONPATH=src .venv/bin/python -m uvicorn artifactdepot.main:app --host 0.0.0.0 --port 8004
```

### 容器部署（带自启）

```bash
./deploy_container.sh --token <你的管理员token>
```

**挂载布局：三卷分离，不随镜像版本变化**（升级镜像后数据与配置原地沿用，不再需要每次 `--data-dir` 指旧目录）：

| 宿主机 | 容器内 | 内容 |
|---|---|---|
| `<部署根>/config/` | `/data/config` | 配置目录：`config.json` + `.env`（首次运行自动用 `config.production.json` 播种） |
| `<部署根>/data/` | `/data/depot` | 业务数据：bucket 与对象文件 |
| `<部署根>/state/` | `/data/state` | 状态文件：`tokens.json` / `signed_links.json` / `audit.log` |

- **默认部署根** `~/SERVER/artifactdepot`；不可写时自动回退到 `${XDG_DATA_HOME:-~/.local/share}/artifactdepot`。可用 `--deploy-root` 或环境变量 `DEPLOY_ROOT` 改基目录
- **单独指定**：`--data-dir` / `--state-dir` / `--config-dir`；或 `--config <文件>` 只挂单个 `config.json`（此时不挂 `.env`，敏感项请用 `--token` / `--datahub-url` 注入）
- **配置即文件即生效**：改 `<部署根>/config/config.json` 后重启容器即可，无需重建镜像
- **默认 host 网络**：容器共享宿主网络，不再映射端口；容器内 `127.0.0.1` 就是宿主机，同机连 DataHub（`http://127.0.0.1:8002/api/data`）最稳。用 `--port-map` 才改为端口映射
- **自启**：Podman 默认生成 `systemd --user` 服务；Docker 自动改用 `--restart=always`
- **非 root 运行**：绑定卷时脚本自动以宿主当前 UID/GID 运行（Podman 另加 `--userns=keep-id`），保证挂载目录可写
- `--token` / `ARTIFACT_DEPOT_ACCESS_TOKEN`：注入共享令牌。脚本用 600 权限临时 env-file 传递，令牌不出现在命令行（`ps` 看不到），容器创建后即删
- `--datahub-url` / `ARTIFACT_DEPOT_DATAHUB_URL`：显式注入 DataHub 地址；不传则以配置文件里的值为准
- `--no-volume`：跳过全部挂载（用镜像内数据，删除容器即丢失，仅调试用）
- `--meta-dir <容器内路径>`：显式覆盖 `meta_dir`（默认已指向状态卷 `/data/state`）

> **从旧版本升级**：0.7.4 之前数据放在 `~/SERVER/artifactdepot/artifactdepot_<版本>/warehouse`（状态文件混在数据目录里）。
> 部署时脚本会自动检测这类旧目录并打印迁移命令；也可直接用
> `--data-dir <旧目录> --state-dir <旧目录>` 沿用旧路径。

### 首次使用流程

1. 打开 `http://<主机>:8004/`，页面自动列出 bucket，点击即选中进入
2. 顶部**令牌框**填入你的 api_token 或管理员 access_token（上传/下载/签名/删除/建目录通用；管理员可做全部；默认共享令牌为 `admin`，上线前请改强令牌）
3. 令牌框填入后「选择文件」才可用；选文件或**拖拽到页面任意位置**，松手自动上传（上传前先校验令牌）。大文件分片上传中，点该任务进度条右侧**「暂停上传」**，之后点**「继续上传」**从断点继续；失败行显示「失败（可续传）」，点该行「继续上传」只补缺失分片。刷新后当前令牌未完成的上传会列在下方，可手动取消
4. 点「新建目录」输入目录名（可含 `/` 多级）组织文件；目录「删除」仅空目录可删；需要清空当前目录时用**「清空本目录文件」/「删除本目录全部内容」**，弹窗先预览文件数/目录数/总大小，再输入目录名确认；需要整目录取回时点**「下载本目录」**（bucket 根即整桶；单文件直接下载，多文件或含子目录自动打包 ZIP）
5. 下载单个文件：文件行点「下载」；大文件自动按 `download.*`（默认 8MB × 4 路）分块并发，弹窗显示进度/速度并可**取消**；Chrome/Edge 可边下边写盘，其它浏览器或超大文件回退原生下载（详见「下载客户端行为」）
6. 需要分享时点文件「签名链接」，弹窗选按时效/按次数/永久，生成后内嵌显示、可复制
7. 管理员在「签名链接」页可看全部链接（含每行复制）、作废；「Token 管理」页同步/登记
8. 需要查接口说明时点标签栏右侧的 **Swagger / ReDoc**（由 FastAPI 依据实际路由生成，可在线调试）
9. 管理员可在 **「配置」标签页** 用共享令牌在线修改 `config.json` 并热加载：全部字段按分组展示，标注「需重启 / 被 .env 覆盖」；密钥字段留空表示保持原值

---

## 测试与验收

```bash
cd ArtifactDepot
# 全量回归
ARTIFACT_DEPOT_CONFIG=$(mktemp) PYTHONPATH=src python3 -m pytest tests -q
# 第 4 批 + 配置重构的接口级业务验收
ARTIFACT_DEPOT_CONFIG=$(mktemp) PYTHONPATH=src python3 -m pytest tests/test_acceptance_batch4.py -q
```

- `tests/conftest.py` 隔离仓库里的真实 `config/.env`，避免测试读到真实密钥。
- `tests/test_acceptance_batch4.py` 按业务语义验证：ETag/304、Range、分页边界、
  可观测性（`/metrics`、`/health/live|ready`、`X-Request-ID`）、CSP、配置重载
  （changed/needs_restart/坏 JSON 保留旧配置）、权限/越权、路径穿越、联动与数据一致性。
- `tests/test_bulk_delete.py` 覆盖批量删除：`files`/`all` 预览与执行、确认名称服务端校验、
  401/403 与资源范围、项数上限 400、审计与指标、符号链接不跟随、UI 标记。
- `tests/test_bulk_download.py` 覆盖一键下载：单文件直下（Range/ETag）、流式 ZIP 结构与空目录、
  隐藏项/符号链接跳过、顶层文件夹命名、空目录 404、401/403/400/404 矩阵、审计与指标、
  项数上限、中断审计、UI 标记。
- `tests/test_upload_cleanup.py` 覆盖上传临时文件清理：成功/落盘硬失败删除会话、缺片保留会话、
  `.part.tmp` 失败清理、启动清理 legacy 会话（共 8 个用例，已适配 0.9.0 新布局）。
- `tests/test_upload_resume.py` 覆盖服务端伴随文件本地化 + 续传（12 个用例）：带 `bucket/key` 的
  `initiate` 会话落目标父目录 `.artifactdepot-uploads` 且不在 `/tmp`、不带则落 `<staging>`、
  `tempfile.tempdir` 指向 staging 并在 lifespan 退出还原、按 `upload_id` 续传返回真实 `uploaded`、
  按 `bucket/key/size` 自动找回（找不到则新建 `resumed=false`）、`chunk`/`complete` 落点与原子清理、
  直传临时文件在目标目录、列表/批量删除/rename 跳过内部目录、服务重启后目标会话仍可续传、
  绝对/空闲 TTL 清理与 registry 更新、系统临时目录无残留、`initiate` 参数校验（400/409）。
- `tests/test_upload_resume_ui.py` 覆盖网页端静态契约（18 个用例）：令牌门槛（未填令牌时
  「选择文件 / 上传」禁用）、每个任务行的「暂停上传 / 继续上传 / 放弃」与 `data-action` 处理器、
  失败行「失败（可续传）」与 `retry-upload`、`initiate` 发送 `resume`/`upload_id`、失败不 `abort`、
  任务独立在分片边界暂停、继续只补缺失分片、刷新后重选文件的指纹缓存、按当前令牌列出/取消未完成上传、
  上传说明精简且无平台信息、CSP 无内联脚本、JS 语法。
- `tests/test_upload_pending.py` 覆盖「当前令牌未完成上传」内部接口（5 个用例）：无 token 401 /
  缺 scope 403、同 token 可见而其它 token 与管理员不可见、字段与百分比、取消后从列表消失、
  完成会话不列出、权限目录 23 条（2026-10-25 新增 `config:write`）且 OpenAPI 操作仍 35 条（内部接口不进 schema）。
- `tests/test_config_ui.py` 覆盖「配置」标签页与新增配置项：配置接口需管理员共享 token 或 `config:write` 权限点、
  快照 schema/文件值/生效值/来源/密钥脱敏、热更新写盘并生效、需重启项只写文件、未知/越界/类型错误 400、
  null/对象/列表项对象拒绝、配置文件缺失时保留 access_token 基线、配置文件 0600、管理员令牌轮换、
  `.env` 覆盖标注、清理间隔/DataHub 同步间隔/网页分页生效、`token.min_length` / `token.max_length` 校验、
  接口不进 OpenAPI、网页静态标记。
- `tests/test_upload_config.py` 覆盖上传参数配置化（`upload.*`）：默认值下发、自定义与越界夹紧、
  `max_chunk_mb` / `max_chunks` 生效、配置重载即时生效、`.env` 点号写法、网页端读取 `upload.*`（静态）。
- `tests/test_download_chunked.py` 覆盖服务端分块并发下载：`access_token` 默认 `admin`、
  `download.*` 默认值下发 / 自定义 / 夹紧 / 点号写法 / 热重载、`HEAD` 响应头与审计
  （`head=true`、`size=0`、不计下载指标）、Range 首/中/尾/后缀区间内容与审计 `size`、
  416 与多区间、并发多 Range 拼接一致、权限目录不新增 scope。
- `tests/test_web_download_chunked.py` 覆盖网页端静态契约：`data.download` 读取与夹紧、
  文件行下载不把 token 拼进 URL、HEAD 探测与 `GET bytes=0-0` 兜底、并行分片与 206/Content-Range 校验、
  File System Access / Blob / 原生单流回退、进度弹窗与取消、SHA-256 校验、CSP 与 JS 语法。
- `client/test_parallel_download.py` 覆盖桌面客户端分块下载（真实 uvicorn 子进程 + 真实 HTTP）：
  并行下载与 SHA-256 一致、进度 `done == total`、小文件 / `concurrency=1` 单流回退、取消清理 `.part`、
  404、服务端多条 `partial` 审计、分片重试不重复计数、ETag 校验失败清理、目录 ZIP 单流。
- `client/test_upload_resume.py` 覆盖桌面客户端伴随状态文件与暂停/续传（11 个用例，真实 uvicorn
  子进程 + 真实 HTTP）：状态文件在源文件目录且 0600 / 成功后删除、暂停→继续只补缺失分片、
  首次失败后续传且最终 SHA-256 一致、多文件并发各自独立状态、下载暂停/续传只补缺失分片、
  下载失败后续传、源目录不可写回退 `~/.artifactdepot/upload-state/`、状态路径/0600/旧全局状态迁移
  （单元级）。

---

## 配置说明

### 配置读取顺序（后者覆盖前者）

程序启动时按四层叠加，**优先级由低到高**：

| 层 | 来源 | 路径 / 方式 |
|---|---|---|
| 1 | 内置默认 | 代码里的 `DEFAULT` 字典（`config/__init__.py`） |
| 2 | 配置文件 | `ARTIFACT_DEPOT_CONFIG` 指定，否则包内 `config/config.json` |
| 3 | `.env` | `ARTIFACT_DEPOT_ENV` 指定，否则包内 `config/.env` |
| 4 | 真实环境变量 | `ARTIFACT_DEPOT_*`（旧 `WAREHOUSE_*` 仅在无新前缀时生效） |

几条容易踩的规则：

- **`.env` 里的空值视为「未设置」**，不会覆盖 `config.json`。例：`config.json` 有 `access_token=abc`，`.env` 写 `access_token=`（空），最终仍是 `abc`
- **`.env` 支持点号嵌套**：`signed_links.count_max=99` 直接写进嵌套字段
- **未知配置项只告警不报错**：启动时提示 `未知配置项：xxx（已忽略）`
- **环境变量入口**：`depot_dir` / `meta_dir` / `datahub_url` / `access_token` / `port` / `max_upload_mb` / `datahub_sync_interval_seconds`，以及 `upload.*` 的 11 个上传参数、`download.*` 的 3 个下载参数、`ui.*` 的 2 个网页分页参数、`token.*` 的 2 个长度参数（见下表）；其余（如 `ui_enabled`、`signed_links`、`audit_max_mb`）只能靠 `config.json` 或 `.env`（`.env` 支持 `upload.chunk_size_mb=8` / `upload.staging_dir=/data/depot/.staging` / `download.chunk_size_mb=8` / `token.max_length=30` 点号写法）
- **相对路径按「项目根」解析，不是按配置文件所在目录**。`"depot_dir": "./data"` 会解析成 `<仓库根>/data`；**容器部署必须写绝对路径**（如 `/data/depot`）

容器部署时，`deploy_container.sh` 注入第 4 层环境变量：

```
ARTIFACT_DEPOT_DIR=/data/depot                  # 覆盖 config.json 的 depot_dir
ARTIFACT_DEPOT_META_DIR=/data/state             # 覆盖 config.json 的 meta_dir
ARTIFACT_DEPOT_CONFIG=/data/config/config.json  # 第 2 层：指向挂载出来的配置
ARTIFACT_DEPOT_ENV=/data/config/.env            # 第 3 层：同目录，敏感项放这里
ARTIFACT_DEPOT_ACCESS_TOKEN=...                 # 仅在使用 --token 时注入
```

### 三个 config 文件的区别

| 文件 | 位置 | 谁读 | 说明 |
|---|---|---|---|
| `config.json` | 仓库内 `src/artifactdepot/config/` | 运行时 | 默认配置，**会进镜像**，无挂载时兜底（`depot_dir=./depot`） |
| `config.production.json` | 仓库内 `src/artifactdepot/config/` | 仅部署脚本 | **纯模板，运行时从不读取**；首次部署时被复制成下面那份 |
| `config.json` | `<部署根>/config/`（如 `~/SERVER/artifactdepot/config/`） | 运行时 | **你的生产配置**，独立于仓库，升级镜像不会被覆盖 |

> 结论：**不要**把仓库里的 `config.production.json` 改名为 `config.json`（会覆盖镜像内默认配置）。
> 要改生产配置，请编辑 `<部署根>/config/config.json`——它由部署脚本首次运行时自动播种。


配置文件：`src/artifactdepot/config/config.json`（非敏感项，会进镜像）；**隐私项放 `src/artifactdepot/config/.env`**（字段名=值，如 `access_token=...`，不进 git/镜像）。可用 `ARTIFACT_DEPOT_CONFIG` 指定外部 config.json、`ARTIFACT_DEPOT_ENV` 指定外部 .env。

> **优先级**：内置默认 < `config.json` < `.env` < 真实环境变量。
> **手动重载**：网页「配置」标签页可在线编辑 `config.json` 并热加载（管理员共享令牌或具备 `config:write` 的 token）；也可在「Token 管理」页点 **「重新加载配置」**，或调 `POST /api/config/reload`；`access_token`/`max_upload_mb`/`upload.*`/`download.*`/`ui.*`/`token.*`/`datahub_sync_interval_seconds`/`signed_links`/`require_read_token`/`ui_enabled`/`metrics_enabled`/`trusted_proxies`/`audit_*`/`datahub_url` 即时生效，`host`/`port`/`depot_dir`/`meta_dir`/`cors_origins`/`allowed_hosts`/`upload.staging_dir` 需重启。

### config.json 字段详解

```json
{
  "depot_dir": "./depot",
  "meta_dir": "",
  "datahub_url": "http://127.0.0.1:8002/api/data",
  "datahub_sync_interval_seconds": 60,
  "host": "0.0.0.0",
  "port": 8004,
  "access_token": "admin",
  "max_upload_mb": 0,
  "ui_enabled": true,
  "trusted_proxies": [],
  "audit_max_mb": 50,
  "audit_backups": 5,
  "cors_origins": [],
  "allowed_hosts": ["*"],
  "require_read_token": false,
  "ui": {
    "file_page_size": 200,
    "audit_page_size": 500
  },
  "signed_links": {
    "count_min": 1,
    "count_max": 10,
    "expire_min_seconds": 60,
    "expire_max_seconds": 604800
  },
  "upload": {
    "chunk_size_mb": 8,
    "chunk_concurrency": 4,
    "file_concurrency": 3,
    "request_timeout_seconds": 86400,
    "session_cleanup_interval_seconds": 600,
    "max_chunk_mb": 64,
    "max_chunks": 100000,
    "max_session_gb": 200,
    "session_ttl_seconds": 86400,
    "session_idle_ttl_seconds": 86400,
    "staging_dir": ""
  },
  "download": {
    "chunk_size_mb": 8,
    "chunk_concurrency": 4,
    "request_timeout_seconds": 3600
  },
  "token": {
    "min_length": 5,
    "max_length": 30
  }
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `depot_dir` | `./depot` | 对象存储根目录。相对路径按**项目根**解析（本地免 sudo）；**容器部署填 `/data/depot` 卷路径** |
| `meta_dir` | `""` | **状态文件目录**（`tokens.json` / `signed_links.json` / `audit.log`）。留空 = 放在 `depot_dir` 根下；设了则独立存放（相对路径按 `depot_dir` 解析）。生产建议 `"/data/state"`（与部署脚本的状态卷挂载点一致），把可再生的状态文件与业务文件隔离、备份更清晰（0.5.0 新增） |
| `datahub_url` | `http://127.0.0.1:8002/api/data` | 从哪个 DataHub 拉取用户 token（collab 权威源）。**同机 host 网络**部署就用 `127.0.0.1:8002`；跨机用 `http://<IP>:8002/api/data` |
| `datahub_sync_interval_seconds` | `60` | DataHub 惰性同步最小间隔（秒）；`0` = 每次校验本地未命中都拉取 |
| `host` | `0.0.0.0` | 监听地址 |
| `port` | `8004` | 服务端口 |
| `access_token` | `admin` | **管理员/工具共享令牌**：识别为「系统/工具」，可执行全部操作；管理接口（Token/审计）也用它。内置默认与 `config.json` / `.env` 默认都是 `admin`（开箱即用、便于内网联调）；**生产/公网部署必须改为 ≥16 位随机串**，可用 `--token` / `ARTIFACT_DEPOT_ACCESS_TOKEN` / config.json / `.env` 覆盖。`config_warnings()` 仍会对空/弱 token 告警（`admin` 属弱口令，长度 <12），设 `ARTIFACT_DEPOT_STRICT_CONFIG=1` 时告警会直接拒绝启动 |
| `max_upload_mb` | `0` | 单文件上传上限（MB）。`0` = 不限 |
| `ui_enabled` | `true` | 是否启用网页 UI（`false` 时 `GET /` 返回 403，API 照常） |
| `metrics_enabled` | `true` | 是否暴露 `/metrics`（Prometheus 文本格式） |
| `trusted_proxies` | `[]` | 可信反向代理 IP/CIDR 列表。非空且直连方在列表内时，才从 `X-Forwarded-For` 解析审计 `public_ip`；否则留空（不再采信客户端自报的 `public_ip`） |
| `audit_max_mb` | `50` | 审计日志单文件上限（MB），超过自动轮转；`0` = 不轮转 |
| `audit_backups` | `5` | 审计日志保留的历史文件数（`audit.log.1` … `.N`） |
| `cors_origins` | `[]` | CORS 允许来源白名单；空 = 不启用 CORS 中间件 |
| `allowed_hosts` | `["*"]` | TrustedHost 白名单；`["*"]` = 不限制 |
| `require_read_token` | `false` | 读接口（`/api/buckets`、`/api/objects/list`）是否要求 `bucket:list` / `object:list` 权限；默认 false 保持内网公开 |
| `ui.file_page_size` | `200` | 文件列表「加载更多」每次条数（10–1000） |
| `ui.audit_page_size` | `500` | 审计日志单次查询条数（10–5000） |
| `signed_links.count_min` | `1` | 签名链接**次数**下限（按次数类型） |
| `signed_links.count_max` | `10` | 签名链接**次数**上限 |
| `signed_links.expire_min_seconds` | `60` | 签名链接**时效**下限（秒） |
| `signed_links.expire_max_seconds` | `604800` | 签名链接**时效**上限（秒，604800 = 7 天） |
| `upload.chunk_size_mb` | `8` | 分片大小（1–64MB）；浏览器/CLI 以 `initiate` 返回值为准 |
| `upload.chunk_concurrency` | `4` | 浏览器每个文件同时上传的分片数（1–16） |
| `upload.file_concurrency` | `3` | 浏览器同时上传的文件数（1–10，网页下拉框默认值） |
| `upload.request_timeout_seconds` | `86400` | 分片/合并请求超时（默认 24h） |
| `upload.session_cleanup_interval_seconds` | `600` | 后台清理过期分片会话的间隔（60s–24h） |
| `upload.max_chunk_mb` | `64` | 单片硬上限（1–64MB） |
| `upload.max_chunks` | `100000` | 单文件最大分片数（上限 100000） |
| `upload.max_session_gb` | `200` | 会话总量上限（`max_upload_mb=0` 时生效，上限 200GB） |
| `upload.session_ttl_seconds` | `86400` | 分片会话绝对有效期（默认 24h） |
| `upload.session_idle_ttl_seconds` | `86400` | 无分片写入的空闲清理阈值（默认 24h，且不小于请求超时） |
| `upload.staging_dir` | `""` | **伴随文件/staging 根目录**：留空 = `<depot_dir>/.staging`；相对路径按 `depot_dir` 解析；可填绝对路径（容器挂独立数据卷）。用途：① 无 `bucket/key` 的旧客户端分片会话根；② 启动时把 `tempfile.tempdir` 指向它，让 **Starlette multipart spool 也落数据盘**（不再写宿主 `/tmp`）。启动创建并打印位置，不可写时告警；**改后需重启才完整生效**（`config/reload` 会在 `needs_restart` 提示） |
| `download.chunk_size_mb` | `8` | 下载分片大小（1–64MB）；网页/桌面客户端按此切 `Range` |
| `download.chunk_concurrency` | `4` | 单个文件同时下载的分片数（1–16） |
| `download.request_timeout_seconds` | `3600` | 单次分片请求超时（60s–7d，默认 1h） |
| `token.min_length` | `5` | 新建 token 允许的最小字符数（1–256） |
| `token.max_length` | `30` | 新建 token 允许的最大字符数（1–256）；服务端自动生成 token 时取该长度 |

> **次数与时效互斥**：签名链接类型为「按时效 / 按次数 / 永久」三选一。选按时效时次数不生效，选按次数时效不生效。越界返回 400；前端按配置显示 min/max 提示并拦截。

### 环境变量覆盖

| 环境变量 | 覆盖字段 |
|---|---|
| `ARTIFACT_DEPOT_CONFIG` | 指定整个配置文件路径（优先级最高） |
| `ARTIFACT_DEPOT_ENV` | 指定 `.env` 文件路径（默认 `src/artifactdepot/config/.env`） |
| `ARTIFACT_DEPOT_DIR` | `depot_dir` |
| `ARTIFACT_DEPOT_META_DIR` | `meta_dir`（0.5.0 新增） |
| `ARTIFACT_DEPOT_DATAHUB_URL` | `datahub_url` |
| `ARTIFACT_DEPOT_DATAHUB_SYNC_INTERVAL_SECONDS` | `datahub_sync_interval_seconds` |
| `ARTIFACT_DEPOT_ACCESS_TOKEN` | `access_token` |
| `ARTIFACT_DEPOT_PORT` | `port` |
| `ARTIFACT_DEPOT_MAX_UPLOAD_MB` | `max_upload_mb` |
| `ARTIFACT_DEPOT_UI_FILE_PAGE_SIZE` | `ui.file_page_size` |
| `ARTIFACT_DEPOT_UI_AUDIT_PAGE_SIZE` | `ui.audit_page_size` |
| `ARTIFACT_DEPOT_UPLOAD_CHUNK_SIZE_MB` | `upload.chunk_size_mb` |
| `ARTIFACT_DEPOT_UPLOAD_CHUNK_CONCURRENCY` | `upload.chunk_concurrency` |
| `ARTIFACT_DEPOT_UPLOAD_FILE_CONCURRENCY` | `upload.file_concurrency` |
| `ARTIFACT_DEPOT_UPLOAD_REQUEST_TIMEOUT_SECONDS` | `upload.request_timeout_seconds` |
| `ARTIFACT_DEPOT_UPLOAD_SESSION_CLEANUP_INTERVAL_SECONDS` | `upload.session_cleanup_interval_seconds` |
| `ARTIFACT_DEPOT_UPLOAD_MAX_CHUNK_MB` | `upload.max_chunk_mb` |
| `ARTIFACT_DEPOT_UPLOAD_MAX_CHUNKS` | `upload.max_chunks` |
| `ARTIFACT_DEPOT_UPLOAD_MAX_SESSION_GB` | `upload.max_session_gb` |
| `ARTIFACT_DEPOT_UPLOAD_SESSION_TTL_SECONDS` | `upload.session_ttl_seconds` |
| `ARTIFACT_DEPOT_UPLOAD_SESSION_IDLE_TTL_SECONDS` | `upload.session_idle_ttl_seconds` |
| `ARTIFACT_DEPOT_UPLOAD_STAGING_DIR` | `upload.staging_dir` |
| `ARTIFACT_DEPOT_DOWNLOAD_CHUNK_SIZE_MB` | `download.chunk_size_mb` |
| `ARTIFACT_DEPOT_DOWNLOAD_CHUNK_CONCURRENCY` | `download.chunk_concurrency` |
| `ARTIFACT_DEPOT_DOWNLOAD_REQUEST_TIMEOUT_SECONDS` | `download.request_timeout_seconds` |
| `ARTIFACT_DEPOT_TOKEN_MIN_LENGTH` | `token.min_length` |
| `ARTIFACT_DEPOT_TOKEN_MAX_LENGTH` | `token.max_length` |

> 优先级：**环境变量 > 配置文件 > 内置默认**。容器部署用 `-e` 传环境变量即可，无需改代码/文件。

### 配置优先级与生产模板

**镜像内不要烤死 `datahub_url` 默认值**：`config/__init__.py` 里环境变量优先级高于配置文件，若 Dockerfile 用 `ENV` 预设 `ARTIFACT_DEPOT_DATAHUB_URL=127.0.0.1`，即使挂载了写好的 config.json 也会被盖掉（0.5.0 已从 Dockerfile 移除该 ENV）。`datahub_url` 以**容器内配置文件的字段**为准，生产用 `--config` 或 `VOLUME_MAPS` 挂载宿主机 config.json 显式填写。

`src/artifactdepot/config/` 下有 **`config.production.json`** 生产模板，可整份复制到生产机：

```bash
# 生产机（假定文件放 ~/SERVER/artifactdepot/config/config.json）
mkdir -p ~/SERVER/artifactdepot/config
cp src/artifactdepot/config/config.production.json ~/SERVER/artifactdepot/config/config.json
# 按需修改 access_token / datahub_url / meta_dir（模板里的 access_token 也是默认 admin，
# 生产务必替换为 >=16 位随机串），然后部署时显式挂载：
./deploy_container.sh --config ~/SERVER/artifactdepot/config/config.json --token <你的token>
# 也可只挂配置、token 从 config.json 读取：
# ./deploy_container.sh --config ~/SERVER/artifactdepot/config/config.json
```

```json
{
  "depot_dir": "/data/depot",
  "meta_dir": "/data/state",
  "datahub_url": "http://127.0.0.1:8002/api/data",
  "host": "0.0.0.0",
  "port": 8004,
  "access_token": "admin",
  "max_upload_mb": 0,
  "ui_enabled": true,
  "trusted_proxies": [],
  "signed_links": {
    "count_min": 1,
    "count_max": 10,
    "expire_min_seconds": 60,
    "expire_max_seconds": 604800
  },
  "upload": {
    "chunk_size_mb": 8,
    "chunk_concurrency": 4,
    "file_concurrency": 3,
    "request_timeout_seconds": 86400,
    "max_chunk_mb": 64,
    "max_chunks": 100000,
    "max_session_gb": 200,
    "session_ttl_seconds": 86400,
    "session_idle_ttl_seconds": 86400,
    "staging_dir": ""
  },
  "download": {
    "chunk_size_mb": 8,
    "chunk_concurrency": 4,
    "request_timeout_seconds": 3600
  },
  "token": {
    "min_length": 5,
    "max_length": 30
  }
}
```

> 生产模板用 host 网络时 `datahub_url` 填 `127.0.0.1:8002`（容器内即宿主机）；跨机部署改为目标 DataHub 的 IP。

---

## 目录结构

```
ArtifactDepot/
- README.md / CHANGELOG.md / VERSION        # VERSION 是版本号唯一源
- LICENSE / LICENSE.zh-CN.md      # Apache-2.0（英文版 / 中文版）
- docs/                          # docs/api_rules.md（接口文档）、docs/architecture.md（架构说明）、
                                 #   docs/LICENSE-HEADER（源文件 SPDX 署名约定，本地留存不进 git）
- check_ad_sync.sh               # 生产诊断脚本（python3-only，不依赖 curl/jq）
- scripts/add_spdx_headers.py    # SPDX 署名头补写/校验（幂等；--check 可用于 CI）
- .dockerignore / .gitignore     # 构建上下文与版本控制排除规则
- Dockerfile                     # 单阶段镜像（python:3.12-alpine + supervisor + tzdata）
- docker/supervisord.conf        # supervisor 托管 uvicorn（8004）
- build_image.sh / build_image.conf  # 构建脚本：构建 + 自动导出 tar（默认 --save）
- deploy_container.sh            # 部署脚本：建容器 + Podman systemd / Docker restart
                                 #   （--config/--meta-dir/--token/--datahub-url/--data-dir/--port-map）
- dev_start_headless.sh          # 本地后台启动脚本（源码模式直启 uvicorn）
- requirements.txt               # 运行依赖（精确锁定）
- src/artifactdepot/
  - main.py                      # FastAPI 入口 + lifespan（建目录 + 启动同步）
  - config/                      # 配置包：__init__.py（加载/手动重载）+ config.json + config.production.json + .env(.example)
  - storage.py                   # 核心引擎：路径安全 / bucket-key / manifest / 签名链接注册表 / token 注册表 / 审计 / DataHub 同步 / mkdir / 一键下载扫描 / meta_dir
  - zipstream.py                 # 流式 ZIP：后台线程 + 有界队列 + 背压（目录一键下载，不落临时文件）
  - auth.py                      # 认证：管理员 token（UTF-8 比较）、用户 token、惰性同步
  - permissions.py               # 权限点目录与角色预设
  - logging_utils.py / netutil.py / observability.py  # 日志脱敏 / 网络工具 / 指标与健康检查
  - api/
    - objects.py                 # 对象 API（上传/列表/下载/一键下载目录/删除/mkdir/签名/作废）
    - system.py                  # /health /api/buckets /api/tokens /api/audit /api/auth/check
    - docs.py                    # /api/docs 文档清单与内容（公开只读，含许可证）
  - web/ui.py                    # 网页 UI（读 resources/index.html）
  - resources/
    - index.html                 # 单页界面（拖拽/新建目录/签名/Token/审计）
    - static/                    # 外置 app.js / app.css + 自托管 Swagger UI / ReDoc 资源
- client/                        # 桌面客户端（单文件、零第三方依赖）+ client/README.md（启动/分块并发下载说明）
- tests/                         # pytest 回归测试（含 conftest 隔离真实 .env）
```

> 文档相关：`docs/api_rules.md` 是对外接口文档；`docs/architecture.md` 是架构说明。
> 两者（含 README / CHANGELOG）都可通过公开只读接口 `GET /api/docs` 读取（`?raw=1` 返回纯文本）；
> 网页 UI 标签栏右侧提供 **Swagger `/docs`**（可直接调试）与 **ReDoc `/redoc`** 入口。

### 磁盘存储布局

```
<depot_dir>/                  # 如 ./depot 或 /data/depot
- <bucket>/                       # bucket = 项目（如 音视门carryVdeio）
  - 任务1/交付物/a.mp4             # key = 路径，含 / 即子目录
  - 任务1/交付物/.keep             # 新建目录的隐藏占位（列表跳过）
  - 任务1/交付物/.artifactdepot-uploads/<upload_id>/
                                  # 活跃分片上传会话（伴随文件本地化；成功后自动删除，
                                  #   .session.json / chunk_<i>.part / .incoming / .merged）
  - .warehouse.json               # 该 bucket 元数据清单（隐藏）
- .staging/                       # 默认 staging（upload.staging_dir 留空时）：
                                  #   legacy 会话 <upload_id>/ + 框架 multipart spool
- ...（bucket 们）

<meta_dir>/（可选，默认在 depot_dir 根下；可用 meta_dir 单独放）
- tokens.json                     # token 与用户名映射注册表（DataHub 同步副本）
- signed_links.json               # 签名链接注册表（时效/次数/作废）
- audit.log                       # 审计日志（JSONL 追加）
```

**伴随文件本地化**：上传/下载过程中自动创建的临时文件与断点文件都放在「目标/源文件所在目录」，不再写宿主 `/tmp`：

- 服务端：分片会话 `<目标父目录>/.artifactdepot-uploads/<upload_id>/`、直传临时 `.<name>.artifactdepot-incoming-<rand>.tmp`、合并临时 `.merged`；legacy 客户端（`initiate` 不带 `bucket/key`）会话落 `<staging>/<upload_id>/`，框架 multipart spool 落 `upload.staging_dir`（默认 `<depot_dir>/.staging`）。
- `.artifactdepot-uploads` / `.staging` 均为隐藏目录：列表、目录递归大小、ZIP 打包均跳过；批量删除 `mode=all` 与 rename 显式保护（不会清掉正在上传的会话）。
- 客户端：上传断点 `.<源文件名>.artifactdepot-upload.json`、下载 `.part` 与 `.<目标名>.artifactdepot-download.json`，见 [client/README.md](client/README.md)。

**key 与目录**：`key` 是文件在 bucket 里的完整路径，含 `/` 自动建子目录（如 `key=任务1/交付物/a.mp4`）。网页点「新建目录」输入名（可含 `/` 多级）创建空目录，或直接以 `key=二级目录/文件名` 上传自动建目录。

**meta_dir**：默认状态文件与 bucket 同根；设 `meta_dir` 后 `tokens.json` / `signed_links.json` / `audit.log` 移到指定目录（相对路径按 `depot_dir` 解析），与上传的业务文件隔离。

---

## 核心模块

| 模块 | 职责 |
|---|---|
| `config/__init__.py` | 合并 默认 < config.json < .env < 环境变量；相对路径按项目根解析；支持 `meta_dir` / `upload.staging_dir` / 手动 `reload_config()`；归一化并夹紧 `upload.*` / `download.*` / `ui.*` 参数（`get_*_settings` / `public_*_settings`），`staging_dir` 改动进入 `needs_restart`；网页「配置」标签页的 `config_schema()` / `config_snapshot()` / `update_config_file()`（原子写 0600、校验、失败回滚、字段来源/覆盖标注） |
| `storage.py` | **核心引擎**：bucket/key 落盘、路径穿越防护、SHA-256、`.warehouse.json` 清单、签名链接注册表（创建/校验/次数递减/作废）、token 注册表、审计日志（双 IP + UTC）、DataHub 同步、mkdir、空目录删除策略、**批量删除目录内容（`bulk_delete`）**、**一键下载只读扫描（`bulk_download_scan`）**、**分片会话（目标目录 `.artifactdepot-uploads` / legacy staging、进程内 registry、续传查找、TTL 清理与启动扫描、`put_object(move=True)` 原子落盘）**、`_dir_recursive_info()` 目录递归统计（大小/最新 mtime）、`_meta_path` 状态文件定位（meta_dir） |
| `zipstream.py` | **流式 ZIP 生成**（目录一键下载）：后台线程写非 seekable 队列 writer + 有界队列消费（背压），`ZIP_STORED`/Zip64、256 KiB 分块读取、不落临时文件；客户端中断可取消，审计恰好一次 |
| `auth.py` | `check_admin_token`（UTF-8 字节常量时间比较，非 ASCII 安全）、`resolve_actor`、`resolve_actor_with_sync`（惰性同步）、`require_write_token` / `require_admin` |
| `api/objects.py` | 对象 API：上传（multipart，临时文件写目标父目录 + `move=True`）、列表、下载（token 鉴权 + `HEAD` 探测 + Range 206 + 分块审计）、删除（`object:delete` + 空目录）、**批量删除目录内容（`object:delete_bulk`）**、**一键下载目录（`download-bulk`，复用 `object:download`）**、**分片上传（`initiate` 支持 query/form 与 `resume`/`upload_id` 续传、`chunk` `.incoming` 临时、`complete` `.merged`）**、mkdir、签名链接（presign / config / list / revoke） |
| `api/system.py` | 健康检查、bucket 列表、Token 注册表 CRUD、`/api/tokens/sync`（含 ok/error）、`/api/auth/check`、`/api/auth/permissions`（下发 `upload` / `download` 参数）、审计查询 |
| `web/ui.py` | 网页：bucket 点击选中高亮、整页拖拽、新建目录、签名弹窗、Token 管理、审计双 IP 列 |
| `client/artifactdepot_upload.py` | 桌面客户端（tkinter）/ CLI：上传（分片/续传/暂停/重试，伴随状态文件）与分块并发下载（`HEAD` + 并行 `Range`、重试、暂停/续传、取消、SHA-256 校验）；说明见 `client/README.md` |
| `main.py` | 应用组装 + lifespan（确保根目录存在、启动清理旧版 `/tmp/dw_chunks` 与 legacy 会话、创建 staging 并把 `tempfile.tempdir` 指向它（spool 落数据盘，退出还原）、扫描 `.artifactdepot-uploads` 重建 registry、启动即拉取用户 token） |
| `check_ad_sync.sh` | 生产诊断：容器配置 / 应用同步 / 容器内访问 DataHub / 宿主对照 / 端口监听 / 挂载检查（仅 python3） |

---

## API 参考

> 全部接口前缀为 `http://<IP>:8004`。返回统一结构：`{"code":0,"message":"success","data":...}`；出错时 HTTP 4xx/5xx + `{"detail":"..."}`。

### 鉴权约定

| 等级 | 令牌 | 权限 |
|---|---|---|
| 管理员共享 token | `config.json` 的 `access_token`（默认 `admin`，生产请改强令牌） | 全部操作（含 Token/审计管理、删除） |
| 用户 token | 协作平台「查看我的 Token」或管理员生成 | 由 token 携带的权限点（scope）决定；旧 token 默认相当于历史全量写权限 |
| 签名链接 | `link` + `tk` 参数 | 仅该链接指向对象的下载（免 token） |

> 0.7.0 起支持**逐接口勾选生成自定义 Token**：管理员在「Token 管理」页选择角色/勾选接口/限定 bucket 与路径前缀/设置过期时间，点击「生成本轮 Token」。接口权限目录见 `GET /api/auth/permissions`；详见 `docs/api_rules.md` 第十二章。
>
> 接口文档在网页 UI 标签栏右侧的 **Swagger**（`/docs`，可直接在线调试）与 **ReDoc**（`/redoc`）查看；
> 若需原始 Markdown，可用公开只读接口 `GET /api/docs`（清单）与 `GET /api/docs/{name}`（`?raw=1` 返回纯文本）。

传递方式：**推荐 `Authorization: Bearer <token>`**（不落 URL、不进访问日志）；也兼容 `?token=` 查询参数与 multipart 表单 `token` 字段。服务端访问日志会把 `token`/`tk`/`sig` 等参数脱敏为 `***`。

### 对象接口（S3 风格）

#### 上传文件 — `POST /api/objects`（PutObject）

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `object:upload`（管理员共享 token 放行全部；按 token 的 bucket/前缀范围校验） |
| 请求 | multipart/form-data：`file`(必)、`bucket`(必，=项目)、`key`(必，含 `/` 自动建目录)、`token`、`source_url`（来源，允许 URL 或自由文本）、`note`（备注）、`overwrite`(默认 `true`)、`public_ip` |
| 成功 | `data:{bucket, key, size, sha256}`；`.warehouse.json` 同时记录 `uploader`（按 token 识别为「系统/工具」或用户名） |
| 错误 | `401` 无效 token；`413` 超过 `max_upload_mb` 上限；`409` `overwrite=false` 且对象已存在 |
| 说明 | 流式落盘到 `<depot_dir>/<bucket>/<key>`，写审计（双 IP、UTC）；key 父目录自动创建；小文件/常规上传直接走本接口；大文件建议使用下面的分片接口 |

#### 分片上传（多线程）— `POST /api/objects/initiate|chunk|complete|abort`

用于**单个大文件多线程并行上传**：文件按 `upload.chunk_size_mb`（默认 8MB）拆分，多路并发传至服务端，最后合并落盘；分片/合并超时默认 24h（`upload.request_timeout_seconds`）。0.9.0 起分片会话与临时文件**随目标文件落盘**，并支持**暂停/继续与断点续传**。

| 接口 | 方法 | 说明 |
|---|---|---|
| `initiate` | `POST /api/objects/initiate` | 创建/恢复分片会话；参数与响应见下 |
| `chunk` | `POST /api/objects/chunk` | multipart：`upload_id`、`index`、`chunk`（分片文件）；请求体临时文件写会话 `.incoming/`，随后原子改名为 `chunk_<i>.part` |
| `complete` | `POST /api/objects/complete` | multipart/form-data：`upload_id`、`bucket`、`key`、`total_chunks`、`source_url`（来源）、`note`（备注）、`overwrite`、`public_ip`；合并到会话内 `.merged` 后 `put_object(move=True)` 原子落盘 |
| `abort` | `POST /api/objects/abort` | `upload_id`；放弃并清理会话目录（幂等） |

**`initiate` 请求参数**（query 与 multipart form 均可，允许空 body 向后兼容；同名时表单优先）：

| 参数 | 必填 | 说明 |
|---|---|---|
| `bucket` / `key` | 可选（必须同时给或都不给） | 提供时会话落目标文件父目录 `.artifactdepot-uploads/<upload_id>/`，并追加 bucket/key 资源范围校验；都不给则是 legacy 会话（`<staging>/<upload_id>/`） |
| `size` | 可选 | 文件总字节数；给出时 `total_chunks = ceil(size / chunk_size)` |
| `resume` | 可选，默认 `false` | `true` 时尝试复用已有会话 |
| `upload_id` | 可选 | `resume=true` 时指定要复用的会话 |

**`initiate` 响应 `data`**：保留原 `upload_id` / `chunk_size` / `chunk_concurrency` / `file_concurrency` / `request_timeout_seconds` / `max_chunk_size` / `max_chunks` / `max_session_gb` / `max_upload_mb`，并新增：

| 字段 | 说明 |
|---|---|
| `resumed` | 本次是否复用了已有会话 |
| `uploaded` | 磁盘上**真实存在**且 `< total_chunks` 的分片索引（升序，唯一事实源） |
| `received_bytes` | 上述分片的实际字节数之和 |
| `total_chunks` | 总片数（`size` 未知时为 `0`） |
| `size` | 文件总字节数（未知时 `0`） |
| `chunk_size` | 本会话分片大小（字节） |
| `staging` | `"target"`（目标目录会话）/ `"legacy"`（staging 根会话） |

**续传语义**（`resume=true`）：

1. 带 `upload_id`：校验会话存在、owner 匹配（admin 例外）、`bucket/key/size`（若请求给出）与 meta 一致 → 复用并返回真实 `uploaded`；**失效/不一致 → `400` 且 `detail` 含「分片会话」**（客户端按会话丢失处理、清本地状态重传）。
2. 不带 `upload_id` 但带 `bucket/key/size`：在目标目录 `.artifactdepot-uploads` 中按 owner + bucket/key + size 找**最新**会话复用；找不到则新建（`resumed=false`）。
3. `resume=false`：总是新建。
4. 响应**不回显服务端绝对路径**；不提供 `GET /api/objects/upload/{id}`（避免新增权限点）。

**会话目录布局与清理**：

```
<depot_dir>/<bucket>/<key 的父目录>/.artifactdepot-uploads/<upload_id>/
- .session.json        # owner / created_at / updated_at / received_bytes / bucket / key / size /
                       #   chunk_size / total_chunks / staging
- chunk_<i>.part       # 已上传分片
- .incoming/<rand>.tmp # 单次 chunk 请求体临时文件（提交后 os.replace，失败清理）
- .merged              # complete 的合并临时文件（落盘后删除）
```

- 内部目录 `.artifactdepot-uploads` 是隐藏目录：列表 / 目录递归大小 / ZIP 打包跳过，**批量删除 `mode=all` 与 rename 也显式跳过**（保护活跃上传）；会话清空后目录自动 `rmdir`。
- 成功或硬失败（磁盘满、I/O）清理会话；**校验类失败（缺片、`total_chunks` 越界、超限、权限、`overwrite=false` 的 409）保留会话**可重试。
- **重启续传**：会话在数据盘，服务启动时扫描 `.artifactdepot-uploads`，未过期会话重新注册进进程内 registry（单进程假设，uvicorn 未开 `--workers`），客户端可继续上传；超过 `upload.session_ttl_seconds`（绝对）/ `upload.session_idle_ttl_seconds`（空闲）的会话被清理，后台按 `upload.session_cleanup_interval_seconds`（默认 600s）再清一次。

> 其余说明：`complete` 会校验所有分片齐全、合并后复用 `put_object` 写 `.warehouse.json` 与审计；
> 前端对大于 `upload.chunk_size_mb`（默认 8MB）的文件自动走分片流程，分片并发/超时也读取服务端配置（默认 4 路、24h）；`<=` 该阈值走普通 `POST /api/objects`。

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
| 成功 | `data:{bucket, prefix, items:[...]}`；文件项含 `name/key/is_dir/size/mtime(UTC Z)/sha256/source_url/note/uploader`；目录项 `key` 以 `/` 结尾，并含 `size`（**递归总大小**）与 `mtime`（**最新修改时间**） |
| 错误 | `400` prefix 越界（越出 bucket 根） |

#### 下载文件 — `GET /api/objects/download`（GetObject）

| 项 | 说明 |
|---|---|
| 鉴权 | `object:download` 权限点，或签名链接 `link`+`tk`，或旧式签名 `expires`+`sig`（三选一） |
| 请求 | Query：`bucket`、`key`、`token` 或 `link`+`tk` 或 `expires`+`sig`；可选请求头 `Range: bytes=a-b` |
| HEAD 探测 | `HEAD /api/objects/download?...`（与 GET 同路径）：返回 `Content-Length` / `Accept-Ranges: bytes` / `ETag` / `Content-Disposition`，无响应体；鉴权与 `404` 行为同 GET。该路由以 `include_in_schema=false` 注册，**OpenAPI 操作数仍为 35、不新增权限点** |
| 成功 | 完整 GET：`200` 文件流；单区间：`206` + `Content-Range: bytes a-b/total`；多区间全部可满足：`206` + `multipart/byteranges`（任一段越界则 `416`）；`If-None-Match` 命中：`304` 不传文件体 |
| 错误 | `401` 无有效凭证；`403` 缺 `object:download` 或资源范围越界；`404` 对象不存在；`416` 区间不可满足（响应带 `Content-Range: bytes */<total>`） |
| 分块并发 | 网页 / 桌面客户端按 `config.download.*`（默认 8MB × 4 路、单请求超时 1h）先 `HEAD` 探测再并行 `Range` 下载；分片失败重试、可取消、完成后按 `ETag`（SHA-256）校验；签名链接与 `download-bulk` ZIP 走单流（见下「下载客户端行为」） |
| 审计/指标 | 每次请求写一条 `download` 审计（`extra` 顶层合并）：HEAD `head=true`、`size=0`；Range `partial=true`、`range=<请求头原文>`、`size=区间字节数`；完整 GET `size=整文件`；`304` 带 `not_modified=true`。HEAD 与 `304` 不计 `DOWNLOADS`；可解析的单区间与完整 GET 各 +1（多区间不计） |
| 说明 | 并发分块下载会产生**多条** `download` 审计；签名链接按次数计费时每个 Range 请求都会消耗一次，分块下载建议使用 token |

#### 一键下载目录 — `GET /api/objects/download-bulk`（DownloadBulk）

| 项 | 说明 |
|---|---|
| 鉴权 | 复用 `object:download`（**不新增权限点**；管理员共享 token 放行全部；按 token 的 bucket/前缀范围校验，范围外 `403`）；仅支持 token（不接签名链接） |
| 请求 | Query：`bucket`(必)、`prefix`(空 / `"/"` = 当前 bucket 根)、`token`（或 `Authorization: Bearer`） |
| 成功 | 目录内**只有 1 个可见文件且无可见子目录** → 直下原文件（原文件名、**Range 206**、ETag/304）；否则 → **流式 ZIP**（`application/zip`，无 Content-Length，顶层文件夹为目录名，bucket 根用 bucket 名；含空目录条目；跳过隐藏项/符号链接） |
| 错误 | `400` prefix 非法 / 解析后越出 bucket / `files + dirs` 超过 100000 项；`401` 无有效 token；`403` 缺权限点或超出资源范围；`404` bucket/prefix 不存在、`prefix` 是文件、**目录为空**（不返回 0 字节 ZIP） |
| 说明 | 只读、不落临时文件；ZIP 用 `ZIP_STORED` + Zip64、256 KiB 分块流式输出（不整包进内存）；ZIP 结束（含客户端中断）写 `download_bulk` 审计（含 `mode/files/dirs/aborted`），单文件直下写 `download` + `extra.bulk=true`；`/metrics` 下载计数 +1；`Content-Disposition` 用 ASCII 回退 + `filename*=UTF-8''` |

#### 删除对象/目录 — `DELETE /api/objects`（DeleteObject）

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `object:delete`（管理员共享 token 放行全部） |
| 请求 | Query：`bucket`、`key` |
| 成功 | `data:{...}` |
| 错误 | `400` 目录非空（需先清空内容才能删）；`404` 不存在 |
| 说明 | 安全策略：**目录仅允许删除空目录**（隐藏 `.keep` 不算内容，自动一并移除），防误删 |

#### 批量删除目录内容 — `POST /api/objects/bulk-delete`（BulkDelete）

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `object:delete_bulk`（管理员共享 token 放行全部；按 token 的 bucket/前缀范围校验，范围外 `403`） |
| 请求 | JSON：`{"bucket", "prefix", "mode": "files\|all", "dry_run": true\|false, "confirm": "目录名"}`；`prefix` 空 / `"/"` = bucket 根；`dry_run` 默认 `false`（省略即执行） |
| 成功 | `data:{bucket, prefix, mode, dry_run, files, dirs, bytes, errors, elapsed_ms}`；预览返回「将删除」统计，执行返回「实际删除」统计 |
| 错误 | `400` `mode` 非法 / `confirm` 缺失或不匹配 / `prefix` 非法 / `files + dirs` 超过 50000 项；`401` 无有效 token；`403` 缺权限点或超出资源范围；`404` bucket 不存在 / `prefix` 不存在或不是目录 |
| 说明 | **只清理目录内容、目录本身保留**；`files` 只删当前目录的直接文件（子目录及其内容、`.keep`、隐藏项保留），`all` 递归删除文件与子目录（隐藏项也删，但 bucket 根 `.warehouse.json` 永不删除）；不跟随符号链接；执行必须输入目录名（bucket 根目录输入 bucket 名）并由服务端二次校验；`dry_run=true` 不落盘、不写审计；执行写 `delete_bulk` 审计并计入 `/metrics` 的删除计数 |

#### 生成签名链接 — `POST /api/objects/presign`

| 项 | 说明 |
|---|---|
| 鉴权 | 权限点 `link:create` |
| 请求 | JSON：`{"bucket", "key", "mode": "count\|time\|permanent", "count": 1-10, "expires": 3600}` |
| 成功 | `data:{url, id, mode, max_uses, remaining, expires}` |
| 说明 | `mode=count` 按次数(1-10)；`time` 按时效秒（默认 1 小时）；`permanent` 永久；次数/时效上下限见 `config.signed_links` |

#### 签名链接辅助

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/objects/signed-links/config` | 返回次数/时效上下限（供前端渲染校验） |
| GET | `/api/objects/signed-links` | 列签名链接（需 `link:list`；带范围 token 只见范围内条目；**完整 URL 仅管理员/创建者可见**；密钥不回传） |
| POST | `/api/objects/signed-links/{link_id}/revoke` | 作废（需 `link:revoke`；管理员可作废任意，用户只能作废自己创建的） |

### 系统 / 管理接口

#### Token 校验 — `GET /api/auth/check`
`?token=` → 成功：`data:{valid:true, actor, role, is_admin, scopes, allow_buckets, allow_prefixes, expires_at, description}`；失败：`valid:false` 且另含 `expired` / `enabled`。上传/下载前先审核。

#### 健康检查 — `GET /health`
`{"status":"ok","depot_dir":"...","exists":true}`

#### 列 bucket — `GET /api/buckets`
返回 `data:[...]` bucket（项目）列表。

#### 运行时配置 — `GET/POST /api/config`、`POST /api/config/reload`（权限点 `config:write`）
`GET` 返回配置快照（`schema` / `values` / `effective` / `sources` / `overridden` / `warnings`，密钥脱敏）；`POST` 只接受已知配置项、原子写回 `config.json` 并热加载（未知项/越界/类型错误 `400`）；`POST /api/config/reload` 重新读取磁盘配置。三者均接受**管理员共享 token**或具备 **`config:write`** 权限点的 token；无 token `401`、缺权限 `403`。`config:write` 用户 token 不得提交新的 `access_token`（`403`，仅管理员可轮换），其他配置项可改。config 接口 `include_in_schema=False`，OpenAPI 操作数仍 35。

#### Token 注册表管理（权限点 `token:*`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tokens` | 查看 token 映射；未「显示明文」时只展示**前 2 位 + `****` + 后 4 位**，管理员可 `?reveal=true` 看明文 |
| POST | `/api/tokens` | 生成/登记：`token` 留空按 `token.max_length` 生成，显式新 token 长度须在 `token.min_length`–`token.max_length`（默认 5–30）内，越界 400 |
| PUT | `/api/tokens/{value}` | 更新权限/范围/过期/启用状态（未传字段保留；不校验 token 长度；显式 token 建议 URL-safe，含 `/` 时该路径不可寻址） |
| DELETE | `/api/tokens?value=<token>` | 移除某个 token（用户重置/注销时） |
| POST | `/api/tokens/sync` | 从 DataHub `users.json` 拉取并合并用户 token（collab 权威源、本地副本只增不删）；返回 `data.ok` 反映 DataHub 可达性 |

#### 审计查询 — `GET /api/audit`（权限点 `audit:read`）
Query：`bucket`/`key`/`actor`/`since`(YYYY-MM-DD 起)/`limit`(默认 500)。返回审计记录（操作类型 / 操作者 / **双 IP** / UTC 时间 / size / sha256）。

### 文档接口（0.7.1 新增）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/docs` | 文档清单（公开）：`[{name,title,available}]`，name ∈ api_rules/architecture/readme/changelog/license/license_zh/license_header |
| GET | `/api/docs/{name}` | 文档内容（公开）：`data:{name,title,content}` |
| GET | `/api/docs/{name}?raw=1` | 直接返回 `text/plain` 原文 |

### 网页 UI

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 网页控制台（bucket 选择、整页拖拽上传、**每个任务行的暂停/继续 + 当前令牌未完成上传列表/取消**、**配置标签页（具备 `config:write` 的令牌在线改 config.json + 热加载，输入令牌后自动加载）**、**分块并发下载（进度/速度/取消）**、一键下载目录、新建目录、批量删除、签名链接/Token/审计管理；标签栏右侧含 Swagger/ReDoc 入口） |

### 上传客户端行为（网页 / 桌面）

**网页端**（`src/artifactdepot/resources/static/app.js`）：

- **参数**：`initiate` 始终带 `bucket`/`key`/`size`；续传再带 `resume=true`（可带 `upload_id` 指定复用）；队列项记录 `upload_id` / `chunk_size` / `uploaded`。
- **令牌门槛**：未填写令牌时 `#upFile` 与上传按钮禁用（`syncUploadGate()`）；拖拽入队前同样再次校验令牌。
- **暂停/继续（按任务行）**：每行独立 `data-action="pause-upload" / "resume-upload"` + `data-uid`，按钮只出现在该任务进度条右侧；`pauseUpload(uid)` 在当前分片传完后生效，`resumeUpload(uid)` 唤醒该任务的 worker，并向服务端 `initiate(resume=true)` 取回真实 `uploaded` 后只补缺失分片。
- **未完成上传（当前令牌）**：页面加载 / 输入令牌后调用内部接口 `GET /api/objects/uploads`（`include_in_schema=False`，复用 `upload:abort` 权限），只返回当前 token 名下未完成的会话；点「取消上传」调用 `POST /api/objects/abort` 并清理本地断点记录。
- **失败续传**：失败**不再 `abortUpload`**，该行显示**「失败（可续传）」**并提供 `data-action="retry-upload"` 的「继续上传」；会话与已传分片保留在服务端。
- **刷新后**：浏览器无法自动恢复文件列表；当前令牌未完成会话会列在下方（可手动取消），重新选择同一文件再点「上传」即可按 `bucket/key/size` 找回并续传（同一浏览器、同一 token 下有效；token 变更或文件 size/mtime 变化时自动新建会话）。
- **CSP**：事件委托 + `data-action`，无内联脚本（`script-src 'self'`）。

**桌面客户端**（`client/`，详见 `client/README.md`）：

- 上传断点状态写源文件同目录 `.<文件名>.artifactdepot-upload.json`（0600 原子写），源目录不可写时回退 `~/.artifactdepot/upload-state/<sha1>.json`；成功/会话失效删除，失败/暂停/取消保留。
- `upload_file(..., pause_event=...)` 在分片边界暂停；失败**不 abort**，下次同文件同目标自动从断点续传（以服务端 `uploaded` 为事实源）；CLI 提示「已保留续传状态，重跑同一命令将从断点继续」（`--no-resume` 可放弃）。

### 下载客户端行为（网页 / 桌面）

**网页端**（`src/artifactdepot/resources/static/app.js`）：

- **参数下发**：页面加载时 `GET /api/auth/permissions` 同一个请求同时读取 `data.upload` 与 `data.download`；下载参数缺失时回退 8MB / 4 路 / 1h，并夹紧到 1–64MB / 1–16 / 60s–7d。
- **token 不进 URL**：文件行下载按钮改为 `data-action="download-file"` + `data-key="..."`，fetch 请求的 token 只放 `Authorization` 头（原生单流兜底时才用 `?token=`，因为 `<a>` 无法带自定义头）。
- **探测**：先 `HEAD /api/objects/download` 读取大小 / `Accept-Ranges` / `ETag` / 文件名；服务端返回 `405/501` 时用 `GET` + `Range: bytes=0-0` 兜底。
- **写盘策略**：支持 File System Access 的浏览器（Chrome/Edge）在点击手势内弹「另存为」，按分片字节偏移**边下边写盘**（内存只保留并发数个分片）；不支持时并行 Range 下载后按顺序拼 `Blob` 再 `a.click()` 保存；文件 **>1GiB** 或服务端忽略 Range（返回 200）时回退浏览器原生单流下载。
- **可靠性**：每个分片最多 3 次尝试（首试 + 2 次重试）；`ETag` 为 `"<64 位 hex>"` 且文件 ≤256MiB 时用 `crypto.subtle` 校验 SHA-256（失败会尝试删除半成品）；`#dlModal` 显示进度 / 速度 / 重试次数，**「取消下载」**中止全部在途请求并丢弃未完成分片。
- **目录 ZIP**：`download-bulk` 仍走浏览器原生单流（ZIP 无 `Content-Length`，不做分块）。

**桌面客户端**（`client/`，启动与完整说明见 `client/README.md`）：

- `download_to(path, dest, progress, timeout, concurrency, cancel)`：先 `HEAD` 探测；文件大于分片且服务端支持 Range 时按区间**并行 GET**，预分配 `.part` 后各线程按偏移写入（不重复下载/计数）。
- **签名链接与 `download-bulk` 强制单流**（跳过 HEAD，避免按次数链接被额外消耗、ZIP 无 `Content-Length`）；`concurrency=1`、小文件、服务端不支持 Range 时同样单流。
- 每片失败最多重试 **3 次指数退避**（仅网络 / 超时 / 408 / 429 / 5xx；4xx 含 416 不重试），从已写入偏移续传；完成后校验总大小，`ETag` 为 `"<64 位 hex>"` 时校验 SHA-256，再 `os.replace`；取消会停止各分片、清理 `.part` 并抛 `Cancelled`。
- **下载暂停/续传**：并发分片每完成一片即更新目标目录的 `.<目标名>.artifactdepot-download.json`（`size`/`etag`/`chunk_size`/`done`，0600）；暂停在分片边界生效并保留 `.part` + 状态；再次下载时校验 `size`/ETag 一致后只补缺失分片（ETag 不一致丢弃 `.part` 从头下载）；失败保留两者可重试，取消仍删除两者。
- GUI 动作栏提供 **「下载并发」1–16**（默认取服务端 `data.download`，其次本地上次值，最后 4）、**「暂停下载」/「继续下载」** 与 **「取消下载」**，状态栏显示已下载字节 / 百分比 / 实时速度（≥150ms 合并刷新）；目录 ZIP 单流；上传动作栏提供 **「暂停上传」/「继续上传」**，失败项标记「失败（可续传）」。
- CLI 仅保留上传（`--file`）；下载走 GUI。

### 调用示例

```bash
BASE=http://192.168.18.43:8004
ADMIN=admin                                # 管理员 token（config.json 的 access_token，默认 admin；生产请改强令牌）
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

# HEAD 探测下载元信息（Content-Length / Accept-Ranges / ETag / 文件名；写审计但不计下载指标）
curl -sI "$BASE/api/objects/download?bucket=音视门carryVdeio&key=任务1/video.mp4&token=$USER"

# Range 分块下载（206 + Content-Range: bytes 0-8388607/total；分段并发后按顺序拼回）
curl -r 0-8388607 -o part0.bin "$BASE/api/objects/download?bucket=音视门carryVdeio&key=任务1/video.mp4&token=$USER"

# 一键下载目录（需 object:download；单文件直下，多文件/含子目录自动打包 ZIP）
curl -fL -o 交付物.zip "$BASE/api/objects/download-bulk?bucket=音视门carryVdeio&prefix=任务1/交付物&token=$USER"

# 签名链接 —— 按时效
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $USER" \
  -d '{"bucket":"音视门carryVdeio","key":"artifactdepot_0.9.0.tar","mode":"time","expires":3600}' \
  "$BASE/api/objects/presign"

# 签名链接 —— 按次数（3 次）
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $USER" \
  -d '{"bucket":"音视门carryVdeio","key":"artifactdepot_0.9.0.tar","mode":"count","count":3}' \
  "$BASE/api/objects/presign"

# 签名链接 —— 永久
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $USER" \
  -d '{"bucket":"音视门carryVdeio","key":"artifactdepot_0.9.0.tar","mode":"permanent"}' \
  "$BASE/api/objects/presign"

# 删除（需 object:delete；目录仅空可删）
curl -X DELETE "$BASE/api/objects?bucket=音视门carryVdeio&key=任务1/video.mp4&token=$ADMIN"

# 批量删除（需 object:delete_bulk）——先预览
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $ADMIN" \
  -d '{"bucket":"音视门carryVdeio","prefix":"任务1","mode":"files","dry_run":true}' \
  "$BASE/api/objects/bulk-delete"

# 批量删除——执行（必须 confirm=目录名；根目录为 bucket 名）
curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $ADMIN" \
  -d '{"bucket":"音视门carryVdeio","prefix":"任务1","mode":"all","confirm":"任务1"}' \
  "$BASE/api/objects/bulk-delete"

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
- **分层**：`api/`（HTTP 契约）、`storage.py`（存储引擎）、`config/`（配置包）；`auth.py` 只做认证、不碰业务
- **安全默认**：写操作必带令牌（管理员/用户）；下载需 token（签名链接除外）；分块下载复用 `object:download`（`HEAD` 探测不新增权限点）；默认共享令牌 `admin` 仅用于内网开箱即用，**生产必须替换为 ≥16 位随机串**（弱口令会告警，`ARTIFACT_DEPOT_STRICT_CONFIG=1` 时拒绝启动）；删除需 `object:delete` + 目录仅空可删；批量删除需 `object:delete_bulk` + 输入名称确认（保留目录本身、保护 `.warehouse.json`）；一键下载复用 `object:download` 且跳过隐藏项/符号链接、`prefix` 解析不得越出 bucket；路径穿越防护；令牌比较 UTF-8 常量时间；签名链接限时/限次/可作废
- **命名**：S3 语义命名（bucket/key/object/mkdir），对外 API 与 S3 一一对应，便于未来迁移 MinIO/OSS
- **可部署性**：脚本约定统一（`build_image.sh` 自动导出、`deploy_container.sh` host 网络默认 + `--config`/`VOLUME_MAPS`），生产问题可用 `check_ad_sync.sh` 定位

新增代码请遵循：新端点放对应 `api/*.py` 并在 `main.py` 注册；文件读写统一走 `storage.py`；不得在路由里直接碰文件系统。

---

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| 上传/下载提示「无效的访问令牌」 | token 未登记（先 `POST /api/tokens` 或从 DataHub 同步）或 `access_token` 未设；`/api/auth/check` 可先自测 |
| 下载提示「无效的访问令牌」 | 0.4.0 起下载需有效 token；直链需带 `&token=`；分享请走签名链接 |
| 默认 `access_token` 是 `admin`，安全吗 | 仅用于内网开箱即用/联调。生产/公网部署必须改为 ≥16 位随机串（`--token` / `ARTIFACT_DEPOT_ACCESS_TOKEN` / config.json / `.env`）；否则启动会打印弱口令告警，设 `ARTIFACT_DEPOT_STRICT_CONFIG=1` 时直接拒绝启动 |
| 一次下载为什么出现多条审计 | 分块并发下载每个 `Range` 请求各写一条 `download` 审计（`partial=true`、`size` 为区间字节数），`HEAD` 探测记 `head=true` 且 `size=0`；完整 GET 才是一条整文件记录 |
| 下载报 `416 Range Not Satisfiable` | 请求区间越界或文件在下载期间被替换（大小变化）。重新 `HEAD` 探测后再下载；`416` 不重试 |
| 下载中途取消后残留 `.part` | 正常取消会清理临时文件；若进程被强杀/断电，可手动删除同名 `.part` 后重下 |
| 网页下载没有分块并发 | 浏览器不支持 File System Access 且文件 >1GiB、服务端未返回 `Accept-Ranges`、或服务端忽略 Range 返回 200 时，会回退浏览器原生单流（属预期保护）；Chrome/Edge 可边下边写盘 |
| 下载速度没跑满 | 调大 `download.chunk_concurrency`（1–16）或检查磁盘/网络；服务端无额外限速 |
| 上传暂停后怎么继续 | 网页点「继续上传」（或失败行「继续」），桌面客户端点「继续上传」；暂停在分片边界生效，已传分片不会重传。持续中断会由服务端 TTL 回收（默认 24h） |
| 上传失败/断网后要重传整个文件吗 | 不需要。分片会话与已传分片保留在服务端 `.artifactdepot-uploads`，再次上传同一文件（同 bucket/key/size）时 `initiate(resume=true)` 会返回真实 `uploaded`，只补缺失分片；失败提示里也会说明「已保留续传状态」 |
| 网页刷新后还能续传吗 | 浏览器无法自动恢复文件列表：**重新选择同一文件**再点「上传」即可按 bucket/key/size 找回未完成会话（同一浏览器、同一 token 下有效）；文件大小或修改时间变化时会自动新建会话 |
| `.artifactdepot-uploads` 是什么？能删吗 | 是目标文件父目录下的**内部上传会话目录**（隐藏，列表不显示）：`.session.json` + `chunk_<i>.part` + `.incoming` + `.merged`。**不要手动删除**——里面有正在上传/可续传的分片；完整上传或超过 TTL 后服务端会自动清理，空目录也会自动移除。批量删除 `mode=all` 与 rename 已显式跳过它 |
| 为什么 `/tmp/dw_chunks` 没了 | 0.9.0 起分片与临时文件不再写宿主 `/tmp`，而是随目标文件落盘（legacy 客户端落 `upload.staging_dir`，默认 `<depot_dir>/.staging`），避免宿主小容量 tmpfs 被大文件上传打爆；旧版 `/tmp/dw_chunks` 只在启动时清理一次 |
| 服务端重启后上传还能续传吗 | 能。会话在数据盘，启动时扫描 `.artifactdepot-uploads` 并把未过期会话重新注册进内存 registry（单进程假设：不要用 `--workers` 多进程）；超过 `upload.session_ttl_seconds` / `upload.session_idle_ttl_seconds` 的会话会被清理 |
| 客户端续传状态文件在哪 | 上传：源文件同目录 `.<文件名>.artifactdepot-upload.json`；下载：目标目录 `.part` + `.<目标名>.artifactdepot-download.json`（均 0600）。**源目录不可写**时上传状态回退 `~/.artifactdepot/upload-state/<sha1>.json` 并在日志提示 |
| 改 `upload.staging_dir` 后没生效 | 该配置只在启动时设置 `tempfile.tempdir`（框架 spool 落点）；`POST /api/config/reload` 会在 `needs_restart` 中提示，需重启服务才完整生效（新会话目录本身即时按新值） |
| 删除提示「权限不足：需要 object:delete」 | 0.7.0 起删除是权限点 `object:delete`：管理员共享 `access_token` 始终可删；普通用户 token 需由管理员在「Token 管理」页勾选该权限点。缺权限返回 403，无效 token 返回 401。 |
| 删除目录提示「目录非空」 | 0.4.0 起目录仅空可删（防误删）；先进去删文件/子目录，清空后再删目录；或用「删除本目录全部内容」批量清空（保留目录本身） |
| 批量删除提示「权限不足：需要 object:delete_bulk」 | 批量删除是独立权限点 `object:delete_bulk`：管理员共享 token 与 `operator` 角色预设包含；普通用户 token 需在「Token 管理」页勾选该权限点，且 bucket/前缀在授权范围内，否则 403（缺权限点或越界） |
| 签名链接提示「次数已用完」/「链接已作废」 | 次数耗尽或已被作废；重新生成或换时效/永久 |
| 复制链接没反应 | `navigator.clipboard` 在 HTTP 下不可用时自动降级 `execCommand`；仍不行就手动选中链接复制 |
| 点生成/作废/删除没弹窗 | 浏览器拦截原生 `confirm/prompt`；已全部改用**页面内自定义弹窗**，不依赖浏览器原生 |
| 令牌含中文/特殊符号报 500 | 已修：`compare_digest` 改 UTF-8 字节比较，非 ASCII 令牌返回 401 而非崩溃 |
| 审计时间不准 / 显示 UTC | 后端统一记 UTC（`...Z`），前端自动转**浏览器本地时间**；容器已设 `TZ=Asia/Shanghai` 兜底 |
| 审计「公网 IP」为空 | 公网 IP 由浏览器调 ipify 尽力上报；外网不可达或签名链接外站直连时为空（局域网 IP 仍记录） |
| 同步不到新用户 token | 两处 users.json 不一致（指向不同 DataHub）、JSON 重复键、该用户 `api_token` 为空；按钮会提示 DataHub 是否可达 |
| **生产同步报 "All connection attempts failed"** | 三层根因（0.5.0 已修前两层）：1. 旧脚本默认注入 `ARTIFACT_DEPOT_DATAHUB_URL=127.0.0.1` 指向自身，0.5.0 起不传 `--datahub-url` 就不注入，以 config.json 为准；2. 旧镜像 Dockerfile 烤死 `ARTIFACT_DEPOT_DATAHUB_URL` 盖掉挂载 config，0.5.0 已移除该 ENV，**需重建镜像**；3. collab 的 DataHub 只绑 `127.0.0.1:8002` 宿主访问不通，supervisord.conf 改 `0.0.0.0`。用 `bash check_ad_sync.sh` 一键定位 |
| **容器访问不到宿主 8002** | rootless pasta 网桥下容器连宿主发布端口常不通；`deploy_container.sh` 默认 host 网络即解决，容器内用 `127.0.0.1:8002` 直连 |
| **tokens.json 生成位置不对** | 0.5.0 起可用 `meta_dir` 指定状态文件目录；改 `config/`/`storage.py` 后**需重建镜像**再部署（运行中镜像不带新代码，只改配置不生效） |
| 访问 `:8002` 连接被重置 | collab 容器默认只映射 8003；用多端口部署把 8002 也 `-p` 出来，并确认 DataHub 绑 `0.0.0.0` |
| 点 Swagger / ReDoc 打不开或样式为空 | 0.7.3 起 Swagger UI / ReDoc 静态资源已自托管在 `/static/`，内网/离线可用；若仍异常请确认镜像包含 `src/artifactdepot/resources/static/`。也可用公开只读接口 `GET /api/docs`（清单）与 `GET /api/docs/{name}?raw=1`（Markdown 原文） |
| 大文件上传「没反应」 | 看进度条；uvicorn 访问日志在**请求完成后**才打印，传输期间终端安静是正常的 |
| 启动报 `No module named uvicorn` | 当前 `python3` 未安装运行依赖；执行 `pip install -r requirements.txt`，或让 `python3` 指向已装依赖的解释器 |
| 容器里服务反复退出 | 缺 `python-multipart`（上传必需）；确认镜像构建时已安装 |

---

## 许可证与支持

- **许可证**：Apache License 2.0（SPDX 标识 `Apache-2.0`），英文版见 [LICENSE](LICENSE)，中文版见 [LICENSE.zh-CN.md](LICENSE.zh-CN.md)。
- **版权**：Copyright 2026 apyyc。
- **源文件署名**：全部第一方源文件带 SPDX 双行头（`# Copyright 2026 apyyc` + `# SPDX-License-Identifier: Apache-2.0`）。补写与校验用 `python3 scripts/add_spdx_headers.py`（`--check` 缺头即退出码 1，可进 CI）。第三方 vendored 静态资源（swagger-ui / redoc）保留原作者版权，不加本声明。各语言注释写法与豁免清单见 `docs/LICENSE-HEADER`——该文件**仅本地与镜像内留存，不随 git 分发**，因此公开仓库中可能不存在；对外以本节的声明与 `LICENSE` 为准。
- **支持**：2479511984@qq.com；版本记录见 [CHANGELOG.md](CHANGELOG.md)。
- **相关**：依赖 DataHub（用户/token 权威源）与协作平台（「查看我的 Token」）；处理端对接见 [docs/architecture.md](docs/architecture.md)。
