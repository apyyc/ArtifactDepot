# Changelog

ArtifactDepot v0.10.0 — 所有对本项目的重要更改都将记录在此文件中。

本日志格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本规范](https://semver.org/lang/zh-CN/)。

---

## [未发布]

### 修复：浏览器断开/刷新后的未完成上传被误显示为仍在传输（2026-10-26）

#### Fixed

- **「未完成的上传（当前令牌）」不再把断点会话显示成正在上传的任务**：页面刷新或浏览器断开后，
  File 与在途 XHR 已丢失，服务端只保留断点会话；此前该行只显示进度条与「已传 X / Y（Z%）」，
  没有状态与续传入口，用户会误以为上传仍在继续（DevTools 里没有任何分片请求），也找不到暂停按钮。
  现在断点行明确显示**「已中断（未在传输）」**（最后分片写入 <=15 秒时显示「最近有分片写入」）、
  **最后活动时间**与保存位置，并用弱化/琥珀色进度条区分；说明文案明确「断点没有可暂停的传输」。
- **断点会话新增「继续上传」**：点击后用隐藏 file input 重新选择同一文件，校验文件名/大小
  （本浏览器有断点指纹时再校验 `lastModified`），通过后绑定会话 `upload_id` + 固定 `bucket/key`，
  只补缺失分片；会话失效时明确报错而不会悄悄新建会话重传整个文件。取消上传仍保留。
- **未完成列表自动刷新**：有断点会话时每 8 秒轮询一次、页面重新可见时立即刷新；其它页面/设备
  继续上传时进度与状态自动更新，完成后断点行自动消失。

#### Added

- `GET /api/objects/uploads` 返回 `idle_seconds`（服务端时钟下最后分片写入距今秒数，无法解析为 `-1`），
  供前端区分「最近有分片写入」与「已中断」；不新增权限点、不改变 OpenAPI 操作数。
- 测试：`tests/test_upload_pending.py` 覆盖 `idle_seconds`/断点保留；`tests/test_upload_resume_ui.py`
  覆盖断点状态文案、最后活动、继续上传（重新选文件 + 固定目标）、轮询与样式契约。

---

## [0.10.0] - 2026-10-25

### 文档审计与稳定文档校对（2026-10-25）

#### Changed

- **`docs/api_rules.md` 全量逐接口校对**：修正 `initiate` 的 token 传递方式（form token 不解析）、
  `public_ip` 交叉引用、错误码（补配置/`rename`/`access_token` 防提权）、列表空 bucket/目录 `mtime`/分页说明、
  `mkdir`/`DELETE`/`abort`/分片响应、`presign` 上下限与响应语义、批量删除内部目录跳过、ZIP 目录 `mtime`、
  `reload` 环境变量、DataHub `mapping` 明文提示、配置表补齐 38 项、`operator` 角色补 `object:download`、
  `auth/check` 成功/失败字段等。
- **`README.md` / `docs/architecture.md`**：修正 `depot_dir` 默认值 `./depot`、补 `PUT /api/tokens/{value}`、
  统一 `{link_id}`、补文档白名单许可证、`auth/check` 字段、删除权限说明；API 契约表补全路径并拆分行。
- **一致性基线**：文档与运行时一致（35 条 OpenAPI 操作、23 个权限点、38 个配置项、9 个角色预设、7 篇文档）；
  文档保持无 emoji。
- **接口文档精简**：`docs/api_rules.md` 由 1481 行精简为 800 行，移除版本/变更日志、历史勘误、内部实现细节、
  接入指南等非接口内容，只保留文档介绍、接口规则、错误码、配置项、权限点与角色。

### 站点配置权限、Token 长度与脱敏（2026-10-25）

#### Added

- **新增权限点 `config:write`（系统 / 审计）**：`GET/POST /api/config` 与 `POST /api/config/reload`
  改为「管理员共享 token 或具备 `config:write` 的 token」可调用；无 token `401`、缺权限 `403`。
  权限点总数 22 → **23 条**，OpenAPI 操作数仍 **35 条**（config 接口仍 `include_in_schema=False`）；
  `admin` 角色预设自动包含新权限点，其他角色预设不变。
- **配置项 `token.min_length` / `token.max_length`（默认 5 / 30，范围 1–256）**：配置中心新增「令牌」分组；
  服务端自动生成 token 长度取 `max_length`；显式创建新 token 长度须在 `[min_length, max_length]` 内，
  越界返回 `400`；覆写既有 token 不校验长度（兼容历史短 token）；支持环境变量
  `ARTIFACT_DEPOT_TOKEN_MIN_LENGTH` / `ARTIFACT_DEPOT_TOKEN_MAX_LENGTH`；配置保存时校验 `min <= max`。
- **`GET /api/auth/permissions` 新增 `data.token`**：`{min_length, max_length}`，供网页生成 token 时读取。

#### Changed

- **Token 脱敏规则**：未勾选「显示明文」时显示**前 2 位 + `****` + 后 4 位**（长度 ≤6 时仅前 2 位 + `****`），
  不再显示哈希后缀；前 2 + 后 4 相同的不同 token 由服务端追加短哈希兜底，避免注册表条目互相覆盖。
- **网页「配置」标签页**：输入令牌后自动加载配置（400ms 防抖），不再需要手动点「加载配置」；
  配置页不再仅限管理员共享令牌，具备 `config:write` 的 token 也可读取/写回/热加载。
- **配置写入加固**：`config:write` 用户 token **不能提交新的 `access_token`**（403，仅管理员共享 token 可轮换），
  避免借配置页接管管理员身份；配置写入用进程内 `_CONFIG_LOCK` 串行化，避免并发保存丢失更新。
- **文档与网页 UI 移除 emoji**：`README.md`、`CHANGELOG.md`、`docs/*.md`、`index.html`、`app.js`、`app.css`
  中的警示等 emoji 改为中文文字，并新增自动化测试防止回潮。
- **测试**：更新权限点计数（23）、config 权限、Token 掩码与长度校验用例；新增 emoji 禁令测试。

### 令牌跨标签页同步（2026-10-25）

#### Added

- **令牌输入框跨标签自动同步**：五个标签页（浏览 / 签名链接 / Token 管理 / 审计 / 配置）各自有一个
  令牌输入框，在任意一个输入后，其余输入框自动填入同一令牌，不必逐页重复输入；清空同理。
  同步会派发 `input` 事件，让上传门槛（选择文件/上传按钮）、当前令牌未完成上传列表随之刷新。
- **URL 带入的 token 也同步**：`/?token=...` 进入时五个标签页的令牌框一起填入（此前只填「浏览」
  与「签名链接」两处）。
- **配置页改管理员令牌后全局换新**：保存 `access_token` 后五个输入框一起更新，避免后续操作 401。

#### Changed

- 令牌只在**当前页面内存**里同步，不写 `localStorage` / `sessionStorage`（与「断点续传只缓存文件
  指纹、不存 token」的策略一致），刷新页面后需重新输入或走 `?token=` 跳转。
- **测试**：`tests/test_upload_resume_ui.py` 新增令牌跨标签同步静态契约（五个输入框 + 监听 +
  URL 带入 + 配置改令牌 + 不落盘）。
- **文档**：`README.md`（网页 UI / 跳转免填两行）同步。

### 上传元数据：来源放宽 + 新增备注（2026-10-25）

#### Added

- **网页新增可选「备注」**：上传工具栏在「来源」右侧新增 `#upNote` 输入框（可选，一句话备注），
  直传与分片上传都会随请求提交，写入对象元数据。
- **接口新增 `note` 字段**：`POST /api/objects` 与 `POST /api/objects/complete` 均接受可选
  multipart 字段 `note`；落盘后写入 `.warehouse.json` 的 `note`，`GET /api/objects/list`
  的文件项返回 `note`（无则为空串）。旧客户端不传即空，行为兼容，权限点/OpenAPI 操作数不变。

#### Changed

- **「来源URL」改为「来源」**：网页标签与占位提示改成「来源 / 可选，来源链接或说明」，
  值不再限定为 URL——接口侧本来就只按字符串存储，因此允许频道名、系统名、客户名等自由文本。
- **元数据文本长度上限**：来源 512 字符、备注 200 字符（超出截断），避免整份重写的
  `.warehouse.json` 被长文本撑大、拖慢所有上传。
- **文档**：`README.md`（上传/分片/列表字段）、`docs/api_rules.md`（§2.1 上传参数、§2.5 列表项、
  §3.5 complete 参数）同步。

### 网页上传交互优化（2026-10-25）

#### Added

- **当前令牌未完成上传列表**：页面加载或输入令牌后，按当前 token 列出上传了一半的分片会话
  （文件名、bucket/key、已传字节/百分比），可逐条「取消上传」。会话来自服务端磁盘，因此页面刷新、
  服务重启后仍能显示；取消会调用 `POST /api/objects/abort` 删除服务端分片并清理本地断点记录。
  内部接口 `GET /api/objects/uploads` 使用 `include_in_schema=False`、复用 `upload:abort` 权限，
  **只返回 `owner == 当前 actor` 的会话**，不新增权限点（当时 22 条，现 23 条）与 OpenAPI 操作（仍 35 条）。
- **任务行内暂停/继续/放弃**：每个上传任务的进度条右侧独立提供「暂停上传」「继续上传」「放弃」，
  顶部「并发数 / 上传」工具栏不再出现全局暂停/继续按钮；暂停按任务在分片边界生效，不影响其它文件；
  小文件单请求直传不显示暂停按钮，但「放弃」会 abort 当前 XHR。
- **令牌门槛**：未填写令牌时「选择文件」与「上传」按钮禁用（页面初始即 `disabled`），
  输入令牌后即时解禁；拖拽入队前仍会再次校验令牌。

#### Changed

- **上传说明文案精简**：缩短为上传目标、多选/拖拽、分片与失败续传、任务行暂停/继续、
  刷新后未完成列表/取消、下载分块与取消几行；**不再出现协作平台 /「查看我的Token」等信息**。
- **测试**：新增 `tests/test_upload_pending.py`（接口/隔离/取消/契约计数 5 个用例）；
  `tests/test_upload_resume_ui.py` 更新为「令牌门槛 + 任务行暂停/继续 + 未完成上传列表」静态契约（18 个用例）。
- **文档**：`README.md`、`docs/api_rules.md`（§3.5 内部接口、§7 网页行为）同步。

### 配置中心 + 配置项补齐（2026-10-25）

#### Added

- **网页「配置」标签页**（仅管理员共享令牌）：读取全部 `config.json` 字段（按基础/DataHub/
  网络与安全/界面/上传/下载/签名链接/系统分组），显示「需重启」「被 .env 覆盖」「被环境变量覆盖」
  标记；支持在线修改、原子写回并热加载，也可只从磁盘重载。密钥字段脱敏展示，留空表示保持原值。
- **管理员配置接口**：`GET /api/config` 返回 schema / 文件值 / 生效值 / 字段来源 / 告警 /
  需重启项；`POST /api/config` 校验类型与范围、原子写 `config.json`（0600）、调用 `reload_config()`
  热加载，失败自动回滚。两者 `include_in_schema=False`，仅共享管理员令牌可用，不新增权限点/操作数。
- **新增配置项**：`upload.session_cleanup_interval_seconds`（后台分片清理间隔，默认 600s）、
  `datahub_sync_interval_seconds`（DataHub 惰性同步间隔，默认 60s，0=每次拉取）、
  `ui.file_page_size`（文件列表分页，默认 200）、`ui.audit_page_size`（审计查询条数，默认 500）。

#### Changed

- `config.json` / `config.production.json` 补齐此前缺失的 `require_read_token`，并加入上述新键；
  `.env.example` 同步点号写法与 `ARTIFACT_DEPOT_*` 环境变量。
- 网页从 `/api/auth/permissions` 的 `data.ui` 读取分页配置；审计查询显式传 `limit`。
- **配置写入校验补强**：`POST /api/config` 对 `null`、对象/列表项对象等错误容器类型一律返回 `400`，
  不再把 `None`/dict 静默转成字符串；配置文件缺失时以**当前生效配置**为基线，避免只提交一个字段
  就把 `access_token` 等其它配置重置为默认值。
- **测试**：新增 `tests/test_config_ui.py`（12 个用例，覆盖接口权限/热更新/回滚/密钥轮换/.env 覆盖/
  新配置项/OpenAPI 计数/网页静态标记/错误类型/缺失文件基线）。
- **文档**：`README.md`、`docs/api_rules.md`（§4.1.3 配置接口、§7 配置标签页、§8 总表、§9 配置项、
  §12.1 `ui` 下发）、`docs/architecture.md` 同步。

---

## [0.9.0] - 2026-10-24

> 本次发布**合并此前 0.9.0 及以上的全部更新**（目录批量删除、目录一键下载、
> 分块并发下载、伴随文件本地化与上传暂停/恢复），均为同一轮更新，统一按 **0.9.0** 发布。


### 目录批量删除（一键清空）

#### Added

- **批量删除目录内容接口** `POST /api/objects/bulk-delete`（BulkDelete）：清理指定目录（`bucket` + `prefix`，
  `prefix` 为空或 `"/"` 表示 bucket 根）的**内容**，目录本身始终保留。`mode=files` 只删当前目录的直接文件
  （子目录及其内容、`.keep`、隐藏项保留）；`mode=all` 递归删除全部文件与子目录（隐藏项一并删除，
  但 bucket 根 `.warehouse.json` 永不删除）。支持 `dry_run=true` 只预览「文件数 / 目录数 / 总大小」，
  预览不落盘、不写审计；响应返回 `files/dirs/bytes/errors/elapsed_ms`，部分失败仍返回 `200`
  并在 `errors` 中给出明细（最多 20 条）。
- **服务端确认校验**：执行（`dry_run=false`；省略 `dry_run` 即为执行）必须携带 `confirm`，且等于目录名
  （`prefix` 最后一段；bucket 根目录为 `bucket` 名），缺失 / 不匹配返回 `400` 且不做任何改动；
  网页端确认按钮在输入匹配前禁用，服务端仍二次校验（前端禁用不能替代服务端）。
- **安全与上限**：不跟随符号链接（只删链接本身）；单次最多处理 `files + dirs = 50000` 项
  （`dry_run` 预览同样受限），超限 `400`；并发删除导致的 `FileNotFoundError` 视为已不存在跳过，
  其余 `OSError` 记入 `errors` 并继续；`prefix` 不存在或不是目录 `404`；路径穿越 / 隐藏路径段由
  `validate_key` 拦截 `400`。
- **新权限点 `object:delete_bulk`**（分组「对象删除」）：对应 `POST /api/objects/bulk-delete`，
  并校验 bucket / prefix 资源范围；`admin` 自动含全部权限点，`operator` 角色预设同步加入。
- **网页 UI**：目录浏览页面包屑卡片新增「清空本目录文件」（`files`）与「删除本目录全部内容」（`all`）
  两个危险按钮（危险样式，仅选中 bucket 后可用）；`#bulkDeleteModal` 先 `dry_run` 预览文件数 / 目录数 /
  总大小并提示不可恢复，输入目录名后确认按钮才可用；成功后 toast 汇总并刷新列表，
  `errors` 非空时红色 toast 提示部分失败。事件委托 + `data-action`，无内联 JS，兼容严格 CSP。
- **审计与指标**：执行写 `delete_bulk` 审计（`bucket` / 规范化后的 `prefix` / actor / 客户端 IP /
  `size=bytes`，`extra` 含 `mode` / `files` / `dirs`）；`dry_run` 不写审计；`DELETES` 指标每次执行 +1。
- **测试**：新增 `tests/test_bulk_delete.py`（13 个测试函数，对应契约 §2.4 的 10 项要求），覆盖
  files / all 预览与执行、清单清理、确认名称校验、401/403/200 权限矩阵与资源范围、`operator` 预设、
  404 / 400、项数上限、审计与 `DELETES` 指标、符号链接不跟随、UI 标记。
- **文档**：`docs/api_rules.md` 新增 §2.12（端点 / 鉴权 / 请求响应 / files 与 all 语义 / 确认规则 /
  错误码 / 审计），并同步 §1.6、§7、§8、§10、§12.2、§12.3；`docs/architecture.md` 补 API 契约、
  可靠性与安全要点；`README.md` 补核心特性、权限口径、API 参考、调用示例与 FAQ。

#### Changed

- **`operator` 角色预设**新增 `object:delete_bulk`（`admin` 自动包含；`publisher` / `user` / `uploader`
  等其余预设保持不变），运营方无需单独勾选即可使用批量删除。
- **版本号统一提升到 `0.9.0`**（`VERSION` / `main.py` 兜底 / `build_image.conf` / `deploy_container.sh` /
  `README` / `docs`）。


### 目录一键下载（单文件直下 / 流式 ZIP）

#### Added

- **一键下载目录接口** `GET /api/objects/download-bulk`（DownloadBulk）：下载当前浏览目录的全部内容；
  **复用权限点 `object:download`（不新增权限点、角色预设不变）**，token 可走 query `?token=` 或
  `Authorization: Bearer`，并校验 bucket/prefix 资源范围（越界 `403`）；不接入签名链接。
- **直下 vs ZIP 规则**：目录内只有 1 个可见文件且无可见子目录 → 直下原文件（原文件名、`Content-Type`、
  Range/206、ETag/304）；其他非空情况 → 打包 ZIP；空目录 / 不存在 / 不是目录 → `404`
  （不返回 0 字节 ZIP）。
- **ZIP 结构**：顶层为与目录同名的文件夹（bucket 根用 bucket 名），内含相对路径的文件与子目录条目；
  空目录也保留条目（名称以 `/` 结尾）；**文件条目保留源文件 mtime**（目录条目为当前时间）；跳过隐藏文件/目录
  （含 `.keep`、`.warehouse.json`）与符号链接（不跟随、不打包）；每层先子目录（深度优先）再文件、名称排序，输出稳定。
- **流式与内存**：`zipstream.py` 用后台线程 + 非 seekable 队列 writer + 有界队列（8 块 × 256 KiB）实现背压，
  `ZIP_STORED` + `allowZip64`，无 `Content-Length`、**不落临时文件、不整包进内存**；文件按 256 KiB 分块读取；
  客户端中断时停止遍历/读取、线程退出，审计仍写一条并标记 `aborted=true`。
- **响应头**：`Content-Disposition: attachment; filename="<ascii 回退>.zip"; filename*=UTF-8''<percent-encoded>`，
  中文目录名走 RFC 5987（ASCII 回退为 `download.zip`）。
- **安全与上限**：`prefix` 解析（含符号链接）后必须仍在本 bucket 内（否则 `400`）；隐藏项一律跳过；
  单次 `files + dirs` 上限 `MAX_BULK_DOWNLOAD_ITEMS = 100000`，超限 `400`；失败请求不写审计、不计指标。
- **网页 UI**：目录浏览页面包屑卡片新增 **「下载本目录」** 按钮（`data-action="download-dir"`，只读、
  无确认弹窗），仅选中 bucket 后可用，bucket 根即下载整个 bucket；读取令牌框，空则提示先填写；
  用隐藏 `<a>` 触发浏览器原生下载（不把 ZIP 读进内存），toast 提示单文件直下 / 多文件或含子目录自动打包 ZIP。
- **审计与指标**：直下写 `download`（`extra` 含 `bulk=true`、`mode=file`）；ZIP 在流结束（含中断）写
  `download_bulk`（`key` 为规范化 prefix、`size` 为实际发送字节数、`extra` 含 `mode/files/dirs/aborted`）；
  `DOWNLOADS` 指标成功请求 +1（失败与 `304` 不计）。
- **测试**：新增 `tests/test_bulk_download.py`（13 个测试函数，覆盖契约 §2.4 的 11 项要求），覆盖单文件直下
  （Range/ETag）、流式 ZIP 结构与空目录、隐藏项/符号链接跳过、顶层命名、401/403/400/404 矩阵、审计与指标、
  项数上限、中断审计、UI 标记。
- **文档**：`docs/api_rules.md` 新增 §2.13（端点 / 鉴权 / 直下与 ZIP 规则 / ZIP 结构 / 流式 / 错误码 / 安全 /
  审计），并同步 §1.6、§7、§8、§10、§11.5；`docs/architecture.md` 补 API 契约、流式 ZIP 可靠性与安全要点；
  `README.md` 补核心特性、权限口径、API 参考、网页 UI 与测试清单。

#### Changed

- **权限模型未变**：一键下载复用 `object:download`，不新增权限点，`viewer` / `downloader` / `operator`
  等角色预设保持不变。

#### Fixed

- **`upload.session_ttl_seconds` 绝对有效期生效**：新增 `cleanup_expired_chunk_sessions()`，按
  `.session.json.created_at` 清理绝对过期会话；新建会话和后台任务同时执行“空闲 TTL + 绝对 TTL”清理。
- **OBJ-003/OBJ-005 修复**：直接上传目标 key 已是目录/父路径是文件、complete 目标 key
  已是目录时返回 `409`（不再 500）；complete 失败清理本次分片会话，并同步移除对应已知缺陷标记。
- **key 控制字符加固**：`validate_key()` 拒绝 CR/LF/TAB 等控制字符；下载响应头文件名做安全替换，
  防止日志污染/header 注入。
- **上传临时文件清理**：`finalize_chunk_upload` 在成功或进入合并/落盘阶段后的硬失败（I/O、磁盘满等）时
  删除本次 `/tmp/dw_chunks/<upload_id>` 与合并临时文件；**校验类失败（缺片、超限、权限、409）保留会话**，
  允许补齐/调整后重试或续传；`store_chunk` 复制失败清理 `.part.tmp`；直传/分片请求读取异常也会清理本次请求的
  临时文件；服务启动时清理所有遗留分片会话（容器/进程重启即释放历史残留，避免几十 GB 分片长期占盘）；
  后台任务每 10 分钟清理超过 upload.session_idle_ttl_seconds（默认 24h）没有分片写入的会话（浏览器被杀/断网无 abort 的兜底）。
  `abort` 路径保持原清理行为。
- **测试**：新增 `tests/test_upload_cleanup.py`（7 个用例：成功/落盘硬失败/缺片保留/`.part.tmp`
  失败清理/启动清理/lifespan 启动清理/空闲清理不误删活跃会话）。
- **文档**：`docs/api_rules.md` §3.3、`docs/architecture.md` 可靠性要点、`README.md` 上传特性同步说明。


### 分块并发下载（HEAD / Range）+ 共享令牌默认 admin

#### Added

- **服务端下载参数 `download.*`**：`config/__init__.py` 的 `DEFAULT["download"]` 内置
  `chunk_size_mb=8`、`chunk_concurrency=4`、`request_timeout_seconds=3600`（1h）；
  `get_download_settings()` 归一化并按合法区间**自动夹紧**（分片 1–64MB、并发 1–16、超时 60s–7d），
  `public_download_settings()` 以「字节 / 整数 / 秒」下发（不含任何密钥）；
  `GET /api/auth/permissions` 的 `data` 新增 `download` 段（与既有 `upload` 并列）；
  `config.json` / `config.production.json` / `.env.example` 同步补齐；`.env` 支持点号写法
  （`download.chunk_size_mb=8`），真实环境变量为 `ARTIFACT_DEPOT_DOWNLOAD_*`；
  每次调用都读当前配置，`POST /api/config/reload` 后**即时生效、无需重启**。
- **`HEAD /api/objects/download` 元信息探测**：返回 `Content-Length` / `Accept-Ranges: bytes` /
  `ETag` / `Content-Disposition`，响应体为空；路由以 `include_in_schema=False` 注册，
  **OpenAPI 操作数仍为 35、不新增权限点**（`GET /api/auth/permissions` 仍 22 条）；
  鉴权与 GET 相同（token / `Authorization: Bearer` / 签名链接），对象不存在或无效 token 分别 `404` / `401`。
- **Range 下载语义与审计口径**：单区间 `Range: bytes=a-b` 返回 `206 + Content-Range`（首段 / 中段 /
  尾段 / 后缀 `bytes=-n` 均支持），不可满足返回 `416`（带 `Content-Range: bytes */<total>`），多区间
  全部可满足时返回 `206 multipart/byteranges`（任一段越界则 `416`，不 500）；**每个 Range 请求写一条 `download` 审计**（并发下载会产生多条），
  `extra` 与记录根层合并后含 `partial=true`、`range=<请求头原文>`，`size` 为**实际区间字节数**；
  可解析的单区间计一次 `DOWNLOADS` 指标，多区间 / 不可解析区间不计。
- **桌面客户端分块并发下载**（`client/artifactdepot_upload.py`）：新增 `download_settings()`（读取
  `data.download`，异常回退 8MiB / 4 路 / 1h 并夹紧）与 `download_to(path, dest, progress, timeout,
  concurrency, cancel)`——先 `HEAD` 探测大小 / `Accept-Ranges` / `ETag` / 文件名，文件大于分片且服务端支持
  Range 时按区间**并行 GET**，预分配 `.part` 后各线程按偏移写入；单片失败最多重试 **3 次指数退避**
  （仅网络错误 / 超时 / 408 / 429 / 5xx；4xx 含 416 不重试，从已写入偏移续传不重复计数）；完成校验总大小，
  `ETag` 为 `"<64 位 hex>"` 时用 SHA-256 校验后再 `os.replace`；取消（`threading.Event`）会停止各分片、
  清理 `.part` 并抛 `Cancelled`；**签名链接（`link`/`tk`/`expires`/`sig`）与 `download-bulk` ZIP 强制单流**
  （避免消耗签名次数、ZIP 无 `Content-Length`）。GUI 动作栏新增「下载并发」1–16、「取消下载」，
  状态栏显示已下载字节 / 百分比 / 实时速度（≥150ms 合并刷新）。
- **网页端分块并发下载**（`resources/static/app.js`）：`loadUploadConfig()` 用同一个
  `GET /api/auth/permissions` 请求读取 `data.download`；文件行下载改为 `data-action="download-file"`
  `data-key="..."`（**token 只走 `Authorization` 头**，不再拼进 `href`）；点击手势内优先
  `showSaveFilePicker` + `createWritable` 边下边写盘（内存只保留并发数个分片），无该 API 时并发 Range →
  按序拼 `Blob` → `a.click()`；**>1GiB 或服务端忽略 Range（返回 200）时回退浏览器原生单流**；
  `#dlModal` 显示进度 / 速度 / 重试次数并支持「取消下载」（`AbortController` 中止全部在途请求）；
  每片最多 3 次尝试（首试 + 2 次重试）；`ETag` 为 `"<64 位 hex>"` 且文件 ≤256MiB 时用
  `crypto.subtle.digest('SHA-256')` 校验；服务端 `HEAD` 返回 405/501 时用 `GET bytes=0-0` 兜底探测。
- **测试**：新增 `tests/test_download_chunked.py`（18 个测试函数：默认 `admin`、`download.*` 下发/夹紧/
  点号写法/热重载、HEAD 头与审计、Range 206/416/多区间、并发多 Range、权限目录不新增 scope）、
  `tests/test_web_download_chunked.py`（14 个静态契约测试：配置读取、无 token-in-URL、HEAD 探测、并行分片、
  FS/Blob/原生回退、取消、SHA-256、进度弹窗、CSP 与语法）、
  `client/test_parallel_download.py`（10 个测试：真实 uvicorn 子进程 + 真实 HTTP，覆盖并行下载与 SHA-256、
  小文件/`concurrency=1` 单流回退、取消清理 `.part`、404、多 Range 审计、重试不重复计数、
  ETag 校验失败清理、目录 ZIP 单流）。
- **文档**：`README.md`、`docs/api_rules.md`（新增 §2.14「分块并发下载」并同步 §1.3 / §1.6 / §2.3 / §7 /
  §8 / §9 / §10 / §11.5 / §12.1）、`docs/architecture.md` 按实现补充分块下载流、可靠性与安全要点。

#### Changed

- **`access_token` 默认值改为 `admin`**（内置 `DEFAULT`、`config.json`、`config/.env`）：开箱即可用管理员
  共享令牌联调，无需先配置；`config.production.json` 同步补齐 `download` 段（模板仍建议部署时替换令牌）。
  **弱口令告警保留**：`config_warnings()` 仍把 `admin` 判为弱口令并告警（长度 <12），
  设 `ARTIFACT_DEPOT_STRICT_CONFIG=1` 时该告警会**直接拒绝启动**——生产/公网部署必须先改成
  ≥16 位随机串（`--token` / `ARTIFACT_DEPOT_ACCESS_TOKEN` / `config.json` / `.env`）。
- **权限模型与接口数量不变**：分块下载复用 `object:download`，`HEAD` 不进 OpenAPI schema，
  权限点仍 22 条、OpenAPI 操作仍 35 条，角色预设不变。
- **部署与文档**：镜像 tag / 导出 tar 文件名随版本变为 `0.9.0`（`build_image.conf`、
  `deploy_container.sh` 兜底同步）；`README.md` 与 `docs/` 补充分块下载的**可靠性**（并行切分、重试、
  取消清理、大小/SHA-256 校验、单流回退）、**安全**（`HEAD` 不新增权限点、签名链接按次数限制、
  默认 `admin` 必须替换）与**部署**说明。

#### Fixed

- **Range / HEAD 审计 `size` 口径**：HEAD 记 `head=true`、`size=0`；Range 记 `partial=true`、
  `range=<请求头原文>`、`size=实际区间字节数`（不再记整文件大小）；完整 GET 仍记整文件大小。
- **HEAD 不计 `DOWNLOADS` 指标**：探测请求只写审计、不计下载数；分块下载按「每个可解析 Range 请求 +1」
  计数，完整 GET 仍 +1；`304 Not Modified` 记 `not_modified=true`（HEAD 时另带 `head=true`）且不计指标。
- **超长 key 返回 400 而非 500**（业务验收发现）：`validate_key` 增加 ≤1024 字符上限；此前 5000 字符 key 会让
  下载接口在 `Path.is_file()` 处抛 `ENAMETOOLONG` → 500，现 HEAD/GET 下载与其余对象接口统一返回
  `400 {"detail": "key 过长..."}`；新增 `tests/test_download_chunked.py::test_oversized_key_rejected_not_500` 回归。

### 伴随文件本地化 + 上传暂停/恢复

#### Added

- **服务端伴随文件本地化（不再写 `/tmp`）**：分片会话在目标明确时落到
  `<depot>/<bucket>/<key 父目录>/.artifactdepot-uploads/<upload_id>/`（内部目录名常量
  `INTERNAL_UPLOAD_DIR`），内含 `.session.json`（`owner/created_at/updated_at/received_bytes/
  bucket/key/size/chunk_size/total_chunks/staging`）、`chunk_<i>.part`、请求体临时目录
  `.incoming/<rand>.tmp` 与合并临时文件 `.merged`；旧客户端（`initiate` 不带 `bucket/key`）
  仍新建 legacy 会话，但改落 `<staging>/<upload_id>/`。宿主 `/tmp` 常是小容量 tmpfs，
  72GB 级上传不再因临时空间不足失败。
- **配置 `upload.staging_dir`**（默认 `""`）：留空 = `<depot_dir>/.staging`；相对路径按
  `depot_dir` 解析（与 `meta_dir` 一致）；允许绝对路径（容器可挂独立数据卷）。用途：① legacy
  会话根目录；② 启动时把 `tempfile.tempdir` 指向该目录，让 Starlette multipart 解析的 **spool
  文件也落数据盘**（退出 lifespan 时恢复原值，测试安全）。启动日志打印位置，`config_warnings()`
  对不可写目录告警；`.env` 支持点号写法，环境变量 `ARTIFACT_DEPOT_UPLOAD_STAGING_DIR`；
  `config.json` / `config.production.json` / `.env.example` 同步补齐。`upload.*` 热重载即时生效，
  但 `staging_dir` 改动在 `config/reload` 的 `needs_restart` 中提示（框架级 spool 只在启动设置）。
- **`initiate` 续传能力（复用 `upload:initiate`，不新增权限点）**：请求同时支持 query 与 multipart
  form（允许空 body，向后兼容），参数 `bucket`/`key`（可选，提供时追加资源范围校验）、`size`
  （可选 int）、`resume`（默认 false）、`upload_id`（resume 时指定复用）。响应在保留原
  `public_upload_settings()` 全部字段的基础上新增 `resumed`、`uploaded`（磁盘上真实存在且
  `< total_chunks` 的分片索引）、`received_bytes`、`total_chunks`、`size`、`chunk_size`、
  `staging`（`"target"`/`"legacy"`），**不回显服务端绝对路径**。`resume=true` 且不传 `upload_id`
  时按 owner + bucket/key + size 在目标目录 `.artifactdepot-uploads` 中自动找回最新会话；找不到
  则新建（`resumed=false`）。会话失效 / 参数不一致返回 `400` 且 `detail` 含「分片会话」，
  客户端据此清状态重传；他人会话 `403`（admin 例外）。**不实现 `GET /upload/{id}`**（避免新增权限点）。
- **服务端清理与重启续传**：启动时先清理旧版 `/tmp/dw_chunks` 残留（趁 `tempfile.tempdir` 仍是
  系统临时目录），再删除 legacy staging 根下全部历史会话，并**扫描 depot 内
  `.artifactdepot-uploads`**：超绝对 TTL / 空闲 TTL 的删除，其余注册进进程内 registry
  （`{upload_id: Path}`）；会话在数据盘不丢，**服务重启后客户端仍可续传**。后台每 10 分钟只遍历
  registry + legacy staging 根（成本与仓库大小无关）；新建会话时同步清理。单进程假设
  （uvicorn 未开 `--workers`），磁盘（`.session.json` + 分片）才是唯一事实源。
- **内部目录保护**：`.artifactdepot-uploads` 为隐藏目录，列表 / 目录递归大小 / ZIP 打包本就跳过；
  **`bulk_delete(mode="all")` 与 `rename_object` 额外显式跳过**，避免清空/搬走活跃上传会话；
  会话删除后若内部目录变空则 `rmdir`（失败忽略）。客户端无法以隐藏路径段作为 key，故无法直接操作。
- **直传 / 分片 / 合并的伴随临时文件**：`POST /api/objects` 临时文件写目标父目录
  `.<name>.artifactdepot-incoming-<rand>.tmp`；`chunk` 请求体写会话 `.incoming/<rand>.tmp` 后
  `os.replace` 为分片（同文件系统零拷贝）；`complete` 合并到会话内 `.merged`，`put_object(move=True)`
  算完 SHA-256 后原子落盘（避免「临时 + 目标」2× 峰值，跨文件系统回退流式拷贝）。
- **桌面客户端断点续传与暂停/恢复**：上传断点状态写**源文件同目录**的
  `.<文件名>.artifactdepot-upload.json`（0600 原子写；成功 / 会话失效删除，失败 / 暂停 / 取消保留），
  源目录不可写时回退 `~/.artifactdepot/upload-state/<sha1(server|bucket|key)>.json` 并明确提示；
  **移除全局 `~/.artifactdepot_upload_state.json`**（读旧文件做一次兼容迁移后删除），多文件并发各自
  独立状态互不覆盖。上传始终 `initiate(bucket/key/size)`，续传时带 `resume=true`（有状态带
  `upload_id`，无状态按 bucket/key/size 找回），**以服务端 `uploaded` 为唯一事实源**；`upload_file()`
  新增 `pause_event`（默认 None，向后兼容），分片边界检查、已传分片不重传；失败**不 abort**，
  异常上抛并保留会话与状态，GUI 标记「失败（可续传）」；CLI 失败/中断提示
  「已保留续传状态，重跑同一命令将从断点继续」（`--no-resume` 保持）。
- **桌面客户端下载断点续传与暂停/恢复**：并发分片下载每完成一片即更新
  `.<目标名>.artifactdepot-download.json`（`size`/`etag`/`chunk_size`/`done`，0600）；暂停在分片
  边界生效、保留 `.part` + 状态，恢复时校验 `size`/ETag 后只补缺失分片（ETag 不一致丢弃重下）；
  失败保留两者（可续传），取消仍删除两者。GUI 新增「暂停上传」「继续上传」「暂停下载」「继续下载」按钮。
- **网页端暂停/续传**：上传队列 `initiate` 始终带 `bucket/key/size/resume/upload_id`，每项记录
  `upload_id/chunk_size/uploaded`；新增 `#upPause` / `#upResume` 按钮（`data-action="pause-upload"` /
  `"resume-upload"`），失败行显示**「失败（可续传）」**并提供 `data-action="retry-upload"` 的「继续」
  （事件委托、严格 CSP、无内联脚本）；失败**不再 `abortUpload`**，继续时 `initiate(resume=true)` 取
  `uploaded` 后只补缺失分片；页面刷新后重新选择同一文件再点上传可按 `bucket/key/size` 找回会话
  （浏览器无法自动恢复文件列表，界面已说明）；token 变更 / 文件大小或 mtime 变化时自动新建会话。
- **测试**：新增 `tests/test_upload_resume.py`（12 个用例：目标目录布局、legacy staging、spool 指向与
  还原、按 `upload_id` / 自动找回续传、`chunk` 落点与 `complete` 原子清理、直传临时文件、内部目录
  保护、重启续传、TTL 清理、无 `/tmp` 残留、initiate 参数校验）、`tests/test_upload_resume_ui.py`
  （14 个静态契约用例：按钮 id/`data-action`、`resume` 参数、「失败（可续传）」文案、不 abort、
  分片边界暂停、只补缺失分片、CSP 无内联脚本、JS 语法）、`client/test_upload_resume.py`
  （11 个用例：真实 uvicorn 子进程 + 真实 HTTP，覆盖状态文件位置/0600/成功删除、暂停→继续只补缺失分片、
  失败后续传 SHA-256 一致、多文件独立状态、下载暂停/续传、源目录不可写回退、旧全局状态迁移）。
  同步更新 `tests/test_upload_cleanup.py`（8 个用例）与 `client/test_parallel_download.py`（13 个用例）
  适配新布局；全量 `pytest tests -q` 322 passed / 5 xfailed。
- **文档**：`README.md`（核心特性、`upload.staging_dir` 配置、`initiate` 参数/响应/续传语义、FAQ、
  目录与磁盘布局、客户端行为）、`docs/api_rules.md`（§3.1–§3.5 initiate/续传/会话布局/清理、§1.6 错误码、
  §8 速查表、§9 配置表、§10 安全）、`docs/architecture.md`（伴随文件布局、续传状态机、重启续传、
  单进程 registry、spool 落 staging）、`client/README.md`（状态文件位置/回退、暂停/恢复、失败续传、CLI、测试）

#### Changed

- **上传临时文件路径**：`/tmp/dw_chunks/<upload_id>` → 目标父目录 `.artifactdepot-uploads/<upload_id>/`
  （legacy 客户端 → `<staging>/<upload_id>/`）；合并临时文件、请求体临时文件、直传临时文件同步迁移。
  旧版 `/tmp/dw_chunks` 仅在启动时清理一次，不再新建。
- **启动清理语义**：`cleanup_all_chunk_sessions()` 只清 legacy staging 根下的历史会话；目标目录会话
  改为按 TTL 清理并注册（`scan_upload_sessions()`），以支持**跨服务重启续传**（此前启动即全删）。
- **后台清理成本**：从「遍历整个 depot」改为「只遍历进程内 registry + legacy staging 根」，
  与仓库文件数无关；启动时的一次性全树扫描负责重建 registry。
- **桌面客户端状态文件位置**：由全局 `~/.artifactdepot_upload_state.json` 改为跟随源文件的伴随文件
  （可写时）或 `~/.artifactdepot/upload-state/`（回退），0600 原子写，支持多文件并发。
- **客户端续传事实源**：本地状态只作缓存，服务端 `initiate(resumed/uploaded)` 为唯一事实源；
  `complete` 失败不再一律丢弃状态，会话未失效时保留以便重试。
- **权限模型与接口数量不变**：续传复用 `upload:initiate`，**不新增权限点**（权限点仍 22 条、
  OpenAPI 操作仍 35 条、角色预设不变，**旧 token 无需变更**）；`initiate` 的响应字段只增不改。

#### Fixed

- **`initiate` 父路径是文件**：`bucket/key` 的父路径不可创建目录时返回 `409`
  （`父路径不是目录，无法创建上传会话`），不再 500 / 不落盘；直传同场景同样 `409`（0.9.0 起）。
- **直传失败零副作用**：读取/超限/落盘任一阶段失败都会删除目标目录下的临时文件，并回滚为写临时文件
  而新建的空父目录；`put_object(move=True)` 的失败不再残留源临时文件。
- **`chunk` 临时文件清理**：`.incoming` 下本次请求的临时文件在读取失败 / 配额失败 / 落盘失败时删除，
  空 `.incoming` 顺手 `rmdir`，不积累残留。
- **`complete` 清理口径**：成功或进入合并 / 落盘阶段后的硬失败（磁盘满、I/O 异常）清理会话与
  `.merged`；校验类失败（缺片、`total_chunks` 越界、超限、权限、`overwrite=false` 的 409）
  **保留会话**，可补齐分片后重试；目标本身是目录的 409 属不可恢复冲突，仍清理。
- **会话归属校验**：`resume` / `chunk` / `complete` / `abort` 均校验 owner（admin 例外），
  历史无 owner 的会话按「无主」放行（向后兼容）。
- **旧版全局续传状态迁移**：仅当旧状态与当前文件/目标完全匹配时迁移并删除，不匹配的保留给其所属
  上传自行处理；损坏的旧文件直接清理。

---

## [0.8.0] - 2026-10-21

### 令牌资源范围硬化（bucket/前缀硬边界）

#### Added

- **Token 管理页 bucket 多选**：「Bucket 白名单」升级为「现有 bucket 复选框（含『全部 bucket』选项）+ 手填」，
  进入 Token 页时自动调用 `GET /api/buckets` 渲染，提交时合并勾选与手填（去重、去空）；
  新增「刷新现有 bucket」按钮，仍支持手填尚未创建的 bucket。
- **生成前前端校验**：`allow_buckets` 与 `allow_prefixes` 同时为空时提示「该 token 可访问全部 bucket」
  并要求二次确认；`allow_prefixes` 里出现不在 `allow_buckets` 中的 bucket 时直接阻止提交并说明原因
  （避免「写了前缀却不生效」）。
- **生效范围展示**：生成结果区展示「生效范围：bucket=…；前缀=…」；Token 注册表「资源范围」列空范围时显示
  「未限制（全部 bucket）」；「显示明文」下的权限明细行也展示「生效范围」。
- 文档：`docs/api_rules.md` §1.4（资源范围为硬边界）、§2.2 / §4.2（带 token 的读接口鉴权与越界 `403`）、
  §5 / §12.4（防提权）、§6（审计范围过滤）、§9（补 `require_read_token` / `metrics_enabled` 配置项）。

#### Changed

- **行为变更（读接口 fail-closed）**：携带**无效 / 过期 / 禁用** token 调用 `GET /api/objects/list`、
  `GET /api/buckets` 由「静默降级为匿名并返回 `200`」改为 **`401`**；携带有效 token 但缺
  `object:list` / `bucket:list`、或 bucket / 前缀**越界**，由 `200` 改为 **`403`**。
  **不带 token 时行为不变**：仍按 `require_read_token` 保持公开（`false`）或 `401`（`true`）。
- **审计查询范围过滤**：非共享 token 的 `GET /api/audit` 只返回其 `allow_buckets` / `allow_prefixes`
  范围内的记录（过滤在收集阶段完成，`limit` 语义不变）。
- **Token 注册表 UI**：「资源范围」列改由统一格式化函数渲染，空范围醒目显示「未限制（全部 bucket）」。

#### Security

- **资源范围成为强制边界**：此前 `allow_buckets` / `allow_prefixes` 在默认公开模式下可被绕过
  （越界 token 按匿名放行、返回范围外内容）；硬化后越界一律 `403`，令牌不再能读取自身范围外的 bucket。
- **防提权**：非共享 token 通过 `POST /api/tokens`、`PUT /api/tokens/{value}` 创建 / 更新 token 时，
  `scopes` 必须是自身子集，`allow_buckets` / `allow_prefixes` 不得超出自身范围；`PUT` **同时校验
  目标 token 的现有授权**（不能改写比自身更大的 token，含把它降级为自身范围）与更新后的最终授权，
  否则 `403`。受限 token 把范围清空 / 放宽为「全部 bucket」同样被拒绝；共享 `access_token` 不受限。
  另：非数组 `allow_prefixes`（如 `{"demo": 123}`）由 `400` 兜底，不再出现未捕获 `TypeError` 的 `500`。
- **审计越权收口**：配置了资源范围的 `audit:read` token 只能看到**归属于其范围内 bucket** 的记录；
  `bucket` 为空的全局事件（`token_create` / `token_update` / `token_delete` / `config_reload`）
  会泄露其他 token 的 `user`/`role`/`scopes`，对受限 token 一律不返回。未配置范围的 token 与共享
  `access_token` 行为不变。
- **`PUT /api/tokens/{value}` 补审计**：此前该接口不写审计（`POST`/`DELETE` 都有），现追加 `token_update`
  （含 `user`/`role`/`scopes`/`allow_buckets`/`allow_prefixes`/`enabled`/`expires_at`）。
- **`POST /api/tokens` 覆写既有 token 也受范围约束**：显式传 `token` 且该 token 已存在时，等同于修改既有 token，
  先校验目标现有授权不超出操作者范围，否则 `403`。此前可借覆写把更大的 token 降级、破坏其他 bucket 的集成，
  甚至把**已禁用 / 已过期的 token 覆写复活**（`enabled=True`、`expires_at` 清空）——与 `PUT`/`DELETE`/legacy 三条路径不一致。
- **路径前缀规范化**：`allow_prefixes` 统一走 `storage.normalize_prefixes()`（URL-decode、去首尾斜杠、去重），
  并拒绝空段 / `.` / `..` / 反斜杠 / `null` / normalize 后为空（如 `"/"`），一律 `400`；
  `"/"` 此前会被静默丢弃、从「只允许根」变成「不限前缀」。
- **资源范围自洽校验**：填了 `allow_prefixes` 却把 `allow_buckets` 留空（或前缀所属 bucket 不在白名单内）时返回 `400`。
  此前这种写法只收窄被点名的 bucket，**其余 bucket 仍完全开放**，与「只给某目录」的意图相反（网页 UI 早已阻止，API 此前未拦）。
- **`POST /api/tokens` 覆写既有 token 改为合并语义**（业务验收 BIZ-TOK-001/002）：显式传入的 `token` 若已存在，
  未传字段一律保留。此前按「全量重建」处理，会静默把 `allow_buckets ["demo"]` 清成 `[]`（资源范围边界失效）、
  把 `expires_at` 清成永久、把已禁用 token 复活、并把审计误记为 `token_create`；现在统一走 `PUT` 的合并语义
  与同一套防提权校验，审计记 `token_update`。
- **审计 `rename` 目标路径按范围裁剪**（业务验收 OBS-02）：前缀受限 token 若能看到一条 `rename` 记录
  （源在其范围内），其 `to_key` 可能指向范围外。现在目标越界的 `rename` 记录整行不返回。
- **bucket 名规范化错配修复**：授权层此前对 `bucket` 做 `strip()` 后比较、落盘却按原样取目录，
  使 `bucket="demo "` 可被按 `demo` 授权、却读取 `demo ` 的数据（跨 bucket 越权）。
  现在 `resource_allowed()` 按原样精确比较，且 `validate_bucket()` 直接拒绝首尾空白（`400`）。

---

#### Docs

- **`docs/api_rules.md` 按实现逐条校对**：修正 19 处与实现不符的描述（`public_ip` 被忽略、`ETag` 回退规则、错误码表补 `503` 与「未配置 access_token → 401」、`auth/check` 成功/失败字段差异、`config/reload` 失败为 `400`、Token 注册表返回结构、§8 总表补全并统一 `{link_id}`、`user` 角色补签名链接权限等），并登记 4 个已知缺陷（`OBJ-001/002/003~005/006`）。
- 版本号统一提升到 `0.8.0`（`VERSION` / `main.py` 兜底 / `build_image.conf` / `deploy_container.sh` / `README` / `docs`）。
## [Unreleased]

### 上传参数配置化（config.json）

#### Added

- **`upload.*` 配置组**：`chunk_size_mb`（默认 8）、`chunk_concurrency`（默认 4）、
  `file_concurrency`（默认 3）、`request_timeout_seconds`（默认 **86400 = 24h**）、
  `max_chunk_mb`（默认 64）、`max_chunks`（默认 100000）、`max_session_gb`（默认 200）、
  `session_ttl_seconds`（默认 86400）、`session_idle_ttl_seconds`（默认 86400，且不小于请求超时）。
  支持 `config.json`、`.env` 点号写法（`upload.chunk_size_mb=8`）与
  `ARTIFACT_DEPOT_UPLOAD_*` 环境变量；`POST /api/config/reload` 后即时生效。
- **服务端下发配置**：`GET /api/auth/permissions` 与 `POST /api/objects/initiate`
  返回 `chunk_size` / `chunk_concurrency` / `file_concurrency` /
  `request_timeout_seconds` / `max_chunk_size` 等，网页和 CLI 以服务端返回值为准。
- **网页**：加载时读取 `upload.*`；分片大小、分片并发、文件并发默认值、
  分片/合并请求超时（默认 24h）均按配置执行。
- **CLI**：`initiate` 读取服务端 `request_timeout_seconds`，分片与 complete
  超时默认 24h，不再写死 600s / 7200s。
- **测试**：新增 `tests/test_upload_config.py`（默认值、自定义值、上限、热重载、
  `.env` 点号覆盖、前端读取标记）。

#### Changed

- 单分片上限/会话上限校验改为「配置值 + 代码常量兜底」：配置只能调小，
  不会被网页/CLI 放大到不安全范围（分片 ≤64MB、并发 ≤16、会话 ≤200GB）。

### 许可证署名工程化（无接口变更）

#### Added

- **源文件 SPDX 署名头**：33 个第一方源文件（25 个 `.py` + 5 个 `.sh` +
  `app.js` + `app.css` + `Dockerfile`）加上双行头
  `Copyright 2026 apyyc` / `SPDX-License-Identifier: Apache-2.0`，呼应
  Apache-2.0 第 4(d) 条「在源文件中保留版权与署名声明」，使单个文件被拷出仓库时
  仍可追溯版权人与许可证。
- **`scripts/add_spdx_headers.py`**：幂等补头工具。按
  `git ls-files --cached --others --exclude-standard` 自动发现第一方源文件，
  按扩展名选择注释符号，保持 shebang 在第 1 行、Python 模块 docstring 仍是首条语句；
  支持 `--dry-run` 与 `--check`（缺头退出码 1，可接 CI / pre-commit）。

#### Changed

- `LICENSE-HEADER` 由「13 行官方样板的套用说明」改写为本项目实际执行的 SPDX 约定
  （各语言注释写法、位置规则、JSON/.conf/vendored 豁免清单），官方长样板降级为文末
  可选项；同时**移入 `docs/LICENSE-HEADER` 并加入 `.gitignore`**（不随 git 分发，
  对外以根目录 `LICENSE` 与 README「许可证与支持」为准）。路由键 `license_header` 与
  `GET /api/docs/license_header` 不变，仅展示标题改为「源文件署名约定」。
- `Dockerfile` 第 42 行的 `COPY` 去掉根目录 `LICENSE-HEADER`：改由上一行的
  `COPY docs/` 一并带入镜像。纯净 clone（无此文件）构建时不再中断，接口按既有
  逻辑降级为 `available=false` / `404` 可读原因。
- README「许可证与支持」与目录结构、`LICENSE.zh-CN.md` 附录指引、`docs/api_rules.md`
  的 `name` 白名单说明同步为 SPDX 写法，并标明该约定文档不进 git。

## [0.7.4] - 2026-10-16

### 部署与挂载重构（无接口变更）

#### Changed

- **挂载框架重做：三卷分离，去掉版本隔离**。此前数据目录名含镜像版本
  （`<部署根>/artifactdepot_<版本>/warehouse`），升级后默认指向新（空）目录，用户必须每次手动
  `--data-dir` 指回旧数据。现改为固定三卷，升级镜像原地沿用：
  - `<部署根>/config/` → `/data/config`：配置目录（`config.json` + `.env`），首次运行用
    `config.production.json` 播种
  - `<部署根>/data/` → `/data/depot`：业务数据
  - `<部署根>/state/` → `/data/state`：状态文件（`tokens.json` / `signed_links.json` / `audit.log`）
  新增 `--deploy-root`（或环境变量 `DEPLOY_ROOT`）与 `--data-dir` / `--state-dir` / `--config-dir`；
  `--config <文件>` 只挂单个 `config.json`。容器内以 `ARTIFACT_DEPOT_DIR` /
  `ARTIFACT_DEPOT_META_DIR` / `ARTIFACT_DEPOT_CONFIG` / `ARTIFACT_DEPOT_ENV` 告知应用实际路径。
- `config.production.json` 的 `meta_dir` 由 `/data/depot/state` 改为 `/data/state`，与状态卷一致；
  README 中 `meta_dir` 生产建议同步更新。
- Dockerfile：`VOLUME` 增列 `/data/state`、`/data/config`，并为其建目录与授权。
- README 新增「配置读取顺序」与「三个 config 文件的区别」两节，明确四层覆盖规则、
  `.env` 空值语义、相对路径按项目根解析等易踩坑点。
- `check_ad_sync.sh` 改为优先读取容器内实际生效的 `ARTIFACT_DEPOT_CONFIG`，
  回退镜像内路径（此前硬编码，挂载外部配置后读不到真实配置）。

#### Fixed

- `deploy_container.sh` 修复 `--deploy-root` 不生效（三卷在参数解析前派生）、
  `META_DIR_CONTAINER` 未初始化导致 `set -u` 中断、`ARTIFACT_DEPOT_META_DIR` 被注入两次
  三个问题。
- 全部 shell 脚本去除 emoji，统一为 `OK:` / `WARN:` / `ERROR:` / `INFO:` 文本前缀
  （deploy 58 处、dev_start 18 处、build_image 13 处、check_ad_sync 8 处）。

### 工程化与发布整理（本版为工程化收尾，无接口变更）

#### Added

- **版本唯一源**：新增项目根 `VERSION` 文件，`build_image.sh`（`--tag` > `VERSION` > `DEFAULT_TAG`）、
  `deploy_container.sh`（可用 `IMAGE_TAG` 覆盖）、`main.py`（FastAPI `version`）、
  Dockerfile（`ARG APP_VERSION` → OCI 版本标签）均从该文件读取，版本号不再散落多处。
- **开源许可**：项目以 Apache License 2.0 授权（SPDX `Apache-2.0`）。新增 `LICENSE`（英文版）、
  `LICENSE.zh-CN.md`（中文版）、`LICENSE-HEADER`（源文件署名模板）；许可证经
  `GET /api/docs/license`、`GET /api/docs/license_zh`、`GET /api/docs/license_header` 公开只读提供，
  满足 Apache-2.0 第 4(a) 条「向接收者提供许可证副本」。
- **构建代理可配置**：`build_image.conf` 支持 `PROXY_MODE=keep|strip|set` 与 `PROXY_URL`；
  构建日志中的代理账密自动打码为 `***:***@`。
- `deploy_container.sh` 支持环境变量 `VOLUME_MAPS`（空格分隔的 `宿主机路径:容器内路径`）追加挂载。

#### Changed

- **部署脚本加固**：token 改经 600 权限临时 env-file 注入（不再出现在 `podman/docker` 命令行，
  `ps` 不可见，容器创建后即删）；容器内配置挂载点从镜像 `WorkingDir` 推导，去掉与 Dockerfile 的隐式耦合；
  附加卷解析改为「取最后一个冒号 + 完整校验」，兼容宿主机路径含冒号的少见情况。
- 移除 `scripts/start.sh` 与 `dev_common.sh`；`dev_start_headless.sh` 改为源码模式直启 uvicorn
  （`PYTHONPATH=src python3 -m uvicorn artifactdepot.main:app`）。
- `.dockerignore` 修正 `test/` → `tests/`（该规则此前从未生效），并补充 `warehouse/`、
  `.pytest_cache/`、`COPYRIGHT-DECLARATION.md`；构建上下文由约 97 MB 降至约 3 MB。

#### Fixed

- **业务数据脱离版本控制**：`warehouse/` 下的视频等产物曾误入git（历史中约 51 MiB），
  已移出索引并加入 `.gitignore` / `.dockerignore`；仓库历史经 `git filter-repo` 重写后
  `.git` 由 55 MB 降至约 1.1 MB。
- `deploy_container.sh` 的 `--token` 缺少「下一个参数不能是选项」校验，误写 `--token --stop`
  会把字面量当作令牌注入；现与其他参数一致直接报错退出。
- 修复构建脚本中 `die` 落在子 shell（`$(...)`）导致非法配置仅打印错误、构建照常继续的问题；
  版本与代理配置校验移至主 shell 执行。
- `config.json` 的 `access_token` 恢复为空（此前被误填弱值 `"y"`）。

#### Removed

- 移除 `.github/workflows/ci.yml`。

---


### Added

- **开源许可证**：项目以 Apache License 2.0 授权（SPDX `Apache-2.0`）。
  新增 `LICENSE`（英文原文）、`LICENSE.zh-CN.md`（中文版）、`LICENSE-HEADER`（源文件署名模板）；
  README「许可证与支持」章节同步更新；许可证通过 `GET /api/docs/license`、`GET /api/docs/license_zh`
  公开只读提供，满足 Apache-2.0 第 4(a) 条「向接收者提供许可证副本」。
- **版本唯一源**：新增项目根 `VERSION` 文件，版本号不再散落各处。
  `build_image.sh`（`--tag` > `VERSION` > `DEFAULT_TAG`）、`deploy_container.sh`（可用 `IMAGE_TAG` 覆盖）、
  `main.py`（FastAPI `version`）、Dockerfile（`ARG APP_VERSION` → OCI 版本标签）均从该文件读取。

### Changed

- **代理配置**：`build_image.conf` 新增 `PROXY_MODE=set` 与 `PROXY_URL`，可把代理地址（含账密）写在配置里；
  构建日志中的账密自动打码为 `***:***@`。
- 移除 `scripts/start.sh`（本地启动改由 `dev_start_headless.sh` 直接以源码模式启动）；
  `dev_common.sh` 一并移除。

### Fixed

- **业务数据脱离版本控制**：`warehouse/` 下的视频等产物曾误入 git（历史中约 51 MiB），
  现已移出索引并加入 `.gitignore` / `.dockerignore`。
- `.dockerignore` 中 `test/` 更正为 `tests/`（该规则从未生效），并补充 `warehouse/`、
  `.pytest_cache/`、`COPYRIGHT-DECLARATION.md`。
- 修复构建脚本中 `die` 落在子 shell 导致非法配置仍继续构建的问题（版本与代理配置校验移至主 shell 执行）。

### Tests

- 新增 `tests/test_acceptance_batch4.py`（32 例）与 `tests/conftest.py`（隔离真实 `.env`）：
  对第 4 批 + 配置目录重构做**接口级业务验收**，覆盖 `/health/live`、`/health/ready`、
  `/metrics`、`/api/config/reload`、`/api/objects/{head,download,list}`、`/`、`/static/app.js|css`、
  `/docs`、`/redoc`；含 ETag/304、Range、分页边界、CSP、权限/越权、路径穿越、
  跨接口联动与数据一致性。

---

## [0.7.3] - 2026-10-08

### 第 4 批（P2 体验与可观测性）

> 第 4 批：ETag 条件请求、可观测性、UI 分页、CSP。

### Added

- **ETag / 条件请求**：`GET /api/objects/head`、`GET /api/objects/download` 返回强 ETag（SHA-256）；
  请求头 `If-None-Match` 命中返回 `304`，不再传输文件体。
- **可观测性**（`observability.py`，无第三方依赖）：`X-Request-ID` 透传/生成、JSON 结构化访问日志、
  `/metrics`（Prometheus 文本：HTTP 计数/耗时直方图、上传/下载/删除计数、token 数、磁盘用量）、
  `/health/live`（存活）、`/health/ready`（depot 可写，否则 503）；新增 `metrics_enabled` 配置。
- **网页 UI 分页**：文件列表默认 200 条/页，底部「加载更多」，配合后端 `limit/offset`。

### Security

- **CSP**：`index.html` 的内联 `<style>`/`<script>` 抽到 `/static/app.css`、`/static/app.js`，
  所有内联事件处理器改为事件委托，`javascript:void(0)` 改为 `#`；应用页 `Content-Security-Policy`
  为 `script-src 'self'`（无 `unsafe-inline`），`/docs`、`/redoc` 单独放行 `unsafe-inline`。

### Tests

- 新增 `tests/test_csp.py`（4 例）、`tests/test_observability.py`（5 例）；
  `tests/test_api_consistency.py` 增加 UI 分页与 ETag 用例。

---

### 配置目录重构

### Changed

- **配置集中到 `src/artifactdepot/config/` 包**：原 `config.py` → `config/__init__.py`；
  原 `resources/config.json` → `config/config.json`；原根目录 `config.production.json` →
  `config/config.production.json`。删除旧路径 `resources/config.json`，所有脚本/文档硬切新路径
  （`scripts/start.sh`、`deploy_container.sh` 的 `CONTAINER_CONFIG_PATH`、`check_ad_sync.sh`、Dockerfile、README/docs 已同步）。
- **新增 `config/.env`**（字段名=值，如 `access_token=...`）承载隐私配置；优先级
  `内置默认 < config.json < .env < 真实环境变量`；`.env` 空值视为未设置；`.env` 不进 git/镜像，
  提供 `.env.example` 模板；可用 `ARTIFACT_DEPOT_ENV` 指定外部 `.env`。
- **手动重载配置**：新增 `POST /api/config/reload`（仅管理员）与网页「Token 管理」页的
  「重新加载配置」按钮；返回 `changed`（已生效）/ `needs_restart`（需重启：`host`/`port`/`depot_dir`/
  `meta_dir`/`cors_origins`/`allowed_hosts`）/ `warnings`；语法错误时保留旧配置并提示。
- **默认 `config.json` 进镜像**（不含密钥）；`.env` 仍排除；`.dockerignore` 同步调整。

### Tests

- 新增 `tests/test_config_reload.py`（6 例）：`.env` 优先级/点号嵌套、真实环境变量最高、
  重载生效/需重启分类、坏 JSON 保留旧配置、`POST /api/config/reload` 仅管理员。

---

## [0.7.2] - 2026-10-08

### 第 1 批（P0 安全加固）

> 第 1 批安全加固（P0）：修复路径穿越、存储型 XSS、弱默认 token、token 进访问日志。

### Security

- **修复分片上传 `upload_id` 路径穿越（严重）**：`storage.py` 此前直接用客户端传入的 `upload_id`
  拼接会话路径，`upload_id="/"` / `"../x"` 可让 `abort` 对任意目录执行 `shutil.rmtree`。
  现严格校验 `^[A-Za-z0-9_-]{16,64}$` 并二次确认解析结果落在会话根内；同时给会话绑定创建者
  （`chunk`/`complete`/`abort` 校验归属），并限制 `total_chunks` 上限。
- **修复网页 UI 存储型 XSS**：文件名/key 等不可信数据此前被 `esc()` 后拼进内联
  `onclick="fn('...')"`，`&#39;` 会被 HTML 解码回 `'` 从而注入 JS。现改为 `data-*` 属性 +
  事件委托，彻底移除内联 `onclick` 拼接。
- **清除弱默认 token**：`resources/config.json` / `config.production.json` / 内置默认的
  `access_token` 由 `"y"` / `"change-me"` 改为空（= 无管理员）；`.dockerignore` 排除
  `**/config.json`，避免把运行配置烤进镜像。启动时 `config_warnings()` 对空/弱 token 告警，
  `ARTIFACT_DEPOT_STRICT_CONFIG=1` 可直接拒绝启动。
- **token 不再走 URL query（前端）**：网页 UI 所有 API 调用改用 `Authorization: Bearer`，
  下载直链加 `referrerpolicy="no-referrer"`；新增 `logging_utils` 对 uvicorn access log 的
  `token`/`tk`/`sig` 等 query 参数脱敏。

### Fixed

- `access_token` 为空时，鉴权请求由 500 改为 401/403（fail-closed），不再抛
  “服务端未配置 access_token”。

### Tests

- 新增 `tests/test_security_hardening.py`（8 例）：路径穿越、会话归属、空 token、
  UI XSS 静态检查、日志脱敏、Bearer 头鉴权。

---

### 第 2 批（P1 可靠性）

> 第 2 批：状态文件原子写+并发锁、分片配额、审计 IP 可信化、容器非 root、依赖锁定。

### Security

- **审计 `public_ip` 不再采信客户端**：改为仅当直连方命中 `config.trusted_proxies`（IP/CIDR）时，
  才从 `X-Forwarded-For` 解析；否则留空。前端移除第三方 `api.ipify.org`/`api.seeip.org` 探测。
- **容器非 root 运行**：`Dockerfile` 创建 `artifactdepot(10001)` 并 `USER artifactdepot`；
  `supervisord.conf` 移除 `user=root`；`deploy_container.sh` 绑定卷时以宿主 UID/GID 运行，
  rootless podman 加 `--userns=keep-id`，保证卷可写。
- **依赖锁定**：新增 `requirements.txt`（fastapi/uvicorn/httpx/python-multipart 精确版本），
  `Dockerfile` 改为 `pip install -r`，构建可复现。

### Changed

- **状态文件原子写 + 并发锁**：`tokens.json` / `signed_links.json` / `.warehouse.json` 改为
  临时文件 + `fsync` + `os.replace` 原子替换；`storage.state_lock()`（进程内 `threading.RLock`
  + 跨进程 `fcntl.flock`）串行化读-改-写，修复并发丢更新、半截 JSON、按次数签名链接超用。
- **分片上传配额**：单分片 ≤64MB；会话总量受 `max_upload_mb` 限制，未配置时 200GB 兜底；
  `store_chunk` 在锁内校验并维护 `received_bytes`（覆盖同 index 不重复计数）。
- **路径包含判断**：`bucket_dir` / `object_path` / `list_objects` / `rename_object` 由
  `str.startswith` 改为 `Path.is_relative_to`，避免前缀碰撞 / symlink 边界绕过。
- `_write_session_meta` 改为原子写；`audit.log` 以 0600 创建；`tokens.json`/`signed_links.json` 权限 0600。

### Tests

- 新增 `tests/test_reliability_hardening.py`（8 例）：并发 add_token / 并发消费按次数链接、
  状态文件 0600、单分片/会话配额、覆盖不重复计数、`trusted_proxies` XFF 解析、客户端 `public_ip` 被忽略。

---

### 第 3 批（P2/P3 体验与工程化）

> 第 3 批：API 一致性、列表分页、审计轮转、UI 时长修正、构建脚本乱码、自托管文档、CI。

### Changed

- **`GET /api/objects/head` 对齐 S3**：不存在返回 `404`（此前 200 + `exists:false`）；`mtime` 统一为 UTC ISO8601（此前为 epoch 整数）。
- **`GET /api/objects/list` 支持分页**：新增 `limit` / `offset`，返回 `total` / `truncated`；`limit=0` 保持全量，向后兼容。
- **目录递归统计**改用 `os.walk` 剪枝隐藏目录，不再把 `.git` 等隐藏目录内容算进大小/时间；文件 `stat` 竞态跳过而不是 500。
- **审计日志轮转**：新增 `audit_max_mb`（默认 50）/ `audit_backups`（默认 5）；`query_audit` 读取轮转文件并用 `deque` 限制内存。
- **网页 UI 签名链接时长**不再硬编码「一小时」，按 `created_at`/`expires` 显示实际时效（秒/分钟/小时/天）。
- **`_mask_token` 加短哈希后缀**，避免不同 token 脱敏后同名导致返回字典覆盖。
- **Swagger / ReDoc 静态资源自托管**（`/static/swagger-ui-bundle.js`、`redoc.standalone.js`），内网/离线不再白屏。
- **新增 CORS / TrustedHost / 安全响应头**：`cors_origins`（默认空=不启用）、`allowed_hosts`（默认 `["*"]`）；响应加 `X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`/`Permissions-Policy`。

### Fixed

- **`build_image.sh` 全文件乱码修复**：此前 355 处中文/emoji 为双重编码（UTF-8→Latin-1），现全部恢复为正确中文，`--help` 输出正常。
- 删除与当前项目无关的残留脚本 `rsync.sh`（指向旧 `data_warehouse` 路径）。

### Added

- **CI**：`.github/workflows/ci.yml`，push/PR 跑 `pytest` + 全部 shell `bash -n`。

### Tests

- 新增 `tests/test_api_consistency.py`（5 例）：head 404/ISO、list 分页、隐藏目录统计、审计轮转、`_mask_token` 唯一性。

---

## [0.7.1] - 2026-10-03

> 本次为**前端文档入口 + 语义一致性修复**发布：网页 UI 标签栏新增 Swagger / ReDoc 入口；
> 同时修复两处「文档描述与实现不一致」的问题（均在 `docs/api_rules.md` 中已有约定）。

### Added

- **网页 UI 文档入口**：顶部标签栏右侧新增 **Swagger**（`/docs`）/ **ReDoc**（`/redoc`）链接
  （`src/artifactdepot/resources/index.html`），由 FastAPI 依据实际路由生成，可直接在线调试。
- **文档 API**（`src/artifactdepot/api/docs.py`，公开只读；用于查看/抓取原始 Markdown）：
  - `GET /api/docs` 文档清单（`name` / `title` / `available`）；
  - `GET /api/docs/{name}` 文档内容（统一响应结构）；
  - `GET /api/docs/{name}?raw=1` 直接返回 `text/plain` 原文（供新窗口查看）。
  - `name` 走白名单（`api_rules` / `architecture` / `readme` / `changelog`），不接受任意路径，避免目录穿越。
- **镜像包含文档**：`Dockerfile` 复制 `docs/` 与 `README.md` / `CHANGELOG.md`，`.dockerignore` 不再排除它们，
  使容器部署同样能使用前端文档入口。
- **回归测试** `tests/test_docs_and_scopes.py`（3 个用例）：文档接口、空 scopes 语义、读接口资源范围越界。

### Fixed

- **`scopes: []` 现在真正表示「无任何权限」**：`_normalize_token_record` 此前把空数组当作「字段缺失」回退成角色预设（`storage.py`），
  导致按文档 `12.4`（`空数组 = 无权限`）生成的 token 反而拿到角色全量权限；改为仅在字段缺失/非数组时回退预设。
- **读接口资源范围越界不再泄露数据**：`require_read_token=true` 时，`GET /api/objects/list` 对
  bucket/前缀白名单之外的目标此前按匿名放行并返回真实列表（可越过范围约束读取任意 bucket）；
  现改为返回 `403`（`api/objects.py`）。公开模式（`require_read_token=false`）行为不变，仍按匿名语义不阻断。

### Changed

- 版本号统一为 `0.7.1`（`main.py` / `build_image.conf` / `Dockerfile` / `deploy_container.sh` / 文档）。
- `docs/api_rules.md`：新增「7.1 文档接口」；「八、接口速查总表」改为**权限点（scope）**列并补全 `PUT /api/tokens/{value}`、
  `/api/docs`；修正各接口「写 token / 仅管理员」等旧表述为对应权限点；补全 `GET /api/buckets`、`/api/auth/check` 的返回结构说明。

## [0.7.0] - 2026-10-02

### Added

- **权限点（scope）模型**：每个受保护接口对应一个权限点，新增 `permissions.py` 权限目录与角色预设（`viewer` / `downloader` / `uploader` / `publisher` / `operator` / `auditor` / `user` / `custom` / `admin`）。
- **自定义 Token 生成**：`POST /api/tokens` 支持 `role` + `scopes`（逐接口勾选）+ `allow_buckets` + `allow_prefixes` + `expires_at` + `description`；`token` 留空由服务端生成，响应一次性返回明文。
- **权限目录接口**：`GET /api/auth/permissions` 返回全部接口、权限点、角色预设，供前端渲染「逐接口勾选」；`GET /api/auth/check` 返回 `role/scopes/allow_buckets/expires_at`。
- **资源范围约束**：token 可限定 bucket 白名单与 bucket 内路径前缀白名单（带路径边界判断，`a/b` 不匹配 `a/bc`）。
- **Token 更新接口**：`PUT /api/tokens/{value}` 可改权限/范围/过期/禁用；`GET /api/tokens?reveal=true` 管理员可看明文（默认脱敏）。
- **Token 管理页改造**：角色预设下拉、逐接口勾选（按功能组）、bucket/前缀/过期/说明输入、「生成本轮 Token」按钮与一次性明文展示；列表展示角色/权限点数/范围/过期/状态。
- **配置项 `require_read_token`**（默认 false）：为 true 时读接口 `GET /api/buckets`、`GET /api/objects/list` 也要求 `bucket:list` / `object:list`，实现全量最小权限。
- **审计**：新增 `token_create` / `token_delete` 动作记录。

### Changed

- **`tokens.json` 升级为 v2 结构**（`{"_version":2,"tokens":{...}}`）；旧字符串格式自动兼容为 `role=user`，行为与历史一致（上传/下载/签名/建目录/建 bucket/改名，无删除与管理）。
- **`GET /api/objects/head` 由“写 token”改为 `object:head` 读权限**。
- **删除接口**由“仅管理员”改为 `object:delete` 权限点，可按需授予运营 token（管理员仍可）。
- **权限依赖重构**：`require_write_token` / `require_admin` 保留兼容，新增 `require_scope(...)` 与端点内 `ensure_scope(...)`；下载、上传、分片、签名、rename 等均做资源范围校验。
- **DataHub 同步语义加固**：同步只更新用户身份，**不覆盖**已有的 role/scopes/资源范围/过期。
- 平台旧协议 `POST /api/tokens {token,user}`（不带权限字段）只更新用户名，保留已有权限，避免把精细权限重置。

### Security

- 非管理员 `GET /api/tokens` 不再以明文 token 作为返回 key（改为脱敏 key + 脱敏展示）。
- 带资源范围的 token 列 bucket / 列表 / 签名链接时按范围过滤。

### Docs

- `docs/api_rules.md` 新增第十二章「权限点与自定义 Token 生成」；`README.md` 更新鉴权表与配置项。

### Backward Compatibility

- 共享 `access_token` 行为不变（超管）。
- 旧 token 无需迁移，继续可用（等价 `role=user`）。
- 公开读接口默认行为不变；如需收紧设 `require_read_token=true`。


## [0.6.1] - 2026-09-28

### Changed

- **项目更名 DataWarehouse → ArtifactDepot**：Python 包 `datawarehouse` → `artifactdepot`、镜像/容器名、Dockerfile LABEL、脚本与文档全面更新；API 路径与响应结构不变（对外契约保持兼容）。
- **配置键更名**：`warehouse_dir` → `depot_dir`（新键优先，旧键兼容读取）；环境变量前缀 `WAREHOUSE_*` → `ARTIFACT_DEPOT_*`（新前缀优先，旧前缀兼容）。
- **数据目录自动迁移**：启动时检测旧 `warehouse/` 目录存在且新 `depot/` 不存在，自动改名迁移（不移动文件内容，同文件系统瞬间完成）。
- `/health` 返回键 `warehouse_dir` → `depot_dir`。

## [0.5.5] - 2026-09-26

### Changed

- 版本号统一为 `0.5.5`（镜像标签、应用版本、文档与部署脚本）。

### Fixed

- **Podman 构建保留 HEALTHCHECK**：`build_image.sh` 在 Podman 下自动追加 `--format docker`，避免默认 OCI 格式忽略 `HEALTHCHECK`。
- **`deploy_container.sh` 真正兼容 Docker**：镜像检查改用 Podman/Docker 都支持的 `image inspect`；Docker 下不使用 `podman generate systemd`，自动改用 `--restart=always`。
- **`meta_dir` 语义修复**：部署时默认不再注入 `ARTIFACT_DEPOT_META_DIR`，以容器内 `config.json` 为准；新增 `--meta-dir` 供显式覆盖。
- **默认数据目录可回退**：`~/SERVER` 不可写时自动回退到 `${XDG_DATA_HOME:-~/.local/share}/artifactdepot/...`，避免开箱即用失败。
- **默认配置路径统一**：`deploy_container.sh` 自动探测 `~/SERVER/artifactdepot/config/config.json`，新增 `--config` 显式挂载；修正数据卷/配置路径文档。
- `supervisord` 配置消除 root 与 `redirect_stderr` 警告。
- 新增 `.dockerignore`，构建上下文排除 `*.tar`、`depot/`、`logs/`、`__pycache__` 等无关大文件。
- 补齐 `config.production.json` 与生产诊断脚本 `check_dw_sync.sh`。

## [0.5.4] - 2026-09-26

### Added

- API 文档补充统一响应例外、422 校验错误、`public_ip` 可选参数、DataHub 同步响应字段与列表边界行为。
- 下载接口与 `/api/auth/check` 支持 `Authorization: Bearer <token>`。

### Changed

- 管理接口权限语义修正：无 token / 无效 token 返回 401；已认证普通用户调用管理接口返回 403。
- 对象 key 的隐藏路径段规则收紧：任意路径段以 `.` 开头均拒绝上传，与 `.depot.json` / `.keep` 隐藏约定保持一致。
- 文档与构建/部署脚本版本号统一为 0.5.4。

### Fixed

- 签名链接下载先检查对象是否存在，再扣减次数，避免对象不存在时仍消费链接次数。
- 手工放入、无 `.depot.json` 记录的文件列表 `mtime` 回退为文件系统 mtime。
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

- **`meta_dir` 配置项**：把状态文件（`tokens.json` / `signed_links.json` / `audit.log`）从仓库根移出到独立目录（留空 = 原行为放 `depot_dir` 根下；相对路径按 `depot_dir` 解析）。新增环境变量 `ARTIFACT_DEPOT_META_DIR` 覆盖
- **`build_image.sh` 构建完自动导出 tar**：默认 `./build_image.sh` 即构建 + 导出 `artifactdepot-0.5.0.tar`；`--no-save` 跳过、`--save [路径]` 指定、`--tag` 覆盖版本标签、`--load` / `--list` 保留
- **`config.production.json`**：生产可整份复制的配置模板（`depot_dir=/data/depot`、`meta_dir=/data/depot/state`、host 网络 `datahub_url=127.0.0.1:8002`）
- **`check_dw_sync.sh`**：生产连通性诊断脚本（只依赖 python3，无需 curl/wget/jq）—— 校验容器内配置 / 应用实际同步 / 容器内访问 DataHub / 宿主对照 / 端口监听 / 挂载列表，逐段定位 "All connection attempts failed"

### Changed

- **`deploy_container.sh` 默认 host 网络**：`DO_HOST_NETWORK=true`，容器共享宿主网络不再映射端口（同机连 DataHub `127.0.0.1:8002` 最稳，规避 rootless pasta 网桥连不通宿主发布端口）；`--port-map` 才改用端口映射
- **`deploy_container.sh` 不再默认注入 `datahub_url`**：只有显式 `--datahub-url` / 环境变量 `ARTIFACT_DEPOT_DATAHUB_URL` 才注入，否则以容器内配置文件的 `datahub_url` 为准（修复：旧脚本默认把 127.0.0.1 注入容器，导致同步指向自身）
- **`Dockerfile` 移除烤死的 `ARTIFACT_DEPOT_DATAHUB_URL=127.0.0.1` ENV**：`config.py` 里环境变量优先级高于配置文件，镜像内预设默认值会把挂载 config.json 的 `datahub_url` 盖掉（正是生产 "All connection attempts failed" 的根因之一）。`datahub_url` 以容器内配置文件字段为准
- **`deploy_container.sh` 新增 `VOLUME_MAPS`**：脚本顶部列表，每项 `宿主机:容器内` 追加挂载；典型用途把改好的 config.json 挂进容器（配置显式可改、无需重建镜像）
- 默认数据目录随版本：`~/SERVER/artifactdepot/artifactdepot_0.5.0`

### Fixed

- **生产 token 同步失败（"All connection attempts failed"）全链路**：
  1. 部署脚本默认注入 `ARTIFACT_DEPOT_DATAHUB_URL=127.0.0.1`（指向容器自身），改为默认不再注入
  2. Dockerfile `ENV` 烤死 `ARTIFACT_DEPOT_DATAHUB_URL` 盖掉挂载配置，移除该 ENV（需重建镜像生效）
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
  - 新增 `Dockerfile`（python:3.12-alpine + supervisor + 非 root + `VOLUME /data/depot` + 健康检查）
  - 新增 `docker/supervisord.conf`（supervisor 托管 uvicorn 8004）
  - 新增 `deploy_container.sh`（参照 collab：建容器 + systemd 开机自启；支持 `--token` / `--datahub-url` / `--data-dir` / `--no-systemd` / `--stop` / `--rm`）
  - 新增 `dev_start_headless.sh`（本地后台启动 + status/tail/logs/stop）
  - `config.py` 支持**环境变量覆盖**：`ARTIFACT_DEPOT_DIR` / `ARTIFACT_DEPOT_DATAHUB_URL` / `ARTIFACT_DEPOT_ACCESS_TOKEN` / `ARTIFACT_DEPOT_PORT` / `ARTIFACT_DEPOT_MAX_UPLOAD_MB`
  - Dockerfile 安装 `python-multipart`（上传必需）

- **网页 UI 增强**
  - bucket 列表**可点击选中**（蓝色胶囊 + 选中高亮），页面加载自动列出
  - API Token 输入框移到顶部 bucket 区域（上传 / 签名链接共用）
  - 文件列表卡片移到「上传文件」下方

- **文档**
  - README 重写为完整规范文档（核心特性 / 架构与工作流 / 环境要求 / 安装 / 快速开始 / 配置详解 / 目录结构 / 核心模块 / API 参考 / 规范 / 常见问题 / 许可）

### Changed

- 构建命令统一为 `env -u HTTP_PROXY ... podman build --network=host -t localhost/artifactdepot:0.4.0 .`

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
  - 新增 `<depot_dir>/tokens.json`，记录每个用户 api_token 对应的用户名
  - 管理接口（需管理员共享 token）：`GET/POST/DELETE /api/tokens`
  - 平台在注册/查 Token 时自动推送登记；网页「Token 管理」标签可查看/添加/移除

- **写操作操作者识别（actor）**
  - 上传/删除/预签名时解析令牌：共享 token 识别为「系统/工具」；注册表命中识别为用户名；否则 401 拒绝
  - 修复此前只校验共享 token、无法区分「谁上传」的问题

- **审计日志（谁在何时做了什么）**
  - 追加写 `<depot_dir>/audit.log`（JSONL，一行一条，只增不改）
  - 记录：time / action(upload/delete/download/presign) / bucket / key / actor / ip / size / sha256
  - 下载记 actor（平台模块带身份）或 anonymous + 来源 IP；预签名记生成者
  - `GET /api/audit`（需管理员 token）：按 bucket/key/actor/since 过滤查询
  - 网页「审计」标签可筛选查看

### Changed

- 上传接口 token 校验从「仅管理员共享 token」改为「管理员 token 或已登记用户 token」

## [0.1.0] - 2026-08-06

### Added

- **初始版本：对象存储仓库站点（S3 模型）**
  - 项目骨架：`src/artifactdepot/`（FastAPI），配置、存储引擎、认证、API、网页 UI 分层
  - 存储布局：`<depot_dir>/<bucket>/<key>`，bucket=项目，key=任务/文件；每 bucket 一个隐藏元数据清单 `.depot.json`（size/sha256/mtime/source_url）
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
