# ArtifactDepot API 文档

ArtifactDepot 是对外提供对象/文件仓库服务的 HTTP API，默认监听 **8004** 端口：

```
http://<主机IP>:8004
```

除文件下载、`/health*`、`/metrics`、`/docs`、`/redoc`、`/static/*` 与 `GET /api/docs/{name}?raw=1` 外，接口返回统一 JSON：

```json
{ "code": 0, "message": "success", "data": {} }
```

- 成功时 HTTP 通常为 `200`，`code` 为 `0`。
- 失败时 HTTP 状态码表示错误类别（见 1.4），响应体为 `{"detail": "原因"}`。

---

## 一、通用约定

### 1.1 鉴权

支持三类身份：

| 身份 | 来源 | 说明 |
|---|---|---|
| 管理员/工具 | 配置 `access_token`（共享令牌，默认 `admin`） | 放行全部权限 |
| 用户 | `tokens.json` 注册表（本地维护） | 由 `role` + `scopes` 决定权限，可限定 bucket/路径范围与过期时间 |
| 签名链接 | `link`+`tk` 或 `expires`+`sig` | 仅用于下载，免 token |

token 传递方式：

1. 请求头（推荐）：`Authorization: Bearer <token>`
2. 查询参数：`?token=<token>`
3. multipart 接口（上传/分片/合并/取消）额外支持表单字段 `token`；`initiate` 的 token 只认 query 或请求头。

校验规则：管理员 token 用常量时间比较；用户 token 查注册表。读接口默认公开；带 token 时必须是有效 token，且权限与资源范围必须匹配。

### 1.2 权限与资源范围

每个受保护接口对应一个权限点（`scope`）。用户 token 携带 `scopes`，可选：

- `allow_buckets`：允许访问的 bucket 列表；空表示全部。
- `allow_prefixes`：`{"bucket": ["前缀/"]}`，在 bucket 内进一步限制路径前缀。

规则：

- 不带 token：`require_read_token=false` 时公开；`true` 时 `401`。
- 带 token 但无效/过期/禁用：`401`。
- 缺少所需 scope：`403`。
- bucket/前缀超出 token 范围：`403`。
- 非共享 token 创建/更新 token 时，授予的 scope 与资源范围不得超出自身，否则 `403`。
- 签名链接只用于下载；`link:create` 决定谁可生成。

### 1.3 bucket 与 key

**bucket**：非空、长度 ≤63；不能以 `.` 开头，不能含 `/`、`\`、控制字符；首尾不允许空白；允许 Unicode；首次上传或 `mkdir` 时自动创建。

**key**：相对路径；前后 `/` 会被去除；不允许空段、`.`、`..`、`\`、CR/LF 等控制字符；任意路径段不能以 `.` 开头；长度 ≤1024。对象操作要求 key 非空；列表的 `prefix` 可为空。

### 1.4 错误码

| 状态码 | 含义 |
|---|---|
| `400` | 参数非法（bucket/key、prefix、配置值、token 长度、签名链接上下限、`rename` 的 new_key、批量删除确认等） |
| `401` | 缺少/无效/过期/禁用令牌；签名无效或过期 |
| `403` | 权限不足；资源范围越界；越权修改 token；非创建者作废链接；非管理员修改 `access_token` |
| `404` | 对象/目录/token/链接/文档不存在；一键下载空目录 |
| `409` | 目标已存在且 `overwrite=false`；上传/分片目标类型冲突 |
| `413` | 超过单文件上传上限或单分片上限 |
| `416` | Range 不可满足 |
| `422` | 请求参数校验失败（字段缺失/类型错误），`detail` 为数组 |
| `500` | 服务端异常 |
| `503` | `/health/ready` 就绪检查失败 |

---

## 二、对象接口

### 2.1 上传对象

```
POST /api/objects
```

**鉴权**：`object:upload`，并校验 bucket/key 范围。

**请求**（`multipart/form-data`）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | 文件内容 |
| `bucket` | 是 | 目标 bucket |
| `key` | 是 | 对象 key |
| `token` | 否 | 也可用 query 或 `Authorization` |
| `source_url` | 否 | 来源，允许 URL 或自由文本；超 512 字符截断 |
| `note` | 否 | 备注；超 200 字符截断 |
| `overwrite` | 否 | 默认 `true`；为 `false` 且已存在时 `409` |
| `public_ip` | 否 | 兼容占位，值被忽略 |

**响应**：

```json
{ "code": 0, "message": "success", "data": { "bucket": "b", "key": "a/b.txt", "size": 123, "sha256": "..." } }
```

规则：目标 key 是目录、或父路径不是目录时 `409`；超过 `max_upload_mb` 时 `413`。

### 2.2 列出对象/目录

```
GET /api/objects/list
```

**鉴权**：默认公开（`require_read_token=true` 时需 `object:list`）；带 token 时必须有效并具备 `object:list`，且 bucket/prefix 在范围内。

**查询参数**：

| 参数 | 必填 | 说明 |
|---|---|---|
| `bucket` | 是 | 目标 bucket |
| `prefix` | 否 | 目录前缀 |
| `limit` | 否 | 默认 `0`（不限）；`>0` 时分页，最大 10000 |
| `offset` | 否 | 分页偏移，默认 `0`，仅 `limit>0` 时生效 |

**响应** `data`：

```json
{ "bucket": "b", "prefix": "", "items": [], "total": 0, "offset": 0, "limit": 0, "truncated": false }
```

`items` 元素：

- 目录：`{name, key（以 / 结尾）, is_dir: true, size, mtime}`。
- 文件：`{name, key, is_dir: false, size, mtime, sha256, source_url, note, uploader}`。

说明：目录 `mtime` 为该目录下可见文件的最新 mtime，空目录为目录自身 mtime；手工放入、无清单记录的文件 `sha256/source_url/note/uploader` 为空。bucket/prefix 不存在时返回 200 空列表，`total=0`。

### 2.3 下载对象

```
GET  /api/objects/download
HEAD /api/objects/download
```

**鉴权**（按优先级）：

1. `link` + `tk`：注册表签名链接，免 token。
2. `expires` + `sig`：旧式 HMAC 签名，免 token。
3. `token`：需 `object:download` 且 bucket/key 在范围内。

**查询参数**：`bucket`、`key` 必填；`link`、`tk`、`expires`、`sig`、`token` 可选。`public_ip` 已忽略。

**响应**：文件流，`Content-Type` 按扩展名推断；支持：

- 单区间 `Range` → `206` + `Content-Range`；不可满足 → `416`；多区间全部可满足 → `206 multipart/byteranges`，任一段越界 → `416`。
- `ETag`：有清单记录为 SHA-256 强 ETag，否则为 `"size-mtime"`；`If-None-Match` 命中 → `304`。
- `HEAD` 返回 `Content-Length`、`Accept-Ranges: bytes`、`ETag`、`Content-Disposition`，无响应体；对象不存在 `404`。

### 2.4 删除对象/目录

```
DELETE /api/objects
```

**鉴权**：`object:delete`，并校验范围。

**查询参数**：`bucket`、`key` 必填。

**响应**：`data = {"deleted": "bucket/key"}`。

规则：目录仅空可删（不隐藏内容）；含非隐藏文件/子目录时 `400`；对象/目录不存在 `404`。

### 2.5 新建目录

```
POST /api/objects/mkdir
```

**鉴权**：`object:mkdir`，并校验范围。

**请求体**：

```json
{ "bucket": "b", "key": "a/b" }
```

**响应**：`data = {"bucket": "b", "key": "a/b/"}`（末尾带 `/`，幂等）。

说明：多级目录自动创建，并放入隐藏 `.keep` 占位。若 key 或父路径已是文件，当前返回 `500`。

### 2.6 重命名/移动

```
POST /api/objects/rename
```

**鉴权**：`object:rename`；源与目标 key 都校验范围。

**请求体**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `bucket` | 是 | bucket |
| `key` | 是 | 现有对象/目录 key |
| `new_key` | 是 | 新 key；可修改目录前缀实现移动 |
| `overwrite` | 否 | 目标已存在时是否覆盖，默认 `false` |

**响应**：`data = {"bucket", "from_key", "to_key", "moved_objects"}`。

规则：`new_key` 与 `key` 相同、或位于 `key` 子目录 → `400`；源不存在 `404`；目标冲突且未覆盖 → `409`。目录迁移为整体移动；业务冲突会回滚，底层 I/O 异常可能留下部分移动。文件/目录类型冲突当前可能返回 `500`。

### 2.7 元信息探测

```
GET /api/objects/head
```

**鉴权**：`object:head`，并校验范围。

**查询参数**：`bucket`、`key`。

**响应**：`data = {exists, is_dir, bucket, key, size, mtime, sha256?}`。文件含 `sha256`；目录 `key` 末尾带 `/` 且不含 `sha256`。存在时返回 `200` 并带 `ETag`，`If-None-Match` 命中 `304`；不存在 `404`。

### 2.8 生成签名下载链接

```
POST /api/objects/presign
```

**鉴权**：`link:create`，并校验范围。

**请求体**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `bucket` | 是 | bucket |
| `key` | 是 | 对象 key |
| `mode` | 否 | `count` / `time`（默认）/ `permanent` |
| `count` | 否 | `mode=count` 的次数，受 `config.signed_links.count_min/max`（默认 1–10）限制 |
| `expires` | 否 | `mode=time` 的秒数，默认 3600，受 `config.signed_links.expire_min/max`（默认 60–604800）限制 |

**响应**：

```json
{ "url": "/api/objects/download?bucket=..&key=..&link=<id>&tk=<token>", "id": "<id>",
  "mode": "time", "max_uses": null, "remaining": null, "expires": 1730000000 }
```

`max_uses`/`remaining` 仅 `count` 模式有值；`expires` 仅 `time` 模式为时间戳。生成时校验参数，不强制对象已存在。key 含 `&`、`#`、`+` 时返回的 `url` 未编码，建议自行编码或使用显式参数。

### 2.9 签名链接配置、列表与作废

```
GET  /api/objects/signed-links/config            # 公开
GET  /api/objects/signed-links                   # link:list
POST /api/objects/signed-links/{link_id}/revoke  # link:revoke
```

- 配置：返回 `count_min/count_max/expire_min_seconds/expire_max_seconds`。
- 列表：带范围 token 只返回范围内链接；完整 URL 仅管理员/创建者可见；`token` 字段不返回。
- 作废：管理员可作废任意，用户只能作废自己创建的；`link_id` 不存在 → `404`。

### 2.10 批量删除目录内容

```
POST /api/objects/bulk-delete
```

**鉴权**：`object:delete_bulk`，并校验 bucket/prefix 范围。

**请求体**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `bucket` | 是 | bucket |
| `prefix` | 否 | 目录；空或 `/` 表示 bucket 根；不能指向隐藏目录 |
| `mode` | 是 | `files`（只删直接文件）/ `all`（递归删文件与子目录） |
| `dry_run` | 否 | `true` 只预览，不删除、不写审计；默认 `false` |
| `confirm` | 执行时是 | 必须等于目录名；根目录等于 bucket 名；前后空白会 strip |

**响应**：`data = {bucket, prefix, mode, dry_run, files, dirs, bytes, errors, elapsed_ms}`。

规则：

- 只清理目录内容，目录本身保留；bucket 根 `.warehouse.json` 永不删除。
- `files` 模式跳过隐藏项、子目录、符号链接；`all` 模式递归删除（隐藏项也删），不跟随符号链接，并跳过 `.artifactdepot-uploads`。
- `prefix` 不存在或不是目录、bucket 不存在 → `404`；路径穿越/隐藏目录 → `400`。
- 一次最多 50000 项（`files+dirs`），超出 `400`；`dry_run` 同样受限。
- `errors` 最多 20 条；部分失败仍返回 `200`。
- 执行写审计 `delete_bulk`；`dry_run` 不写。

### 2.11 一键下载目录

```
GET /api/objects/download-bulk?bucket=<bucket>&prefix=<prefix>
```

**鉴权**：`object:download`，并校验范围；不支持签名链接。

**规则**：

- 目录中只有 1 个可见文件且无可见子目录 → 直接返回该文件（支持 Range/ETag）。
- 其他非空情况 → 流式 ZIP（`ZIP_STORED`、无 `Content-Length`、不落临时文件）；ZIP 顶层文件夹名为 `prefix` 最后一段，bucket 根为 bucket 名。
- 隐藏项（`.` 开头，含 `.keep`、`.warehouse.json`）与符号链接跳过，不跟随。
- 空目录、不存在或不是目录 → `404`；`prefix` 解析后越出 bucket → `400`。
- 单次 `files+dirs` 上限 100000，超出 `400`。
- 失败请求不写审计、不计下载指标；ZIP 不支持 Range。

### 2.12 分块并发下载

服务端不新增分块端点，仍使用 `GET /api/objects/download`：客户端先 `HEAD` 探测大小、`Accept-Ranges`、`ETag`，再按 `Range` 并行请求；每片校验 `206` 与 `Content-Range`。

参数从 `GET /api/auth/permissions` 的 `data.download` 读取：

```json
{ "chunk_size": 8388608, "chunk_concurrency": 4, "request_timeout_seconds": 3600 }
```

来源于 `download.*` 配置，服务端自动夹紧到分片 1–64MB、并发 1–16、超时 60s–7d。签名链接按次数计费，每个 Range 请求都会消耗次数，建议分块下载使用 token。

---

## 三、分片上传

流程：`initiate` → `chunk`（多次）→ `complete`；取消用 `abort`。

### 3.1 发起/恢复上传会话

```
POST /api/objects/initiate
```

**鉴权**：`upload:initiate`；提供 bucket/key 时额外校验范围。

**参数**（query 或 multipart form；允许空 body，同名时 form 优先）：

| 参数 | 必填 | 说明 |
|---|---|---|
| `bucket` | 否 | 与 `key` 同时提供或都不提供 |
| `key` | 否 | 目标 key |
| `size` | 否 | 文件总字节数；给出时计算 `total_chunks` |
| `resume` | 否 | 是否复用会话，默认 `false`（接受 `1/true/yes/on`） |
| `upload_id` | 否 | `resume=true` 时指定会话 |

**响应**：

```json
{ "upload_id": "...", "chunk_size": 8388608, "chunk_concurrency": 4,
  "file_concurrency": 3, "request_timeout_seconds": 86400,
  "max_chunk_size": 67108864, "max_chunks": 100000,
  "max_session_gb": 200, "max_upload_mb": 0, "resumed": true,
  "uploaded": [0,1], "received_bytes": 16777216, "total_chunks": 9,
  "size": 75497472, "staging": "target" }
```

续传规则：

- `resume=true` + `upload_id`：校验存在、owner、bucket/key/size 一致；失效/不一致 → `400`（`detail` 含「分片会话」）；他人会话 → `403`（admin 例外）。
- `resume=true` 不带 `upload_id` 但带 bucket/key/size：按 owner + bucket/key/size 找最新会话；找不到则新建。
- `resume=false`：总是新建。
- 不带 bucket/key：创建 legacy 会话（旧客户端兼容）。

**错误**：bucket/key 只给一个、`size` 非整数/负数、`upload_id` 非法、会话失效 → `400`；范围外/他人会话 → `403`；目标父路径是文件 → `409`。

### 3.2 上传分片

```
POST /api/objects/chunk
```

**鉴权**：`upload:chunk`。

**请求**（multipart）：`upload_id`、`index`、`chunk`。响应：`data={upload_id,index,size}`。

单分片上限 `upload.max_chunk_mb`（默认 64MB），超出 `413`。

### 3.3 合并分片

```
POST /api/objects/complete
```

**鉴权**：`upload:complete`，并校验 bucket/key 范围。

**请求**（multipart）：`upload_id`、`bucket`、`key`、`total_chunks`；可选 `source_url`、`note`、`overwrite`。

**响应**：`data={bucket,key,size,sha256}`。

规则：分片缺片、`total_chunks` 越界、超出 `max_upload_mb`、`overwrite=false` 冲突会保留会话；目标 key 是目录属不可恢复冲突（`409`），会清理会话；成功后会话删除。

### 3.4 取消上传

```
POST /api/objects/abort
```

**鉴权**：`upload:abort`。**请求**：表单 `upload_id`。

**响应**：`data={aborted,existed}`；会话不存在时 `existed=false`（幂等）。

### 3.5 未完成会话与清理

```
GET /api/objects/uploads
```

网页内部接口，`include_in_schema=false`；复用 `upload:abort` 权限。只返回当前 token 身份创建的会话：

```json
{ "items": [{ "upload_id": "...", "bucket": "b", "key": "a/f.bin",
  "name": "f.bin", "size": 100, "received_bytes": 50, "percent": 50,
  "uploaded_chunks": [0], "total_chunks": 2, "chunk_size": 8388608,
  "created_at": "...", "updated_at": "...", "idle_seconds": 12 }], "count": 1 }
```

会话保存在服务端，进程重启后仍可续传；超过 `upload.session_ttl_seconds`（绝对）或 `upload.session_idle_ttl_seconds`（空闲）会被清理，后台按 `upload.session_cleanup_interval_seconds` 周期执行。`idle_seconds` 是服务端时钟下「最后分片写入距今秒数」（无法解析时为 `-1`）：网页据此把页面刷新/浏览器断开后已无传输的断点显示为「已中断（未在传输）」，并给出「最后活动」时间；`idle_seconds <= 15` 显示为「最近有分片写入」。断点会话不占用前端上传队列，因此没有「暂停上传」，只能「继续上传」（重新选择同一文件）或「取消上传」。

---

## 四、系统接口

### 4.1 健康检查

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/health` | `{"status":"ok","depot_dir":"...","exists":true}` |
| GET | `/health/live` | `{"status":"alive"}` |
| GET | `/health/ready` | `{"status":"ready","depot_dir":"..."}`；depot 不可写 → `503` |
| GET | `/metrics` | Prometheus 文本；`metrics_enabled=false` → `404` |

均公开。

### 4.2 重新加载配置

```
POST /api/config/reload
```

**鉴权**：管理员共享 token，或具备 `config:write` 的 token。

**响应**：`data={ok,error,changed,needs_restart,warnings}`。`changed` 已生效；`needs_restart` 需重启。配置文件语法错误等返回 `400`，旧配置保留。

### 4.3 读取/修改配置

```
GET  /api/config
POST /api/config   {"values": {"upload.chunk_size_mb": 16, "token.max_length": 20}}
```

**鉴权**：管理员共享 token，或具备 `config:write` 的 token（读取与写入共用该权限点）。两个接口均 `include_in_schema=false`。

**GET 响应**：

- `path`/`exists`/`error`：配置文件路径与状态；
- `groups`/`schema`：分组与字段元数据（`key/label/group/type/default/min/max/restart/description`）；
- `values`：`config.json` 文件值；`effective`：当前生效值；
- `sources`：字段来源 `config.json`/`.env`/`environment`/`default`；
- `overridden`：被 `.env` 或环境变量覆盖的字段；`restart_required`：需重启字段；
- `warnings`：配置告警；`access_token` 等密钥字段返回 `********`。

**POST 规则**：

- 只接受 `schema` 内字段，未知字段、`null`、类型错误、越界 → `400`；
- `token.min_length` 不能大于 `token.max_length`；
- 原子写回 `config.json`（`0600`）并热加载；失败自动回滚；
- 配置文件缺失时以当前生效配置为基线；
- `.env`/环境变量覆盖项会写入文件但不生效，响应 `overridden` 列出；
- `access_token` 留空或提交 `********` 表示保持原值；仅管理员共享 token 可提交新值；`config:write` 用户 token 提交新值 → `403`；
- 进程内串行化写入，避免并发覆盖；
- 响应：`data={ok,changed,needs_restart,warnings,overridden,path}`。

### 4.4 bucket

```
GET  /api/buckets   # 默认公开；带 token 时需 bucket:list，按 allow_buckets 过滤
POST /api/buckets   # bucket:create
```

`GET` 返回 bucket 字符串数组。

`POST` 请求体 `{"bucket":"name"}`；幂等，响应 `data={"bucket","created":true|false}`。

### 4.5 校验 token

```
GET /api/auth/check?token=<token>
```

公开。成功：

```json
{ "valid": true, "actor": "用户或系统/工具", "role": "viewer", "is_admin": false,
  "scopes": [], "allow_buckets": [], "allow_prefixes": {}, "expires_at": "", "description": "" }
```

无效/过期/禁用：`valid=false`，另带 `expired`、`enabled`。

---

## 五、Token 管理

以下接口分别需要 `token:read` / `token:write` / `token:delete`；管理员共享 token 放行全部。token 通过 query 或 `Authorization` 传递。

### 5.1 查看注册表

```
GET /api/tokens?reveal=true
```

**鉴权**：`token:read`。

响应 `data` 为对象：键为脱敏 token，值为记录：

```json
{ "user": "alice", "role": "viewer", "scopes": [], "allow_buckets": [],
  "allow_prefixes": {}, "enabled": true, "expires_at": "", "expired": false,
  "description": "", "created_by": "", "created_at": "", "last_used_at": "",
  "token": "<脱敏值>" }
```

脱敏规则：显示前 2 位 + `****` + 后 4 位；长度 ≤6 时仅前 2 位 + `****`。只有管理员共享 token 且 `reveal=true` 才返回明文；其他 token 始终脱敏。前 2+后 4 相同的不同 token 追加短哈希兜底。

### 5.2 创建/更新 token

```
POST /api/tokens
PUT  /api/tokens/{value}
```

**鉴权**：`token:write`。

**POST 请求体**：

| 字段 | 说明 |
|---|---|
| `user` | 必填，操作者名 |
| `token` | 可选；留空按 `token.max_length` 生成 |
| `role` | 角色预设，默认 `user` |
| `scopes` | 权限点数组；显式 `[]` 表示无权限 |
| `allow_buckets` | bucket 白名单；空表示全部 |
| `allow_prefixes` | `{"bucket": ["前缀/"]}`；必须同时出现在 `allow_buckets` 中 |
| `expires_at` | `YYYY-MM-DD` 或 ISO 时间；空表示永久 |
| `description` | 备注 |

**PUT 请求体**（字段均可选）：`user`、`role`、`scopes`、`allow_buckets`、`allow_prefixes`、`expires_at`、`description`、`enabled`。未传字段保留；传 `role` 而未传 `scopes` 时按角色预设重置。

规则：

- 新 token 长度须在 `token.min_length`–`token.max_length`（默认 5–30），越界 `400`；覆写既有 token 不校验长度。
- 显式 token 含 `/` 时 `PUT /api/tokens/{value}` 路径不可用，建议使用 URL-safe 字符（`[A-Za-z0-9_-]`）。
- 非共享 token 不能授予超出自身 scope/bucket/前缀的权限，也不能修改/删除更大的 token（`403`）。
- `POST` 显式传入已存在 token 时按 `PUT` 合并语义更新，审计记 `token_update`。
- 响应：`data` 为 token 记录，**仅本次返回明文 `token`**。

### 5.3 移除 token

```
DELETE /api/tokens?value=<token>
```

**鉴权**：`token:delete`。

响应：`data={"removed":"<token>","user":"..."}`；不存在 `404`；非共享 token 只能删除授权不超出自身的 token。

---

## 六、审计查询

```
GET /api/audit
```

**鉴权**：`audit:read`。

**查询参数**：

| 参数 | 说明 |
|---|---|
| `bucket` | 按 bucket 过滤 |
| `key` | 按 key 精确过滤 |
| `actor` | 按操作者过滤 |
| `since` | 只返回 >= 该时间（`YYYY-MM-DD` 或完整时间戳） |
| `limit` | 返回条数，默认 500；`0` 返回空，负数 `422` |

响应为审计记录数组，每条含 `time`、`action`、`bucket`、`key`、`actor`、`ip`、`public_ip`、`size`、`sha256` 及动作附加字段。

带资源范围的用户 token 只返回范围内、且归属于具体 bucket 的记录；`bucket=""` 的全局事件（token/config 变更等）不返回。管理员与未受限 token 可看全量。

---

## 七、文档接口

```
GET /api/docs                 # 文档清单 [{name,title,available}]
GET /api/docs/{name}          # {"name","title","content"}
GET /api/docs/{name}?raw=1    # text/plain 原文
```

公开。`name` 白名单：`api_rules`、`architecture`、`readme`、`changelog`、`license`、`license_zh`、`license_header`；未知名称 `404`。

网页 UI 为 `GET /`；Swagger/ReDoc 为 `/docs`、`/redoc`；OpenAPI 为 `/openapi.json`。

---

## 八、接口速查总表

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/health` | 公开 |
| GET | `/health/live` | 公开 |
| GET | `/health/ready` | 公开 |
| GET | `/metrics` | 公开 |
| POST | `/api/config/reload` | `config:write` 或管理员 |
| GET | `/api/config` | `config:write` 或管理员 |
| POST | `/api/config` | `config:write` 或管理员 |
| GET | `/api/auth/check` | 公开 |
| GET | `/api/auth/permissions` | 公开 |
| GET | `/api/docs` | 公开 |
| GET | `/api/docs/{name}` | 公开 |
| GET | `/docs` | 公开（Swagger UI） |
| GET | `/redoc` | 公开（ReDoc） |
| GET | `/openapi.json` | 公开 |
| GET | `/api/objects/signed-links/config` | 公开 |
| GET | `/` | 公开（网页 UI） |
| GET | `/api/buckets` | 公开 / `bucket:list` |
| GET | `/api/objects/list` | 公开 / `object:list` |
| POST | `/api/buckets` | `bucket:create` |
| POST | `/api/objects` | `object:upload` |
| POST | `/api/objects/mkdir` | `object:mkdir` |
| POST | `/api/objects/rename` | `object:rename` |
| GET | `/api/objects/head` | `object:head` |
| POST | `/api/objects/presign` | `link:create` |
| GET / HEAD | `/api/objects/download` | `object:download` / 签名 |
| GET | `/api/objects/download-bulk` | `object:download` |
| DELETE | `/api/objects` | `object:delete` |
| POST | `/api/objects/bulk-delete` | `object:delete_bulk` |
| GET | `/api/objects/signed-links` | `link:list` |
| POST | `/api/objects/signed-links/{link_id}/revoke` | `link:revoke` |
| POST | `/api/objects/initiate` | `upload:initiate` |
| POST | `/api/objects/chunk` | `upload:chunk` |
| POST | `/api/objects/complete` | `upload:complete` |
| POST | `/api/objects/abort` | `upload:abort` |
| GET | `/api/objects/uploads` | `upload:abort`（内部接口） |
| GET | `/api/tokens` | `token:read` |
| POST | `/api/tokens` | `token:write` |
| PUT | `/api/tokens/{value}` | `token:write` |
| DELETE | `/api/tokens` | `token:delete` |
| GET | `/api/audit` | `audit:read` |

> `GET /api/config`、`POST /api/config`、`GET /api/objects/uploads`、`HEAD /api/objects/download`、`/docs`、`/redoc`、`/openapi.json` 不进入 OpenAPI，不改变操作数。

---

## 九、配置项

配置优先级：内置默认 < `config.json`（或 `ARTIFACT_DEPOT_CONFIG`）< `config/.env` < 真实环境变量。`.env` 空值视为未设置；改 `host`、`port`、`depot_dir`、`meta_dir`、`cors_origins`、`allowed_hosts`、`upload.staging_dir` 需重启。

| 配置 | 默认 | 说明 |
|---|---|---|
| `depot_dir` | `./depot` | 仓库根目录；相对路径按项目根解析（`ARTIFACT_DEPOT_DIR`） |
| `meta_dir` | `""` | 状态文件目录；留空=放在 depot 下（`ARTIFACT_DEPOT_META_DIR`） |
| `host` | `0.0.0.0` | 监听地址 |
| `port` | 8004 | 服务端口（`ARTIFACT_DEPOT_PORT`） |
| `access_token` | `admin` | 管理员共享令牌（`ARTIFACT_DEPOT_ACCESS_TOKEN`）；生产必须改为 ≥16 位随机串 |
| `max_upload_mb` | 0 | 单文件上传上限（0=不限） |
| `ui_enabled` | true | 是否启用网页 UI |
| `metrics_enabled` | true | 是否暴露 `/metrics` |
| `trusted_proxies` | `[]` | 可信代理 IP/CIDR；非空才解析 XFF 作为审计 public_ip |
| `audit_max_mb` | 50 | 审计日志单文件上限（0=不轮转） |
| `audit_backups` | 5 | 审计历史文件保留数 |
| `cors_origins` | `[]` | CORS 来源白名单；空=不启用 |
| `allowed_hosts` | `["*"]` | TrustedHost 白名单 |
| `require_read_token` | false | 读接口是否强制携带 token |
| `ui.file_page_size` | 200 | 文件列表分页条数（10–1000） |
| `ui.audit_page_size` | 500 | 审计单次查询条数（10–5000） |
| `signed_links.count_min` | 1 | 签名链接次数下限（1–100000） |
| `signed_links.count_max` | 10 | 签名链接次数上限（1–100000） |
| `signed_links.expire_min_seconds` | 60 | 签名链接时效下限（1–31536000 秒） |
| `signed_links.expire_max_seconds` | 604800 | 签名链接时效上限（1–31536000 秒） |
| `upload.chunk_size_mb` | 8 | 分片大小（1–64MB） |
| `upload.chunk_concurrency` | 4 | 每文件分片并发（1–16） |
| `upload.file_concurrency` | 3 | 同时上传文件数（1–10） |
| `upload.request_timeout_seconds` | 86400 | 分片/合并请求超时（60s–7d） |
| `upload.session_cleanup_interval_seconds` | 600 | 分片会话清理间隔（60s–24h） |
| `upload.max_chunk_mb` | 64 | 单片硬上限（1–64MB） |
| `upload.max_chunks` | 100000 | 单文件最大分片数（1–100000） |
| `upload.max_session_gb` | 200 | 会话总量上限（1–200GB） |
| `upload.session_ttl_seconds` | 86400 | 会话绝对有效期（60s–7d） |
| `upload.session_idle_ttl_seconds` | 86400 | 会话空闲清理阈值（60s–7d，不小于请求超时） |
| `upload.staging_dir` | `""` | staging 根目录；留空=`<depot_dir>/.staging`；相对路径按 depot 解析 |
| `download.chunk_size_mb` | 8 | 下载分片大小（1–64MB） |
| `download.chunk_concurrency` | 4 | 下载分片并发（1–16） |
| `download.request_timeout_seconds` | 3600 | 下载分片请求超时（60s–7d） |
| `token.min_length` | 5 | 新建 token 最小长度（1–256） |
| `token.max_length` | 30 | 新建 token 最大长度（1–256）；自动生成时取该长度 |

常用环境变量：`ARTIFACT_DEPOT_ACCESS_TOKEN`、`ARTIFACT_DEPOT_PORT`、`ARTIFACT_DEPOT_MAX_UPLOAD_MB`、`ARTIFACT_DEPOT_TOKEN_MIN_LENGTH`/`MAX_LENGTH`、`ARTIFACT_DEPOT_DOWNLOAD_*`、`ARTIFACT_DEPOT_UPLOAD_*`、`ARTIFACT_DEPOT_UI_FILE_PAGE_SIZE`/`UI_AUDIT_PAGE_SIZE`。

---

## 十、权限点与角色

### 10.1 权限目录接口

```
GET /api/auth/permissions
```

公开，返回：`{permissions, public, roles, default_role, require_read_token, upload, download, ui, token}`。

`permissions` 每项为 `{key, group, label, method, path, write}`；`upload`/`download`/`ui`/`token` 下发对应运行参数。

### 10.2 权限点（22 条）

| 权限点 | 接口 | 说明 |
|---|---|---|
| `bucket:list` | GET `/api/buckets` | 列 bucket |
| `bucket:create` | POST `/api/buckets` | 创建 bucket |
| `object:list` | GET `/api/objects/list` | 列对象/目录 |
| `object:head` | GET `/api/objects/head` | 对象元信息 |
| `object:download` | GET/HEAD `/api/objects/download` | 下载（签名链接免） |
| `object:upload` | POST `/api/objects` | 单请求上传 |
| `upload:initiate` | POST `/api/objects/initiate` | 发起/恢复分片会话 |
| `upload:chunk` | POST `/api/objects/chunk` | 上传分片 |
| `upload:complete` | POST `/api/objects/complete` | 合并分片 |
| `upload:abort` | POST `/api/objects/abort` | 取消分片 |
| `object:mkdir` | POST `/api/objects/mkdir` | 新建目录 |
| `object:rename` | POST `/api/objects/rename` | 重命名/移动 |
| `object:delete` | DELETE `/api/objects` | 删除对象/空目录 |
| `object:delete_bulk` | POST `/api/objects/bulk-delete` | 批量删除目录内容 |
| `link:create` | POST `/api/objects/presign` | 生成签名链接 |
| `link:list` | GET `/api/objects/signed-links` | 列签名链接 |
| `link:revoke` | POST `/api/objects/signed-links/{link_id}/revoke` | 作废签名链接 |
| `token:read` | GET `/api/tokens` | 查看注册表 |
| `token:write` | POST `/api/tokens`、PUT `/api/tokens/{value}` | 创建/更新 token |
| `token:delete` | DELETE `/api/tokens` | 移除 token |
| `audit:read` | GET `/api/audit` | 查询审计 |
| `config:write` | GET/POST `/api/config`、POST `/api/config/reload` | 修改站点配置（不能轮换 access_token） |

### 10.3 角色预设

| role | scopes |
|---|---|
| `viewer` | bucket:list, object:list |
| `downloader` | viewer + object:head, object:download |
| `uploader` | viewer + object:head, object:upload, upload:initiate/chunk/complete/abort, object:mkdir |
| `publisher` | uploader + object:rename, link:create |
| `operator` | publisher + bucket:create, object:download, object:delete, object:delete_bulk, link:list, link:revoke |
| `auditor` | bucket:list, object:list, audit:read, token:read |
| `user` | bucket:list, bucket:create, object:list, object:head, object:download, object:upload, upload:initiate/chunk/complete/abort, object:mkdir, object:rename, link:create, link:list, link:revoke |
| `custom` | 按勾选，可为空 |
| `admin` | 全部 22 个权限点 |

### 10.4 生成 Token

`POST /api/tokens` 的字段与规则见 5.2。示例：

```bash
curl -X POST "http://<IP>:8004/api/tokens?token=<admin>" \
  -H "Content-Type: application/json" \
  -d '{"user":"处理端-8","role":"custom",
       "scopes":["object:upload","upload:initiate","upload:chunk","upload:complete","upload:abort","object:list","object:head"],
       "allow_buckets":["voicevideo"],
       "allow_prefixes":{"voicevideo":["carryvideo/2026/"]},
       "description":"2026 批次"}'
```

- `allow_prefixes` 中的 bucket 必须同时在 `allow_buckets` 中，否则 `400`。
- 新 token 长度受 `token.min_length/max_length` 约束；覆写旧 token 不校验长度。
- 非共享 token 不能授予自身没有的 scope 或更宽的资源范围。
- 旧格式 token（`{"<token>":"用户名"}`）按 `role=user` 兼容。

---

## 十一、安全摘要

1. 读接口默认内网公开；如需鉴权设 `require_read_token=true`；不要把服务直接暴露公网。
2. 使用 `Authorization: Bearer` 传 token，避免进入 URL、日志、浏览器历史。
3. `config:write` 是高权限（可改站点配置），但不能轮换 `access_token`；仅授予可信管理者。
4. 删除、批量删除、重命名等写操作需对应 scope 与资源范围；目录仅空可删，批量删除需输入名称确认。
5. 路径穿越、隐藏路径、跨 bucket 符号链接均有防护；`rename` 非事务，底层 I/O 异常可能留下部分移动。
6. 注册表默认脱敏，仅管理员可 `reveal=true`。
