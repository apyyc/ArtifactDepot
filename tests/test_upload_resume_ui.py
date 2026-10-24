# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""网页端「上传暂停/继续 + 断点续传」静态回归测试（契约 §D）

只读取前端源码文本，不启动服务、不跑浏览器：
  - index.html：upPause / upResume 按钮、队列行 retry-upload 结构、「失败（可续传）」文案、
    无内联 <script>/<style>/on* 处理器（严格 CSP，交互全部走 data-action 事件委托）；
  - app.js：initiate 带 bucket/key/size/resume/upload_id；队列项记录 upload_id/chunk_size/uploaded；
    失败不再 abortUpload（保留服务端分片会话）；暂停在分片边界生效；继续/重试只补缺失分片；
    页面刷新后重新选择同一文件可自动找回会话（本地只缓存文件指纹，不存 token）；
  - app.css：暂停/继续与队列行按钮样式。

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

def test_pause_resume_buttons():
    """「暂停上传」「继续上传」按钮 id 与 data-action 齐全，初始禁用并由 JS 动态启用。"""
    html, js = _html(), _js()
    assert 'id="upPause"' in html
    assert 'id="upResume"' in html
    assert 'data-action="pause-upload"' in html
    assert 'data-action="resume-upload"' in html
    # 严格 CSP：事件委托处理器必须存在
    assert "a === 'pause-upload'" in js
    assert "a === 'resume-upload'" in js
    assert "function pauseUploads(" in js
    assert "function resumeUploads(" in js
    # 无上传任务时按钮禁用，避免误点
    assert re.search(r'<button\s+id="upPause"[^>]*\sdisabled', html)
    assert re.search(r'<button\s+id="upResume"[^>]*\sdisabled', html)


def test_all_data_actions_have_js_handler():
    """index.html 里出现的每个 data-action 都要有 app.js 处理器（含队列行注释里的新动作）。"""
    html, js = _html(), _js()
    html_actions = set(re.findall(r'data-action="([a-z-]+)"', html))
    js_actions = set(re.findall(r"a === '([a-z-]+)'", js))
    assert {"pause-upload", "resume-upload", "retry-upload", "cancel-upload"} <= html_actions
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
    """暂停不中断在途分片：worker 在发下一片前 upWaitResume()，恢复后继续且不重传。"""
    js = _js()
    chunked = _js_function(js, "uploadOneChunked")
    assert "upWaitResume()" in chunked
    assert "upPaused" in chunked
    assert "upPauseWaiters" in js
    assert "function upWaitResume(" in js
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


def test_css_has_pause_resume_styles():
    """app.css 提供暂停态文案、队列行按钮、全局按钮禁用样式。"""
    css = _css()
    assert ".up-item .st.paused" in css
    assert ".up-item .up-act" in css
    assert "#upPause:disabled" in css and "#upResume:disabled" in css


# ===========================================================================
# 7. 语法兜底（有 node 时校验 app.js）
# ===========================================================================

def test_app_js_syntax_if_node_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过 app.js 语法校验（静态断言已兜底）")
    r = subprocess.run([node, "--check", str(JS_PATH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
