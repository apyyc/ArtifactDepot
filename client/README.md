# ArtifactDepot 桌面客户端

深蓝主题的桌面客户端，覆盖当前 Web 端功能：文件浏览、上传（分片/暂停/续传）、下载（分块并发/暂停/续传）、签名链接、Token 管理、审计。

![界面](screenshot.png)

## 功能

**文件浏览**
- 列出/进入 bucket，目录导航，分页加载更多；
- 上传：多选文件、拖拽文件到窗口、来源 URL、文件并发数、大文件自动分片、失败重试、断点续传；**支持暂停/继续**（分片边界生效，已传分片不重传），失败不 abort、再次开始从断点续传；断点状态写源文件同目录的 `.<文件名>.artifactdepot-upload.json`（源目录不可写时回退 `~/.artifactdepot/upload-state/`）；
- 下载：单个文件自动分块并发（分块大小与并发数默认取服务端 `data.download`，可在动作栏「下载并发」调整 1–16）；显示已下载字节、百分比与实时速度；可随时「取消下载」，并发分片下载还支持**暂停/继续**（保留 `.part` + 断点状态，恢复只补缺失分片）；
- 目录下载：「下载本目录」在目录只有 1 个可见文件且没有任何可见子目录时直接下载（保留原文件名，支持 Range）；否则（≥2 个文件，或至少 1 个可见子目录，含「1 个文件 + 空子目录」）由服务端流式打包 ZIP；空目录/不存在/不是目录返回 404；ZIP 无 Content-Length，保持单流，不做分块并发；
- 新建目录；删除文件/空目录；
- 批量删除：「清空本目录文件」/「删除本目录全部内容」，先预览数量与大小，输入目录名确认；
- 签名链接：为文件生成按时效/按次数/永久链接，复制、查看、作废。

**签名链接**

查看创建者、文件、类型、剩余、创建时间、状态、链接；复制或作废。

**Token 管理**

- 查询注册表、显示明文（管理员）、移除 token；
- 从 DataHub 同步、重新加载配置；
- 按权限点生成 Token：角色预设、过期时间、说明、Bucket 白名单（勾选/手填）、路径前缀白名单、全选/全不选/仅读/仅写、生效范围预览；
- 生成结果仅展示一次，可复制。

**审计**

按 bucket、key、操作者、起始时间查询审计记录（时间、动作、bucket、key、操作者、IP、代理 IP、大小）。

## 伴随文件与断点续传

上传/下载时自动创建的断点状态与临时文件都放在**「文件所在目录」**，不再写用户 home 或系统临时目录：

| 场景 | 路径 | 说明 |
|---|---|---|
| 上传断点 | `<源文件目录>/.<文件名>.artifactdepot-upload.json` | 0600 原子写；内容含 `version / server / bucket / key / file_path / file_size / file_mtime_ns / chunk_size / total_chunks / upload_id / uploaded / updated_at / staging` |
| 上传断点（回退） | `~/.artifactdepot/upload-state/<sha1(server\|bucket\|key)>.json` | **源目录不可写**时自动回退，日志提示「源目录不可写（…），续传状态回退到 …」 |
| 下载临时 | `<目标目录>/<目标名>.part` | 并发分片按字节偏移写入 |
| 下载断点 | `<目标目录>/.<目标名>.artifactdepot-download.json` | `size / etag / chunk_size / done`；每完成一片即更新（0600） |

> 应用配置 `~/.artifactdepot_client_ui.json` 不属于伴随文件，仍保留；旧版全局 `~/.artifactdepot_upload_state.json`
> 只在首次遇到匹配状态时做一次兼容迁移（迁移后删除），不匹配的旧状态留给其所属上传处理。

**上传续传**：下次选择**同一文件、同一目标（bucket/key）、文件大小与 mtime 未变**时自动续传——先读本地状态 →
`initiate(resume=true, upload_id, bucket, key, size)`（本地无 `upload_id` 时按 `bucket/key/size` 让服务端找回）→
**以服务端返回的 `uploaded` 为唯一事实源**，只补缺失分片。会话失效（`400` 且 `detail` 含「分片会话」）时
清本地状态、新建会话重传。成功 complete 后删除状态文件。

**暂停/继续**：`upload_file(..., pause_event=...)` 与 GUI **「暂停上传」/「继续上传」**在分片边界生效；
已传分片不重传、进度保持。下载同理：GUI **「暂停下载」/「继续下载」**保留 `.part` + 断点状态。

**失败续传**：上传失败**不 abort**，保留服务端会话与本地状态；GUI 标记「失败（可续传）」，再次开始上传即从断点续传。

**下载续传**（仅并发分片下载）：恢复时校验 `size` / ETag 一致后只补缺失分片；ETag 不一致说明对象已变化，
丢弃 `.part` 从头下载。失败保留 `.part` + 状态（可续传），**取消仍删除两者**。

**多文件并发**：每个文件独立状态文件，互不覆盖（不再有全局单文件状态）。

## 下载（分块并发）

- **分块并发**：客户端先 `HEAD /api/objects/download` 探测文件大小、`Accept-Ranges`、ETag 与文件名；文件大于分块且服务端支持 Range 时，按区间并发 `GET`（`Range: bytes=a-b`，期望 206 + `Content-Range`），各分片写入 `.part` 文件对应偏移，完成后校验大小并 `os.replace` 为最终文件。分块大小与并发数取自 `GET /api/auth/permissions` 的 `data.download`（服务端夹紧 1–64MiB / 1–16 / 60s–7d）；服务端不可用时回退 8MiB / 4 路 / 1h。
- **回退单流**：小文件（≤ 分块）、服务端不支持 `Accept-Ranges`、`HEAD` 不可用、`concurrency=1`、目录 ZIP、签名链接，均沿用原来的单流下载（`.part` + `os.replace`）。
- **失败重试**：每个分片失败最多重试 3 次（指数退避）；只对网络错误/超时/5xx/429 重试，4xx（含 416）直接报错。
- **取消**：下载中可点「取消下载」，客户端尽快停止各分片线程、删除 `.part` 并复位界面。
- **暂停/续传**：并发分片下载可点「暂停下载」，客户端在分片边界停止取新分片、保留 `.part` 与目标目录的
  `.<目标名>.artifactdepot-download.json`；点「继续下载」时校验 `size`/ETag 一致后**只补缺失分片**；
  失败同样保留两者可重试；ETag 不一致（对象已变化）会丢弃 `.part` 从头下载。
- **完整性校验**：服务端 ETag 是对象 SHA-256 时，并发下载完成后会计算本地 SHA-256 比对，不一致会报错并清理临时文件。
- **进度与速度**：状态栏显示「已下载 x / y（p%） · 速度 z/s」；进度回调按 ≥150ms 合并刷新，避免刷屏。ZIP 流总大小未知，只显示已下载字节与速度。
- **签名链接慎用**：签名链接按次数计数，客户端不会对签名链接做 `HEAD` 探测或分块并发，避免额外消耗次数；按次数的链接请勿用多线程/工具反复重试。

## 启动

Linux / macOS：

```bash
cd client
chmod +x run.sh
./run.sh
```

`run.sh` 会：

1. 检测系统 Python（3.9+）；
2. 没有 uv 时自动安装到 `client/.uv/`；
3. GUI 缺 tkinter 时，优先下载 Debian/Ubuntu 的 Tk 8.6 包到 `client/.tk86/`（Xft 抗锯齿，中文更清晰）；失败时退回 uv 托管 Python；
4. 在 `client/.venv/` 创建/复用虚拟环境；
5. GUI 模式自动安装可选拖拽依赖 `tkinterdnd2`（失败不影响文件选择上传）；
6. 启动客户端，参数原样透传。

生成的 `.uv/`、`.venv/`、`.tk86/` 已在 `client/.gitignore` 中忽略。首次运行需联网下载 uv/Tk 包，之后可离线。

> Windows：使用官方 Python 直接运行 `python artifactdepot_upload.py`（自带 tkinter）；拖拽依赖可选。

## 使用

1. 顶部填服务地址与访问令牌（令牌只保存在内存，不写盘）；
2. 「文件浏览」选择/输入 bucket，进入目录；
3. 上传：点「选择文件…」多选，或直接把文件拖到窗口；设置来源 URL 与并发数后点「开始上传」；大文件分片上传中可点**「暂停上传」**，之后点**「继续上传」**从断点继续；失败行标记「失败（可续传）」，再次开始即只补缺失分片；
4. 下载：选中文件点「下载」；大文件自动分块并发，可在动作栏「下载并发」调整 1–16；下载中显示进度/速度，可点**「暂停下载」/「继续下载」**（只补缺失分片）或「取消下载」；下载当前目录点「下载本目录」（单文件直下，多文件或含子目录打包 ZIP，ZIP 为单流）；
5. 目录操作：新建目录、删除、批量删除（需输入目录名确认）；
6. 签名链接 / Token 管理 / 审计：切换对应页签。Token 与审计页签使用管理员令牌。

## CLI

CLI 仅保留上传功能，适合脚本或无桌面环境：

```bash
export ARTIFACTDEPOT_TOKEN='你的token'

python3 artifactdepot_upload.py \
  --server http://192.168.18.48:8004 \
  --bucket apyyc \
  --path carryvideo/20261024 \
  --file /path/to/file.mp4 \
  --source-url https://example.com/source
```

常用参数：`--no-overwrite`、`--no-resume`、`--source-url`。大文件自动分片；**断点续传状态保存在源文件同目录的 `.<文件名>.artifactdepot-upload.json`（0600，不含 token）**，源目录不可写时回退 `~/.artifactdepot/upload-state/<sha1>.json` 并在日志提示。上传失败 / 键盘中断（Ctrl-C）会提示「已保留续传状态，重跑同一命令将从断点继续」；加 `--no-resume` 可放弃续传重新开始（服务端会话由 TTL 回收）。

## 测试

`client/test_parallel_download.py` 会启动真实 uvicorn 子进程（随机端口 + 临时 config/depot，`access_token=admin-test`），通过 HTTP API 上传 20MiB+123B 文件，覆盖：分块并发下载与 SHA-256 一致、进度多次回调且 `done == total`、小文件/`concurrency=1` 回退单流、取消清理 `.part`、404、服务端 `audit.log` 出现 ≥2 条 `partial=true` 的 download 审计记录（顶层字段，证明多 Range）、分片重试不重复计数、ETag(SHA-256) 校验失败清理、目录 ZIP 单流：

```bash
cd ArtifactDepot
PYTHONPATH=src /home/apyyc/work/ArtifactDepot/.venv/bin/python -m pytest client/test_parallel_download.py -q
```

`client/test_upload_resume.py` 同样用真实 uvicorn 子进程 + 真实 HTTP（11 个用例），覆盖伴随状态文件与暂停/续传：
状态文件位于源文件目录、0600、成功后删除；暂停→继续只补缺失分片（断言服务端累计 `received_bytes` / 请求次数不再从 0 增长）；
首次失败后续传且最终 SHA-256 一致；多文件并发各自独立状态；下载暂停/续传与失败后续传只补缺失分片；
源目录不可写回退 `~/.artifactdepot/upload-state/`；状态路径 / 0600 / 旧全局状态一次性迁移（单元级）：

```bash
cd ArtifactDepot
PYTHONPATH=src /home/apyyc/work/ArtifactDepot/.venv/bin/python -m pytest client/test_upload_resume.py client/test_parallel_download.py -q
```

## 权限

- 文件浏览/下载/上传/签名/删除：按 token 的 scope 和资源范围；
- Token 管理与审计：需要管理员权限（共享 `access_token` 或具备相应 scope 的管理员 token）；
- 上传所需权限点：`upload:initiate` / `upload:chunk` / `upload:complete` / `upload:abort`；
- 下载目录 ZIP：`object:download`。

## 常见问题

| 现象 | 处理 |
|---|---|
| 启动提示没有 tkinter | 用 `./run.sh` 自动准备 Tk 8.6；或安装 `python3-tk`；CLI 不需要 tkinter |
| 中文有锯齿 | 确认启动日志出现「Tk 8.6 + Xft」；否则删除 `client/.venv` 重跑 `./run.sh` |
| 拖拽无效 | 拖拽依赖 `tkinterdnd2` 安装失败；仍可用「选择文件…」多选上传 |
| `HTTP 401/403` | token 无效、过期、缺少权限点或资源范围越界 |
| `HTTP 413` | 超过服务端上传上限 |
| 服务端分片会话已失效 | 超过服务端 TTL（`upload.session_ttl_seconds` / `upload.session_idle_ttl_seconds`，默认 24h）或被 abort；客户端收到 `400`（detail 含「分片会话」）会自动清除本地状态，重新上传。**服务重启不影响续传**（会话在数据盘，启动扫描重建索引） |
| 上传失败后必须重传整个文件吗 | 不需要。失败不 abort，服务端会话与本地状态保留；再次开始上传（同文件/同目标）会 `initiate(resume=true)` 取回真实 `uploaded`，只补缺失分片 |
| 断点状态文件在哪？能删吗 | 上传：`<源文件目录>/.<文件名>.artifactdepot-upload.json`；下载：`<目标目录>/.<目标名>.artifactdepot-download.json`（均 0600、不含 token）。成功完成或会话失效会自动删除；手动删除只会丢失续传能力（服务端分片仍由 TTL 回收） |
| 源目录是只读的（不可写）怎么办 | 上传断点会自动回退到 `~/.artifactdepot/upload-state/<sha1>.json`，日志提示「源目录不可写，续传状态回退到 …」；功能不受影响。下载断点写在目标目录，目标目录也必须可写 |
| 上传/下载怎么暂停和继续 | 上传：动作栏「暂停上传」→「继续上传」；下载（并发分片）：动作栏「暂停下载」→「继续下载」。暂停均在分片边界生效，已传分片不重传 |
| 取消上传会删除已传分片吗 | 不会。取消/暂停/失败都保留服务端会话与已传分片，便于续传；确实要释放磁盘可用 `POST /api/objects/abort`（服务端也会按 TTL 自动回收） |
| 服务重启后还能续传吗 | 能。分片会话在数据盘，启动扫描未过期会话后客户端可继续；服务端按单进程运行（不要开 `--workers`） |
| 下载目录 404 | 目录为空、不存在或不是目录 |
| 下载速度没跑满 | 调大「下载并发」；若服务端 `data.download` 限制或磁盘较慢则提升有限 |
| 取消下载后残留 `.part`？ | 正常取消会自动删除临时文件；若进程被强杀，可手动删除同名 `.part` |
| 签名链接下载多次后失效 | 按次数的签名链接每次访问都计数，请按需生成或改用 token 下载 |

## 说明

- Token 不写入磁盘；
- 服务端文件不会因为客户端关闭而丢失；
- 上传/下载的断点状态与 `.part` 都放在**文件所在目录**（0600 原子写），不含 token；源目录不可写时上传状态回退 `~/.artifactdepot/upload-state/`；
- 取消上传保留已上传分片与本地状态，下次可续传；暂停/失败同样保留；
- 取消下载会删除 `.part` 与下载断点状态；暂停/失败则保留，恢复时只补缺失分片；
- 上传续传以服务端 `initiate(resume=true)` 返回的 `uploaded` 为唯一事实源，本地状态只做缓存；服务端分片会话在数据盘，**服务重启后仍可续传**（受服务端 TTL 约束）；
- 下载参数（分块大小/并发/超时）优先取服务端 `data.download`，失败回退 8MiB / 4 路 / 1h，客户端数值夹紧到 1–64MiB / 1–16 / 60s–7d；
- 本客户端未修改服务端代码，使用现有 HTTP API。
