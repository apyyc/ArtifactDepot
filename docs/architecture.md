# ArtifactDepot 架构说明

> 版本 **0.9.0**

## 定位

ArtifactDepot 是一个**对象存储 / 工件仓库**站点（不是传统意义的"数据仓库/OLAP"）。
它负责收存处理产物（视频、文件等），按 **S3 模型**提供标准接口：bucket/key 命名、
上传、列表、下载（`HEAD` 探测 + Range 分块并发）、删除、预签名 URL，并提供网页浏览。

它不承担业务逻辑，也不存协作平台的 JSON 数据。

## 拓扑

```
处理端（carryVideo server.py，跑在 8 或本地）
    │  处理完成后 POST /api/objects 推送（token 认证）
    ▼
ArtifactDepot（独立 FastAPI 服务，端口 8004）
    │  文件落盘 <depot_dir>/<bucket>/<key>
    ├─ POST /api/objects/initiate|chunk|complete  ← 分片上传（会话落目标目录 .artifactdepot-uploads，可暂停/续传）
    ├─ GET  /api/objects/list      ← ProjectCollab"任务数据"模块 / 网页 UI
    ├─ HEAD /api/objects/download  ← 元信息探测（大小 / Accept-Ranges / ETag，不计下载指标）
    ├─ GET  /api/objects/download  ← 下载（Range 206；网页/桌面客户端并行分块）
    ├─ GET  /                      网页浏览
    ▼
用户浏览器
```

## 目录结构

```
ArtifactDepot/
├── README.md / CHANGELOG.md
├── check_ad_sync.sh              # 生产诊断（仅 python3）
├── docs/
│   ├── api_rules.md              # 对外接口文档（也由 /api/docs 提供原文）
│   └── architecture.md           # 本文档
├── dev_start_headless.sh         # 本地后台启动（源码模式直启 uvicorn）
├── build_image.sh                # 构建 + 自动导出 tar
├── deploy_container.sh           # 部署（host 网络默认 + --config/VOLUME_MAPS）
├── client/                       # 桌面客户端 + CLI（分块并发下载；client/README.md）
└── src/artifactdepot/
    ├── main.py                 # FastAPI 入口 + lifespan（建目录；启动清理旧版 /tmp/dw_chunks 与 legacy 会话、
    │                           #   创建 staging 并把 tempfile.tempdir 指向它（spool 落数据盘，退出还原）、
    │                           #   扫描 .artifactdepot-uploads 重建 registry）
    ├── config/                 # 配置包：__init__.py（加载/手动重载）+ config.json + config.production.json + .env(.example)
    ├── storage.py              # 对象存储引擎：路径安全 / put/list/delete/bulk_delete / bulk_download_scan / manifest / 预签名 /
    │                           #   签名链接注册表 / token 注册表 / 审计 / meta_dir /
    │                           #   分片会话（目标目录 .artifactdepot-uploads 与 legacy staging、进程内 registry、续传与 TTL 清理）
    ├── packaging.py            # 目录签名链接后台打包（扫描→复制/ZIP、进度、.package 就绪标记、失效清理、改名/删除钩子）
    ├── zipstream.py            # 流式 ZIP（后台线程 + 有界队列 + 背压；目录一键下载，不落临时文件）
    ├── auth.py                 # 写操作 token 校验（query / Bearer）
    ├── api/
    │   ├── objects.py          # S3-like 对象 API
    │   ├── system.py           # /health /api/buckets /api/tokens /api/audit /api/config /api/auth
    │   └── docs.py             # /api/docs 文档清单与内容（前端文档页数据源）
    ├── web/ui.py               # 网页 UI
    ├── permissions.py          # 权限点目录与角色预设
    └── resources/
        ├── config.json         # 运行配置
        └── index.html          # 单页浏览界面
```

## 存储布局

```
<depot_dir>/                  # 默认 ./depot（项目内）；容器部署用 /data/depot 卷
├── <bucket>/                     # bucket = 项目（如 project_1）
│   ├── task_5/                   # key 的父目录（可多级）
│   │   ├── xxx.mp4               # key = 任务/文件名（扁平路径式命名）
│   │   ├── .artifactdepot-uploads/<upload_id>/
│   │   │                         # 活跃分片上传会话（伴随文件本地化；完成/超时自动清理）
│   │   │                         #   .session.json / chunk_<i>.part / .incoming/ / .merged
│   │   └── .package/<link_id>/   # 目录签名链接成品（后台打包；链接失效后清理）
│   │                             #   <目录名>.zip 或唯一文件名 + .ready.json
│   └── .warehouse.json           # 每 bucket 元数据清单（隐藏，列表跳过）
├── .staging/                     # 默认 staging（upload.staging_dir 留空时）：
│                                 #   legacy 会话 <upload_id>/ + Starlette multipart spool
└── …

<meta_dir>/（可选；默认放 depot_dir 根下，0.5.0 起可用 meta_dir 单独放）
├── tokens.json                   # token→用户名 注册表（本地维护）
├── signed_links.json             # 签名链接注册表
└── audit.log                     # 审计日志（JSONL 追加）
```

- **伴随文件本地化（0.9.0）**：上传/下载自动创建的临时与断点文件都放「文件所在目录」，不再写宿主 `/tmp`
  （宿主 `/tmp` 常是 6.83GiB tmpfs，72GB 上传必然失败）：
  - 服务端：目标明确的会话在 `<目标父目录>/.artifactdepot-uploads/<upload_id>/`；legacy 客户端
    （`initiate` 不带 `bucket/key`）在 `<staging>/<upload_id>/`；直传临时文件
    `.<name>.artifactdepot-incoming-<rand>.tmp`；合并临时 `.merged`；框架 multipart spool 由
    `upload.staging_dir`（默认 `<depot_dir>/.staging`，启动时设置 `tempfile.tempdir`）决定。
  - 客户端（桌面）：上传断点 `.<源文件名>.artifactdepot-upload.json`、下载 `.part` 与
    `.<目标名>.artifactdepot-download.json`（0600）；源目录不可写时上传断点回退
    `~/.artifactdepot/upload-state/<sha1(server|bucket|key)>.json`。
- `.artifactdepot-uploads` / `.staging` / `.package` 均为隐藏目录：列表、目录递归大小、ZIP 打包均跳过；
  **批量删除 `mode="all"` 与 `rename_object` 显式跳过 `.artifactdepot-uploads`**（内部目录保护）。
  `.package` 由目录签名链接专用：对象 key 校验禁止隐藏路径段，外部无法直接访问；删除/清空目录前会
  先作废相关链接并删包，重命名时随目录一起移动并同步链接 key。
- **文件系统是唯一事实源**：列表以目录扫描为准，手工放入的文件也能列出；
  `.warehouse.json` 清单只做元数据补充（size/sha256/mtime/source_url）。
- 隐藏点文件（`.` 开头）不在列表展示、不可作为上传 key，防止覆盖清单。
- **`meta_dir`**：`tokens.json` / `signed_links.json` / `audit.log` 是**可再生状态**，
  与业务文件（bucket）分层存放，便于隔离与备份；留空时保持旧行为在仓库根下。

## 配置优先级

```
内置默认 < config/config.json（或 ARTIFACT_DEPOT_CONFIG 指定文件）< config/.env < 环境变量
```

- 支持环境变量：`ARTIFACT_DEPOT_DIR` / `ARTIFACT_DEPOT_META_DIR` /
  `ARTIFACT_DEPOT_ACCESS_TOKEN` / `ARTIFACT_DEPOT_PORT` / `ARTIFACT_DEPOT_MAX_UPLOAD_MB`，
  以及 `upload.*`（10 个 `ARTIFACT_DEPOT_UPLOAD_*`，含 `ARTIFACT_DEPOT_UPLOAD_STAGING_DIR`）、
  `download.*`（3 个 `ARTIFACT_DEPOT_DOWNLOAD_*`）、`signed_links.allow_directory`
  （`ARTIFACT_DEPOT_SIGNED_LINKS_ALLOW_DIRECTORY`）与 `package.*`
  （`ARTIFACT_DEPOT_PACKAGE_*`）；
  `.env` 支持点号写法（`upload.chunk_size_mb=8` / `upload.staging_dir=/data/depot/.staging` /
  `download.chunk_size_mb=8` / `package.max_items=100000`）
- `upload.staging_dir` 只在启动时设置 `tempfile.tempdir`（框架 spool 落点）并创建目录；
  热重载改它会在 `config/reload` 的 `needs_restart` 中提示，需重启才完整生效。

## API 契约（S3 对应）

> 下表列出主要接口（完整权限/参数/返回见 `docs/api_rules.md` §8、§12）。

| 方法 | 路径 | 说明 | 对应 S3 |
|---|---|---|---|
| POST | `/api/objects` | multipart 上传（bucket/key/token/source_url/overwrite；临时文件写目标父目录 + `put_object(move=True)`） | PutObject |
| POST | `/api/objects/initiate` | 发起/恢复分片会话（query 或 form；`resume`/`upload_id` 续传，返回 `resumed`/`uploaded`/`staging`） | - |
| POST | `/api/objects/chunk` | 上传分片（请求体落会话 `.incoming`，再原子改名为 `chunk_<i>.part`） | - |
| POST | `/api/objects/complete` | 合并分片（`.merged` + `put_object(move=True)` 原子落盘；成功/硬失败清理会话） | - |
| POST | `/api/objects/abort` | 取消分片会话（删除会话目录，幂等） | - |
| GET | `/api/objects/uploads` | 网页内部：列出当前令牌未完成的分片会话（`include_in_schema=false`，复用 `upload:abort`，不新增权限点） | - |
| POST | `/api/objects/mkdir` | 新建目录（key 可含 `/` 多级） | - |
| POST | `/api/objects/rename` | 重命名/移动目录或对象（源与目标都校验范围） | - |
| GET | `/api/objects/head` | 对象元信息探测（`object:head`；404/304） | HeadObject |
| GET | `/api/objects/list` | 列对象（bucket/prefix） | ListObjects |
| GET / HEAD | `/api/objects/download` | 下载（`HEAD` 探测 + Range 206 分块并发；HEAD 以 `include_in_schema=false` 注册，不新增权限点），需 token（签名链接免） | GetObject |
| GET | `/api/objects/download-bulk` | 一键下载目录：单文件直下 / 多文件或含子目录流式 ZIP（复用 `object:download`；见下「可靠性要点」） | - |
| DELETE | `/api/objects` | 删除对象/空目录（权限点 `object:delete`） | DeleteObject |
| POST | `/api/objects/bulk-delete` | 批量删除目录内容（`object:delete_bulk`；保留目录本身、需输入名称确认；见下「可靠性要点」） | - |
| POST | `/api/objects/presign` | 生成签名链接（`kind=file` 文件 / `kind=dir` 目录打包；count/time/permanent） | Presigned URL |
| GET | `/api/objects/signed-links` | 列签名链接（`link:list`；完整 URL 仅管理员/创建者；目录链接带 `package_state`） | - |
| GET | `/api/objects/signed-links/config` | 签名链接次数/时效上下限 + 目录打包能力（公开） | - |
| POST | `/api/objects/signed-links/{link_id}/revoke` | 作废签名链接（`link:revoke`；本人或管理员；目录链接按配置删包） | - |
| GET | `/api/objects/signed-links/{link_id}/package` | 目录打包状态/进度（`link:create`；内部接口，`include_in_schema=false`） | - |
| POST | `/api/objects/signed-links/{link_id}/package/cancel` | 取消目录打包并作废链接（`link:create`；内部接口） | - |
| GET/POST/DELETE | `/api/tokens` | Token 注册表管理（`token:read/write/delete`；默认脱敏前 2 + 后 4） | - |
| PUT | `/api/tokens/{value}` | 更新 token 权限/范围/过期/启用状态（`token:write`） | - |
| GET/POST | `/api/config` | 运行时配置快照 / 在线写回 config.json 并热加载（`config:write`；`include_in_schema=false`） | - |
| POST | `/api/config/reload` | 手动重载配置（`config:write` 或管理员共享 token） | - |
| GET | `/api/auth/permissions` | 权限点/接口目录（公开） | - |
| GET | `/api/auth/check` | 校验 token 身份/状态（公开，返回 valid/expired/enabled） | - |
| GET | `/api/buckets` | 列 bucket | ListBuckets |
| GET | `/api/audit` | 审计查询（`audit:read`） | - |
| GET | `/health` `/health/live` `/health/ready` | 健康/存活/就绪检查 | - |
| GET | `/metrics` | Prometheus 指标（可禁用） | - |
| GET | `/api/docs` `/api/docs/{name}` | 文档清单/内容（公开只读，白名单；`?raw=1` → text/plain） | - |
| GET | `/` | 网页 UI（标签栏含 Swagger/ReDoc 入口） | - |

0.7.0 起每个受保护接口对应一个**权限点（scope）**（见 `permissions.py`）：
- 管理员共享 `access_token` 放行全部；
- 用户 token 携带 role/scopes，可限定 bucket 与路径前缀范围、设置过期时间；
- 生成方式：`POST /api/tokens`（逐接口勾选）或网页「Token 管理 → 生成本轮 Token」；
- 旧 token 自动兼容为 `role=user`（历史全量非管理权限）；
- 读接口（list/buckets）默认内网公开，`require_read_token=true` 时改为必须 `bucket:list` / `object:list`。

## 可靠性要点

- 状态文件（`tokens.json` / `signed_links.json` / `.warehouse.json`）统一原子写（临时文件 + `fsync` + `os.replace`），
  并用 `storage.state_lock()`（进程内 `threading` 锁 + 跨进程 `fcntl.flock`）串行化读-改-写，避免并发丢更新 / 半截 JSON。
- 分片上传会话绑定创建者，`upload_id` 严格校验；单分片 ≤64MB（`upload.max_chunk_mb`），会话总量受 `max_upload_mb` / `upload.max_session_gb`（默认 200GB）约束；分片大小、并发、请求超时（默认 24h）均可由 `upload.*` 配置。会话目录布局（目标明确时）：
  `<目标父目录>/.artifactdepot-uploads/<upload_id>/` 内放 `.session.json`（owner / created_at / updated_at /
  received_bytes / bucket / key / size / chunk_size / total_chunks / staging）、`chunk_<i>.part`、
  `.incoming/<rand>.tmp`（单次 chunk 请求体）、`.merged`（complete 合并临时）。
- **续传状态机**（服务端以磁盘为唯一事实源，本地状态只是缓存）：
  1. `initiate(resume=false)` 或首次 → 新建会话（`staging="target"`，目标父目录不存在则创建；父路径是文件 `409`）；
  2. `chunk` 逐片落盘，每片更新 `.session.json` 的 `updated_at` / `received_bytes`（同时刷新目录 mtime）；
  3. `complete` → 合并到 `.merged`，`put_object(move=True)` 算完 SHA-256 后 `os.replace` 原子落盘，成功即删除会话；
  4. 失败 / 暂停 / 取消（客户端不 abort）→ **保留会话**，可再次 `initiate(resume=true)` 续传；
  5. `resume=true` 时服务端返回磁盘上真实的 `uploaded` / `received_bytes`，客户端只补缺失分片；
  6. 会话失效 / 参数不一致 → `400` 且 `detail` 含「分片会话」，客户端清本地状态、新建会话重传；
  7. 超过绝对 TTL / 空闲 TTL → 后台或启动扫描清理（终态：过期）。
- **清理**：`finalize_chunk_upload` 在成功或进入合并/落盘阶段后的硬失败（I/O、磁盘满等）时删除本次会话目录与
  `.merged`；校验类失败（缺片 / 超限 / 权限 / `overwrite=false` 的 409）保留会话以便重试；`store_chunk` 复制失败清理
  `.part.tmp`；直传/分片请求读取异常清理临时文件。启动时先清旧版 `/tmp/dw_chunks` 残留与 legacy staging 根下历史会话，
  再 `scan_upload_sessions()` 扫描 depot 内 `.artifactdepot-uploads`：超 TTL 的删除、其余注册进 registry；
  后台每 10 分钟只遍历 registry + legacy staging 根（成本与仓库大小无关），`cleanup_stale_chunk_sessions()` 查空闲、
  `cleanup_expired_chunk_sessions()` 查绝对寿命（浏览器被杀/断网、无法发送 abort 时的兜底）。
- **重启续传与单进程假设**：会话元数据与分片在数据盘，进程重启不会丢；启动扫描重建进程内 registry
  （`{upload_id: Path}`）后客户端即可继续 `initiate(resume=true)`。registry 只是加速索引（避免每请求全树扫描），
  **磁盘才是唯一事实源**；uvicorn 未开 `--workers`（单进程假设），多进程部署需改为共享存储的会话索引。
- **框架级 spool 落数据盘**：lifespan 在清理旧 `/tmp/dw_chunks` **之后**把 `tempfile.tempdir` 指向
  `upload.staging_dir`（默认 `<depot_dir>/.staging`），因此 Starlette multipart 解析产生的 spool 文件也落数据盘而非
  宿主 `/tmp`；退出 lifespan 时恢复原值（测试/嵌入场景安全）。该配置只在启动生效，热重载改它会在 `needs_restart` 提示。
- **分块并发下载**（`HEAD` / `GET /api/objects/download` + `Range`，0.9.0）：服务端不新增分块端点，只提供
  `HEAD` 元信息（`Content-Length` / `Accept-Ranges: bytes` / `ETag` / `Content-Disposition`，`include_in_schema=false`、
  不新增权限点）与单区间 `Range`（`206` + `Content-Range`；越界 `416`；多区间 `multipart/byteranges`）。
  下载参数 `download.*`（分片 1–64MB、并发 1–16、超时 60s–7d；默认 8MB / 4 路 / 1h）经
  `GET /api/auth/permissions` 的 `data.download` 下发（字节/整数/秒），`POST /api/config/reload` 即时生效。
  客户端（网页 / 桌面）策略：先 `HEAD`，满足 `Accept-Ranges` 且文件大于分片时按闭区间并行 GET，每片校验
  `206` + `Content-Range` 起点与长度；桌面预分配 `.part` 后按偏移写入（锁保护，重试从已写入偏移续传、
  不重复计数），网页优先 File System Access 按位置写盘、否则按顺序拼 `Blob`；完成后校验总大小，`ETag`
  为 `"<64 位 hex>"` 时校验 SHA-256；取消时停止在途请求并清理临时产物；服务端忽略 Range（200）、网页文件
  >1GiB 且无 FS API、HEAD 不可用（405/501）、签名链接与 `download-bulk` ZIP 均回退单流。
  审计：每个 Range 一条 `download`（`partial=true`、`range` 为请求头原文、`size` 为区间字节数），HEAD 一条
  （`head=true`、`size=0`）；HEAD / `304` 不计 `DOWNLOADS`，可解析单区间与完整 GET 各 +1。
- **配置中心**（网页「配置」标签页，管理员共享令牌或具备 `config:write` 的 token）：`GET /api/config` 返回由 `DEFAULT` 递归生成的
  schema、config.json 文件值、当前生效值、字段来源（config.json/.env/环境变量/default）、需重启项与告警；
  `POST /api/config` 校验类型/范围后原子写回 config.json（0600），再 `reload_config()` 热加载，失败回滚；
  写入用进程内 `_CONFIG_LOCK` 串行化（防并发保存互相覆盖）；非管理员共享 token **不能提交新的 `access_token`**（403，防提权接管）；
  `POST /api/config/reload` 重新读取磁盘配置。网页在配置页输入令牌后自动加载（400ms 防抖）。
  两个 config 接口 `include_in_schema=false`，OpenAPI 操作数不变；密钥字段脱敏，`.env`/环境变量覆盖的字段
  会标注「写入文件但不生效」，与 `config/reload` 的 `needs_restart` 语义一致。
- **批量删除目录内容**（`storage.bulk_delete`，`POST /api/objects/bulk-delete`）：先**只读收集**目标并检查上限
  （`MAX_BULK_DELETE_ITEMS = 50000`，`files + dirs` 超限 `400`，`dry_run` 预览同样受限），再执行删除；
  `dry_run=true` 在删除前返回统计，不落盘、不写审计。执行时 `FileNotFoundError` 视为「已不存在」跳过
  （并发删除幂等），其余 `OSError` 记入 `errors` 继续；符号链接只删链接本身、**不跟随**；
  先删文件、再自底向上删目录；`prefix` 目录本身与 bucket 根 `.warehouse.json` 始终保留；
  清单在 `state_lock()` 内一次性清理「被删文件 key」与「被删目录前缀下的条目」。
- **目录一键下载流式 ZIP**（`zipstream.py` + `storage.bulk_download_scan`，`GET /api/objects/download-bulk`）：
  zipfile 在**后台线程**写入一个非 seekable 的队列 writer（`ZIP_STORED` + `allowZip64`，自动 data descriptor），
  异步生成器从**有界队列**（8 块 × 256 KiB）取块 `yield` 给 `StreamingResponse`；队列满时生产线程阻塞形成**背压**，
  内存占用恒定，**不落临时 ZIP、无 `Content-Length`**。读文件固定 256 KiB 块，绝不整文件 `read()`；
  客户端中断置位 `threading.Event`，生产线程退出并由 `finish_once()` 兜底写一条 `aborted=true` 审计。
  预检只读扫描（`bulk_download_scan`）决定「单文件直下 / ZIP」，隐藏项与符号链接跳过、不跟随；
  `prefix` 解析（含符号链接）后必须仍在本 bucket 内，单次 `files + dirs` 上限 `MAX_BULK_DOWNLOAD_ITEMS = 100000`。
- **目录签名链接后台打包**（`packaging.py` + `.package`）：
  `presign(kind=dir)` 立即创建链接并返回，随后在后台 daemon 线程按 `package.max_concurrent_jobs` 限流打包；
  成品先写临时文件，`os.replace` 原子落盘为 `.package/<link_id>/<目录名>.zip`（或唯一文件），再写 `.ready.json`。
  下载接口先 `inspect_signed_link`（只校验不消费）再检查就绪，未就绪返回 `409` 且不扣次数；成品是真实文件，
  通过 `FileResponse` 支持 `HEAD/Range/ETag`。失败/取消自动作废链接并删包；链接失效后按
  `package.cleanup_interval_seconds` + `package.cleanup_grace_seconds` 后台删包；启动时按
  `package.cleanup_on_startup` 清理残留并恢复未完成任务。`.package` 是隐藏内部目录（列表/ZIP/目录统计跳过，
  对象 key 不能直接访问）；目录改名时链接 key 跟随、旧 URL 仍可用（下载以注册表 key 为准），
  删除/批量清空目录时相关链接作废并删包。进度保存在进程内 registry（单进程假设）；`.ready.json`
  是持久完成标记，重启后已完成包仍可下载。
- 容器以非 root 用户 `artifactdepot(10001)` 运行；绑定宿主机目录时由 `deploy_container.sh` 以宿主 UID/GID + `--userns=keep-id` 运行，保证卷可写。

## 可观测性

- `X-Request-ID` 中间件透传/生成请求 ID，随响应头返回；结构化 JSON 访问日志含 request_id/method/route/status/duration_ms/client。
- `/metrics` 输出 Prometheus 文本格式：HTTP 计数/耗时直方图、上传/下载/删除计数、token 数、磁盘用量；`metrics_enabled=false` 时 404。分块下载按「可解析 Range 请求」各计一次下载，`HEAD` 与 `304` 不计。
- `/health/live`（进程存活）、`/health/ready`（depot 目录可写，否则 503）。
- 前端脚本/样式外置（`/static/app.js`、`/static/app.css`），事件委托 + 严格 CSP（`script-src 'self'`）；Swagger/ReDoc 单独放行 `unsafe-inline`。

## 安全要点

- 路径穿越防护：bucket 名校验 + key 拒绝绝对路径/`..`/空段/反斜杠/点前缀，解析后校验落在仓库根内。
- 上传上限：`max_upload_mb`（0 不限）；分片/并发/超时见 `upload.*`（`request_timeout_seconds` 默认 24h）。
- 写令牌：UTF-8 字节常量时间比较（hmac.compare_digest，非 ASCII 安全）。
- 预签名：HMAC-SHA256(secret=access_token, bucket|key|expires)，过期即拒。
- 删除：权限点 `object:delete`（管理员共享 token 默认拥有）；目录仅空可删（防误删）。
- 批量删除：权限点 `object:delete_bulk`（`operator` 预设包含），服务端二次校验 `confirm`
  （目录名 / 根目录为 bucket 名），只清内容不删目录本身，bucket 根 `.warehouse.json` 受保护。
- 一键下载：复用 `object:download` + bucket/前缀范围；只打包可见项（隐藏文件/目录、`.keep`、`.warehouse.json`、
  符号链接一律跳过且不跟随），`prefix` 解析后不得越出 bucket；ZIP 条目名来自真实相对路径，中文名用 RFC 5987
  `filename*`；流式生成、单次上限 100000 项。
- 目录签名链接：创建复用 `link:create` + 资源范围（与文件链接一致，不新增权限点）；打包内容同样是**只打包可见项**，
  `package.max_items` / `package.max_total_mb` 限制资源消耗；`.package` 隐藏且对象 key 校验禁止隐藏段，
  外部无法直接访问，下载只能通过链接；状态/取消接口要求 `link:create` 且仅创建者/管理员；失败/取消自动作废链接，
  过期/次数用完/作废后清理包。注意：链接是能力凭证，持有者可在有效期内下载整目录快照（与文件链接一致）。
- 共享令牌：`access_token` 默认 `admin`（内置 / `config.json` / `.env`），仅用于内网开箱即用/联调；
  `admin` 属弱口令会触发启动告警，`ARTIFACT_DEPOT_STRICT_CONFIG=1` 时拒绝启动；生产必须改为 ≥16 位随机串。
- 分块下载：复用 `object:download`，`HEAD` 探测不新增权限点；每个 Range 独立鉴权、独立写审计；
  签名链接按次数计费时每个 Range 都消耗次数，分块下载应改用 token；`download-bulk` ZIP 无 `Content-Length`，
  不支持分块。
- 站点配置：`config:write` 权限点可读取配置快照、写回 config.json 并热加载，仅授予可信管理者；
  但**不能轮换 `access_token`**（403，仅管理员共享 token 可改），避免用户 token 通过配置页接管管理员；
  `/api/config*` 不进 OpenAPI，无 token 401、缺权限 403。
- Token 展示与长度：注册表默认只显示**前 2 位 + `****` + 后 4 位**；新建 token 长度受
  `token.min_length` / `token.max_length`（默认 5–30，范围 1–256）约束，越界 400；覆写既有 token 不校验长度。
- **伴随文件与续传（0.9.0）**：分片会话绑定 owner（`initiate` / `chunk` / `complete` / `abort` 均校验，admin 例外）；
  `upload_id` 严格字符集校验、会话目录二次确认落在 depot 内；删除/清理不跟随符号链接。
  内部目录 `.artifactdepot-uploads` 被列表 / 目录大小统计 / ZIP 打包跳过，并被 `bulk_delete(mode="all")` 与
  `rename_object` **显式跳过**，防止误删/搬走活跃上传；客户端 key 不允许隐藏路径段，无法直接操作内部目录。
  **不新增权限点**（权限点为 22 条；OpenAPI 操作数不变、旧 token 无需变更），`initiate` 响应只回传
  `uploaded` 等相对信息、**不回显服务端绝对路径**；服务端按**单进程**运行（不要 `--workers`），
  registry 仅作索引、磁盘才是唯一事实源。

## 部署

- **build → save → 拷贝 → load → deploy**：
  - 开发机 `./build_image.sh`（构建 + 自动导出 `artifactdepot-0.9.0.tar`）
  - 拷贝 tar 到生产机 → `podman load -i` → `./deploy_container.sh --token <你的管理员token>`
- **host 网络默认**：容器共享宿主网络，容器内 `127.0.0.1` 即宿主机；
  `--port-map` 可改回端口映射（不推荐，rootless 桥接常不通）。
- **三卷分离挂载**（`deploy_container.sh`，均不随镜像版本变化，升级后原地沿用）：
  - `<部署根>/config/` → `/data/config`：配置目录（`config.json` + `.env`），首次运行用
    `config.production.json` 播种；改完重启容器即生效，无需重建镜像
  - `<部署根>/data/` → `/data/depot`：业务数据（bucket 与对象）
  - `<部署根>/state/` → `/data/state`：状态文件（`tokens.json` / `signed_links.json` / `audit.log`）
  - 默认部署根 `~/SERVER/artifactdepot`；不可写时回退到 `~/.local/share/artifactdepot`
  - `--deploy-root` 改基目录；`--data-dir` / `--state-dir` / `--config-dir` 单独覆盖；
    `--config <文件>` 只挂单个 `config.json`（不含 `.env`）
  - 容器内通过 `ARTIFACT_DEPOT_DIR` / `ARTIFACT_DEPOT_META_DIR` / `ARTIFACT_DEPOT_CONFIG` /
    `ARTIFACT_DEPOT_ENV` 四个环境变量告知应用实际路径
  - 旧布局（`artifactdepot_<版本>/warehouse`，状态文件混在数据目录）会被自动检测并打印迁移命令
- **自启**：Podman 走 `systemd --user`；Docker 走 `--restart=always`。
- 排障：`bash check_ad_sync.sh` 逐段检查容器、挂载、配置、健康检查与端口连通性。
- 可选 Nginx `location /depot/` 反代到 8004。
- carryVideo `server.py` 处理完推送 + ProjectCollab"任务数据"子模块对接，见 README 集成指引。

