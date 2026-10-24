# ArtifactDepot 接口文档

> 版本 0.10.0 · FastAPI 服务 · 默认端口 8004
>
> **文档勘误（本次按实现逐条校对）**：修正 `public_ip`（客户端自报值一律忽略）、`ETag` 回退规则、错误码表（补 `503`、修正「未配置 access_token → 401」、补「删除非空目录 → 400」）、`auth/check` 成功/失败字段差异、`config/reload` 失败为 `400`、`head`/`health/ready` 返回字段、`Token` 注册表返回结构、`§8` 总表补全并统一 `{link_id}`、`user` 角色补 `link:list`/`link:revoke`；并登记 4 个**已知缺陷**（`OBJ-001/002/003~005/006`，证据见 `reports/final_test_report.md`）。
>
> Unreleased 变更：**令牌资源范围（bucket / 路径前缀）硬化为硬边界**——带 token 的读请求在无效 token 时返回 `401`、缺 scope 或范围越界返回 `403`（此前会静默降级为匿名 / 返回 `200`）；审计只返回 token 范围内的记录；非共享 token 不能创建 / 更新超出自身范围的 token（`403`）。详见 1.4、2.2、4.2、5、6、12.4。
>
> 0.7.1 变更：新增 `GET /api/docs`（公开、只读文档清单 + 内容，供查看原始 Markdown）；网页 UI 标签栏右侧提供 Swagger / ReDoc 入口（FastAPI 自动生成，可在线调试）；修复两处与文档语义不一致的问题：`scopes: []` 现在真正表示「无权限」（此前会被回退成角色预设），`require_read_token=true` 时资源范围外调用 `GET /api/objects/list` 返回 `403`（此前会忽略范围过滤、返回白名单外 bucket 的内容）。
>
> 0.7.0 变更：引入**权限点（scope）+ 自定义 Token**：管理员可逐接口勾选生成不同权限的 token（见第十二章）；`GET /api/auth/permissions` 返回接口/权限目录；旧 token 自动按 `user` 角色兼容；新增配置 `require_read_token`（默认 false，读接口仍公开）。
> 0.6.0 变更：新增 `POST /api/buckets`（显式创建 bucket）、`POST /api/objects/rename`（重命名/移动目录）、`GET /api/objects/head`（对象元信息探测）；新增「十一、其他项目接入指南（bucket/目录/文件 push-pull）」。
>
> 本文档描述 ArtifactDepot（对象存储仓库站点）对外提供的全部 HTTP 接口、调用规范与权限模型。
> 接口定义源码：`src/artifactdepot/api/objects.py`、`src/artifactdepot/api/system.py`、`src/artifactdepot/web/ui.py`；鉴权实现：`src/artifactdepot/auth.py`。

---

## 一、概述与通用约定

### 1.1 服务地址与端口

ArtifactDepot 是独立 FastAPI 服务，默认监听 **8004** 端口（host 网络下 8004 即宿主机端口）：

```
http://<主机IP>:8004
```

可选通过 Nginx 反代为 `location /depot/` 前缀，此时路径为 `http://<主机IP>/depot/...`，本文档以裸 8004 为基准。

### 1.2 统一响应格式

除下载文件、网页 UI、`/health`、`/health/live`、`/health/ready` 与 `/metrics`（Prometheus 文本）外，所有接口返回 JSON，统一结构：

```json
{
  "code": 0,          // 0 = 成功；非 0 或 HTTP 错误码表示失败
  "message": "success",
  "data": { ... }     // 各接口不同，见下文
}
```

- 成功时 HTTP 状态码通常为 **200**，`code` 字段为 `0`。
- 失败时 HTTP 状态码直接表达错误类别（见 1.6 错误码），`detail` 字段带具体原因（FastAPI HTTPException 风格）。
- `/health` 为健康检查专用，直接返回 `{"status":"ok","depot_dir":"...","exists":true}`，不使用统一包装。

### 1.3 鉴权模型（核心）

所有**写操作与敏感操作**都要求携带有效 token。token 的两类身份：

| 身份 | 来源 | actor 记录 |
|---|---|---|
| 管理员 / 工具 | 配置 `access_token`（共享令牌） | `系统/工具` |
| 用户 | `tokens.json` 注册表（可经 DataHub `users.json` 惰性同步） | 用户名 |

token 的传递方式（**推荐请求头**）：

1. **请求头（推荐）**：`Authorization: Bearer <令牌>`——不落 URL、不进浏览器历史/Referer/访问日志。
2. **URL query（兼容）**：`?token=<令牌>`——服务端访问日志会把 `token` / `tk` / `sig` 等参数值脱敏为 `***`，但仍建议优先用请求头。

> 注意：**multipart 接口**（`POST /api/objects`、`/api/objects/chunk`、`/api/objects/complete`、`/api/objects/abort`）额外支持把 token 作为 **multipart 表单字段** `token=` 传递（兼容旧客户端）。`POST /api/objects/initiate` 的 body 为空，**只认** query `?token=` 或 `Authorization: Bearer`。

token 校验规则：

- 管理员 token 用 **常量时间比较**（`hmac.compare_digest`）与 `access_token` 比对。
- 用户 token 在 `tokens.json` 注册表命中；本地未命中时先从 DataHub 拉一次再判定（惰性同步）。

### 1.4 权限模型（0.7.0）

普通用户 token 不再是“万能写权限”，而是携带一组**权限点（scope）**，每个受保护接口对应一个权限点，可在生成 token 时逐接口勾选。

| 身份 | 说明 |
|---|---|
| 管理员共享 token | 配置 `access_token`，放行全部权限（含 token/审计/删除） |
| 用户 token | `tokens.json` 注册表，携带 `role` + `scopes` + 可选 bucket/路径前缀范围 + 过期时间 |
| 签名链接 | `link`+`tk` / `expires`+`sig`，免 token 下载（由 `link:create` 决定谁能签发） |

| 等级 | 说明 | 适用接口 |
|---|---|---|
| 公开（默认，可收紧） | 知道地址即可调，内网设计；`require_read_token=true` 时读接口也需 scope | `/health`、`/health/live`、`/health/ready`、`/metrics`、`/api/auth/check`、`/api/auth/permissions`、`/api/docs`、`/api/docs/{name}`、`/api/objects/signed-links/config`、`/`，以及默认公开的 `GET /api/buckets`、`GET /api/objects/list` |
| 按 scope 授权 | 生成 token 时勾选对应权限点 | 上传/下载/建目录/改名/删除/签名链接/分片/审计/token 管理等 |
| 管理员 | 共享 `access_token` | 全部 |

> **资源范围（bucket / 路径前缀）是硬边界（本版硬化）**：用户 token 可携带 `allow_buckets` / `allow_prefixes`（字段见 12.4）。设置后该 token 只能访问范围内的 bucket 与路径前缀：
>
> - **范围内**：正常放行（仍需具备对应权限点）；
> - **范围外**：一律 `403`，不再「按匿名处理」静默放行，也不再返回范围外内容；
> - **带了 token 就必须有效**：无效 / 过期 / 禁用 token 返回 `401`，不再静默降级为匿名；
> - **范围为空 = 不限制**（全部 bucket，含将来新建的 bucket）；共享 `access_token` 永不限范围；
> - **不能借 token 提权**：非共享 token 创建 / 更新 token 时，请求的权限点与范围不得超出自身（见第五章、12.4）。
>
> 这是**行为变更**：此前携带越界 token 的读请求（`GET /api/objects/list`、`GET /api/buckets`）会返回 `200`，现在返回 `403`。

> 安全提示：默认仍保留 `list` / `buckets` 内网公开以兼容历史；若需最小权限，可设 `require_read_token=true` 并给读方签发 `bucket:list` / `object:list`。**不宜将 8004 直接暴露公网。**

### 1.5 bucket 与 key 约束

**bucket**（`validate_bucket`）：

- 非空，长度 ≤ 63 字符。
- 不允许以 `.` 开头，不允许包含 `/`、`\` 及控制字符。
- **首尾不允许空白字符**：落盘校验返回 `400`（携带受限 token 时可能先被资源范围校验拦成 `403`）。否则会出现「授权按去空白后的名字比较、落盘按原名取目录」的错配，可跨 bucket 越权。
- 允许中文等 Unicode。
- **无需预创建**：首次上传或 mkdir 时自动创建目录。

**key**（`validate_key`，相对路径式，会被 URL-decode 后去首尾斜杠）：

- 不允许含 `\`；前后 `/` 会被自动去除，去除后不能为空。
- 不允许 CR/LF/TAB 等控制字符（会污染审计/日志、响应头），返回 `400`。
- 不允许出现空段、`.`、`..`（防路径穿越）。
- 任意路径段都不允许以 `.` 开头（避免与 `.warehouse.json`、`.keep` 等隐藏约定冲突）。
- 支持多级目录，如 `carryvideo/20260815/tranvideo-1_20260815/tranvideo-1_20260815.mp4`。
- 对象操作接口（上传 / 下载 / 删除 / presign）要求 key 非空；列表接口的 `prefix` 可以为空（表示根目录）。

### 1.6 错误码

| HTTP 状态码 | 含义 |
|---|---|
| 400 | 参数非法（bucket/key 非法、prefix 越界、**删除非空目录**、签名链接 `count`/`expires` 越界、`allow_prefixes` 规范化或范围自洽校验失败、批量删除 `mode` 非法 / `confirm` 缺失或不匹配 / 项数超上限） |
| 401 | 缺少/无效令牌、token 无效/过期/禁用、签名无效或过期 |
| 403 | 已认证但权限不足（缺权限点、token 资源范围越界、非创建者作废他人链接、非共享 token 越权创建/修改/删除 token） |
| 404 | 对象/目录不存在（token 不存在时 `DELETE`/`PUT` 也返回 404）；一键下载空目录（无可见内容） |
| 409 | 对象已存在（`overwrite=false` 时） |
| 413 | 超过单文件上传上限 `max_upload_mb`（分片单块超限亦同） |
| 422 | 请求参数校验失败（字段缺失、类型错误等），`detail` 为数组 |
| 500 | 服务端异常。**注意**：未配置 `access_token` 时为 `401`（fail-closed），不是 500 |
| 503 | `/health/ready` 就绪检查失败（depot 目录不存在或不可写） |

> **已知缺陷（待修，证据见 `reports/final_test_report.md`）**
> - `OBJ-004`：`mkdir` 的 key 或父路径已是文件时仍返回 **500**（应为 `400`/`409`）；上传与分片合并的目标类型冲突已在 0.10.0 修复为 `409`。
> - `OBJ-001`：`rename` 的文件↔目录类型冲突 → **500**；目录移动**中途失败不回滚**（可能留下部分移动 + 清单不一致）。
> - `OBJ-002`：`rename` 以 `overwrite=true` 覆盖既有文件后，清单与 `ETag` 仍为旧值，旧 `ETag` 条件下载会返回 `304`。
> - `OBJ-006`：`presign` 返回的 `url` 未做 URL 编码，key 含 `&`、`#`、`+` 时该链接不可直接使用（改用显式 `bucket`/`key`/`link`/`tk` 参数即可）。

---

## 二、对象接口（`/api/objects`）

### 2.1 上传对象（PutObject）

```
POST /api/objects
```

**鉴权**：权限点 `object:upload`（并校验 bucket/key 资源范围）

**请求**（`multipart/form-data`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | 文件 | 是 | 上传的文件内容 |
| `bucket` | string | 是 | 目标 bucket |
| `key` | string | 是 | 对象 key（相对路径式，可多级目录） |
| `token` | string | 条件 | 访问令牌（也可走 query `?token=` 或 `Authorization: Bearer`） |
| `source_url` | string | 否 | 来源 URL，记入元数据 |
| `overwrite` | bool | 否 | 是否覆盖同名对象，默认 `true` |
| `public_ip` | string | 否 | **兼容占位，值被忽略**：审计公网 IP 只由服务端按 `trusted_proxies` 解析 `X-Forwarded-For`（见 10.8） |

**成功响应**（200）：

```json
{
  "code": 0,
  "message": "success",
  "data": { "bucket": "voicevideo", "key": "carryvideo/...", "size": 123, "sha256": "..." }
}
```

**示例**：

```bash
curl -X POST "http://<IP>:8004/api/objects" \
  -F "file=@成品.mp4" \
  -F "bucket=voicevideo" \
  -F "key=carryvideo/20260912/xxx/xxx.mp4" \
  -F "token=<token>" \
  -F "overwrite=true"
```

### 2.2 列出对象 / 目录（ListObjects）

```
GET /api/objects/list
```

**鉴权**：

- **不带 token**：公开（`require_read_token=false` 时匿名可调；`true` 时返回 `401` 缺少访问令牌）。
- **带 token**：必须**有效**（无效 / 过期 / 禁用 → `401`）且具备权限点 `object:list`（缺失 → `403`）；
  并且 `bucket` / `prefix` 必须落在该 token 的资源范围内，**越界 → `403`**（不再降级为匿名返回 `200`）。
  共享 `access_token` 不受资源范围限制。

> 注意：`require_read_token=true` 时「无 token」与「token 无效」都是 `401`；`token` 有效但缺 `object:list` 或 bucket/prefix 越界都是 `403`。资源范围为空表示不限制。

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | 目标 bucket |
| `prefix` | string | 否 | 限定前缀，列出该目录下的直接子项 |
| `limit` | int | 否 | >0 时按 `offset` 分页，最多返回 limit 项（0=不限，上限 10000） |
| `offset` | int | 否 | 分页起始偏移（默认 0，仅 `limit>0` 时生效） |

**成功响应**（200）的 `data`：`{ "bucket", "prefix", "items", "total", "offset", "limit", "truncated" }`。

`data.items` 元素：

- 目录：`{ "name", "key"（末尾带 /）, "is_dir": true, "size", "mtime" }`；空目录的 `mtime` 为目录自身 mtime。
- 文件：`{ "name", "key", "is_dir": false, "size", "mtime", "sha256", "source_url", "uploader" }`。
- 手工放入、没有 `.warehouse.json` 记录的文件也能列出，但 `sha256/source_url/uploader` 为空，`mtime` 回退为文件系统 mtime。

**示例**：

```bash
curl "http://<IP>:8004/api/objects/list?bucket=voicevideo&prefix=carryvideo/20260815"
```

### 2.3 下载对象（GetObject）

```
GET /api/objects/download
```

**鉴权**：三种方式，按优先级：

1. `link` + `tk`（注册表签名链接）—— 校验次数/过期/作废，免 token
2. `expires` + `sig`（旧式 HMAC 签名）—— 校验签名，免 token
3. `token`—— 需权限点 `object:download`（管理员共享 token 放行全部）

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | 目标 bucket |
| `key` | string | 是 | 对象 key |
| `link` | string | 否 | 签名链接 ID |
| `tk` | string | 否 | 签名链接密钥 |
| `expires` | string | 否 | 旧式 HMAC 过期时间戳 |
| `sig` | string | 否 | 旧式 HMAC 签名 |
| `token` | string | 否 | 访问令牌（非签名方式时必填）；也支持 `Authorization: Bearer` |
| `public_ip` | string | 否 | 已废弃/忽略；审计公网 IP 由服务端按 `trusted_proxies` 解析 XFF |

**响应**：文件流，支持 **Range（206）** 视频拖动，`Content-Type` 按扩展名推断。

**ETag / 条件请求**：响应带 `ETag`——有清单记录时为强 ETag（SHA-256）；手工放入、无 `.warehouse.json` 记录的文件回退为 `"<size>-<mtime>"`。请求头 `If-None-Match` 命中时返回 **304**，不传输文件体。

> 对象不存在时返回 `404`；签名链接会先检查对象存在，再扣减次数，避免无效消费。

**示例**：

```bash
# 带 token 下载
curl -o out.mp4 "http://<IP>:8004/api/objects/download?bucket=voicevideo&key=carryvideo/...&token=<token>"

# 共享链接下载（link + tk 来自 presign）
curl -o out.mp4 "http://<IP>:8004/api/objects/download?bucket=voicevideo&key=...&link=<id>&tk=<secret>"
```

### 2.4 删除对象 / 目录（DeleteObject）

```
DELETE /api/objects
```

**鉴权**：权限点 `object:delete`（管理员共享 token 始终放行；普通用户 token 需显式授予该权限点）

**查询参数**：`bucket`（必填）、`key`（必填）。审计公网 IP 由服务端按 `trusted_proxies` 解析，不接受客户端自报。

**说明**：删除文件或目录；目录仅空目录可删（防误删）。无 token/无效 token 返回 `401`，token 缺少 `object:delete` 返回 `403`。

```bash
curl -X DELETE "http://<IP>:8004/api/objects?bucket=voicevideo&key=path/to/obj&token=<admin_token>"
```

### 2.5 新建目录（Mkdir）

```
POST /api/objects/mkdir
```

**鉴权**：权限点 `object:mkdir`（并校验资源范围）

**请求体**（JSON）：

```json
{ "bucket": "voicevideo", "key": "story/20260912" }
```

**说明**：按 key 建目录树（`/` 多级自动创建），放隐藏 `.keep` 占位；bucket 不存在时随目录一并创建。

> **已知缺陷 `OBJ-004`**：若 key 或父路径已是**文件**，当前返回 `500`（应为 `400`）。

```bash
curl -X POST "http://<IP>:8004/api/objects/mkdir?token=<token>" \
  -H "Content-Type: application/json" \
  -d '{"bucket":"voicevideo","key":"story/20260912"}'
```

### 2.6 重命名 / 移动目录（Rename）—— 0.6.0 新增

```
POST /api/objects/rename
```

**鉴权**：权限点 `object:rename`（源与目标 key 都校验）

**请求体**（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | 目标 bucket |
| `key` | string | 是 | 现有目录（或对象）的 key |
| `new_key` | string | 是 | 新 key；允许修改目录前缀实现跨目录移动，如 `carryvideo/0815` → `carryvideo/0912` |
| `overwrite` | bool | 否 | 新位置已存在同名对象时是否覆盖，默认 `false`，冲突返回 `409` |

**语义说明**：

- 主要用于**目录改名/移动**：服务端整体迁移该前缀下的全部对象与 `.keep` 占位。**业务冲突**（如目标已存在且 `overwrite=false`）会整体回滚；但底层 I/O 异常（如目标路径类型冲突）**不会回滚**，可能留下部分移动——见下方已知缺陷。
- 也可用于单个对象改名（key 指向文件时）。
- `new_key` 与 `key` 校验规则同 1.5；不允许把目录移动到自身子目录（`new_key` 以 `key + "/"` 开头时返回 `400`）。
- 源不存在返回 `404`；审计记录含 `from_key` / `to_key`。审计公网 IP 由服务端按 `trusted_proxies` 解析，不接受客户端自报。
- **已知缺陷 `OBJ-001`**：文件↔目录类型冲突（如把文件改名为已存在的目录）返回 **`500`**，且目录移动中途失败**不回滚**——部分文件已被移动、清单仍指向旧路径。
- **已知缺陷 `OBJ-002`**：`overwrite=true` 覆盖既有文件后，`.warehouse.json` 与 `ETag` 仍是**被覆盖文件的旧值**；用旧 `ETag` 条件下载会得到 `304`，客户端拿不到新内容。

**成功响应**（200）：

```json
{
  "code": 0,
  "message": "success",
  "data": { "bucket": "voicevideo", "from_key": "story/old", "to_key": "story/new", "moved_objects": 12 }
}
```

```bash
curl -X POST "http://<IP>:8004/api/objects/rename?token=<token>" \
  -H "Content-Type: application/json" \
  -d '{"bucket":"voicevideo","key":"story/old","new_key":"story/new"}'
```

### 2.7 对象元信息探测（Head）—— 0.6.0 新增

```
GET /api/objects/head
```

**鉴权**：权限点 `object:head`（并校验资源范围）

**查询参数**：`bucket`（必填）、`key`（必填）。

**说明**：轻量探测文件或目录是否存在。存在时返回 `data`：`{ "exists": true, "is_dir": false, "bucket": "...", "key": "...", "size": 123, "sha256": "...", "mtime": "2026-10-07T16:20:09Z" }`（`mtime` 统一为 UTC ISO8601，与 list 一致；**目录分支不含 `sha256`**，`is_dir=true`）。**不存在时返回 `404`**（S3 HeadObject 语义）。响应带 `ETag`，`If-None-Match` 命中返回 **304**。

### 2.8 生成签名下载链接（Presign / 共享链接）

```
POST /api/objects/presign
```

**鉴权**：权限点 `link:create`（并校验资源范围）

**请求体**（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | 目标 bucket |
| `key` | string | 是 | 对象 key |
| `mode` | string | 否 | `count`（限次 1-10）/ `time`（限时，默认）/ `permanent`（永久） |
| `count` | int | 否 | `mode=count` 时的次数（1-10） |
| `expires` | int | 否 | `mode=time` 时的秒数（默认 3600，即 1 小时） |

**成功响应**的 `data`：

```json
{
  "url": "/api/objects/download?bucket=..&key=..&link=<id>&tk=<token>",
  "id": "<link_id>",
  "mode": "time",
  "max_uses": null,
  "remaining": null,
  "expires": "<时间戳>"
}
```

**说明**：返回的 `url` 即「共享链接」，可在无 token 情况下限次/限时/永久下载。生成时只校验 bucket/key 格式，不强制对象已存在。`max_uses` / `remaining` 仅在 `mode=count` 时有值，`time` / `permanent` 为 `null`。

> **已知缺陷 `OBJ-006`**：`url` 未做 URL 编码，key 含 `&`、`#`、`+` 时链接不可直接使用（`&` → key 被截断为 404；`#` → 丢掉 `link`/`tk` 变 401；`+` → 被当成空格 404）。此时请改用显式参数并自行编码：`/api/objects/download?bucket=<enc>&key=<enc>&link=<id>&tk=<tk>`。

### 2.9 签名链接配置

```
GET /api/objects/signed-links/config
```

**鉴权**：公开

返回次数/时效的上下限（供前端渲染校验），如 `count_min/count_max/expire_min_seconds/expire_max_seconds`。

### 2.10 列出签名链接

```
GET /api/objects/signed-links
```

**鉴权**：权限点 `link:list`；带资源范围的 token 只能看到范围内的链接。完整链接 URL 仅管理员/创建者可看，密钥不回传（`token` 字段被剥离）。

### 2.11 作废签名链接

```
POST /api/objects/signed-links/{link_id}/revoke
```

**鉴权**：权限点 `link:revoke`；管理员可作废任意，用户只能作废自己创建的。审计公网 IP 由服务端按 `trusted_proxies` 解析，不接受客户端自报。

### 2.12 批量删除目录内容（BulkDelete）

```
POST /api/objects/bulk-delete
```

**鉴权**：权限点 `object:delete_bulk`（同时校验 bucket/prefix 资源范围）。管理员共享 token 始终放行；普通用户 token 需显式授予该权限点，且 `bucket` 与 `prefix` 必须在 token 的 `allow_buckets` / `allow_prefixes` 范围内，否则 `403`。

> 服务端**先做资源范围校验、再做参数与确认校验**：范围外请求即使 `mode` / `confirm` 也非法，仍返回 `403`（而非 `400`）。

**请求体**（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | bucket 名 |
| `prefix` | string | 否（默认 `""`） | 当前目录相对路径；`""` / `"/"` = bucket 根；允许多级，首尾 `/` 自动规范化；不能指向隐藏目录（路径段以 `.` 开头由 `validate_key` 拒绝，`400`） |
| `mode` | string | 是 | `files`（只删当前目录的直接文件，不递归）或 `all`（递归删除文件与子目录）；其他值 `400` |
| `dry_run` | bool | 否（默认 `false`） | `true` 只预览统计，不删除、不写审计；省略即为**执行**，必须带 `confirm` |
| `confirm` | string | 执行时必填 | 必须等于「目录名」：`prefix` 的最后一段；`prefix` 为空时等于 `bucket` 名。服务端对输入 `strip()` 后精确比较，不匹配 `400`；预览（`dry_run=true`）不需要 |

**请求示例**：

```json
{
  "bucket": "project_1",
  "prefix": "task_5/交付物",
  "mode": "files",
  "dry_run": true,
  "confirm": "交付物"
}
```

**成功响应**（200，统一 `{"code":0,"message":"success","data":...}`）：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "bucket": "project_1",
    "prefix": "task_5/交付物",
    "mode": "files",
    "dry_run": true,
    "files": 3,
    "dirs": 0,
    "bytes": 1048576,
    "errors": [],
    "elapsed_ms": 12
  }
}
```

`data` 字段：

- `prefix`：规范化后的前缀（根目录为 `""`）；
- `dry_run=true` 时 `files` / `dirs` / `bytes` 是「将删除」的统计，磁盘、清单、审计均不变；`dry_run=false` 时是「实际成功删除」的统计；
- `dirs`：被删除的子目录数（**不含 `prefix` 目录本身**），`files` 模式恒为 `0`；
- `bytes`：被删文件删除前 `st_size` 之和；
- `errors`：部分失败明细 `[{"key":"a/b","error":"..."}]`，最多返回 20 条（`key` 相对 bucket 根）；存在 `errors` 时仍返回 `200`，调用方应提示「部分失败」；全部成功时为空数组；
- `elapsed_ms`：服务端耗时（整数毫秒）。

**语义**（设 `base = <bucket>/<prefix>`，`base` 目录本身始终保留）：

`mode=files`：

- 只处理 `base` 的**直接子项中的普通文件**，**不进入子目录**；
- 跳过：隐藏文件 / 隐藏目录（名字以 `.` 开头，含 `.keep`）、bucket 清单 `MANIFEST = ".warehouse.json"`、符号链接；
- 保留：所有子目录及其内容、`base` 本身、`.keep` 占位文件；
- 删除文件对应的清单项（`.warehouse.json` 中的 key）。

`mode=all`：

- 递归处理 `base` 下的所有文件与子目录；**不跟随符号链接**（符号链接本身按文件删除，不触碰链接目标）；
- 删除文件包括隐藏文件（如 `.keep`、`.hidden`）；**但 bucket 根下的 `.warehouse.json` 永不删除**；
- 子目录自底向上删除；`base` 本身保留（`base` 为 bucket 根时保留 bucket 目录与清单文件）；
- 清理清单：删除「被删文件的 key」以及「被删目录前缀下的所有条目」。

通用规则：

- `base` 不存在 → `404`；`base` 是文件而非目录 → `404`；bucket 不存在 → `404`；
- 路径穿越 / 非法 key 沿用 1.5 的 `validate_key`（因此 `prefix` 不能指向隐藏目录）；
- `prefix` 解析（含符号链接）后必须仍位于**目标 bucket 内**：若目录中存在指向其他 bucket 的符号链接，`prefix` 指向它时返回 `400`（防止跨 bucket 误删；`files` 模式也会跳过符号链接，`all` 模式只删链接本身、不跟随）；
- 单次上限 `MAX_BULK_DELETE_ITEMS = 50000`：`files + dirs` 超过即 `400`（提示缩小范围），`dry_run` 预览同样受限；
- 并发删除导致文件消失时视为「已不存在」跳过，不计错误；其他 I/O 异常记入 `errors` 并继续处理其余项；
- 幂等/空目录：没有任何可删项时返回 `200`，计数全为 `0`。

**确认规则**：

1. 预览（`dry_run=true`）不需要 `confirm`，也不产生任何写入；
2. 执行（`dry_run=false`，或省略该字段）必须携带 `confirm`，且等于 `prefix` 最后一段；`prefix` 为空（bucket 根）时等于 `bucket` 名；
3. `confirm` 缺失 / 不匹配 → `400`，**不执行任何删除、不写审计**；输入前后空白会被 `strip()` 后再比较；
4. 前端在输入匹配前禁用「确认删除」按钮只是交互体验，**不能替代服务端二次校验**。

**错误码**：

| HTTP 状态码 | 场景 |
|---|---|
| 400 | `mode` 非法；`confirm` 缺失或不匹配；`prefix` 非法（隐藏路径段 / 穿越 / 空段等）；`files + dirs` 超过 50000 项 |
| 401 | 缺少 / 无效 token |
| 403 | 缺少 `object:delete_bulk` 权限点，或 bucket/prefix 超出 token 资源范围 |
| 404 | bucket 不存在；`prefix` 不存在或不是目录 |

**审计与指标**：

- 执行（`dry_run=false`）写审计：`action=delete_bulk`，`bucket` / `key`（规范化后的 `prefix`，根目录为 `""`）/ `actor` / 客户端 IP / `size=bytes`，`extra` 含 `mode` / `files` / `dirs`；
- `dry_run=true` 不写审计；
- `DELETES` 指标每次**执行** +1（`dry_run` 不计），见 `/metrics`。

**示例**：

```bash
# 预览：只统计将删除的内容，不落盘
curl -X POST "http://<IP>:8004/api/objects/bulk-delete" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"bucket":"project_1","prefix":"task_5/交付物","mode":"files","dry_run":true}'

# 执行：清空 task_5/交付物 下全部内容（保留该目录本身），confirm 必须是目录名
curl -X POST "http://<IP>:8004/api/objects/bulk-delete" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"bucket":"project_1","prefix":"task_5/交付物","mode":"all","confirm":"交付物"}'
```

### 2.13 一键下载目录内容（DownloadBulk）—— 0.10.0 新增

```
GET /api/objects/download-bulk?bucket=<bucket>&prefix=<prefix>&token=<token>
```

**鉴权**：复用权限点 `object:download`（**不新增权限点**，旧 token 立即可用），并校验 `bucket` + `prefix` 资源范围。token 可放 query `?token=` 或 `Authorization: Bearer`；**不接入签名链接**（`link` / `tk` 不适用）。无 token / 无效 token → `401`；缺 `object:download` 或范围越界 → `403`（资源范围校验先于参数校验）。

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | bucket 名 |
| `prefix` | string | 否（默认 `""`） | 当前目录相对路径；空 / `"/"` = bucket 根；首尾 `/` 自动规范化；穿越 / 隐藏路径段 → `400` |
| `token` | string | 条件 | 访问令牌（也可用 `Authorization: Bearer`） |

**返回规则**（只读操作，不修改数据、不产生临时文件）：

**A. 单文件直下**：目录中**只有 1 个可见文件且没有任何可见子目录** → `200`，`FileResponse` 直下该文件：

- 保留原文件名与 `Content-Type`，支持 **Range/206** 与 `ETag` / `If-None-Match` → `304`（与 `/api/objects/download` 一致）；
- 审计：`action=download`，`key=<prefix>/<file>`，`size=文件大小`，`extra={"bulk":true,"mode":"file"}`；`DOWNLOADS` 指标 +1（`304` 不计）；
- 注意：**1 个可见文件 + 1 个（空）子目录仍走 ZIP**（只要存在可见子目录就不直下）。

**B. ZIP 打包**（其他非空情况：≥2 个可见文件，或至少 1 个可见子目录）→ `200`，`Content-Type: application/zip`：

- **流式响应**：边遍历边输出，无 `Content-Length`（chunked）；不先落临时文件、不整包进内存；
- 压缩方式 `ZIP_STORED`（不压缩，避免大文件 CPU 卡顿）+ `allowZip64=True`；文件按固定 256 KiB 块读取；
- ZIP 结构（先有与目录同名的**顶层文件夹**，再放相对内容）：

  ```
  <root_name>/
  <root_name>/<file-or-subdir>...
  ```

  - `root_name`：`prefix` 最后一段；`prefix` 为空（bucket 根）时为 bucket 名；
  - 包含可见子目录条目（**含空目录**，名称以 `/` 结尾）；
  - 文件条目保留源文件 `mtime`（ZIP 内为本地时间；目录条目为当前时间）；
  - 跳过：隐藏文件/目录（名字以 `.` 开头，含 `.keep`、`.warehouse.json`）与符号链接（不跟随、不打包）；
  - 输出顺序稳定：每层先子目录（深度优先）再文件，名称排序；
- 响应头：`Content-Disposition: attachment; filename="<ascii 回退>.zip"; filename*=UTF-8''<percent-encoded>`；ASCII 回退名剔除不支持的字符（中文目录回退为 `download.zip`），真实文件名（可含中文）放 `filename*`；
- 审计：ZIP 流结束（含客户端中断）写 `action=download_bulk`，`key=规范化后的 prefix`（根目录 `""`），`size=实际发送字节数`，`extra={"mode":"zip","files":N,"dirs":M,"aborted":bool}`（`files`/`dirs` 为**已写入**条目数，客户端中断时可能小于实际内容）；`DOWNLOADS` 指标每次请求 +1（响应开始时；失败不计）。

**错误码**：

| HTTP 状态码 | 场景 |
|---|---|
| 400 | bucket/prefix 非法（穿越 / 隐藏路径段）；`prefix` 解析（含符号链接）后越出 bucket（如跨 bucket 符号链接）；`files + dirs` 超过 100000 项 |
| 401 | 缺少 / 无效 token |
| 403 | 缺少 `object:download` 权限点，或 bucket/prefix 超出 token 资源范围 |
| 404 | bucket 不存在；`prefix` 不存在或不是目录；**目录为空**（无可见文件/子目录，不返回 0 字节 ZIP） |

**安全与限制**：

- `prefix` 解析（含符号链接）后必须仍在**本 bucket** 内，否则 `400`（防借符号链接跨 bucket 读取）；
- 遍历绝不跟随符号链接；隐藏项（含 `.keep`、`.warehouse.json`）全部跳过；
- 单次 `files + dirs` 上限 `MAX_BULK_DOWNLOAD_ITEMS = 100000`（防御性），超限 `400` 并提示缩小范围；普通目录远低于此值；
- ZIP 内名称由真实相对路径生成，不做用户输入拼接；UTF-8 文件名保留（zipfile 自动置 UTF-8 标志）；
- 客户端中断：停止遍历/读取、后台线程退出，审计仍会写一条且标记 `aborted=true`；
- 失败请求（400/401/403/404）不写审计、不计 `DOWNLOADS` 指标。

**示例**：

```bash
# 目录含多个文件/子目录 → 流式 ZIP
curl -fL -o 交付物.zip \
  "http://<IP>:8004/api/objects/download-bulk?bucket=project_1&prefix=task_5/交付物&token=<token>"

# 目录内只有 1 个可见文件 → 直接得到原文件（文件名由浏览器/服务端决定）
curl -fL -o report.pdf \
  "http://<IP>:8004/api/objects/download-bulk?bucket=project_1&prefix=task_5/交付物&token=<token>"

# bucket 根：下载整个 bucket（ZIP 顶层文件夹为 bucket 名）
curl -fL -o project_1.zip \
  "http://<IP>:8004/api/objects/download-bulk?bucket=project_1&prefix=&token=<token>"
```

---

## 三、分片上传（大文件多路并发）

适用于超大文件，分 4 步：initiate → chunk（多次）→ complete → abort。

### 3.1 发起上传会话

```
POST /api/objects/initiate
```

**鉴权**：权限点 `upload:initiate`

返回 `{ "upload_id", "chunk_size", "chunk_concurrency", "file_concurrency",
"request_timeout_seconds", "max_chunk_size", "max_chunks", "max_session_gb", "max_upload_mb" }`：

- 默认 `chunk_size=8MB`、`chunk_concurrency=4`、`file_concurrency=3`、
  `request_timeout_seconds=86400`（24h）；
- 全部来自 `config.json` 的 `upload.*`（见 §9），`POST /api/config/reload` 后即时生效；
- 浏览器/CLI 应以服务端返回值为准（`chunk_size` 不再固定 8MB）。

### 3.2 上传分片

```
POST /api/objects/chunk
```

**鉴权**：权限点 `upload:chunk`

**请求**（multipart）：`upload_id`、`index`（分片序号）、`chunk`（文件）。

### 3.3 合并分片完成上传

```
POST /api/objects/complete
```

**鉴权**：权限点 `upload:complete`（并校验 bucket/key 资源范围）

**请求**（multipart）：`upload_id`、`bucket`、`key`、`total_chunks`，可选 `source_url`、`overwrite`。审计公网 IP 由服务端按 `trusted_proxies` 解析（`public_ip` 字段仅为兼容占位，值被忽略）。

**清理**：成功或 `abort` 时删除本次 `/tmp/dw_chunks/<upload_id>` 与合并临时文件；进入合并/落盘阶段后的硬失败（磁盘满、I/O 异常等）同样删除，避免大文件残留。校验类失败（缺片、`total_chunks` 越界、超出 `max_upload_mb`、权限不足、409 冲突）**保留会话**，允许补齐分片或调整参数后重试/续传。服务启动时清理所有历史遗留会话（容器重启即释放）；后台任务每 10 分钟清理**超过 `upload.session_idle_ttl_seconds`（默认 24h，且不小于请求超时）没有分片写入**的会话（浏览器被杀/断网、无法发送 abort 时的兜底）。

> **类型冲突处理（0.10.0 修复 OBJ-005）**：若目标 key 已是**目录**，返回 `409`（不再 500）；失败会清理本次会话，不落盘、不残留分片。

### 3.4 取消分片上传

```
POST /api/objects/abort
```

**鉴权**：权限点 `upload:abort`

**请求**：`upload_id`（multipart 表单）。

---

## 四、系统接口

### 4.1 健康检查

```
GET /health
```

**鉴权**：公开

返回 `{ "status": "ok", "depot_dir": "...", "exists": true }`。

### 4.1.1 存活/就绪

- `GET /health/live`：进程存活即 200 `{ "status": "alive" }`；
- `GET /health/ready`：depot 目录存在且可写才 200 `{ "status": "ready", "depot_dir": "..." }`，否则 `503`；
- `GET /metrics`：Prometheus 文本格式（`metrics_enabled=false` 时 404）。

### 4.1.2 手动重新加载配置

```
POST /api/config/reload
```

**鉴权**：仅管理员共享 token。

**说明**：重新读取 `config/config.json` 与 `config/.env`。

- **成功**：HTTP `200`，`data = {ok: true, error: "", changed: [...], needs_restart: [...], warnings: [...]}`。`changed` 已即时生效；`needs_restart` 需重启才生效（当前保留旧值）。
- **失败**（配置文件语法错误等）：HTTP **`400`**，响应体为 `{"detail": "<可读原因>"}`；**不会**返回 `200 + data.ok=false`。旧配置及其行为保持不变。

### 4.2 列出所有 bucket

```
GET /api/buckets
```

**鉴权**：

- **不带 token**：公开（默认内网开放）；`require_read_token=true` 时返回 `401`。
- **带 token**：必须**有效**且具备权限点 `bucket:list`（无效 → `401`，缺失 → `403`）。
- **范围过滤**：带资源范围的 token 只返回其 `allow_buckets` 内的 bucket（范围外的 bucket 不会出现在结果中）；`allow_buckets` 为空时不限制、返回全部。

**说明**：返回 `data` 为 bucket 名字符串数组。

### 4.3 创建 bucket —— 0.6.0 新增

```
POST /api/buckets
```

**鉴权**：权限点 `bucket:create`

**请求体**（JSON）：

```json
{ "bucket": "voicevideo" }
```

**说明**：显式创建空 bucket（内部放 `.keep` 占位）。bucket 命名约束见 1.5；已存在时幂等返回 HTTP 200，`data = { "bucket": "...", "created": false }`，新建成功返回 `created: true`。

```bash
curl -X POST "http://<IP>:8004/api/buckets?token=<token>" \
  -H "Content-Type: application/json" -d '{"bucket":"voicevideo"}'
```


### 4.4 校验 token

```
GET /api/auth/check?token=<token>
```

**鉴权**：公开（带 token 返回其身份与权限）

返回 `data`（**成功**）：`{ "valid": true, "actor": "系统/工具" 或 用户名, "role", "is_admin", "scopes", "allow_buckets", "allow_prefixes", "expires_at", "description" }`。

**失败**（不存在 / 无效 / 过期 / 禁用）返回 `valid: false`，并**额外**带 `expired`、`enabled` 两个字段，便于前端区分「过期」与「无效」。注意：**成功分支不返回** `expired` / `enabled`。

---

## 五、Token 管理接口（按权限点授权）

以下接口分别需要权限点 `token:read` / `token:write` / `token:delete` / `token:sync`（管理员共享 `access_token` 放行全部）。token 通过 `?token=` 或 `Authorization: Bearer` 传递。

> **防提权（本版硬化）**：非共享 token（普通用户 token）调用下列写接口创建 / 更新 token 时，请求的 `scopes`、`allow_buckets`、`allow_prefixes` 必须**不超出自身授权**，否则返回 `403`：
>
> - `scopes` ⊆ 自身 scopes（`role` 预设先展开成 scopes 再校验）；
> - `allow_buckets` ⊆ 自身 `allow_buckets`（自身为空 = 不限，则允许任意）；受限 token 把范围**清空 / 放宽为「全部 bucket」**同样 `403`；
> - `allow_prefixes` 只能比自身更窄：自身在某 bucket 上有限制时，新 token 覆盖该 bucket 必须显式给出更窄前缀，不能靠「不写前缀」绕过。
>
> `PUT /api/tokens/{value}` 还会校验**目标 token 的现有授权**也不超出自身，防止借更新他人 token 扩权。共享 `access_token` 不受上述限制。

### 5.1 查看 token 注册表

```
GET /api/tokens
```

返回 `data` 为**对象**：键是**脱敏后的 token**（`前缀****后缀-短哈希`，保证唯一），值是完整记录
`{user, role, scopes, allow_buckets, allow_prefixes, expires_at, expired, enabled, description, created_at, created_by, last_used_at, token}`。
非共享 token 调用时 `token` 字段仍是脱敏值；只有**共享 `access_token`** 且 `?reveal=true` 时才返回明文键与明文 `token`。

### 5.2 登记 / 更新 token（POST / PUT）

```
POST /api/tokens
```

**请求体**（JSON）：完整字段见 12.4（`user` / `token` / `role` / `scopes` / `allow_buckets` / `allow_prefixes` / `expires_at` / `description`）。仅传 `token`+`user` 为兼容旧协议的登记，只更新用户名、保留已有权限。

**覆写既有 token = 更新（合并语义）**：显式传入的 `token` 若已在注册表中，按 `PUT` 的合并规则处理——**未传字段一律保留**。因此不会因为「重新登记一次」就把 `allow_buckets` 清空成「全部 bucket」、把 `expires_at` 清成「永久」、或把已禁用 token 复活；审计记为 `token_update` 而非 `token_create`。要改这些字段请显式传值（或走 `PUT`）。

**防提权**：非共享 token 调用时，请求的 scopes / 资源范围必须 ⊆ 自身授权，否则 `403`（规则见本章开头）。

**更新已有 token：`PUT /api/tokens/{value}`**

```
PUT /api/tokens/{value}
```

- 路径参数 `value` 为要更新的 token（不存在 → `404`）。
- 请求体（JSON，字段**均可选**）：`user` / `role` / `scopes` / `allow_buckets` / `allow_prefixes` / `expires_at` / `description` / `enabled`。
- **不传的字段沿用原值**（含 `enabled`、`last_used_at`）；传 `role` 而未传 `scopes` 时按角色预设重置 scopes。
- 写审计 `token_update`。
- **防提权（双向）**：先校验**目标 token 现有授权** ⊆ 自身，再校验**更新后授权** ⊆ 自身；任一越界 → `403`。因此受限 token 既不能给自己扩权，也不能修改/降级比自己更大的 token。
- `allow_prefixes` 的规范化与「前缀必须属于 `allow_buckets`」校验同 12.4，违规 `400`。

### 5.3 移除 token

```
DELETE /api/tokens?value=<token>
```

### 5.4 从 DataHub 同步用户 token

```
POST /api/tokens/sync
```

从 DataHub `users.json` 拉取并合并用户 token（DataHub 是权威源，本地为副本）。

**响应** `data`：`{ "mapping": {...}, "ok": true, "error": "", "datahub_url": "..." }`。
`ok=false` 时表示 DataHub 不可达或解析失败，前端应提示，避免误判同步成功。

---

## 六、审计查询

```
GET /api/audit
```

**鉴权**：权限点 `audit:read`（无效 token → `401`，缺失 → `403`）

**范围过滤（本版硬化）**：**配置了资源范围**的非共享 token（有 `allow_buckets` 或 `allow_prefixes`）只返回其范围内、且**归属到具体 bucket** 的审计记录；过滤在收集阶段完成，`limit` 语义不变（最多返回 `limit` 条**命中**记录）。

> bucket 为空的**全局事件**（`token_create` / `token_update` / `token_delete` / `config_reload` 等）无法归属到任何范围，且会泄露其他 token 的 `user`/`role`/`scopes`，因此**对受限 token 不返回**；未配置范围的普通 token 与共享 `access_token` 行为不变（可见全量）。

> 另：`POST` / `PUT` / `DELETE /api/tokens` 均写审计（`token_create` / `token_update` / `token_delete`）。
> 受限 token 看到的 `rename` 记录同样按范围裁剪：若目标路径（`to_key`）落在其范围外，该记录整行不返回，避免泄露范围外路径。

**查询参数**：

| 参数 | 说明 |
|---|---|
| `bucket` | 按 bucket 过滤 |
| `key` | 按 key 精确过滤 |
| `actor` | 按操作者过滤 |
| `since` | 只取 >= 此时间（YYYY-MM-DD 或完整时间戳） |
| `limit` | 最多返回条数，默认 500；`0` 表示不返回，负数返回 422 |

---

## 七、网页 UI

```
GET /
```

**鉴权**：公开（可设 `ui_enabled=false` 禁用）。

单页控制台：目录浏览 + 上传（拖拽/分片）+ 下载 + **一键下载目录** + 新建目录 + 批量删除（一键清空目录内容）+ 签名链接管理 + Token 管理 + 审计。

- 目录浏览页在面包屑卡片提供 **「下载本目录」**（`data-action="download-dir"`，只读、无确认弹窗）：读取页面令牌框，为空则提示先填写；点击后用隐藏 `<a>` 触发浏览器原生下载 `GET /api/objects/download-bulk?bucket=&prefix=&token=`（交给浏览器下载管理，不把 ZIP 读进内存）；仅选中 bucket 后可用，**在 bucket 根时即下载整个 bucket**；目录内**只有 1 个可见文件时直接下载该文件**，多文件或含子目录时自动打包 ZIP（toast 提示）；
- 目录浏览页在面包屑卡片提供两个危险操作按钮：**「清空本目录文件」**（`mode=files`，只删当前目录的直接文件）与**「删除本目录全部内容」**（`mode=all`，递归删除文件与子目录）；点击后弹窗先调用 `dry_run=true` 预览「文件数 / 目录数 / 总大小」，并提示删除不可恢复、`files` 模式会保留子目录及其内容；
- 执行前必须在弹窗输入框输入**目录名**（bucket 根目录输入 bucket 名），输入匹配前确认按钮禁用；服务端会二次校验该名称（见 2.12）。成功后关闭弹窗、刷新列表并 toast 汇总删除数量；`errors` 非空时用红色 toast 提示「部分失败」；
- 标签栏右侧提供 **Swagger**（`/docs`）/ **ReDoc**（`/redoc`）入口，由 FastAPI 依据实际路由自动生成；前端资源（`/static/swagger-ui-bundle.js` 等）**自托管**，内网/离线可用；
- 内网/离线环境也可用公开只读接口 `GET /api/docs` 查看原始 Markdown；许可证（`/api/docs/license`、`/api/docs/license_zh`）同样通过该接口提供，满足 Apache-2.0 第 4(a) 条「向接收者提供许可证副本」的要求。

### 7.1 文档接口（0.7.1 新增，公开）

```
GET /api/docs              # 文档清单：[{name, title, available}]
GET /api/docs/{name}       # 文档内容（JSON：{name,title,content}）
GET /api/docs/{name}?raw=1 # 直接返回 text/plain 原文
```

`name` 白名单（不可任意路径读取）：`api_rules`（本文档）、`architecture`、`readme`、`changelog`、
`license`（Apache-2.0 英文版）、`license_zh`（Apache-2.0 中文版）、`license_header`（源文件署名模板）。
文档按**项目根**解析（开发态 `<repo>/docs/...`，容器内 `/app/artifactdepot/docs/...`）；
镜像未包含文档时 `available=false`、获取返回 `404` 并带可读原因。

---

## 八、接口速查总表

| 方法 | 路径 | 权限点（scope） | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | 健康检查 |
| GET | `/health/live` | 公开 | 存活探针 |
| GET | `/health/ready` | 公开 | 就绪探针（depot 不可写 → 503） |
| GET | `/metrics` | 公开 | Prometheus 指标（`metrics_enabled=false` → 404） |
| POST | `/api/config/reload` | 仅共享 token | 手动重新加载配置 |
| GET | `/api/auth/check` | 公开 | 校验 token |
| GET | `/api/auth/permissions` | 公开 | 权限点目录 |
| GET | `/api/docs` | 公开 | 文档清单 |
| GET | `/api/docs/{name}` | 公开 | 文档内容（`?raw=1` 返回 text/plain） |
| GET | `/docs` | 公开 | Swagger UI（自托管，`include_in_schema=false`） |
| GET | `/redoc` | 公开 | ReDoc（自托管） |
| GET | `/openapi.json` | 公开 | OpenAPI 规范 |
| GET | `/api/objects/signed-links/config` | 公开 | 签名链接上下限 |
| GET | `/` | 公开 | 网页 UI |
| GET | `/api/buckets` | 公开 · `bucket:list` | 列出 bucket |
| GET | `/api/objects/list` | 公开 · `object:list` | 列对象/目录 |
| POST | `/api/buckets` | `bucket:create` | 创建 bucket |
| POST | `/api/objects` | `object:upload` | 上传对象 |
| POST | `/api/objects/mkdir` | `object:mkdir` | 建目录 |
| POST | `/api/objects/rename` | `object:rename` | 目录/对象重命名·移动 |
| GET | `/api/objects/head` | `object:head` | 元信息探测 |
| POST | `/api/objects/presign` | `link:create` | 生成共享链接 |
| GET | `/api/objects/download` | `object:download` / 签名 | 下载（Range） |
| GET | `/api/objects/download-bulk` | `object:download` | 一键下载目录（单文件直下 / 多文件或含子目录流式 ZIP） |
| DELETE | `/api/objects` | `object:delete` | 删除对象/空目录 |
| POST | `/api/objects/bulk-delete` | `object:delete_bulk` | 批量删除目录内容（保留目录本身，需输入名称确认） |
| GET | `/api/objects/signed-links` | `link:list` | 列签名链接 |
| POST | `/api/objects/signed-links/{link_id}/revoke` | `link:revoke` | 作废签名链接 |
| POST | `/api/objects/initiate` | `upload:initiate` | 发起分片上传 |
| POST | `/api/objects/chunk` | `upload:chunk` | 上传分片 |
| POST | `/api/objects/complete` | `upload:complete` | 合并分片 |
| POST | `/api/objects/abort` | `upload:abort` | 取消分片 |
| GET | `/api/tokens` | `token:read` | 查看 token 注册表 |
| POST | `/api/tokens` | `token:write` | 生成/登记 token |
| PUT | `/api/tokens/{value}` | `token:write` | 更新 token 权限 |
| DELETE | `/api/tokens` | `token:delete` | 移除 token |
| POST | `/api/tokens/sync` | `token:sync` | 从 DataHub 同步 |
| GET | `/api/audit` | `audit:read` | 审计查询 |

---

## 九、配置项与调用相关

ArtifactDepot 配置优先级：内置默认 < `config/config.json`（或 `ARTIFACT_DEPOT_CONFIG` 指定文件）< `config/.env` < 真实环境变量。

> 隐私项放 `config/.env`（字段名=值，如 `access_token=...`），不进 git/镜像；`.env` 空值视为未设置。
> 手动重载：`POST /api/config/reload`（仅管理员），或网页「Token 管理」页的「重新加载配置」按钮。

影响接口调用的关键配置：

| 配置 | 默认 | 说明 |
|---|---|---|
| `port` | 8004 | 服务端口（环境变量 `ARTIFACT_DEPOT_PORT`） |
| `access_token` | — | 管理员共享令牌，生产必须修改（`ARTIFACT_DEPOT_ACCESS_TOKEN`） |
| `max_upload_mb` | 0（不限） | 单文件上传上限 |
| `upload.chunk_size_mb` | 8 | 分片大小（1–64MB），服务端下发给浏览器/CLI |
| `upload.chunk_concurrency` | 4 | 浏览器每个文件同时上传的分片数（1–16） |
| `upload.file_concurrency` | 3 | 浏览器同时上传的文件数（1–10，网页下拉框默认值） |
| `upload.request_timeout_seconds` | 86400（24h） | 分片/合并请求超时（浏览器 XHR、CLI） |
| `upload.max_chunk_mb` | 64 | 单片硬上限（1–64MB，代码常量兜底） |
| `upload.max_chunks` | 100000 | 单文件最大分片数（上限 100000） |
| `upload.max_session_gb` | 200 | 会话总量上限（`max_upload_mb=0` 时生效，上限 200GB） |
| `upload.session_ttl_seconds` | 86400（24h） | 分片会话绝对有效期 |
| `upload.session_idle_ttl_seconds` | 86400（24h） | 无分片写入的空闲清理阈值（默认不小于请求超时，浏览器被杀兜底） |
| `datahub_url` | `http://127.0.0.1:8002/api/data` | 用户 token 权威源 |
| `ui_enabled` | true | 是否启用网页 UI |
| `require_read_token` | false | 读接口（`GET /api/objects/list`、`GET /api/buckets`）是否强制要求携带有效 token 且具备对应 scope；`true` 时无 token → `401` |
| `metrics_enabled` | true | 是否暴露 `GET /metrics`（Prometheus 文本格式）；`false` 时该路由返回 `404` |
| `trusted_proxies` | `[]` | 可信反代 IP/CIDR；非空才从 XFF 解析审计 public_ip |
| `audit_max_mb` | 50 | 审计日志单文件上限（MB），超过轮转 |
| `audit_backups` | 5 | 审计日志保留历史文件数 |
| `cors_origins` | `[]` | CORS 来源白名单；空=不启用 |
| `allowed_hosts` | `["*"]` | TrustedHost 白名单 |
| `signed_links.*` | 见 config | 签名链接次数/时效上下限 |

---

## 十、安全注意事项

1. 读接口（list/buckets）为「内网开放」设计，切勿直接暴露公网，建议前置 Nginx 鉴权或防火墙/VPN 隔离。
2. 管理员 `access_token` 与用户 token 属敏感凭据，不得写入 git / 文档 / 日志；**推荐用 `Authorization: Bearer` 传递**，避免 `?token=` 落访问日志/浏览器历史。服务端已对 access log 中的 `token`/`tk`/`sig` 脱敏。
3. key/bucket 有路径穿越防护，但调用方仍应避免传不可信输入。
4. 删除接口需要权限点 `object:delete`（管理员共享 token 默认拥有全部），且目录仅空可删。
5. `access_token` 默认空（无管理员）；启动时会对空/弱 token 告警，设 `ARTIFACT_DEPOT_STRICT_CONFIG=1` 可直接拒绝启动。生产请用 >=16 位随机串。
6. 分片上传的 `upload_id` 由服务端生成并严格校验字符集，会话绑定创建者；客户端不得自行构造/猜测 `upload_id`。
7. 网页 UI 使用事件委托（`data-*` 属性）渲染文件名等不可信数据，不再把用户输入拼进内联 `onclick`。
8. 审计的 `public_ip` 只在 `config.trusted_proxies` 命中直连代理时才由 `X-Forwarded-For` 解析；未配置时为空，客户端自报的 `public_ip` 参数一律忽略。
9. 状态文件（`tokens.json`/`signed_links.json`/`.warehouse.json`）采用「临时文件 + `os.replace`」原子写，并用进程内锁 + `fcntl.flock` 串行化读-改-写；按次数签名链接不会并发超用。
10. 分片上传有单分片上限（默认 64MB，可由 `upload.max_chunk_mb` 调小）与会话总量上限（`max_upload_mb`；未配置时 `upload.max_session_gb` 默认 200GB），防止把临时目录写满。
11. Swagger UI / ReDoc 前端资源自托管于 `/static/`，不再依赖外部 CDN；`/docs`、`/redoc` 仅暴露 OpenAPI，不额外放行管理接口。
12. 默认不加 CORS / TrustedHost 限制（内网部署）；跨源或域名访问请按 `cors_origins` / `allowed_hosts` 收紧。
13. 网页 UI 脚本/样式已外置到 `/static/app.js`、`/static/app.css`，应用页 CSP 为 `script-src 'self'`（不允许内联脚本）；Swagger/ReDoc 因自带内联初始化脚本，单独放行 `'unsafe-inline'`。
14. 批量删除（`POST /api/objects/bulk-delete`）在 `object:delete_bulk` 权限点与 bucket/前缀范围之外，还要求：**执行必须输入名称确认**（目录名；根目录为 bucket 名）并由服务端二次校验；**只清理目录内容、目录本身保留**；**bucket 根 `.warehouse.json` 永不删除**；单次最多处理 50000 项（`dry_run` 预览同样受限）；`dry_run=true` 只预览、不落盘、不写审计。前端禁用确认按钮只是体验，不能替代服务端校验。
15. 一键下载（`GET /api/objects/download-bulk`）：复用 `object:download` 权限点与 bucket/前缀范围；**只打包可见内容**——隐藏项（`.keep`、`.warehouse.json` 等）与符号链接一律跳过、不跟随；`prefix` 解析（含符号链接）后必须仍在本 bucket 内（否则 `400`，防跨 bucket 读取）；ZIP 条目名来自真实相对路径，中文名走 RFC 5987 `filename*`；流式生成不落临时文件，单次最多 100000 项。

## 十一、其他项目接入指南（bucket / 目录 / 文件 push-pull）

面向需要与 ArtifactDepot 集成的业务项目，按功能给出标准调用序列。客户端建议封装成轻量 SDK，统一携带 `Authorization: Bearer <token>`、统一解析 1.2 响应结构。

### 11.1 创建 bucket

```
POST /api/buckets          # 幂等创建（推荐）
```

```bash
curl -X POST "http://<IP>:8004/api/buckets" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"bucket":"myproject"}'
```

> 即使不显式调用，首次 mkdir/上传也会自动建 bucket；但显式创建可提前发现命名非法（400）并明确归属。

### 11.2 创建目录

```
POST /api/objects/mkdir
```

```bash
curl -X POST "http://<IP>:8004/api/objects/mkdir" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"bucket":"myproject","key":"task/20260912/run-01"}'
```

- 多级路径一次建全；已存在则幂等成功。
- 创建后可用 `GET /api/objects/list?bucket=myproject&prefix=task/20260912` 验证（is_dir=true）。

### 11.3 更改目录名（重命名 / 移动）

```
POST /api/objects/rename
```

```bash
curl -X POST "http://<IP>:8004/api/objects/rename" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"bucket":"myproject","key":"task/20260912/run-01","new_key":"task/20260912/run-01-done"}'
```

- 整体迁移目录下全部对象，失败回滚；`overwrite=false`（默认）时目标冲突返回 409。
- 跨目录移动同样使用本接口（移动 = 修改目录前缀）。

### 11.4 Push：向指定 bucket 的指定目录上传文件

key = `<目录>/<文件名>`，目录由 key 隐含表达，无需先 mkdir（但建议先建，便于目录被浏览）。

**常规文件 —— 单请求上传：**

```bash
curl -X POST "http://<IP>:8004/api/objects" \
  -H "Authorization: Bearer <token>" \
  -F "file=@result.mp4" \
  -F "bucket=myproject" \
  -F "key=task/20260912/run-01/result.mp4" \
  -F "overwrite=true"
```

- 响应返回 `sha256`，push 侧应保存用于校验。
- `overwrite=false` 收到 409 表示已存在同名对象，可先 `GET /api/objects/head` 比对 sha256 决定跳过或覆盖。

**大文件 —— 分片上传（三步）：**

```
POST /api/objects/initiate                → upload_id, chunk_size(默认8MB，可由 upload.chunk_size_mb 调整)
POST /api/objects/chunk   × N（upload_id, index, chunk）
POST /api/objects/complete（upload_id, bucket, key, total_chunks）
POST /api/objects/abort   （失败/取消时清理）
```

### 11.5 Pull：从指定 bucket 的指定目录下载文件

**方式 A：项目持有 token，直接下载（最简）：**

```bash
curl -o result.mp4 "http://<IP>:8004/api/objects/download?bucket=myproject&key=task/20260912/run-01/result.mp4" \
  -H "Authorization: Bearer <token>"
```

- 下载完成后用 push 时记录的 `sha256` 做完整性校验。
- 支持 `Range` 头断点续传（返回 206）。

**方式 B：接收方无 token 时，先 presign 再分发链接：**

```
POST /api/objects/presign（bucket, key, mode=count/time/permanent）
→ 返回 data.url，接收方直接 GET 该 url 即可，无需 token
```

**Pull 整个目录**：可用 `GET /api/objects/download-bulk?bucket=..&prefix=..` 一键下载（单文件直下 / 多文件或含子目录自动打包 ZIP，见 2.13）；也可先 `GET /api/objects/list?bucket=..&prefix=..` 枚举 `is_dir=false` 的 items，再逐个（或并发）download。

### 11.6 推荐的完整工作流

```
1. POST /api/buckets                 建 bucket（幂等）
2. POST /api/objects/mkdir           建任务目录
3. GET  /api/objects/head            检查同名对象是否已存在
4. POST /api/objects（或分片三步）    push 文件，保存 sha256
5. （可选）POST /api/objects/rename  目录状态流转，如 run-01 → run-01-done
6. GET  /api/objects/download        pull 文件，按 sha256 校验
7. DELETE /api/objects（管理员）      清理过期目录（须先清空，空目录才可删）
```


---

## 十二、权限点与自定义 Token 生成（0.7.0 新增）

### 12.1 权限目录接口（公开）

```
GET /api/auth/permissions
```

返回四部分，供「逐接口勾选 → 生成 token」的前端渲染：

- `permissions`：受保护接口清单，每项 `{key, group, label, method, path, write}`；
- `public`：公开接口清单（不可勾选）；
- `roles`：角色预设 `{key, label, scopes}`；`default_role`；`require_read_token`；
- `upload`：上传参数（`chunk_size` / `chunk_concurrency` / `file_concurrency` /
  `request_timeout_seconds` / `max_chunk_size` / `max_chunks` / `max_session_gb` / `max_upload_mb`），
  网页启动时读取（见 §9 `upload.*`）。

### 12.2 权限点总表

| 权限点 | 接口 | 说明 |
|---|---|---|
| `bucket:list` | GET `/api/buckets` | 列 bucket（默认公开；`require_read_token=true` 时校验） |
| `bucket:create` | POST `/api/buckets` | 创建 bucket |
| `object:list` | GET `/api/objects/list` | 列对象/目录（默认公开） |
| `object:head` | GET `/api/objects/head` | 探测对象元信息（由“写”改为“读”） |
| `object:download` | GET `/api/objects/download` | 下载（签名链接免） |
| `object:upload` | POST `/api/objects` | 单请求上传 |
| `upload:initiate` | POST `/api/objects/initiate` | 分片-发起会话 |
| `upload:chunk` | POST `/api/objects/chunk` | 分片-上传分片 |
| `upload:complete` | POST `/api/objects/complete` | 分片-合并完成（校验 bucket/key 范围） |
| `upload:abort` | POST `/api/objects/abort` | 分片-取消 |
| `object:mkdir` | POST `/api/objects/mkdir` | 新建目录 |
| `object:rename` | POST `/api/objects/rename` | 重命名/移动（源与目标都校验） |
| `object:delete` | DELETE `/api/objects` | 删除对象/空目录 |
| `object:delete_bulk` | POST `/api/objects/bulk-delete` | 批量删除目录内容（保留目录本身，需输入名称确认） |
| `link:create` | POST `/api/objects/presign` | 生成签名链接 |
| `link:list` | GET `/api/objects/signed-links` | 查看签名链接列表 |
| `link:revoke` | POST `/api/objects/signed-links/{link_id}/revoke` | 作废（本人；管理员任意） |
| `token:read` | GET `/api/tokens` | 查看 token 注册表 |
| `token:write` | POST `/api/tokens`、PUT `/api/tokens/{value}` | 生成/登记/更新 token |
| `token:delete` | DELETE `/api/tokens` | 移除 token |
| `token:sync` | POST `/api/tokens/sync` | 从 DataHub 同步 |
| `audit:read` | GET `/api/audit` | 查询审计 |

### 12.3 角色预设

| role | 权限点 |
|---|---|
| `viewer` | bucket:list, object:list |
| `downloader` | viewer + object:head, object:download |
| `uploader` | viewer + object:head, object:upload, upload:initiate/chunk/complete/abort, object:mkdir |
| `publisher` | uploader + object:rename, link:create |
| `operator` | publisher + bucket:create, object:delete, object:delete_bulk, link:list, link:revoke |
| `auditor` | bucket:list, object:list, audit:read, token:read |
| `user` | 历史兼容：上传/下载/签名/建目录/建 bucket/改名 + 查看/作废自己的签名链接（`link:list` / `link:revoke`），无删除、无 token 管理、无审计 |
| `custom` | 完全按勾选 |
| `admin` | 全部权限点 |

### 12.4 生成 Token（逐接口勾选）

```
POST /api/tokens
```

**鉴权**：`token:write`

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user` | string | 是 | 操作者名（审计 actor） |
| `token` | string | 否 | 留空由服务端生成（`secrets.token_urlsafe(32)`） |
| `role` | string | 否 | 角色预设，默认 `user` |
| `scopes` | string[] | 否 | **逐接口勾选结果**；传入则以此为准。显式空数组 = **无任何权限**（0.7.1 修复：此前会被回退成角色预设） |
| `allow_buckets` | string[] | 否 | bucket 白名单，空 = 全部（网页「Token 管理」页可从现有 bucket 多选，也支持手填未创建的 bucket） |
| `allow_prefixes` | object | 否 | `{"bucket": ["前缀/"]}`，路径边界安全匹配 |
| `expires_at` | string | 否 | `YYYY-MM-DD` 或 ISO 时间；空 = 永久 |
| `description` | string | 否 | 备注 |

> `enabled`（启用 / 禁用）**不在本接口请求体中**，只能通过 `PUT /api/tokens/{value}` 修改（POST 传了会被忽略）。

**资源范围语义**：`allow_buckets` 与 `allow_prefixes` 均为**硬边界**（见 1.4）。两者都为空表示该 token **可访问全部 bucket**；填了 `allow_prefixes` 时，按 bucket 内的路径前缀收窄。

> **只填 `allow_prefixes` 不合法**：必须同时把这些 bucket 列入 `allow_buckets`，否则未点名的 bucket 不受任何限制（静默放大权限），API 返回 **`400`**。

> `allow_prefixes` 会被规范化：URL-decode、去首尾斜杠、去重；**拒绝**空段 / `.` / `..` / 反斜杠 / `null`，以及 normalize 后为空（如 `"/"`）的写法，一律 `400`。否则会造出「能创建但永不匹配」的死 token，或把「只允许根」静默变成「不限」。

**网页前端校验**（Token 管理页「生成本轮 Token」）：

- `allow_prefixes` 里出现的 bucket 必须已在 `allow_buckets` 中，否则**阻止提交**并提示（该规则后端同样强制，违反返回 `400`）；
- `allow_buckets` 与 `allow_prefixes` 同时为空时，弹出**二次确认**：「该 token 可访问全部 bucket」；
- 生成结果区展示「生效范围」，Token 注册表「资源范围」列为空时显示 **`⚠️ 全部 bucket`**。

**防提权**：非共享 token 调用本接口时，请求的 `scopes` / `allow_buckets` / `allow_prefixes` 不得超出自身授权，否则 `403`（规则见第五章）；`role` 预设会先展开成 scopes 再校验。若 `token` **已存在**（即覆写既有 token），还会先校验**目标现有授权**不得超出自身，否则 `403`——防止降级他人更大的 token，或借覆写**复活已禁用 / 已过期的 token**。

**响应**：`data` 为 token 记录（含 `scopes` / `allow_buckets` / `allow_prefixes` 等生效范围字段），**仅本次返回明文 `token`**，请立即保存。

```bash
# 只给“往 voicevideo/carryvideo/2026/ 上传 + 列表”的 token
curl -X POST "http://<IP>:8004/api/tokens?token=<admin>" \
  -H "Content-Type: application/json" \
  -d '{
    "user": "处理端-8",
    "role": "custom",
    "scopes": ["object:upload","upload:initiate","upload:chunk","upload:complete","upload:abort","object:list","object:head"],
    "allow_buckets": ["voicevideo"],
    "allow_prefixes": {"voicevideo": ["carryvideo/2026/"]},
    "description": "2026 批次处理端推送"
  }'
```

### 12.5 Token 管理补充

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/auth/permissions` | 公开 | 权限目录 |
| GET | `/api/docs` | 公开 | 文档清单 |
| GET | `/api/docs/{name}` | 公开 | 文档内容（`?raw=1` 返回纯文本） |
| GET | `/api/auth/check` | 公开 | 返回 `valid/actor/role/scopes/allow_buckets/expires_at` |
| GET | `/api/tokens` | `token:read` | 默认脱敏；管理员可 `?reveal=true` 看明文 |
| POST | `/api/tokens` | `token:write` | 生成/登记（支持逐接口勾选） |
| PUT | `/api/tokens/{value}` | `token:write` | 更新权限/范围/过期/禁用 |
| DELETE | `/api/tokens` | `token:delete` | 移除 |
| POST | `/api/tokens/sync` | `token:sync` | 从 DataHub 同步（权限字段不被覆盖） |
| POST | `/api/objects/*` 分片四步 | `upload:initiate` / `upload:chunk` / `upload:complete` / `upload:abort` | 大文件分片上传 |

### 12.6 兼容性

- 旧 `tokens.json`（`{"<token>":"用户名"}`）自动迁移为 v2 结构并解释为 `role=user`；
- 协作平台旧协议 `POST /api/tokens {token,user}`（不带 role/scopes/范围）只更新用户名，**保留已有权限配置**；
- DataHub 同步只新增/更新用户身份，不覆盖已配置的 role/scopes/资源范围；
- 共享 `access_token` 行为不变（超管）。
