# ArtifactDepot 接口文档

> 版本 0.6.0 · FastAPI 服务 · 默认端口 8004
>
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

除下载文件、网页 UI 与 `/health` 外，所有接口返回 JSON，统一结构：

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

token 的传递方式（两种等价，二选一）：

1. **URL query**：`?token=<令牌>`
2. **请求头**：`Authorization: Bearer <令牌>`

> 注意：上传接口（`POST /api/objects`）额外支持把 token 作为 **multipart 表单字段** `token=` 传递（见 2.1）。

token 校验规则：

- 管理员 token 用 **常量时间比较**（`hmac.compare_digest`）与 `access_token` 比对。
- 用户 token 在 `tokens.json` 注册表命中；本地未命中时先从 DataHub 拉一次再判定（惰性同步）。

### 1.4 权限等级

| 等级 | 说明 | 适用接口 |
|---|---|---|
| 公开（无鉴权） | 知道地址即可调，用于内网 | `/health`、`/api/buckets`、`/api/objects/list`、`/`、`/api/auth/check`、`/api/objects/signed-links/config` |
| 写 token | 管理员或已登记用户 token | 上传、mkdir、rename（目录改名/移动）、head、presign、分片上传、签名链接管理 |
| 下载 | token 或签名链接（三选一） | `/api/objects/download` |
| 仅管理员 | 仅共享 `access_token` | `/api/tokens`、`/api/audit`、删除对象 |

> 安全提示：`list` / `buckets` 等读接口为「内网开放」设计，依赖网络层隔离兜底；**不宜将 8004 直接暴露公网**。

### 1.5 bucket 与 key 约束

**bucket**（`validate_bucket`）：

- 非空，长度 ≤ 63 字符。
- 不允许以 `.` 开头，不允许包含 `/`、`\` 及控制字符。
- 允许中文等 Unicode。
- **无需预创建**：首次上传或 mkdir 时自动创建目录。

**key**（`validate_key`，相对路径式，会被 URL-decode 后去首尾斜杠）：

- 不允许含 `\`；前后 `/` 会被自动去除，去除后不能为空。
- 不允许出现空段、`.`、`..`（防路径穿越）。
- 任意路径段都不允许以 `.` 开头（避免与 `.depot.json`、`.keep` 等隐藏约定冲突）。
- 支持多级目录，如 `carryvideo/20260815/tranvideo-1_20260815/tranvideo-1_20260815.mp4`。
- 对象操作接口（上传 / 下载 / 删除 / presign）要求 key 非空；列表接口的 `prefix` 可以为空（表示根目录）。

### 1.6 错误码

| HTTP 状态码 | 含义 |
|---|---|
| 400 | 参数非法（bucket/key 非法、prefix 越界等） |
| 401 | 缺少/无效令牌、token 无效、签名无效或过期 |
| 403 | 已认证但权限不足（如普通用户调管理员接口、非创建者作废他人链接） |
| 404 | 对象/目录不存在 |
| 409 | 对象已存在（`overwrite=false` 时） |
| 413 | 超过单文件上传上限 `max_upload_mb` |
| 422 | 请求参数校验失败（字段缺失、类型错误等），`detail` 为数组 |
| 500 | 服务端异常（如未配置 access_token） |

---

## 二、对象接口（`/api/objects`）

### 2.1 上传对象（PutObject）

```
POST /api/objects
```

**鉴权**：写 token（管理员或用户）

**请求**（`multipart/form-data`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | 文件 | 是 | 上传的文件内容 |
| `bucket` | string | 是 | 目标 bucket |
| `key` | string | 是 | 对象 key（相对路径式，可多级目录） |
| `token` | string | 条件 | 访问令牌（也可走 query `?token=` 或 `Authorization: Bearer`） |
| `source_url` | string | 否 | 来源 URL，记入元数据 |
| `overwrite` | bool | 否 | 是否覆盖同名对象，默认 `true` |
| `public_ip` | string | 否 | 前端上报的公网 IP，表单字段或 query，记入审计 |

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

**鉴权**：公开（无鉴权）

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | 目标 bucket |
| `prefix` | string | 否 | 限定前缀，列出该目录下的直接子项 |

**成功响应**（200）的 `data.items` 元素：

- 目录：`{ "name", "key"（末尾带 /）, "is_dir": true, "size", "mtime" }`；空目录的 `mtime` 为目录自身 mtime。
- 文件：`{ "name", "key", "is_dir": false, "size", "mtime", "sha256", "source_url", "uploader" }`。
- 手工放入、没有 `.depot.json` 记录的文件也能列出，但 `sha256/source_url/uploader` 为空，`mtime` 回退为文件系统 mtime。

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
3. `token`（管理员或用户）—— 必需

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
| `public_ip` | string | 否 | 前端上报的公网 IP，记入审计 |

**响应**：文件流，支持 **Range（206）** 视频拖动，`Content-Type` 按扩展名推断。

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

**鉴权**：仅管理员（共享 `access_token`）

**查询参数**：`bucket`（必填）、`key`（必填）、`public_ip`（可选，记入审计）。

**说明**：删除文件或目录；目录仅空目录可删（防误删）。仅管理员 token 可调用；普通用户 token 返回 `403`。

```bash
curl -X DELETE "http://<IP>:8004/api/objects?bucket=voicevideo&key=path/to/obj&token=<admin_token>"
```

### 2.5 新建目录（Mkdir）

```
POST /api/objects/mkdir
```

**鉴权**：写 token

**请求体**（JSON）：

```json
{ "bucket": "voicevideo", "key": "story/20260912" }
```

**说明**：按 key 建目录树（`/` 多级自动创建），放隐藏 `.keep` 占位；bucket 不存在时随目录一并创建。可选 query `public_ip` 记入审计。

```bash
curl -X POST "http://<IP>:8004/api/objects/mkdir?token=<token>" \
  -H "Content-Type: application/json" \
  -d '{"bucket":"voicevideo","key":"story/20260912"}'
```

### 2.6 重命名 / 移动目录（Rename）—— 0.6.0 新增

```
POST /api/objects/rename
```

**鉴权**：写 token

**请求体**（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `bucket` | string | 是 | 目标 bucket |
| `key` | string | 是 | 现有目录（或对象）的 key |
| `new_key` | string | 是 | 新 key；允许修改目录前缀实现跨目录移动，如 `carryvideo/0815` → `carryvideo/0912` |
| `overwrite` | bool | 否 | 新位置已存在同名对象时是否覆盖，默认 `false`，冲突返回 `409` |

**语义说明**：

- 主要用于**目录改名/移动**：服务端整体迁移该前缀下的全部对象与 `.keep` 占位，保证原子性（失败即整体回滚）。
- 也可用于单个对象改名（key 指向文件时）。
- `new_key` 与 `key` 校验规则同 1.5；不允许把目录移动到自身子目录（`new_key` 以 `key + "/"` 开头时返回 `400`）。
- 源不存在返回 `404`；审计记录含 `from_key` / `to_key`。可选 query `public_ip` 记入审计。

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

**鉴权**：写 token

**查询参数**：`bucket`（必填）、`key`（必填）。

**说明**：轻量探测文件或目录是否存在，返回 `data`：`{ "exists": true, "is_dir": false, "size": 123, "sha256": "...", "mtime": 1690000000 }`。用于 push 前判断覆盖、pull 前判断目标存在；不存在时 HTTP 仍为 200，以 `exists:false` 表达（区别于下载接口的 404）。

### 2.8 生成签名下载链接（Presign / 共享链接）

```
POST /api/objects/presign
```

**鉴权**：写 token

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
  "max_uses": 1,
  "remaining": 1,
  "expires": "<时间戳>"
}
```

**说明**：返回的 `url` 即「共享链接」，可在无 token 情况下限次/限时/永久下载。生成时只校验 bucket/key 格式，不强制对象已存在；可选 query `public_ip` 记入审计。

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

**鉴权**：写 token

**说明**：任一有效 token 可见全部条目；完整链接 URL 仅管理员/创建者可看，密钥不回传（`token` 字段被剥离）。

### 2.11 作废签名链接

```
POST /api/objects/signed-links/{link_id}/revoke
```

**鉴权**：写 token；管理员可作废任意，用户只能作废自己创建的。可选 query `public_ip` 记入审计。

---

## 三、分片上传（大文件多路并发）

适用于超大文件，分 4 步：initiate → chunk（多次）→ complete → abort。

### 3.1 发起上传会话

```
POST /api/objects/initiate
```

**鉴权**：写 token

返回 `{ "upload_id", "chunk_size" }`（`chunk_size` 固定 8 MB）。

### 3.2 上传分片

```
POST /api/objects/chunk
```

**鉴权**：写 token

**请求**（multipart）：`upload_id`、`index`（分片序号）、`chunk`（文件）。

### 3.3 合并分片完成上传

```
POST /api/objects/complete
```

**鉴权**：写 token

**请求**（multipart）：`upload_id`、`bucket`、`key`、`total_chunks`、可选 `source_url`、`overwrite`、`public_ip`。

### 3.4 取消分片上传

```
POST /api/objects/abort
```

**鉴权**：写 token

**请求**：`upload_id`（multipart 表单）。

---

## 四、系统接口

### 4.1 健康检查

```
GET /health
```

**鉴权**：公开

返回 `{ "status": "ok", "depot_dir": "...", "exists": true }`。

### 4.2 列出所有 bucket

```
GET /api/buckets
```

**鉴权**：公开

**说明**：查看已存在的 bucket 列表，返回 `data.buckets` 为字符串数组。

### 4.3 创建 bucket —— 0.6.0 新增

```
POST /api/buckets
```

**鉴权**：写 token

**请求体**（JSON）：

```json
{ "bucket": "voicevideo" }
```

**说明**：显式创建空 bucket（内部放 `.keep` 占位）。bucket 命名约束见 1.5；已存在时幂等返回 HTTP 200，`data = { "bucket": "...", "created": false }`，新建成功返回 `created: true`。可选 query `public_ip` 记入审计。

```bash
curl -X POST "http://<IP>:8004/api/buckets?token=<token>" \
  -H "Content-Type: application/json" -d '{"bucket":"voicevideo"}'
```


### 4.4 校验 token

```
GET /api/auth/check?token=<token>
```

**鉴权**：公开（带 token 返回其身份）

返回 `{ "valid": true/false, "actor": "系统/工具" 或 用户名 }`。

---

## 五、Token 管理接口（仅管理员）

以下接口均需**管理员共享 `access_token`**（`?token=` 或 `Authorization: Bearer`）。

### 5.1 查看 token 映射

```
GET /api/tokens
```

返回 token → 用户名的映射。

### 5.2 登记 / 更新 token

```
POST /api/tokens
```

**请求体**（JSON）：`{ "token": "...", "user": "..." }`

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

## 六、审计查询（仅管理员）

```
GET /api/audit
```

**鉴权**：仅管理员

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

单页目录浏览 + 上传 + 下载。

---

## 八、接口速查总表

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | 健康检查 |
| GET | `/api/buckets` | 公开 | 列出 bucket |
| GET | `/api/auth/check` | 公开 | 校验 token |
| GET | `/api/objects/list` | 公开 | 列对象/目录 |
| GET | `/api/objects/signed-links/config` | 公开 | 签名链接上下限 |
| GET | `/` | 公开 | 网页 UI |
| POST | `/api/objects` | 写 token | 上传对象 |
| POST | `/api/buckets` | 写 token | 创建 bucket（0.6.0） |
| POST | `/api/objects/mkdir` | 写 token | 建目录 |
| POST | `/api/objects/rename` | 写 token | 目录/对象重命名·移动（0.6.0） |
| GET | `/api/objects/head` | 写 token | 元信息探测（0.6.0） |
| POST | `/api/objects/presign` | 写 token | 生成共享链接 |
| GET | `/api/objects/download` | token/签名 | 下载（Range） |
| DELETE | `/api/objects` | 仅管理员 | 删除 |
| GET | `/api/objects/signed-links` | 写 token | 列签名链接 |
| POST | `/api/objects/signed-links/{id}/revoke` | 写 token | 作废签名链接 |
| POST | `/api/objects/initiate` | 写 token | 发起分片上传 |
| POST | `/api/objects/chunk` | 写 token | 上传分片 |
| POST | `/api/objects/complete` | 写 token | 合并分片 |
| POST | `/api/objects/abort` | 写 token | 取消分片 |
| GET | `/api/tokens` | 仅管理员 | 查看 token→用户映射 |
| POST | `/api/tokens` | 仅管理员 | 登记/更新 token |
| DELETE | `/api/tokens` | 仅管理员 | 移除 token |
| POST | `/api/tokens/sync` | 仅管理员 | 从 DataHub 同步 |
| GET | `/api/audit` | 仅管理员 | 审计查询 |

---

## 九、配置项与调用相关

ArtifactDepot 配置优先级：内置默认 < `resources/config.json`（或 `ARTIFACT_DEPOT_CONFIG` 指定文件）< 环境变量。

影响接口调用的关键配置：

| 配置 | 默认 | 说明 |
|---|---|---|
| `port` | 8004 | 服务端口（环境变量 `ARTIFACT_DEPOT_PORT`） |
| `access_token` | — | 管理员共享令牌，生产必须修改（`ARTIFACT_DEPOT_ACCESS_TOKEN`） |
| `max_upload_mb` | 0（不限） | 单文件上传上限 |
| `datahub_url` | `http://127.0.0.1:8002/api/data` | 用户 token 权威源 |
| `ui_enabled` | true | 是否启用网页 UI |
| `signed_links.*` | 见 config | 签名链接次数/时效上下限 |

---

## 十、安全注意事项

1. 读接口（list/buckets）为「内网开放」设计，切勿直接暴露公网，建议前置 Nginx 鉴权或防火墙/VPN 隔离。
2. 管理员 `access_token` 与用户 token 属敏感凭据，不得写入 git / 文档 / 日志。
3. key/bucket 有路径穿越防护，但调用方仍应避免传不可信输入。
4. 删除接口仅管理员可调，且目录仅空可删。

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
POST /api/objects/initiate                → upload_id, chunk_size(8MB)
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

**Pull 整个目录**：先 `GET /api/objects/list?bucket=..&prefix=..` 枚举 `is_dir=false` 的 items，再逐个（或并发）download。

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
