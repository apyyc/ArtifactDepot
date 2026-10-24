# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""网页端「单文件并行 Range 分块下载」静态防回归测试

服务端契约（由 tests/test_bulk_download.py / test_permissions.py 覆盖功能面）：
- GET  /api/auth/permissions → data.download = {chunk_size, chunk_concurrency, request_timeout_seconds}
- HEAD /api/objects/download?bucket=&key= → Content-Length / Accept-Ranges / Content-Disposition / ETag
- GET  /api/objects/download + Range: bytes=a-b → 206 + Content-Range: bytes a-b/total
- 全部同源请求，token 只走 Authorization 头（不拼进 URL / DOM）

本文件只做静态契约校验（不启动服务）：
- app.js 有下载配置读取、并行分块、HEAD 探测、FS Access、Blob/原生回退、重试、取消、SHA-256 校验
- index.html 有下载进度弹窗（dlModal / 取消按钮）且无内联事件
- app.css 有进度条样式
- 文件行下载不再把 token 拼进 href（data-action="download-file" + data-key）
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "src" / "artifactdepot" / "resources"


def _html() -> str:
    return (BASE / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (BASE / "static" / "app.js").read_text(encoding="utf-8")


def _css() -> str:
    return (BASE / "static" / "app.css").read_text(encoding="utf-8")


def _js_function(js: str, name: str) -> str:
    """截取 app.js 里某个顶层函数（含嵌套体）。

    顶层声明都从第 0 列开始，嵌套函数有缩进，因此用「下一个第 0 列的
    function/const/let/var 声明」作为边界即可，避免手写大括号配对。
    """
    m = re.search(r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", js, re.M)
    assert m, f"app.js 未找到函数 {name}()"
    tail = js[m.end():]
    nxt = re.search(r"\n(?=(?:async\s+)?function\s+[A-Za-z_$]|(?:const|let|var)\s+[A-Za-z_$])", tail)
    end = m.end() + (nxt.start() if nxt else len(js) - m.end())
    return js[m.start():end]


# ===========================================================================
# 1. 下载参数配置：默认值 + 从 /api/auth/permissions 的 data.download 读取
# ===========================================================================

def test_app_js_download_cfg_defaults_and_clamp():
    """DEFAULT_DOWNLOAD_CFG 默认 8MiB / 4 并发 / 3600s，且有夹紧逻辑（防脏配置）。"""
    js = _js()
    assert "DEFAULT_DOWNLOAD_CFG" in js
    assert "let downloadCfg" in js and "Object.assign({}, DEFAULT_DOWNLOAD_CFG)" in js

    m = re.search(r"const DEFAULT_DOWNLOAD_CFG = \{(.*?)\n\}", js, re.S)
    assert m, "未找到 DEFAULT_DOWNLOAD_CFG 默认值块"
    block = m.group(1)
    assert "8 * 1024 * 1024" in block          # 8MiB 分片
    assert re.search(r"chunk_concurrency:\s*4", block)
    assert re.search(r"request_timeout_seconds:\s*3600", block)

    body = _js_function(js, "applyDownloadConfig")
    assert "downloadCfg.chunk_size" in body
    assert "downloadCfg.chunk_concurrency" in body
    assert "downloadCfg.request_timeout_seconds" in body
    assert "Math.min" in body and "Math.max" in body   # 上下界夹紧


def test_load_upload_config_also_reads_download_cfg():
    """loadUploadConfig（保留函数名，test_upload_config 有防回归断言）同请求读取 data.download。"""
    js = _js()
    body = _js_function(js, "loadUploadConfig")
    assert "/api/auth/permissions" in body
    assert "applyUploadConfig" in body          # 原有上传配置读取不能丢
    assert "applyDownloadConfig" in body        # 新增下载配置读取
    assert "download" in body


# ===========================================================================
# 2. 文件行下载按钮：data-action + data-key，不再把 token 拼进 href
# ===========================================================================

def test_file_row_download_has_no_token_in_href():
    js = _js()
    body = _js_function(js, "renderRows")
    assert 'data-action="download-file"' in body
    assert "data-key=" in body
    assert "&token=" not in body, "文件行下载链接不得再把 token 拼进 URL"
    assert "encodeURIComponent($('upToken')" not in body


def test_click_delegation_handles_download_actions():
    js = _js()
    assert "a === 'download-file'" in js
    assert "a === 'cancel-download'" in js
    assert "downloadFile(el.dataset.key)" in js


# ===========================================================================
# 3. 下载实现：HEAD 探测 / Range 并行 / 重试 / FS Access / Blob 回退 / 取消 / 完整性
# ===========================================================================

def test_probe_uses_head_and_download_endpoint():
    js = _js()
    body = _js_function(js, "dlProbe")
    assert "/api/objects/download" in body
    assert "'HEAD'" in body
    # 服务端未实现 HEAD 时的兜底探测
    assert "405" in body and "bytes=0-0" in body
    once = _js_function(js, "dlProbeOnce")
    assert "Authorization" not in once          # 走 authHeaders() 统一加头
    assert "authHeaders(token" in once
    assert "cache: 'no-store'" in once


def test_parallel_range_chunks():
    js = _js()
    body = _js_function(js, "dlFetchChunk")
    assert "/api/objects/download?bucket=" in js
    assert "Range: 'bytes='" in body
    assert "206" in body                        # 必须校验 206
    assert "Content-Range" in body              # 校验分片起始
    assert "DL_MAX_RETRIES" in body             # 失败重试
    assert "AbortController" in body
    assert "r.status === 200" in body           # 服务端忽略 Range → 回退
    assert "rangeIgnored" in body

    par = _js_function(js, "dlParallel")
    assert "downloadCfg.chunk_concurrency" in par
    assert "Promise.all" in par
    assert "position: idx * chunkSize" in par   # FS 路径按字节位置写入
    assert "task.parts.push(buf)" in par        # Blob 路径按顺序拼
    assert "nextWrite" in par                   # 顺序写入保证不串块


def test_fs_access_and_blob_and_native_fallbacks():
    js = _js()
    assert "window.showSaveFilePicker" in js
    body = _js_function(js, "downloadFile")
    assert "showSaveFilePicker" in body
    assert "suggestedName" in body
    assert "createWritable" in body
    assert "createWritable" in body
    assert "writable.close()" in body
    assert "dlDropWritable" in body             # 失败/取消时 abort writable

    native = _js_function(js, "nativeDownload")
    assert "/api/objects/download" in native
    assert "&token=" in native                  # 原生单流仍用 query（a 标签无法带 Authorization）
    assert "a.click()" in native

    assert "DL_BLOB_LIMIT" in js                # >1GiB 放弃 Blob 并行
    assert "1024 * 1024 * 1024" in js
    assert "dlSaveBlob" in js

    # 取消：中止所有在途请求 + 唤醒等写入顺序的 worker
    cancel = _js_function(js, "cancelDownload")
    assert "AbortController" in js
    assert "c.abort()" in cancel
    assert "task.wake" in cancel
    assert "dlHide" in cancel


def test_download_progress_and_cleanup():
    js = _js()
    upd = _js_function(js, "dlUpdate")
    assert "dlBarFill" in upd
    assert "fmtSize" in upd
    assert "%" in upd
    assert "speed" in upd
    clean = _js_function(js, "dlCleanup")
    assert "clearInterval" in clean
    assert "c.abort()" in clean
    assert "dlTask = null" in clean
    # 结束/失败/取消都隐藏弹窗
    body = _js_function(js, "downloadFile")
    assert body.count("dlHide()") >= 4
    assert "dlCleanup(task)" in body


def test_integrity_sha256_verification():
    js = _js()
    assert "crypto.subtle.digest" in js
    assert "SHA-256" in js
    verify = _js_function(js, "dlVerify")
    assert "crypto.subtle" in js
    assert "dlEtagSha256" in verify
    etag = _js_function(js, "dlEtagSha256")
    assert "{64}" in etag                       # 仅认 64 位 hex 的 ETag
    assert "hashSkipped" in verify              # 超大文件跳过校验并提示


# ===========================================================================
# 4. index.html / app.css 标记
# ===========================================================================

def test_html_download_modal_markers():
    html = _html()
    assert 'id="dlModal"' in html
    assert re.search(r'id="dlModal"[^>]*style="display:\s*none;?"', html)   # 默认隐藏
    assert 'data-action="download-file"' in html
    assert 'data-action="cancel-download"' in html
    assert 'id="dlBar"' in html and 'id="dlBarFill"' in html
    assert 'id="dlStat"' in html
    assert 'id="dlName"' in html
    # 文案：上传/下载提示要说明「大于分片大小时自动多线程分块下载」
    assert "多线程分块下载" in html
    assert "config.json 的 download.*" in html


def test_html_has_no_inline_event_handlers():
    html = _html()
    assert not re.search(r"\son[a-z]+\s*=", html), "index.html 不得出现内联事件处理器"
    assert "onclick=" not in html
    assert "javascript:void(0)" not in html
    assert "<script>" not in html
    assert "<style>" not in html


def test_css_has_download_progress_bar():
    css = _css()
    assert ".dl-bar" in css
    assert re.search(r"\.dl-bar\s*>\s*span", css), ".dl-bar > span 进度条填充样式缺失"
    assert ".dl-stat" in css
    assert ".dl-name" in css


# ===========================================================================
# 5. 约束：不引入第三方库 / CDN；JS 语法可解析
# ===========================================================================

def test_no_third_party_or_cdn():
    js = _js()
    assert "https://" not in js and "http://" not in js
    assert "cdn." not in js.lower()
    html = _html()
    assert "https://" not in html and "http://" not in html


def test_js_syntax_with_node():
    """node --check 校验 app.js 语法（无 node 时跳过，静态关键字测试已兜底）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过 JS 语法检查")
    r = subprocess.run([node, "--check", str(BASE / "static" / "app.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout
