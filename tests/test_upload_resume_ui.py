# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""网页端「令牌门槛 + 任务行暂停/继续 + 断点续传 + 未完成上传」静态回归测试（契约 §D）

只读取前端源码文本，不启动服务、不跑浏览器：
  - index.html：文件选择按钮初始禁用（必须先填令牌）、**每个任务行**的
    pause-upload / resume-upload / retry-upload / cancel-upload 结构、「失败（可续传）」文案、
    当前令牌未完成上传列表（cancel-pending-upload）、无内联 <script>/<style>/on* 处理器
    （严格 CSP，交互全部走 data-action 事件委托）；
  - app.js：initiate 带 bucket/key/size/resume/upload_id；队列项记录 upload_id/chunk_size/uploaded；
    失败不再 abortUpload（保留服务端分片会话）；暂停按任务独立在分片边界生效；继续/重试只补缺失分片；
    页面刷新后可按当前令牌拉取未完成会话并手动取消；重新选择同一文件可自动找回会话
    （本地只缓存文件指纹，不存 token）；
  - app.css：任务行按钮、暂停态与未完成上传列表样式。

运行：
    cd ArtifactDepot/ArtifactDepot
    PYTHONPATH=src ../.venv/bin/python -m pytest tests/test_upload_resume_ui.py -q
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "src" / "artifactdepot" / "resources"
HTML_PATH = BASE / "index.html"
JS_PATH = BASE / "static" / "app.js"
CSS_PATH = BASE / "static" / "app.css"


def _html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _js() -> str:
    return JS_PATH.read_text(encoding="utf-8")


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _js_function(js: str, name: str) -> str:
    """截取 app.js 里的顶层函数（含嵌套体）。

    顶层声明都从第 0 列开始，嵌套函数有缩进，因此用「下一个第 0 列的
    function/const/let/var 声明」作为边界即可（与 test_web_download_chunked 一致）。
    """
    m = re.search(r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", js, re.M)
    assert m, f"app.js 未找到函数 {name}()"
    tail = js[m.end():]
    nxt = re.search(r"\n(?=(?:async\s+)?function\s+[A-Za-z_$]|(?:const|let|var)\s+[A-Za-z_$])", tail)
    end = m.end() + (nxt.start() if nxt else len(js) - m.end())
    return js[m.start():end]


# ===========================================================================
# 1. 暂停 / 继续按钮（index.html + 事件委托）
# ===========================================================================

def test_pause_resume_buttons_are_per_task():
    """「暂停上传 / 继续上传」只作为队列行按钮（data-action + data-uid），顶部工具栏不再有全局按钮。"""
    html, js = _html(), _js()
    # 顶部「并发数 / 上传」那一行不得再有全局暂停/继续按钮
    assert 'id="upPause"' not in html
    assert 'id="upResume"' not in html
    assert '$(\'upPause\')' not in js and '$(\'upResume\')' not in js
    # 队列行结构说明里仍有行级暂停/继续动作，严格 CSP 事件委托处理器必须存在
    assert 'data-action="pause-upload"' in html
    assert 'data-action="resume-upload"' in html
    assert "a === 'pause-upload'" in js
    assert "a === 'resume-upload'" in js
    assert "function pauseUpload(" in js
    assert "function resumeUpload(" in js
    assert "pauseUpload(el.dataset.uid)" in js
    assert "resumeUpload(el.dataset.uid)" in js
    # 行级按钮必须带 data-uid（按任务操作，而不是全局队列）
    assert 'data-action="pause-upload" data-uid="' in js
    assert 'data-action="resume-upload" data-uid="' in js


def test_all_data_actions_have_js_handler():
    """index.html 里出现的每个 data-action 都要有 app.js 处理器（含队列行注释里的新动作）。"""
    html, js = _html(), _js()
    html_actions = set(re.findall(r'data-action="([a-z-]+)"', html))
    js_actions = set(re.findall(r"a === '([a-z-]+)'", js))
    assert {"pause-upload", "resume-upload", "retry-upload", "cancel-upload",
            "cancel-pending-upload"} <= html_actions
    assert html_actions <= js_actions, f"缺少处理器: {sorted(html_actions - js_actions)}"


# ===========================================================================
# 2. 队列行结构：每项「继续」「放弃」按钮（事件委托，无内联 JS）
# ===========================================================================

def test_queue_row_retry_structure():
    """失败队列行由 renderUpQueue() 渲染：data-action="retry-upload" + data-uid。"""
    html, js = _html(), _js()
    assert 'data-action="retry-upload"' in html      # index.html 的队列行结构说明
    assert 'data-action="retry-upload"' in js
    assert 'data-action="cancel-upload"' in js
    assert 'data-uid="' in js
    assert "function renderUpQueue(" in js
    assert "function retryUpload(" in js
    assert "function cancelUpload(" in js
    # 队列行不允许内联事件
    assert "onclick" not in html


def test_failed_status_text_and_resumable_flag():
    """失败文案为「失败（可续传）」，暂停态「已暂停」，且失败可续传时保留 upload_id。"""
    js = _js()
    assert "失败（可续传）" in js
    assert "已暂停" in js
    assert "resumable" in js
    label = _js_function(js, "upItemLabel")
    assert "失败（可续传）" in label
    assert "已暂停" in label


# ===========================================================================
# 3. initiate 参数与队列项续传状态
# ===========================================================================

def test_initiate_sends_resume_parameters():
    """initiate 带 bucket/key/size；续传再带 resume=true 与 upload_id（multipart，服务端同时支持 query）。"""
    body = _js_function(_js(), "initiateUpload")
    assert "'/api/objects/initiate'" in body
    for field in ("bucket", "key", "size", "resume", "upload_id"):
        assert "fd.append('%s'" % field in body, f"initiate 缺少参数 {field}"
    # resume 是布尔开关；upload_id 只在续传时附加
    assert "fd.append('resume', 'true')" in body
    assert "if (upload_id) fd.append('upload_id', upload_id)" in body
    # 不使用写死的 token/query 拼接；token 只经 Authorization 头
    assert "?token=" not in body
    # 服务端不提供 GET /api/objects/upload/{id}，续传信息只来自 initiate(resume=true)
    assert "/api/objects/upload/" not in body


def test_queue_item_tracks_upload_session():
    """队列项记录 upload_id / chunk_size / uploaded（Set），完成后清理。"""
    js = _js()
    make = _js_function(js, "makeUploadItem")
    for field in ("upload_id", "chunk_size", "uploaded"):
        assert field in make, f"队列项缺少 {field}"
    assert "uploaded: new Set()" in make
    chunked = _js_function(js, "uploadOneChunked")
    assert "item.uploaded.add(index)" in chunked
    # 服务端返回的 uploaded/total_chunks/chunk_size 是唯一事实源
    init = _js_function(js, "initiateUpload")
    assert "data.uploaded" in init
    assert "data.total_chunks" in init
    assert "data.chunk_size" in init


# ===========================================================================
# 4. 失败不 abort，暂停在分片边界
# ===========================================================================

def test_failure_keeps_session_does_not_abort():
    """上传/合并失败不再 abortUpload：会话保留，继续时只补缺失分片。"""
    js = _js()
    chunked = _js_function(js, "uploadOneChunked")
    assert "abortUpload" not in chunked
    assert "resumable: true" in chunked
    assert "服务端分片会话保留" in chunked
    # 队列调度同样不得在失败时 abort
    assert "abortUpload" not in _js_function(js, "processQueue")
    # 合并失败同样保留会话（可重试 complete）
    assert "合并失败" in chunked
    # 只有用户显式「放弃」才 abort + 清理本地记录
    cancel = _js_function(js, "cancelUpload")
    assert "abortUpload" in cancel
    assert "upResumeDel" in cancel


def test_pause_takes_effect_at_chunk_boundary():
    """单个任务暂停不中断在途分片：worker 在发下一片前 upWaitResume(item)，恢复后继续且不重传。"""
    js = _js()
    chunked = _js_function(js, "uploadOneChunked")
    assert "upWaitResume(item)" in chunked
    assert "item.paused" in chunked
    assert "item.pauseWaiters" in js
    assert "function upWaitResume(" in js
    # 暂停只影响当前任务，不再用全局 upPaused
    assert "upPaused" not in js
    # 暂停不主动取消在途分片（失败路径也不 abort，见上一个用例）
    assert "abortUpload" not in chunked


def test_resume_only_uploads_missing_chunks():
    """继续/重试只补缺失分片：用服务端 uploaded 初始化 Set，再只派发缺失索引。"""
    chunked = _js_function(_js(), "uploadOneChunked")
    assert "if (!item.uploaded.has(i)) pending.push(i)" in chunked
    assert "item.uploaded.forEach" in chunked
    # 进度按已确认分片累计，续传后百分比不归零
    assert "chunkLoaded" in chunked
    assert "item.loaded" in chunked


# ===========================================================================
# 5. 刷新后续传 / token 与文件指纹
# ===========================================================================

def test_refresh_reselect_documented_and_fingerprint_cache():
    """文案说明「刷新后重新选择同一文件」；本地缓存指纹（不存 token）用于发现文件变化。"""
    html, js = _html(), _js()
    assert "页面刷新" in html
    assert "重新选择同一文件" in html
    assert "UP_RESUME_STORE" in js
    assert "artifactdepot.uploadResume" in js
    assert "localStorage" in js
    # 指纹缓存只存 size/lastModified/upload_id/chunk_size，绝不写 token
    put = _js_function(js, "upResumePut")
    assert "localStorage.setItem" in put
    assert "token" not in put


def test_token_or_file_change_creates_new_session():
    """token 变更 / 文件 size·lastModified 变化 → 不复用旧会话，新建会话。"""
    init = _js_function(_js(), "initiateUpload")
    assert "lastModified" in init
    assert "upResumeDel(bucket, key)" in init
    assert "item.tokenSig" in init
    assert "resume = false" in init


# ===========================================================================
# 6. 严格 CSP / 样式标记
# ===========================================================================

def test_no_inline_script_style_or_handlers():
    """index.html 无内联 <script>/<style>/on* 处理器，只用 data-action 事件委托。"""
    html = _html()
    assert "<style>" not in html
    assert "<script>" not in html
    assert '<script src="/static/app.js" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/app.css" />' in html
    assert not re.search(r"\son[a-z]+\s*=", html)
    assert "javascript:void(0)" not in html


def test_css_has_per_task_pause_resume_styles():
    """app.css 提供暂停态文案、任务行内按钮、未完成上传列表与令牌门槛样式。"""
    css = _css()
    assert ".up-item .st.paused" in css
    assert ".up-item .up-act" in css
    assert ".up-item .up-act:disabled" in css
    assert ".up-item.pending" in css
    assert "#upFile:disabled" in css


# ===========================================================================
# 8. 「选择文件」令牌门槛 + 当前令牌未完成上传
# ===========================================================================

def test_token_required_before_selecting_file():
    """未填写令牌时「选择文件」与「上传」按钮禁用；输入令牌后 JS 即时解禁。"""
    html, js = _html(), _js()
    assert re.search(r'<input type="file" id="upFile"[^>]*\bdisabled\b', html)
    assert re.search(r'<button id="upBtn"[^>]*\bdisabled\b', html)
    assert 'id="upTokenHint"' in html
    gate = _js_function(js, "syncUploadGate")
    assert "$('upFile')" in gate
    assert "file.disabled = !on" in gate
    assert "$('upBtn')" in gate
    assert "uploadTokenValue()" in gate
    # 令牌输入即同步门槛，停止输入后刷新「未完成上传」
    assert "_upToken.addEventListener('input'" in js
    assert "syncUploadGate()" in js and "schedulePendingRefresh()" in js


def test_pending_uploads_list_for_current_token():
    """页面展示当前令牌未完成的分片会话，并允许手动取消（服务端 /api/objects/uploads）。"""
    html, js = _html(), _js()
    assert 'id="upPendingWrap"' in html
    assert 'id="upPending"' in html
    assert "未完成的上传（当前令牌）" in html
    assert "/api/objects/uploads" in js
    assert "function refreshPendingUploads(" in js
    assert "function renderPendingUploads(" in js
    assert "function cancelPendingUpload(" in js
    assert "data-action=\"cancel-pending-upload\"" in js
    assert "a === 'cancel-pending-upload'" in js
    # 只显示当前 token 的会话：同一个 upload_id 若已在本页队列中则不重复展示
    render = _js_function(js, "renderPendingUploads")
    assert "pendingUploads" in render and "upload_id" in render
    assert "active" in render
    # 取消时向服务端 abort 并清理本地断点记录
    cancel = _js_function(js, "cancelPendingUpload")
    assert "/api/objects/abort" in cancel
    assert "upResumeDropByUploadId" in cancel


def test_help_text_is_concise_and_has_no_platform_info():
    """上传说明精简，且不再出现协作平台 / 查看我的Token 等平台信息。"""
    html = _html()
    assert "协作平台" not in html
    assert "查看我的Token" not in html
    start = html.index("上传到：")
    end = html.index("</div>", start)
    text = html[start:end]
    assert "失败（可续传）" in text
    assert "拖拽" in text and "暂停上传 / 继续上传" in text
    assert len(text) < 700, "上传说明应精简（当前 %d 字符）" % len(text)


def test_no_global_pause_buttons_in_toolbar():
    """「并发数 / 上传」所在工具栏只保留文件选择、来源、并发与上传按钮。"""
    html = _html()
    m = re.search(r'<div class="row">\s*<input type="file" id="upFile".*?</div>', html, re.S)
    assert m, "未找到上传工具栏"
    toolbar = m.group(0)
    assert 'data-action="do-upload"' in toolbar
    assert 'data-action="pause-upload"' not in toolbar
    assert 'data-action="resume-upload"' not in toolbar
    assert 'id="upPause"' not in toolbar and 'id="upResume"' not in toolbar


def test_upload_toolbar_has_source_and_note():
    """上传工具栏：标签为「来源」（不再是「来源URL」）+ 可选「备注」，两条上传路径都提交 note。"""
    html = _html()
    assert "来源URL" not in html, "标签应已改为「来源」"
    assert '<label>来源: <input id="upSource"' in html
    assert '<label>备注: <input id="upNote"' in html
    js = _js()
    assert js.count("fd.append('note'") == 2, "直传与分片 complete 都要带上备注"
    assert js.count("fd.append('source_url'") == 2


def test_token_inputs_sync_across_tabs():
    """五个标签页的令牌输入框：任一填写后其余自动同步；URL 带入与改管理员令牌同样生效。"""
    html = _html()
    for tid in ("upToken", "lkToken", "admToken", "audToken", "cfgAdminToken"):
        assert 'id="%s"' % tid in html, "缺少令牌输入框 %s" % tid
    js = _js()
    assert ("const TOKEN_INPUT_IDS = ['upToken', 'lkToken', 'admToken', 'audToken', "
            "'cfgAdminToken']") in js
    assert "function setAllTokens(" in js
    # 五个输入框逐个挂 input 监听，输入即同步其余
    assert ("el.addEventListener('input', () => setAllTokens(el.value, id))") in js
    assert "TOKEN_INPUT_IDS.forEach((id) => {" in js
    # URL 带入的 token、配置页修改管理员令牌都走同一个同步入口
    assert "setAllTokens(urlToken, '')" in js
    assert "setAllTokens(values[CFG_SECRET_KEY], '')" in js
    # 令牌只在本页内存同步，不落浏览器存储
    body = js[js.index("function setAllTokens("):][:600]
    assert "localStorage" not in body and "sessionStorage" not in body


# ===========================================================================
# 7. 刷新/断线后的断点会话：明确「已中断」并提供「继续上传」重新选文件续传
# ===========================================================================

def test_pending_rows_show_interrupted_state_and_last_activity():
    """pending 行必须区分「最近有分片写入」与「已中断（未在传输）」，并显示最后活动时间。"""
    html, js = _html(), _js()
    assert 'id="upResumeFile"' in html
    assert "up-pending-tip" in html
    assert "idle_seconds" in js
    assert "PENDING_ACTIVE_SECONDS" in js
    assert "function pendingActivity(" in js
    assert "function fmtAgo(" in js
    render = _js_function(js, "renderPendingUploads")
    assert "已中断（未在传输）" in render
    assert "最近有分片写入" in render
    assert "最后活动：" in render
    assert "pending-state" in render
    assert "pending-sub" in render
    assert "pending-ago" in render
    # 进度条仍显示已传字节，但状态文字不再让断点看起来像正在上传
    assert "已传 " in render


def test_pending_resume_reselects_same_file_and_pins_target():
    """「继续上传」重新选择同一文件：校验名称/大小/指纹，固定会话 bucket/key，只补缺失分片。"""
    js = _js()
    assert "function resumePendingUpload(" in js
    assert "function onResumeFilePicked(" in js
    assert "addEventListener('change', onResumeFilePicked)" in js
    pick = _js_function(js, "resumePendingUpload")
    assert "input.click()" in pick
    assert "resumePendingId" in pick and "resumePendingToken" in pick
    # 最近仍有写入时先确认，避免与其它页面/设备重复上传
    assert "confirmDialog(" in pick
    picked = _js_function(js, "onResumeFilePicked")
    for token in ("file.size", "file.name", "upResumeFindByUploadId",
                  "lastModified", "item.pinned = true", "item.forcedChunked = true",
                  "item.upload_id = it.upload_id", "item.bucket = it.bucket",
                  "item.key = it.key", "processQueue()"):
        assert token in picked, f"onResumeFilePicked 缺少 {token}"
    # 续传必须把目标固定在会话的 bucket/key（不随当前浏览目录漂移）
    init = _js_function(js, "initiateUpload")
    assert "item.pinned ? item.bucket" in init and "item.pinned ? item.key" in init
    chunked = _js_function(js, "uploadOneChunked")
    assert "item.pinned ? item.bucket" in chunked and "item.pinned ? item.key" in chunked
    process = _js_function(js, "processQueue")
    assert "item.forcedChunked" in process and "if (!item.pinned)" in process
    # 会话失效时不能悄悄新建会话重传 66GB
    assert "断点会话已失效" in init
    assert "item.pinned" in init


def test_pending_list_polls_and_refreshes_when_visible_again():
    """断点列表定时轮询（其它页面/设备继续传输时进度自动更新），页面重新可见时立即刷新。"""
    js = _js()
    assert "function startPendingPoll(" in js
    assert "function stopPendingPoll(" in js
    assert "setInterval(" in _js_function(js, "startPendingPoll")
    assert "8000" in _js_function(js, "startPendingPoll")
    assert "pendingPollTimer" in js
    # 请求序号避免旧响应覆盖新状态
    assert "pendingSeq" in js
    assert "visibilitychange" in js
    assert "document.visibilityState === 'visible'" in js
    assert "refreshPendingUploads()" in js


def test_pending_css_has_live_stalled_states():
    """断点行样式：live/stalled 状态徽标、弱化进度条、第二行位置与最后活动。"""
    css = _css()
    assert ".up-item.pending.live .pending-state" in css
    assert ".up-item.pending.stalled .pending-state" in css
    assert ".up-item.pending.stalled progress" in css
    assert ".pending-sub .pending-key" in css
    assert ".pending-sub .pending-ago" in css
    assert ".up-pending-tip" in css


# ===========================================================================
# 8. 语法兜底（有 node 时校验 app.js）
# ===========================================================================

def test_app_js_syntax_if_node_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过 app.js 语法校验（静态断言已兜底）")
    r = subprocess.run([node, "--check", str(JS_PATH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
