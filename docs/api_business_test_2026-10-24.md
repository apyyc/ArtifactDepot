# ArtifactDepot 本次新增功能接口业务验收报告

- 日期：2026-10-24
- 代码基线：`ArtifactDepot` 工作区（未提交），`VERSION=0.10.0`
- 测试方式：真实 `uvicorn` + `httpx` HTTP 请求，独立 `depot_dir` 与 `TMPDIR`
- 测试数据：全部在 `.api-test/run` 临时目录；未接触生产数据；测试后已清理
- 参考契约：`docs/api_rules.md` §2.12、§2.13、三、§9；`CHANGELOG.md` 0.10.0

## 1. 测试范围

### 1.1 核心接口（9 个，计入逐接口 PASS/FAIL）

| # | 方法 | URL | 用途 | 权限 | 关键规则 | 数据影响 |
|---|---|---|---|---|---|---|
| 1 | GET | `/api/auth/permissions` | 权限目录 + 上传配置下发 | 公开 | upload 配置裁剪、reload 后即时变化 | 无 |
| 2 | POST | `/api/config/reload` | 重载 config/.env | 管理员共享 token | 仅共享 token；非法配置保留旧值；upload 不需重启 | 内存配置 + 审计 |
| 3 | POST | `/api/objects/initiate` | 创建分片会话 | `upload:initiate` | owner 绑定、下发配置、清理过期会话 | 新建会话目录/元数据 |
| 4 | POST | `/api/objects/chunk` | 上传分片 | `upload:chunk` | 分片大小/数量/会话总量/owner | 写 `.part`、更新 received_bytes |
| 5 | POST | `/api/objects/complete` | 合并完成 | `upload:complete` + bucket/key 范围 | 缺片保留、成功清理、409 保留、目录冲突清理 | 对象 + manifest + 审计 |
| 6 | POST | `/api/objects/abort` | 取消会话 | `upload:abort` | 仅 owner/admin、幂等 | 删除会话 |
| 7 | GET | `/api/objects/download-bulk` | 目录一键下载 | `object:download` + 范围 | 单文件直下 / 流式 ZIP、隐藏与符号链接跳过、100000 项上限、审计一次 | 只读；审计/指标 |
| 8 | POST | `/api/objects/bulk-delete` | 批量删除目录内容 | `object:delete_bulk` + 范围 | dry-run 不落盘、confirm 名称、files/all 语义、50000 项上限、根清单保护 | 删除文件/目录、清 manifest、审计/指标 |
| 9 | POST | `/api/objects` | 直接上传（本次清理逻辑修改） | `object:upload` + 范围 | 超限/落盘失败清临时文件、overwrite 冲突 | 对象 + manifest + 审计 |

完整接口清单（必填/可选参数、返回结构、前置状态）见：
`/home/apyyc/work/ArtifactDepot/.api-test/interface-inventory.md`

### 1.2 辅助接口（只用于造数/交叉验证，不计入逐接口统计）
`POST /api/buckets`、`GET /api/objects/list`、`GET /api/objects/head`、`GET /api/objects/download`、`DELETE /api/objects`、`POST /api/objects/mkdir`、`POST /api/tokens`、`GET /api/auth/check`、`GET /api/audit`、`GET /metrics`、`GET /health`。

### 1.3 非 HTTP 生命周期
- 服务启动 `lifespan` 清理遗留 `/tmp/dw_chunks/<id>`。
- 后台 600s 清理任务（按空闲 TTL / 绝对 TTL）；本次通过启动清理、新建会话清理及单元测试验证，未实际等待 10 分钟。

## 2. 测试结果总览

| 指标 | 结果 |
|---|---:|
| 核心接口总数 | 9 |
| 已测试核心接口 | 9 |
| 核心接口 PASS | **9 / 9** |
| HTTP 业务用例总数 | **225** |
| HTTP 业务用例 PASS | **225** |
| HTTP 业务用例 FAIL | **0** |
| HTTP 业务用例 BLOCKED | **0** |
| 全量 pytest | **296 passed，5 xfailed**（5 个 xfail 为既有 OBJ-001/002/004/006，不在本次功能范围） |
| 测试数据清理 | 已清理 `.api-test/run`；未修改生产数据 |

### 2.1 分接口结果

| 接口 | 用例数 | PASS | FAIL | BLOCKED | 结论 |
|---|---:|---:|---:|---:|---|
| GET `/api/auth/permissions` | 7 | 7 | 0 | 0 | **PASS** |
| POST `/api/config/reload` | 9 | 9 | 0 | 0 | **PASS** |
| POST `/api/objects/initiate` | 10 | 10 | 0 | 0 | **PASS**（修复后） |
| POST `/api/objects/chunk` | 26 | 26 | 0 | 0 | **PASS** |
| POST `/api/objects/complete` | 36 | 36 | 0 | 0 | **PASS**（修复后） |
| POST `/api/objects/abort` | 12 | 12 | 0 | 0 | **PASS** |
| GET `/api/objects/download-bulk` | 43 | 43 | 0 | 0 | **PASS** |
| POST `/api/objects/bulk-delete` | 48 | 48 | 0 | 0 | **PASS** |
| POST `/api/objects`（直接上传） | 21 | 21 | 0 | 0 | **PASS**（修复后） |
| 启动清理生命周期 | 2 | 2 | 0 | 0 | **PASS** |
| 端到端链路 | 7 | 7 | 0 | 0 | **PASS** |
| 安全输入专项 | 4 | 4 | 0 | 0 | **PASS** |

> 原始结果数据：`/home/apyyc/work/ArtifactDepot/.api-test/results.json`
> 测试脚本：`/home/apyyc/work/ArtifactDepot/.api-test/api_test.py`

## 3. 逐接口业务验证摘要

### 3.1 GET `/api/auth/permissions`
- 公开访问 200，`code=0`；普通 token 携带时仍可读取公共配置。
- `data.upload` 8 个字段齐全；默认值：8MB / 4 / 3 / 86400s。
- 权限目录包含新权限点 `object:delete_bulk`；`operator` 角色预设包含该权限点。
- 响应不包含管理员共享 token。
- `config.reload` 后新 upload 配置立即反映。

### 3.2 POST `/api/config/reload`
- 无 token → 401；普通用户 → 403；共享管理员 → 200。
- 成功后 `data.ok=true`，`changed` 含 `upload`，写一条 `config_reload` 审计。
- 修改 `upload.chunk_size_mb=4/max_chunk_mb=2/max_chunks=2/...` 后，`/permissions` 与 `/initiate` 立即返回裁剪后的 `2MB/2`。
- 非法 JSON → 400，旧配置保持不变。
- 越界/自相矛盾值被夹紧：chunk 64MB、并发 16、请求超时 60s、file_concurrency 1。
- 所有成功/失败路径均核对审计与内存配置。

### 3.3 POST `/api/objects/initiate`
- 无 token → 401；viewer 缺 scope → 403；过期 token → 401；uploader → 200。
- 返回字段齐全且默认值正确；`upload_id` 格式合法、目录存在。
- 会话 `.session.json.owner` 正确绑定创建者；重复发起产生不同 upload_id。
- 空闲 TTL 到期的旧会话在下次 initiate 时被清理；活跃会话不受影响。
- **修复后**：`session_ttl_seconds` 绝对 TTL 到期会话也会被清理。
- 新增会话目录、元数据、过期清理前状态均落盘核对。

### 3.4 POST `/api/objects/chunk`
- 无 token → 401；缺 scope → 403；禁用 token → 401；正常 uploader → 200。
- 缺 `upload_id/index/chunk`、JSON 代替 multipart → 422。
- `upload_id` 路径穿越/不存在 → 400，且未创建外部目录。
- 负 index、index ≥ max_chunks → 400。
- 超过 `max_chunk_mb` → 413；不落分片、会话保留、请求临时文件清理。
- 恰好等于上限 → 200；同 index 覆盖后内容/`received_bytes` 正确。
- 超过 `max_upload_mb` 会话总量 → 413，未落第二片。
- 其他用户 → 403；管理员可写；multipart 表单 token 可用；空分片可用；4 路并发分片全部成功且 received_bytes 汇总正确。

### 3.5 POST `/api/objects/complete`
- 无 token/缺 scope → 401/403。
- 正常合并：200；对象内容、manifest、`source_url` 正确；会话删除；一条 upload 审计；UPLOADS +1。
- 缺片、total_chunks=0、超 max_chunks、超 max_upload_mb → 400/413，且会话保留供重试。
- `overwrite=false` 已存在 → 409，会话保留；改 true 后 200，会话删除。
- 他人 complete → 403 且会话保留；资源范围越界 → 403 且保留；key 穿越 → 400 且不写出仓库。
- 完成后重复 complete / chunk → 400；重复操作不重复审计/指标。
- 并发 complete 同一会话：仅一个 200、一个 400，对象内容完整。
- **修复后**：目标 key 已是目录 → 409（不再 500），会话清理，不写 upload 审计。
- 直接上传目标 key 是目录 / 父路径是文件 → 409（不再 500）。

### 3.6 POST `/api/objects/abort`
- 无 token 401；缺 scope 403；owner 取消 200 `existed=true` 并删除会话。
- 重复取消 200 `existed=false`；他人取消 403 且不删会话；管理员可取消。
- 非法 upload_id 400；格式合法但不存在 200 `existed=false`。
- 取消后 chunk/complete → 400。

### 3.7 GET `/api/objects/download-bulk`
- 无/坏 token 401；viewer 403；范围内 200；bucket/prefix 范围外 403；Bearer 可用。
- 单文件且无目录：原文件直下，保留 Content-Type/文件名/ETag；Range 206；If-None-Match 304；304 不计下载指标。
- 一个文件 + 空目录 → ZIP；ZIP 顶层文件夹、嵌套文件、空目录条目、隐藏/符号链接跳过、内容正确、无 Content-Length、不落临时 zip。
- 中文目录顶层名/`filename*=UTF-8''` 正确。
- 空目录/仅隐藏/prefix 是文件/不存在/bucket 不存在 → 404。
- `../`、隐藏 prefix、跨 bucket 符号链接 prefix → 400。
- ZIP 审计恰好一条：key/prefix、files/dirs、aborted、size 与实际相符；404 不写审计。
- 客户端 RST 中断：审计恰好一条且 `aborted=true`。
- 100001 项 → 400，审计不变；100000 上限逻辑生效。
- 特殊文件名/控制字符安全拒绝或安全编码，无 header 注入。

### 3.8 POST `/api/objects/bulk-delete`
- 无 token 401；缺 scope 403；过期/禁用 token 401；范围内 200；范围外 403；范围校验先于 mode 校验。
- dry_run files/all 计数/bytes 正确；磁盘、manifest、审计、DELETES 指标均不变。
- 执行缺少/错误 confirm → 400，不删除；confirm 前后空白可匹配。
- files 模式只删直接可见文件，子目录/隐藏项/`.keep` 保留，manifest 对应项清理。
- all 模式递归删除文件/目录/隐藏项，base 目录保留；符号链接只删链接不跟随目标。
- bucket 根执行 all 后 `.warehouse.json` 与 bucket 目录保留。
- 空目录重复执行 200 且计数 0（幂等）。
- 不存在 bucket/prefix、prefix 是文件、非法 mode、隐藏 prefix、路径穿越、超 50000 项 → 对应 404/400。
- 并发执行同一 bucket 均无 500，最终内容清空。
- 审计仅执行写一条；指标仅执行 +1；partial errors 字段结构正确（单元回归）。

### 3.9 POST `/api/objects`（直接上传）
- 无 token 401；无 scope 403；禁用 token 401；正常 200。
- 内容落盘、manifest、source_url、审计、UPLOADS +1 正确。
- overwrite=false 409 且保留旧内容。
- 缺 file/bucket、JSON/错误 Content-Type → 422。
- key 路径穿越、含换行控制字符 → 400。
- bucket 范围外 → 403。
- 超过 max_upload_mb 413：不落对象、不写审计/指标、请求临时文件清理。
- 目标 key 是目录或父路径是文件 → **修复后 409**，不再 500。

## 4. 发现的问题与处理

### BUG-API-001：`upload.session_ttl_seconds` 绝对有效期未生效（P2，已修复）
- 接口：`POST /api/objects/initiate`（以及后台清理）
- 请求：配置 `session_ttl_seconds=60`、`session_idle_ttl_seconds=3600`；创建会话后将其 `created_at` 置为 2020；再次 initiate。
- 预期：绝对 TTL 到期会话被删除。
- 实际：旧会话仍存在；`create_chunk_session()` 和后台任务只使用了 idle TTL；`session_ttl_seconds` 实际未生效。
- 修复：新增 `cleanup_expired_chunk_sessions()`，按 `.session.json.created_at` 清理；新建会话与后台任务同时执行 idle + absolute 清理。
- 回归：INIT-10 PASS；新增 `test_absolute_session_ttl_removes_expired_keeps_active`。

### BUG-API-002：complete 目标 key 已是目录返回 500（P2，已修复）
- 接口：`POST /api/objects/complete`
- 请求：先 `mkdir demo/dir`，分片上传后 `complete` 到 key=`dir`。
- 预期：400/409，统一 JSON；失败清理会话。
- 实际：`shutil.copyfile` 抛 `IsADirectoryError`，返回 500（已知 OBJ-005）。
- 修复：`put_object()` 对目标目录返回 409；`finalize_chunk_upload()` 对目录冲突不保留会话。
- 回归：CMP-26 PASS（409）；CMP-27 会话清理 PASS；full test 已移除 xfail。

### BUG-API-003：直接上传目标/父路径类型冲突返回 500（P2，已修复）
- 接口：`POST /api/objects`
- 请求：上传到已是目录的 key；或 `f.txt/sub.txt` 且 `f.txt` 已是文件。
- 预期：400/409。
- 实际：未捕获 `IsADirectoryError/FileExistsError` → 500（OBJ-003）。
- 修复：`put_object()` 拦截目标目录和父路径非目录，返回 409；已有测试 `test_upload_type_conflict_should_not_500` 从 xfail 转为 PASS。
- 回归：full suite 296 passed。

### SEC-001：key 控制字符/响应头文件名加固（P2，已修复）
- 接口：`POST /api/objects`、`GET /api/objects/download-bulk`
- 现象：`validate_key()` 未拒绝 CR/LF/TAB；特殊文件名进入下载 `Content-Disposition` 路径时可能污染日志/响应头或触发 400/500。
- 修复：`validate_key()` 拒绝控制字符；下载 `FileResponse` 文件名通过 `_safe_header_filename()` 替换控制字符。
- 回归：SEC-01、DL-37、DL-37b PASS；全量测试通过。

### 测试方法问题（非产品缺陷）
- DL-31 中断审计用 `httpx` 关闭时偶发拿不到审计（服务端在收尾/连接复用存在时序）；改用 raw socket `SO_LINGER` RST 后稳定 PASS，证明服务端中断审计路径有效。
- “空 key”实际由 FastAPI 参数校验返回 422，不是 400；按框架语义修正预期。
- `tests/test_upload_cleanup.py` 文件头注释原写“缺片删除会话”，实际/文档均为“保留会话”，已修正注释。

## 5. 权限、越权与数据隔离结果

全部 PASS：

- 未登录/无效 token：401；过期/禁用 token：401。
- 普通 viewer 缺少下载/删除/上传 scope：403。
- 受限 token 的 bucket 范围、路径前缀范围均生效；范围外 bulk download / bulk delete / direct upload → 403。
- 管理员共享 token 与 `operator` 角色（含 `object:delete_bulk`）可执行；非管理员不能重载配置。
- 分片会话 owner 隔离：A 创建，B chunk/complete/abort → 403 且会话保留；admin 可操作。
- 跨 bucket 符号链接 prefix → 400；不会读取/删除其他 bucket。
- 测试中未发现 IDOR：无法通过修改 bucket/prefix/key/upload_id 越过范围。
- 未发现跨请求数据污染：并发 complete / 并发 bulk delete 结果一致，无重复对象/审计。

## 6. 状态流转与数据一致性

| 状态流 | 结果 |
|---|---|
| initiate → chunk → complete | 成功，对象/manifest 正确，会话清理 |
| complete 缺片/超限/total 非法 | 4xx，会话保留可重试 |
| complete overwrite=false 409 | 409，会话保留；改 true 成功 |
| complete 目标为目录 | **409**（修复），会话清理，不写上传审计 |
| abort | 成功删除；重复 abort 幂等 |
| complete 后再次 complete/chunk | 400，不重复审计/指标 |
| abort 后 chunk/complete | 400 |
| 并发 complete 同一会话 | 只有一个成功，对象完整 |
| 并发 bulk delete | 均无 500，最终内容清空 |
| dry-run bulk delete | 磁盘、manifest、审计、指标均不变 |
| bulk download | 只读；无临时 zip；审计恰好一次（含中断） |

数据一致性均通过“修改前快照 → 请求 → 返回 → 磁盘/manifest/审计/指标 → 再次查询”核对。

## 7. 未覆盖/受限项目

1. `upload.max_session_gb` 的 200GB 真实边界未构造（资源消耗过大）；已通过 `max_upload_mb` 的等价路径测试 413，并验证配置裁剪。
2. 后台 600s 定时清理未等待 10 分钟；已通过启动清理、新建会话清理、`test_cleanup_idle_sessions_keeps_active` 与新增绝对 TTL 单测覆盖。
3. bulk-delete 的 21+ 条部分 I/O 错误明细未在真实 HTTP 环境构造；由既有单元测试覆盖 errors 上限逻辑。
4. SQL 注入：本服务文件存储无 SQL，N/A；已验证路径穿越、控制字符、header 注入输入。
5. 浏览器 UI 端到端不在本次“接口级业务测试”范围；API 契约与客户端调用点已覆盖。

## 8. 既有但不在本次范围的 xfail（未修复）

全量测试仍有 5 个 xfail，均为既有缺陷，不属于本次 4 组新功能：

- OBJ-001（P1）：rename 类型冲突 500，目录移动失败不回滚（2 个测试）。
- OBJ-002（P2）：rename overwrite 后 manifest/sha256 陈旧。
- OBJ-004（P2）：mkdir 的 key/父路径是文件返回 500。
- OBJ-006（P2）：presign 特殊字符 key 的 URL 未编码。

建议另开修复任务，不要与本次上线捆绑。

## 9. 结论与上线建议

**建议：可进入预发/灰度，不建议跳过部署验证直接替换生产。**

理由：
- 9/9 核心接口、225/225 HTTP 业务用例 PASS；全量 296 passed；
- 本次发现并修复的 3 个接口问题（session TTL、complete 类型冲突、direct upload 类型冲突）及 1 个安全加固均已回归；
- 数据一致性、权限/越权、状态流转、并发、审计/指标均通过；
- 本次为服务端代码变更，需要重新构建/部署容器（非数据库迁移）；测试使用的是本工作区源码，不是当前生产容器；
- 5 个既有 xfail 与本功能无关，但其中 OBJ-001（P1）建议排期。

## 10. 交付物与复现

- 完整接口清单：`.api-test/interface-inventory.md`
- 黑盒测试脚本：`.api-test/api_test.py`
- 机器可读结果：`.api-test/results.json`
- 全量回归：`../.testenv/bin/python -m pytest -q tests -p no:cacheprovider`
- 新功能专项：`../.testenv/bin/python -m pytest -q tests/test_bulk_delete.py tests/test_bulk_download.py tests/test_upload_config.py tests/test_upload_cleanup.py`
