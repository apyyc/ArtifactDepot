// Copyright 2026 apyyc
// SPDX-License-Identifier: Apache-2.0

const $ = (id) => document.getElementById(id)

// 令牌输入框：五个标签页各一个，任一标签填写后其余自动同步，避免逐页重复输入。
// 只在当前页面内存里同步，不写 localStorage/sessionStorage（令牌不落盘；刷新后可用 ?token= 重新带入）。
const TOKEN_INPUT_IDS = ['upToken', 'lkToken', 'admToken', 'audToken', 'cfgAdminToken']

function setAllTokens(value, sourceId) {
  TOKEN_INPUT_IDS.forEach((id) => {
    if (id === sourceId) return
    const el = $(id)
    if (!el || el.value === value) return
    el.value = value
    // 派发 input 事件，让该输入框自己的联动（上传门槛、未完成上传列表等）一起生效
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

let state = { bucket: '', prefix: '', limit: 200, offset: 0, items: [], total: 0, truncated: false }
// 网页分页配置（GET /api/auth/permissions 的 data.ui，可在「配置」标签页热改）
let uiCfg = { file_page_size: 200, audit_page_size: 500 }
// 公网 IP 由服务端按 config.trusted_proxies 解析 X-Forwarded-For，前端不再自报/调用第三方

function toast(text, ok = true) {
  const m = $('msg')
  m.style.background = ok ? '#67c23a' : '#f56c6c'
  m.textContent = text
  m.style.display = 'block'
  setTimeout(() => { m.style.display = 'none' }, 3000)
}

function switchTab(t) {
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.t === t))
  ;['browse', 'links', 'tokens', 'audit', 'config'].forEach(s => {
    $('sec-' + s).style.display = s === t ? '' : 'none'
  })
  // 进入 Token 页时拉取现有 bucket，渲染「资源范围」多选框（拉过一次后复用，「刷新现有 bucket」可强制刷新）
  if (t === 'tokens') loadTokenBuckets()
  if (t === 'config') loadConfigPanel(false)
}

function authHeaders(token, extra) {
  const h = Object.assign({}, extra || {})
  if (token) h['Authorization'] = 'Bearer ' + token
  return h
}

// token 优先走 Authorization 头，避免出现在 URL query（访问日志/历史泄露）
async function api(url, opts = {}, token = '', timeoutMs = 0) {
  opts = Object.assign({}, opts)
  opts.headers = authHeaders(token, opts.headers)
  let timer = null
  if (timeoutMs > 0 && !opts.signal) {
    const ctrl = new AbortController()
    opts.signal = ctrl.signal
    timer = setTimeout(() => ctrl.abort(), timeoutMs)
  }
  try {
    const r = await fetch(url, opts)
    let body = null
    try { body = await r.json() } catch (e) { body = null }
    if (!r.ok) {
      const err = new Error((body && body.detail) || ('HTTP ' + r.status))
      err.status = r.status   // 便于调用方区分 401/403/404（如批量删除的权限提示）
      throw err
    }
    return body
  } catch (e) {
    if (e && e.name === 'AbortError') throw new Error('请求超时')
    throw e
  } finally {
    if (timer) clearTimeout(timer)
  }
}

// 事件委托：不可信 key/id 只放 data-* 属性，避免拼进内联 onclick 造成 XSS
document.addEventListener('click', (e) => {
  const el = e.target && e.target.closest ? e.target.closest('[data-action]') : null
  if (!el) return
  e.preventDefault()
  const a = el.dataset.action
  if (a === 'enter-dir') enterDir(el.dataset.key)
  else if (a === 'del-dir') delDir(el.dataset.key)
  else if (a === 'presign') presign(el.dataset.key)
  else if (a === 'del-file') delFile(el.dataset.key)
  else if (a === 'revoke-link') revokeLink(el.dataset.id)
  else if (a === 'remove-token') removeToken(el.dataset.token)
  else if (a === 'go-up') goUp()
  else if (a === 'load-more') loadMore()
  else if (a === 'switch-tab') switchTab(el.dataset.tab)
  else if (a === 'list-buckets') listBuckets()
  else if (a === 'open-bucket') openBucket()
  else if (a === 'do-upload') doUpload()
  else if (a === 'pause-upload') pauseUpload(el.dataset.uid)
  else if (a === 'resume-upload') resumeUpload(el.dataset.uid)
  else if (a === 'retry-upload') retryUpload(el.dataset.uid)
  else if (a === 'cancel-upload') cancelUpload(el.dataset.uid)
  else if (a === 'cancel-pending-upload') cancelPendingUpload(el.dataset.uid)
  else if (a === 'open-mkdir') openMkdirModal()
  else if (a === 'do-mkdir') doMkdir()
  else if (a === 'close-mkdir') closeMkdirModal()
  else if (a === 'download-dir') downloadDir()
  else if (a === 'download-file') downloadFile(el.dataset.key)
  else if (a === 'cancel-download') cancelDownload()
  else if (a === 'bulk-delete') openBulkDeleteModal(el.dataset.mode)
  else if (a === 'do-bulk-delete') doBulkDelete()
  else if (a === 'close-bulk-delete') closeBulkDeleteModal()
  else if (a === 'do-presign') doPresign()
  else if (a === 'close-presign') closePsModal()
  else if (a === 'copy-presign') copyPsResult()
  else if (a === 'copy-link') copyLink(el)
  else if (a === 'load-links') loadLinks()
  else if (a === 'load-tokens') loadTokens()
  else if (a === 'load-tk-buckets') loadTokenBuckets(true)
  else if (a === 'generate-token') generateToken()
  else if (a === 'copy-token') copyTkResult()
  else if (a === 'sync-tokens') syncTokens()
  else if (a === 'reload-config') reloadConfig()
  else if (a === 'load-config') loadConfigPanel(true)
  else if (a === 'save-config') saveConfigPanel()
  else if (a === 'reload-config-file') reloadConfigFile()
  else if (a === 'select-write') tkSelectWrite(el.dataset.on === 'true')
  else if (a === 'select-all') tkSelectAll(el.dataset.on === 'true')
  else if (a === 'confirm') cfmResolve(el.dataset.ok === 'true')
  else if (a === 'run-audit') runAudit()
})

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ))
}

// 时间显示：后端存 UTC（...Z）→ 转浏览器本地时间；旧数据无 Z 视为本地原样显示
function fmtDuration(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0))
  if (sec < 60) return sec + ' 秒'
  if (sec < 3600) return Math.round(sec / 60) + ' 分钟'
  if (sec < 86400) return (Math.round(sec / 360) / 10) + ' 小时'
  return (Math.round(sec / 8640) / 10) + ' 天'
}

function fmtTime(s) {
  if (!s) return ''
  if (typeof s === 'string' && /Z$/.test(s)) {
    const d = new Date(s)
    if (!isNaN(d)) {
      const p = n => String(n).padStart(2, '0')
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
        p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
    }
  }
  return s
}

function fmtSize(n) {
  if (!n && n !== 0) return '-'
  if (n < 1024) return n + ' B'
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB'
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB'
  return (n / 1073741824).toFixed(2) + ' GB'
}

// ---- 浏览 ----

function renderBuckets(arr, selected) {
  const box = $('bucketList')
  box.innerHTML = ''
  if (!arr.length) { box.innerHTML = '<span class="muted">（暂无 bucket，可直接在输入框输入新名字进入）</span>'; return }
  arr.forEach(b => {
    const chip = document.createElement('a')
    chip.href = '#'
    chip.className = 'bucket-chip' + (b === selected ? ' selected' : '')
    chip.textContent = b
    chip.onclick = (e) => { e.preventDefault(); $('bucket').value = b; openBucket() }
    box.appendChild(chip)
  })
}

async function listBuckets() {
  try {
    const body = await api('/api/buckets')
    renderBuckets(body.data || [], state.bucket)
  } catch (e) { toast(e.message, false) }
}

// 页面加载即列出 bucket（可点击选中）
listBuckets()

// URL 携带 token/bucket 进入（从外部系统跳转自动认证）
// 从 query 读 token 填入输入框，并立即清除地址栏里的 token（防进历史/Referer 泄露）
const urlParams = new URLSearchParams(location.search)
const urlToken = urlParams.get('token')
const urlBucket = urlParams.get('bucket')
if (urlToken) {
  setAllTokens(urlToken, '')   // 五个标签页的令牌输入框一起填入（浏览/签名链接/Token/审计/配置）
  // 顺带校验一次，提示当前身份（无效 token 不打扰，让用户自行填写）
  checkToken(urlToken).then(actor => { if (actor) toast('已识别身份：' + actor) })
}
history.replaceState({}, '', location.pathname + (urlBucket ? '?bucket=' + encodeURIComponent(urlBucket) : ''))
if (urlBucket) {
  $('bucket').value = urlBucket
  openBucket()   // 自动进入目标 bucket
}

async function openBucket() {
  const b = $('bucket').value.trim()
  if (!b) { toast('请输入 bucket', false); return }
  state.bucket = b; state.prefix = ''
  await refresh()
  // 刷新 bucket 列表并高亮当前选中的
  try {
    const body = await api('/api/buckets')
    renderBuckets(body.data || [], state.bucket)
  } catch (e) { /* 忽略 */ }
}

function enterDir(key) {
  state.prefix = key.replace(/\/$/, '')
  refresh()
}

function goUp() {
  const parts = state.prefix.split('/').filter(Boolean)
  parts.pop()
  state.prefix = parts.join('/')
  refresh()
}

async function refresh() {
  const crumb = state.bucket + ' / ' + (state.prefix ? '/' + state.prefix + '/' : '/')
  $('crumb').textContent = crumb
  $('upTarget').textContent = state.bucket + ' / ' + (state.prefix ? state.prefix + '/' : '')
  applyBulkOpsState()   // 批量删除按钮仅在已选择 bucket 后出现
  // 重置分页，重新拉第一页
  state.offset = 0
  state.items = []
  state.total = 0
  state.truncated = false
  await loadPage()
}

async function loadPage() {
  const q = 'bucket=' + encodeURIComponent(state.bucket) + '&prefix=' + encodeURIComponent(state.prefix) +
            '&limit=' + state.limit + '&offset=' + state.offset
  try {
    const body = await api('/api/objects/list?' + q)
    const d = body.data || {}
    state.items = state.items.concat(d.items || [])
    state.total = d.total || state.items.length
    state.truncated = !!d.truncated
    state.offset = state.items.length
  } catch (e) { toast(e.message, false) }
  renderRows()
}

function loadMore() { loadPage() }

function renderRows() {
  const tbody = document.querySelector('#tbl tbody')
  tbody.innerHTML = ''
  if (state.prefix) {
    const tr = document.createElement('tr')
    tr.innerHTML = '<td colspan="6"><a class="dir" href="#" data-action="go-up">.. 上一级</a></td>'
    tbody.appendChild(tr)
  }
  for (const it of state.items) {
    const tr = document.createElement('tr')
    if (it.is_dir) {
      tr.innerHTML = '<td><a class="dir" href="#" data-action="enter-dir" data-key="' + esc(it.key) + '">' + esc(it.name) + '/</a></td>' +
        '<td>' + fmtSize(it.size) + '</td>' +
        '<td class="muted">' + esc(fmtTime(it.mtime)) + '</td>' +
        '<td>-</td>' +
        '<td>-</td>' +
        '<td><a href="#" data-action="del-dir" data-key="' + esc(it.key) + '" style="color:#f56c6c;">删除</a></td>'
    } else {
      // 下载按钮只带 data-key，token 由 downloadFile() 走 Authorization 头（不再拼进 href）
      tr.innerHTML =
        '<td>' + esc(it.name) + '</td>' +
        '<td>' + fmtSize(it.size) + '</td>' +
        '<td class="muted">' + esc(fmtTime(it.mtime)) + '</td>' +
        '<td class="muted" title="' + esc(it.source_url) + '">' + esc((it.source_url || '').slice(0, 28)) + '</td>' +
        '<td class="muted">' + esc(it.uploader || '-') + '</td>' +
        '<td><a href="#" data-action="download-file" data-key="' + esc(it.key) + '">下载</a> <a href="#" data-action="presign" data-key="' + esc(it.key) + '">签名链接</a> <a href="#" data-action="del-file" data-key="' + esc(it.key) + '" style="color:#f56c6c;">删除</a></td>'
    }
    tbody.appendChild(tr)
  }
  if (state.truncated) {
    const tr = document.createElement('tr')
    tr.innerHTML = '<td colspan="6" class="muted">已显示 ' + state.items.length + ' / ' + state.total +
      ' 项　<a href="#" data-action="load-more">加载更多</a></td>'
    tbody.appendChild(tr)
  }
}

let psKey = ''
let psCfg = { count_min: 1, count_max: 10, expire_min_seconds: 60, expire_max_seconds: 604800 }

function psTypeChange() {
  const t = document.querySelector('input[name="psType"]:checked').value
  $('psTimeRow').style.display = t === 'time' ? 'flex' : 'none'
  $('psCountRow').style.display = t === 'count' ? 'flex' : 'none'
  $('psPermanentHint').style.display = t === 'permanent' ? 'block' : 'none'
}

async function presign(key) {
  const token = $('upToken').value.trim()
  if (!token) { toast('请先填写令牌', false); return }
  psKey = key
  $('psTarget').textContent = '文件：' + state.bucket + ' / ' + key
  $('psResult').value = ''
  $('psResultRow').style.display = 'none'
  // 拉取次数/时效上下限（config.signed_links）
  try {
    const b = await api('/api/objects/signed-links/config')
    psCfg = b.data || psCfg
  } catch (e) { /* 用默认值 */ }
  $('psCountHint').textContent = '（' + psCfg.count_min + '-' + psCfg.count_max + ' 次）'
  $('psCount').value = psCfg.count_min
  $('psCount').min = psCfg.count_min; $('psCount').max = psCfg.count_max
  const hmin = Math.max(0.1, Math.round(psCfg.expire_min_seconds / 3600 * 10) / 10)
  const hmax = Math.max(hmin, Math.round(psCfg.expire_max_seconds / 3600 * 10) / 10)
  $('psHours').value = Math.min(1, hmax)
  $('psHours').min = hmin; $('psHours').max = hmax
  $('psTimeHint').textContent = '（' + hmin + '-' + hmax + ' 小时）'
  // 默认按时效
  document.querySelector('input[name="psType"][value="time"]').checked = true
  psTypeChange()
  $('presignModal').style.display = 'flex'
}
function closePsModal() { $('presignModal').style.display = 'none' }
async function doPresign() {
  const token = $('upToken').value.trim()
  const type = document.querySelector('input[name="psType"]:checked').value
  const body = { bucket: state.bucket, key: psKey, mode: type }
  if (type === 'count') {
    const c = parseInt($('psCount').value)
    if (isNaN(c) || c < psCfg.count_min || c > psCfg.count_max) {
      toast('次数需在 ' + psCfg.count_min + '-' + psCfg.count_max + ' 之间', false); return
    }
    body.count = c
  } else if (type === 'time') {
    const h = parseFloat($('psHours').value)
    const secs = Math.round(h * 3600)
    if (isNaN(secs) || secs < psCfg.expire_min_seconds || secs > psCfg.expire_max_seconds) {
      toast('时效需在 ' + (psCfg.expire_min_seconds / 3600) + '-' + (psCfg.expire_max_seconds / 3600) + ' 小时之间', false); return
    }
    body.expires = secs
  }
  try {
    const r = await api('/api/objects/presign', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }, token)
    const u = window.location.origin + (r.data.url || '')
    $('psResult').value = u
    $('psResultRow').style.display = 'flex'
    toast('链接已生成')
    $('psResult').focus(); $('psResult').select()
  } catch (e) { toast(e.message, false) }
}
async function copyPsResult() {
  const v = $('psResult').value
  if (!v) return
  copyText(v, '链接已复制')
}

// ---- 新建目录 ----
function openMkdirModal() {
  if (!state.bucket) { toast('请先选择 bucket', false); return }
  $('mkdirTarget').textContent = '位置：' + state.bucket + (state.prefix ? ' / ' + state.prefix + '/' : ' / ')
  $('mkdirName').value = ''
  $('mkdirModal').style.display = 'flex'
  setTimeout(() => $('mkdirName').focus(), 50)
}
function closeMkdirModal() { $('mkdirModal').style.display = 'none' }
async function doMkdir() {
  const t = $('upToken').value.trim()
  const name = $('mkdirName').value.trim()
  if (!t) { toast('请先填写令牌', false); return }
  if (!name) { toast('请输入目录名', false); return }
  const key = state.prefix ? state.prefix + '/' + name : name
  try {
    await api('/api/objects/mkdir', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bucket: state.bucket, key }) }, t)
    toast('已创建目录：' + key)
    closeMkdirModal()
    await refresh()
  } catch (e) { toast(e.message, false) }
}

// 自定义确认框（替代 confirm：部分环境浏览器原生弹窗被拦截）
let _cfmResolve = null
function confirmDialog(msg) {
  $('cfmMsg').textContent = msg
  $('confirmModal').style.display = 'flex'
  return new Promise(res => { _cfmResolve = res })
}
function cfmResolve(v) {
  $('confirmModal').style.display = 'none'
  if (_cfmResolve) { _cfmResolve(v); _cfmResolve = null }
}

// ---- 批量删除（当前目录一键清空）----
// 入口按钮：data-action="bulk-delete" data-mode="files|all"；确认按钮 data-action="do-bulk-delete"。
// 契约：POST /api/objects/bulk-delete {bucket,prefix,mode,dry_run,confirm}；先 dry_run 预览，再 dry_run:false 执行。
let bulkMode = 'files'
let bulkBusy = false
let bulkPreviewOk = false
let bulkReqSeq = 0   // 防止在途预览响应覆盖新打开的弹窗

// 需要输入的确认名称：prefix 最后一段；bucket 根目录为 bucket 名（与服务端一致）
function bulkTargetName() {
  if (!state.prefix) return state.bucket
  const parts = state.prefix.split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : state.bucket
}

function bulkTargetPath() {
  return state.bucket + ' / ' + (state.prefix ? state.prefix + '/' : '')
}

function bulkModeLabel(mode) {
  return mode === 'all' ? '删除本目录全部内容' : '清空本目录文件'
}

function bulkModalOpen() {
  const m = $('bulkDeleteModal')
  return !!m && m.style.display !== 'none'
}

// 按钮仅在已选择 bucket 时可见/可用
function applyBulkOpsState() {
  const box = $('bulkOps')
  if (!box) return
  const on = !!state.bucket
  box.style.display = on ? 'inline-flex' : 'none'
  box.querySelectorAll('[data-action="bulk-delete"]').forEach(b => { b.disabled = !on })
  // 「下载本目录」入口同样只在已选 bucket 后出现（只读操作，无确认弹窗）
  const dl = $('downloadDirBtn')
  if (dl) {
    dl.style.display = on ? '' : 'none'
    dl.disabled = !on
  }
}

// 403/401/404 等给出可操作提示（不拼接 HTML，toast 内部用 textContent）
function bulkErrText(e) {
  const msg = (e && e.message) ? e.message : String(e)
  const status = e && e.status
  if (status === 403) {
    if (msg.indexOf('object:delete_bulk') >= 0) {
      return '权限不足：缺少 object:delete_bulk 权限点，请联系管理员在「Token 管理」里为该 token 勾选「批量删除目录内容」（operator / admin 预设已包含）'
    }
    return '权限不足：该 token 的资源范围不允许删除此 bucket / 路径（' + msg + '）'
  }
  if (status === 401) return '令牌无效或已过期，请重新填写令牌（' + msg + '）'
  if (status === 404) return '目录不存在或不是目录（' + msg + '）'
  return msg
}

function bulkUpdateConfirm() {
  const btn = $('bulkDoBtn')
  const input = $('bulkConfirmInput')
  if (!btn || !input) return
  const name = bulkTargetName()
  const match = input.value.trim() === name
  btn.disabled = bulkBusy || !bulkPreviewOk || !match
  const hint = $('bulkNameHint')
  if (hint) {
    if (!input.value.trim()) { hint.textContent = '请输入目录名：' + name; hint.className = 'bulk-hint' }
    else if (match) { hint.textContent = '名称匹配，可以执行删除'; hint.className = 'bulk-hint ok' }
    else { hint.textContent = '名称不匹配：需精确输入「' + name + '」'; hint.className = 'bulk-hint bad' }
  }
}

async function openBulkDeleteModal(mode) {
  if (!state.bucket) { toast('请先选择 bucket', false); return }
  const t = $('upToken').value.trim()
  if (!t) { toast('请先填写令牌（批量删除需要 object:delete_bulk 权限）', false); return }
  bulkMode = mode === 'all' ? 'all' : 'files'
  bulkBusy = false
  bulkPreviewOk = false
  bulkReqSeq++
  const seq = bulkReqSeq
  const name = bulkTargetName()
  $('bulkTitle').textContent = bulkModeLabel(bulkMode)
  $('bulkTarget').textContent = '目标位置：' + bulkTargetPath()
  $('bulkWarning').textContent = bulkMode === 'all'
    ? '注意：删除不可恢复。将递归删除本目录下的全部文件与子目录（当前目录本身保留，bucket 清单文件受保护）。'
    : '注意：删除不可恢复。只删除本目录下的直接文件；子目录及其内部文件会保留；隐藏文件与 .keep 占位文件保留。'
  const input = $('bulkConfirmInput')
  input.value = ''
  input.placeholder = '请输入目录名「' + name + '」以确认'
  const preview = $('bulkPreview')
  preview.className = 'bulk-preview'
  preview.textContent = '（正在统计将删除的内容…）'
  bulkUpdateConfirm()
  $('bulkDeleteModal').style.display = 'flex'
  setTimeout(() => input.focus(), 50)
  await loadBulkPreview(seq)
}

async function loadBulkPreview(seq) {
  const t = $('upToken').value.trim()
  if (!t) return
  const preview = $('bulkPreview')
  try {
    const r = await api('/api/objects/bulk-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bucket: state.bucket,
        prefix: state.prefix || '',
        mode: bulkMode,
        dry_run: true,
      }),
    }, t)
    if (seq !== bulkReqSeq || !bulkModalOpen()) return
    const d = r.data || {}
    const files = Number(d.files) || 0
    const dirs = Number(d.dirs) || 0
    const bytes = Number(d.bytes) || 0
    let txt = '将删除 ' + files + ' 个文件'
    if (bulkMode === 'all') txt += '、' + dirs + ' 个目录'
    txt += '，共 ' + fmtSize(bytes)
    const errs = Array.isArray(d.errors) ? d.errors : []
    if (errs.length) txt += '（有 ' + errs.length + ' 项无法预览）'
    preview.className = 'bulk-preview'
    preview.textContent = txt
    bulkPreviewOk = true
  } catch (e) {
    if (seq !== bulkReqSeq || !bulkModalOpen()) return
    bulkPreviewOk = false
    preview.className = 'bulk-preview bulk-preview-err'
    preview.textContent = '预览失败：' + bulkErrText(e)
  }
  bulkUpdateConfirm()
}

async function doBulkDelete() {
  if (bulkBusy) return
  const t = $('upToken').value.trim()
  if (!t) { toast('请先填写令牌', false); return }
  const name = bulkTargetName()
  const input = $('bulkConfirmInput')
  if (input.value.trim() !== name) { toast('请输入正确的目录名以确认', false); return }
  if (!bulkPreviewOk) { toast('预览未成功，暂时不能执行删除', false); return }
  const btn = $('bulkDoBtn')
  const mode = bulkMode
  bulkBusy = true
  btn.disabled = true
  btn.textContent = '删除中…'
  try {
    const r = await api('/api/objects/bulk-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bucket: state.bucket,
        prefix: state.prefix || '',
        mode: mode,
        dry_run: false,
        confirm: name,
      }),
    }, t)
    const d = r.data || {}
    const files = Number(d.files) || 0
    const dirs = Number(d.dirs) || 0
    const bytes = Number(d.bytes) || 0
    const errs = Array.isArray(d.errors) ? d.errors : []
    const target = bulkTargetPath()
    closeBulkDeleteModal()
    if (errs.length) {
      const first = errs[0] || {}
      const detail = (first.key ? '（如 ' + first.key + (first.error ? '：' + first.error : '') + '）' : '')
      toast('部分失败：成功删除 ' + files + ' 个文件' +
        (mode === 'all' ? '、' + dirs + ' 个目录' : '') +
        '，有 ' + errs.length + ' 项失败' + detail, false)
    } else {
      toast('已删除 ' + files + ' 个文件' +
        (mode === 'all' ? '、' + dirs + ' 个目录' : '') +
        '，共 ' + fmtSize(bytes) + '　' + target)
    }
    await refresh()
  } catch (e) {
    bulkBusy = false
    btn.textContent = '确认删除'
    bulkUpdateConfirm()
    toast('删除失败：' + bulkErrText(e), false)
  }
}

function closeBulkDeleteModal() {
  const m = $('bulkDeleteModal')
  if (m) m.style.display = 'none'
  bulkBusy = false
  bulkPreviewOk = false
  bulkReqSeq++   // 作废在途预览响应
  const input = $('bulkConfirmInput')
  if (input) { input.value = ''; input.placeholder = '' }
  const preview = $('bulkPreview')
  if (preview) { preview.textContent = ''; preview.className = 'bulk-preview' }
  const warning = $('bulkWarning')
  if (warning) warning.textContent = ''
  const hint = $('bulkNameHint')
  if (hint) { hint.textContent = ''; hint.className = 'bulk-hint' }
  const btn = $('bulkDoBtn')
  if (btn) { btn.disabled = true; btn.textContent = '确认删除' }
}

// ---- 签名链接管理 ----
async function loadLinks() {
  const t = $('lkToken').value.trim()
  if (!t) { toast('请输入令牌', false); return }
  try {
    const body = await api('/api/objects/signed-links', {}, t)
    const tb = document.querySelector('#lkTbl tbody')
    tb.innerHTML = ''
    const rows = (body.data || []).slice().sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    if (!rows.length) tb.innerHTML = '<tr><td colspan="8" class="muted">（无链接）</td></tr>'
    for (const l of rows) {
      const tr = document.createElement('tr')
      let modeTxt
      if (l.mode === 'count') modeTxt = '次数 ' + l.max_uses
      else if (l.mode === 'time') {
        const createdSec = Date.parse(l.created_at || '') / 1000
        const secs = (l.expires && !isNaN(createdSec)) ? (l.expires - createdSec) : null
        modeTxt = secs != null ? '时效 ' + fmtDuration(secs) : '时效'
      } else modeTxt = '永久'
      const remaining = l.mode === 'count' ? (l.remaining || 0) + ' 次' : '—'
      let statusHtml
      if (l.revoked) statusHtml = '<span style="color:#f56c6c;">已作废</span>'
      else if (l.mode === 'count' && (l.remaining || 0) <= 0) statusHtml = '<span style="color:#909399;">已用完</span>'
      else statusHtml = '<span style="color:#67c23a;">有效</span>'
      const canRevoke = !l.revoked && !(l.mode === 'count' && (l.remaining || 0) <= 0)
      const linkCell = l.url
        ? '<a href="#" data-action="copy-link" data-url="' + esc(l.url) + '">复制链接</a>'
        : '-'
      tr.innerHTML =
        '<td>' + esc(l.created_by) + '</td>' +
        '<td class="mono">' + esc(l.bucket + '/' + l.key) + '</td>' +
        '<td>' + esc(modeTxt) + '</td>' +
        '<td>' + esc(remaining) + '</td>' +
        '<td class="muted">' + esc(fmtTime(l.created_at)) + '</td>' +
        '<td>' + statusHtml + '</td>' +
        '<td>' + linkCell + '</td>' +
        '<td>' + (canRevoke ? '<a href="#" style="color:#f56c6c;" data-action="revoke-link" data-id="' + esc(l.id) + '">作废</a>' : '-') + '</td>'
      tb.appendChild(tr)
    }
  } catch (e) { toast(e.message, false) }
}
function copyText(text, okMsg) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => toast(okMsg))
      .catch(() => copyFallback(text))
  } else {
    copyFallback(text)   // http 环境 clipboard API 不可用，用 execCommand 兜底
  }
}
function copyFallback(text) {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'; ta.style.left = '-9999px'
  document.body.appendChild(ta)
  ta.select()
  let ok = false
  try { ok = document.execCommand('copy') } catch (e) { ok = false }
  document.body.removeChild(ta)
  toast(ok ? '已复制' : text, ok)
}
function copyLink(btn) {
  const u = btn.dataset.url
  if (!u) { toast('无链接', false); return }
  copyText(window.location.origin + u, '链接已复制')
}
async function revokeLink(id) {
  const t = $('lkToken').value.trim()
  if (!t) { toast('请输入令牌', false); return }
  const ok = await confirmDialog('确定作废该链接？作废后立即失效')
  if (!ok) return
  try {
    await api('/api/objects/signed-links/' + encodeURIComponent(id) + '/revoke', { method: 'POST' }, t)
    toast('已作废'); await loadLinks()
  } catch (e) { toast(e.message, false) }
}

async function delFile(key) {
  const t = $('upToken').value.trim()
  if (!t) { toast('请先填写令牌（删除仅管理员令牌可用）', false); return }
  const ok = await confirmDialog('确定删除 ' + key + ' ？删除不可恢复')
  if (!ok) return
  try {
    await api('/api/objects?bucket=' + encodeURIComponent(state.bucket) + '&key=' + encodeURIComponent(key), { method: 'DELETE' }, t)
    toast('已删除'); await refresh()
  } catch (e) { toast(e.message, false) }
}

async function delDir(key) {
  const t = $('upToken').value.trim()
  if (!t) { toast('请先填写令牌（删除仅管理员令牌可用）', false); return }
  const ok = await confirmDialog('确定删除目录 ' + key + ' ？\n仅空目录可删除：若内含文件/子目录会被拒绝，需先清空。')
  if (!ok) return
  try {
    await api('/api/objects?bucket=' + encodeURIComponent(state.bucket) + '&key=' + encodeURIComponent(key), { method: 'DELETE' }, t)
    toast('已删除'); await refresh()
  } catch (e) { toast(e.message, false) }
}

// ---- 目录一键下载（单文件直下 / 多文件或含子目录自动打包 ZIP）----
// 入口按钮：data-action="download-dir"；只读操作，无确认弹窗。
function downloadDir() {
  if (!state.bucket) { toast('请先选择 bucket', false); return }
  const t = $('upToken').value.trim()
  if (!t) { toast('请先填写令牌（下载需要 object:download 权限）', false); return }
  const url = '/api/objects/download-bulk?bucket=' + encodeURIComponent(state.bucket) +
    '&prefix=' + encodeURIComponent(state.prefix || '') +
    '&token=' + encodeURIComponent(t)
  // 交给浏览器原生下载（不 fetch 成 blob，避免大 ZIP 常驻内存；文件名由服务端 Content-Disposition 决定）
  const a = document.createElement('a')
  a.href = url
  a.rel = 'noopener'
  a.referrerPolicy = 'no-referrer'
  a.style.display = 'none'
  document.body.appendChild(a)
  try { a.click() } finally { document.body.removeChild(a) }
  toast('开始下载；多文件或含子目录时自动打包 ZIP')
}

// ---- 单文件下载：大文件并行 Range 分块 + 进度/取消 + 完整性校验 ----
// 服务端契约：HEAD /api/objects/download 返回 Content-Length / Accept-Ranges: bytes /
// Content-Disposition / ETag；GET + Range: bytes=a-b → 206 + Content-Range: bytes a-b/total。
// 请求一律同源（CSP connect-src 'self'），token 只走 Authorization 头，不拼进 URL / DOM。
// 策略：优先 File System Access（点击手势内弹保存框，边下边写盘，内存只留并发数个分片）；
//      无该 API 时按顺序拼 Blob → a.click()；>1GiB 或服务端忽略 Range 时回退浏览器原生单流下载。
const DEFAULT_DOWNLOAD_CFG = {
  chunk_size: 8 * 1024 * 1024,
  chunk_concurrency: 4,
  request_timeout_seconds: 3600,
}
let downloadCfg = Object.assign({}, DEFAULT_DOWNLOAD_CFG)
const DL_BLOB_LIMIT = 1024 * 1024 * 1024   // 无 FS API 时 Blob 拼接上限 1GiB，超出改原生单流
const DL_HASH_LIMIT = 256 * 1024 * 1024    // SHA-256 需要整块数据；超过上限跳过校验（避免再占一份内存）
const DL_MAX_RETRIES = 3                   // 每个分片最多尝试次数（首试 + 重试）
let dlTask = null                          // 当前下载任务（同一时刻只允许一个）

function applyDownloadConfig(cfg) {
  if (!cfg) return
  const pick = (v, d, lo, hi) => {
    const n = Number(v)
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : d
  }
  downloadCfg.chunk_size = pick(cfg.chunk_size, downloadCfg.chunk_size, 64 * 1024, 64 * 1024 * 1024)
  downloadCfg.chunk_concurrency = pick(cfg.chunk_concurrency, downloadCfg.chunk_concurrency, 1, 16)
  downloadCfg.request_timeout_seconds = pick(cfg.request_timeout_seconds,
    downloadCfg.request_timeout_seconds, 60, 7 * 24 * 3600)
}

function downloadTimeoutMs() {
  return Math.max(60000, (Number(downloadCfg.request_timeout_seconds) || 3600) * 1000)
}

// 网页分页配置（config.ui.*）：文件列表每页条数 / 审计单次条数
function applyUiConfig(cfg) {
  if (!cfg) return
  const pick = (v, d, lo, hi) => {
    const n = Number(v)
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : d
  }
  const oldLimit = uiCfg.file_page_size
  uiCfg.file_page_size = pick(cfg.file_page_size, uiCfg.file_page_size, 10, 1000)
  uiCfg.audit_page_size = pick(cfg.audit_page_size, uiCfg.audit_page_size, 10, 5000)
  if (uiCfg.file_page_size !== oldLimit) {
    state.limit = uiCfg.file_page_size
    if (state.bucket) refresh()   // 配置加载晚于 URL 自动进入 bucket 时，用新页大小重拉
  }
}

function dlActive() { return !!(dlTask && !dlTask.done) }

function dlAbortError() {
  const e = new Error('已取消下载')
  e.name = 'AbortError'
  return e
}

// 401/403/404/416 给出可操作提示（toast 内部用 textContent，不拼 HTML）
function dlHttpError(status) {
  const map = {
    401: '令牌无效或已过期，请重新填写令牌',
    403: '权限不足：该 token 缺少 object:download，或资源范围不允许此 bucket / key',
    404: '对象不存在或已被删除',
    416: '服务端拒绝该下载范围（Range Not Satisfiable）',
  }
  const e = new Error(map[status] || ('HTTP ' + status))
  e.status = status
  return e
}

function dlShow(name) {
  const m = $('dlModal')
  if (!m) return
  if ($('dlName')) $('dlName').textContent = name || ''
  if ($('dlBarFill')) $('dlBarFill').style.width = '0%'
  if ($('dlStat')) $('dlStat').textContent = '准备中…'
  m.style.display = 'flex'
}

function dlHide() {
  const m = $('dlModal')
  if (m) m.style.display = 'none'
}

// 进度条 + 统计（已下载/总数/百分比/速度/重试次数）
function dlUpdate(task) {
  const pct = task.total > 0 ? Math.min(100, task.loaded / task.total * 100) : 0
  const fill = $('dlBarFill')
  if (fill) fill.style.width = pct.toFixed(1) + '%'
  const now = Date.now()
  const dt = (now - task.lastTick) / 1000
  if (dt >= 0.2) {
    const inst = Math.max(0, (task.loaded - task.lastLoaded) / dt)
    task.speed = task.speed > 0 ? task.speed * 0.6 + inst * 0.4 : inst
    task.lastTick = now
    task.lastLoaded = task.loaded
  }
  const parts = [
    '已下载 ' + fmtSize(task.loaded) + ' / ' + fmtSize(task.total),
    pct.toFixed(1) + '%',
  ]
  if (task.speed > 0) parts.push(fmtSize(task.speed) + '/s')
  if (task.retries) parts.push('重试 ' + task.retries + ' 次')
  const st = $('dlStat')
  if (st) st.textContent = parts.join(' · ')
}

// 统一清理：作废任务、停表、中止全部在途请求（成功/失败/取消都走这里）
function dlCleanup(task) {
  if (!task) return
  task.done = true
  task.wake = null
  if (dlTask === task) dlTask = null
  if (task.timer) { clearInterval(task.timer); task.timer = null }
  for (const c of task.controllers.slice()) {
    try { c.abort() } catch (e) { /* 忽略 */ }
  }
  task.controllers.length = 0
}

// 关闭 / 中止已打开的 FileSystemWritableFileStream（失败时 abort，成功路径已先 close）
async function dlDropWritable(task) {
  const w = task && task.writable
  if (!w) return
  task.writable = null
  try { await w.abort() } catch (e) {
    try { await w.close() } catch (e2) { /* 忽略 */ }
  }
}

// Content-Disposition：filename*=（RFC 5987）优先，退回 filename="..."
function dlParseFilename(cd, fallback) {
  if (!cd) return fallback
  let m = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(cd)
  if (m) {
    try { return decodeURIComponent(m[1].trim().replace(/^"|"$/g, '')) } catch (e) { /* 继续 */ }
  }
  m = /filename\s*=\s*"([^"]*)"/i.exec(cd) || /filename\s*=\s*([^;]+)/i.exec(cd)
  if (m) return m[1].trim()
  return fallback
}

function dlMetaFromHeaders(r) {
  let size = Number(r.headers.get('content-length')) || 0
  const cr = r.headers.get('content-range') || ''
  const m = /bytes\s+\d+-\d+\/(\d+)/i.exec(cr)   // 探测用 Range 时总长在 Content-Range 里
  if (m) size = Number(m[1])
  return {
    size: size,
    ranges: /bytes/i.test(r.headers.get('accept-ranges') || ''),
    filename: dlParseFilename(r.headers.get('content-disposition') || '', ''),
    etag: r.headers.get('etag') || '',
  }
}

async function dlProbeOnce(url, token, method, range) {
  const headers = authHeaders(token, range ? { Range: range } : {})
  let r
  try {
    r = await fetch(url, { method: method, headers: headers, cache: 'no-store' })
  } catch (e) {
    throw new Error('网络错误：' + ((e && e.message) || e))
  }
  if (!r.ok) return { ok: false, status: r.status }
  const meta = dlMetaFromHeaders(r)
  if (method !== 'HEAD' && r.body) { try { r.body.cancel() } catch (e) { /* 忽略 */ } }
  return { ok: true, meta: meta }
}

// HEAD 探测大小/范围/文件名/ETag；服务端未实现 HEAD（405/501）时用 GET + bytes=0-0 兜底
async function dlProbe(bucket, key, token) {
  const url = '/api/objects/download?bucket=' + encodeURIComponent(bucket) +
    '&key=' + encodeURIComponent(key)
  const head = await dlProbeOnce(url, token, 'HEAD')
  if (head.ok) return head.meta
  if (head.status === 405 || head.status === 501) {
    const probe = await dlProbeOnce(url, token, 'GET', 'bytes=0-0')
    if (probe.ok) return probe.meta
    throw dlHttpError(probe.status)
  }
  throw dlHttpError(head.status)
}

// ETag → SHA-256 十六进制（仅认 "64位hex" 形式，含弱校验前缀 W/）
function dlEtagSha256(etag) {
  const m = /^(?:W\/)?"([0-9a-f]{64})"$/i.exec(String(etag || '').trim())
  return m ? m[1].toLowerCase() : ''
}

async function dlSha256Hex(buf) {
  const subtle = window.crypto && window.crypto.subtle
  if (!subtle || !subtle.digest) return ''
  const d = await subtle.digest('SHA-256', buf)
  return Array.from(new Uint8Array(d)).map(b => b.toString(16).padStart(2, '0')).join('')
}

// 完整性：ETag 是 "<64位hex>" 时用 crypto.subtle.digest 校验 SHA-256
async function dlVerify(task, meta, readBuf) {
  const expected = dlEtagSha256(meta.etag)
  if (!expected) return
  if (task.total > DL_HASH_LIMIT) { task.hashSkipped = true; return }
  let buf = null
  try { buf = await readBuf() } catch (e) { task.hashSkipped = true; return }
  const got = await dlSha256Hex(buf)
  if (!got) { task.hashSkipped = true; return }
  if (got !== expected) {
    throw new Error('完整性校验失败：SHA-256 与 ETag 不一致，文件可能已损坏，请重试')
  }
  task.verified = true
}

// 浏览器原生单流下载（不 fetch 成 blob，避免大文件常驻内存）：隐藏 a 元素 + token query
function nativeDownload(bucket, key, token) {
  const url = '/api/objects/download?bucket=' + encodeURIComponent(bucket) +
    '&key=' + encodeURIComponent(key) + '&token=' + encodeURIComponent(token)
  const a = document.createElement('a')
  a.href = url
  a.rel = 'noopener'
  a.referrerPolicy = 'no-referrer'
  a.style.display = 'none'
  document.body.appendChild(a)
  try { a.click() } finally { document.body.removeChild(a) }
}

function dlSaveBlob(blob, name) {
  const u = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = u
  a.download = name || 'download.bin'
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  try { a.click() } finally { document.body.removeChild(a) }
  // 交给浏览器接管后再释放，过早 revoke 会让部分浏览器的下载中断
  setTimeout(() => { try { URL.revokeObjectURL(u) } catch (e) { /* 忽略 */ } }, 30000)
}

// 单个分片：Range 请求 + 校验 206 / Content-Range 起始 / 分片长度；失败重试 DL_MAX_RETRIES 次
async function dlFetchChunk(task, token, url, idx, chunkSize) {
  const start = idx * chunkSize
  const end = Math.min(task.total - 1, start + chunkSize - 1)
  const want = end - start + 1
  let lastErr = null
  for (let attempt = 0; attempt < DL_MAX_RETRIES; attempt++) {
    if (task.aborted) return null
    const ctrl = new AbortController()
    task.controllers.push(ctrl)
    const timer = setTimeout(() => { try { ctrl.abort() } catch (e) { /* 忽略 */ } }, downloadTimeoutMs())
    try {
      const r = await fetch(url, {
        method: 'GET',
        headers: authHeaders(token, { Range: 'bytes=' + start + '-' + end }),
        signal: ctrl.signal,
        cache: 'no-store',
      })
      if (r.status === 200) {
        // 服务端忽略 Range（返回整份文件）→ 立刻停并行，回退浏览器原生单流下载
        task.rangeIgnored = true
        try { if (r.body) r.body.cancel() } catch (e) { /* 忽略 */ }
        if (typeof task.wake === 'function') task.wake()
        return null
      }
      if (r.status !== 206) throw dlHttpError(r.status)
      const cr = r.headers.get('content-range') || ''
      const m = /bytes\s+(\d+)-(\d+)\/(\d+|\*)/i.exec(cr)
      if (!m || Number(m[1]) !== start) {
        throw new Error('服务端返回的分块范围不符（' + (cr || '无 Content-Range') + '）')
      }
      if (m[3] !== '*' && Number(m[3]) !== task.total) {
        throw new Error('下载过程中文件大小发生变化，请重试')
      }
      const buf = await r.arrayBuffer()
      if (buf.byteLength !== want) throw new Error('分块长度不符：' + buf.byteLength + ' / ' + want)
      task.loaded += buf.byteLength
      dlUpdate(task)
      return buf
    } catch (e) {
      if (e && e.name === 'AbortError') {
        if (task.aborted) return null
        lastErr = new Error('分块请求超时（' + Math.round(downloadTimeoutMs() / 1000) + ' 秒）')
      } else {
        lastErr = e
      }
      task.retries++
      dlUpdate(task)
      if (attempt + 1 < DL_MAX_RETRIES) {
        await new Promise(res => setTimeout(res, 300 * (attempt + 1)))
      }
    } finally {
      clearTimeout(timer)
      const i = task.controllers.indexOf(ctrl)
      if (i >= 0) task.controllers.splice(i, 1)
    }
  }
  throw lastErr || new Error('分块下载失败')
}

// 并行分块：最多 chunk_concurrency 个 worker；分片按顺序写入（FS）或按顺序入 Blob 数组
async function dlParallel(task, token, meta, url) {
  const chunkSize = Math.max(1, Math.floor(downloadCfg.chunk_size))
  const total = Math.max(1, Math.ceil(task.total / chunkSize))
  const conc = Math.max(1, Math.min(16, Math.floor(downloadCfg.chunk_concurrency) || 1))
  const waiters = new Map()   // idx -> resolve（等自己写入顺序的 worker）
  let nextIdx = 0
  let nextWrite = 0
  let failure = null

  function releaseWaiter(idx) {
    const w = waiters.get(idx)
    if (w) { waiters.delete(idx); w() }
  }
  function failAll(err) {
    if (!failure) failure = err || new Error('分块下载失败')
    for (const [idx, res] of Array.from(waiters.entries())) {
      waiters.delete(idx)
      res()
    }
  }
  task.wake = () => failAll(dlAbortError())

  async function writeOrdered(idx, buf) {
    if (idx !== nextWrite) {
      await new Promise(res => { waiters.set(idx, res) })
    }
    if (task.aborted) throw dlAbortError()
    if (failure) throw failure
    if (task.writable) {
      // FS 路径：按分片起点的字节位置顺序写入本地文件，内存只留并发数个分片
      await task.writable.write({ type: 'write', position: idx * chunkSize, data: buf })
    } else {
      task.parts.push(buf)   // 无 FS API：按顺序收集，最后拼 Blob
    }
    nextWrite++
    releaseWaiter(nextWrite)
  }

  async function worker() {
    for (;;) {
      if (task.aborted || failure) return
      const idx = nextIdx++
      if (idx >= total) return
      let buf = null
      try {
        buf = await dlFetchChunk(task, token, url, idx, chunkSize)
      } catch (e) {
        failAll(e)
        return
      }
      if (!buf) return
      try {
        await writeOrdered(idx, buf)
      } catch (e) {
        failAll(e)
        return
      }
    }
  }

  const workers = []
  const n = Math.min(conc, total)
  for (let i = 0; i < n; i++) workers.push(worker())
  await Promise.all(workers)
  task.wake = null
  if (task.rangeIgnored) return false
  if (task.aborted) return false
  if (failure) throw failure
  if (nextWrite !== total) throw new Error('分块数量不符：已写入 ' + nextWrite + ' / ' + total)
  return true
}

// FS 路径但不分块（小文件 / 不支持 Range）：单连接流式读 → 按位置写入，内存只留一个数据块
async function dlWriteWhole(task, token, url) {
  const ctrl = new AbortController()
  task.controllers.push(ctrl)
  const timer = setTimeout(() => { try { ctrl.abort() } catch (e) { /* 忽略 */ } }, downloadTimeoutMs())
  try {
    const r = await fetch(url, { headers: authHeaders(token), signal: ctrl.signal, cache: 'no-store' })
    if (!r.ok) throw dlHttpError(r.status)
    const reader = r.body && r.body.getReader ? r.body.getReader() : null
    if (!reader) {
      const buf = await r.arrayBuffer()
      await task.writable.write({ type: 'write', position: 0, data: buf })
      task.loaded = buf.byteLength
      dlUpdate(task)
      return
    }
    let pos = 0
    for (;;) {
      if (task.aborted) throw dlAbortError()
      const step = await reader.read()
      if (step.done) break
      await task.writable.write({ type: 'write', position: pos, data: step.value })
      pos += step.value.byteLength
      task.loaded = pos
      dlUpdate(task)
    }
  } finally {
    clearTimeout(timer)
    const i = task.controllers.indexOf(ctrl)
    if (i >= 0) task.controllers.splice(i, 1)
  }
}

// 取消：中止所有在途 fetch + 唤醒等写入顺序的 worker（UI 复位在 downloadFile 的收尾里统一做）
function cancelDownload() {
  const task = dlTask
  if (!task || task.done) { dlHide(); return }
  task.aborted = true
  for (const c of task.controllers.slice()) {
    try { c.abort() } catch (e) { /* 忽略 */ }
  }
  if (typeof task.wake === 'function') task.wake()
  const st = $('dlStat')
  if (st) st.textContent = '正在取消…'
}

// 文件行「下载」入口：data-action="download-file" data-key="..."
async function downloadFile(key) {
  if (!state.bucket) { toast('请先选择 bucket', false); return }
  const bucket = state.bucket
  const token = $('upToken').value.trim()
  if (!token) { toast('请先填写令牌（下载需要 object:download 权限）', false); return }
  if (!key) { toast('请在文件列表点击对应行的「下载」按钮', false); return }
  if (dlActive()) { toast('已有下载任务进行中，请先取消或等待完成', false); return }
  const fallbackName = String(key).split('/').pop() || 'download.bin'
  const task = {
    key: key, bucket: bucket, done: false, aborted: false, rangeIgnored: false,
    controllers: [], writable: null, handle: null, timer: null, parts: [],
    loaded: 0, total: 0, speed: 0, lastTick: Date.now(), lastLoaded: 0,
    retries: 0, verified: false, hashSkipped: false, wake: null,
  }
  dlTask = task
  let name = fallbackName
  try {
    // 1) File System Access：必须在点击手势内弹出保存框（await 过后手势会失效）
    if (typeof window.showSaveFilePicker === 'function') {
      try {
        task.handle = await window.showSaveFilePicker({ suggestedName: fallbackName })
      } catch (e) {
        if (e && e.name === 'AbortError') { dlCleanup(task); return }   // 用户在保存框点了取消
        task.handle = null   // 其它错误（权限/非安全上下文）→ 退回 Blob 或原生
      }
    }
    // 2) HEAD 探测：大小 / 是否支持 Range / 文件名 / ETag
    const meta = await dlProbe(bucket, key, token)
    task.total = meta.size
    task.etag = meta.etag
    name = meta.filename || fallbackName
    // 3) 有 FS 句柄就尽量流式落盘（不占内存）；否则按能力/大小选择 Blob 分块或原生单流
    if (task.handle) {
      try { task.writable = await task.handle.createWritable() }
      catch (e) { task.handle = null; task.writable = null }
    }
    const useFs = !!task.writable
    const canChunk = meta.ranges && meta.size > downloadCfg.chunk_size &&
      downloadCfg.chunk_concurrency > 1
    const url = '/api/objects/download?bucket=' + encodeURIComponent(bucket) +
      '&key=' + encodeURIComponent(key)

    // 3a) 无 FS 句柄：不支持 Range / 不够分块 / 超过 1GiB → 浏览器原生单流下载（保持现状）
    if (!useFs && (!canChunk || meta.size > DL_BLOB_LIMIT)) {
      dlCleanup(task)
      nativeDownload(bucket, key, token)
      if (!canChunk) {
        toast('已开始下载：' + name)
      } else {
        toast('文件 ' + fmtSize(meta.size) + ' 超过 1GiB，已改用浏览器原生单流下载；Chrome/Edge 可边下边写盘', false)
      }
      return
    }

    dlShow(name)
    task.timer = setInterval(() => { if (dlTask === task && !task.done) dlUpdate(task) }, 500)
    dlUpdate(task)

    if (useFs) {
      // 3b) FS 路径：分块并行 → 按位置顺序写入；不分块则单连接流式写入
      let ok = true
      if (canChunk) {
        ok = await dlParallel(task, token, meta, url)
        if (task.rangeIgnored) {
          await dlDropWritable(task)
          dlCleanup(task)
          dlHide()
          nativeDownload(bucket, key, token)
          toast('服务端未按 Range 返回分块（HTTP 200），已改为浏览器原生单流下载', false)
          return
        }
      } else {
        await dlWriteWhole(task, token, url)
      }
      if (!ok) throw dlAbortError()
      await task.writable.close()
      task.writable = null
      if (task.aborted) throw dlAbortError()   // 收尾阶段点了取消：不留半成品
      try {
        await dlVerify(task, meta, async () => {
          const f = await task.handle.getFile()
          return f.arrayBuffer()
        })
      } catch (e) {
        // 校验失败：尽量删掉写了一半的文件，避免留下坏文件
        try { await task.handle.remove() } catch (e2) { /* 忽略：浏览器可能不支持 */ }
        throw e
      }
      task.loaded = task.total
      dlUpdate(task)
      const tail = (task.verified ? '，SHA-256 校验通过' : '') +
        (task.hashSkipped ? '（文件较大，已跳过 SHA-256 校验）' : '')
      toast('下载完成：' + name + '（' + fmtSize(task.total) + tail + '）')
      dlCleanup(task)
      dlHide()
      return
    }

    // 3c) Blob 路径：并发分片 → 按顺序拼 Blob → 校验 → a.click()
    const ok = await dlParallel(task, token, meta, url)
    if (task.rangeIgnored) {
      dlCleanup(task)
      dlHide()
      nativeDownload(bucket, key, token)
      toast('服务端未按 Range 返回分块（HTTP 200），已改为浏览器原生单流下载', false)
      return
    }
    if (!ok) throw dlAbortError()
    const blob = new Blob(task.parts, { type: 'application/octet-stream' })
    task.parts = []
    await dlVerify(task, meta, () => blob.arrayBuffer())
    if (task.aborted) throw dlAbortError()   // 收尾阶段点了取消：不触发保存
    dlSaveBlob(blob, name)
    task.loaded = task.total
    dlUpdate(task)
    toast('下载完成：' + name + '（' + fmtSize(task.total) +
      (task.verified ? '，SHA-256 校验通过' : '') + '）')
    dlCleanup(task)
    dlHide()
  } catch (e) {
    const aborted = task.aborted || (e && e.name === 'AbortError')
    dlCleanup(task)
    await dlDropWritable(task)
    dlHide()
    if (aborted) toast('已取消下载', false)
    else toast('下载失败：' + ((e && e.message) || e), false)
  }
}

// 上传（拖拽 / 多选 / 先审核 token / 并发 / 大文件分片 / 失败续传 / 暂停继续）
//
// 断点续传模型（与服务端契约 §B3/§D 一致）：
//  - 每个队列项记录 upload_id / chunk_size / uploaded（服务端已确认的分片索引，唯一事实源）；
//  - initiate 带 bucket/key/size；续传再带 resume=true（可选 upload_id 指定复用会话）；
//  - 失败不 abort：保留服务端分片会话，状态置「失败（可续传）」，继续时只补缺失分片；
//  - 暂停按任务行独立生效（分片边界，不再发新分片），已传分片不重传；「继续上传」从断点继续；
//  - 页面刷新后浏览器无法恢复 File 列表：网页会列出当前令牌名下未完成的分片会话
//    （服务端 GET /api/objects/uploads），可手动取消；重新选择同一文件上传时带
//    resume=true + bucket/key/size 由服务端自动找回未完成会话（本地只缓存文件指纹，
//    绝不存 token；指纹不一致 → 新建会话，避免把旧分片拼进被改过的文件）。
const upItems = []   // { uid, file, pct, loaded, phase, reason, resumable, retryable, upload_id, chunk_size, uploaded, paused, chunked }
let upInFlight = 0
let upSeq = 0               // 队列项唯一 id（data-uid 用，避免按数组下标取项因移除而错位）
let pendingUploads = []     // 当前令牌名下未完成的分片会话（服务端磁盘为准，刷新后仍可显示）
let pendingTimer = null     // 输入令牌时防抖刷新未完成列表

// 浏览器侧断点指纹（localStorage）：bucket|key → { size, lastModified, upload_id, chunk_size }
const UP_RESUME_STORE = 'artifactdepot.uploadResume.v1'

function upResumeKey(bucket, key) { return bucket + '|' + key }

function upResumeMap() {
  try {
    const raw = window.localStorage.getItem(UP_RESUME_STORE)
    const m = raw ? JSON.parse(raw) : null
    return m && typeof m === 'object' ? m : {}
  } catch (e) { return {} }   // 隐私模式/存储禁用：缓存失效不影响上传
}

function upResumeGet(bucket, key) {
  const rec = upResumeMap()[upResumeKey(bucket, key)]
  return rec && typeof rec === 'object' ? rec : null
}

function upResumePut(bucket, key, rec) {
  try {
    const m = upResumeMap()
    m[upResumeKey(bucket, key)] = rec
    window.localStorage.setItem(UP_RESUME_STORE, JSON.stringify(m))
  } catch (e) { /* 忽略：缓存失败只是不能跨页面刷新续传 */ }
}

function upResumeDel(bucket, key) {
  try {
    const m = upResumeMap()
    delete m[upResumeKey(bucket, key)]
    window.localStorage.setItem(UP_RESUME_STORE, JSON.stringify(m))
  } catch (e) { /* 忽略 */ }
}

// 取消未完成会话后，清掉指向该 upload_id 的本地断点记录，避免重选文件时先撞一次失效会话
function upResumeDropByUploadId(uploadId) {
  if (!uploadId) return
  try {
    const m = upResumeMap()
    let changed = false
    Object.keys(m).forEach((k) => {
      const rec = m[k]
      if (rec && rec.upload_id === uploadId) { delete m[k]; changed = true }
    })
    if (changed) window.localStorage.setItem(UP_RESUME_STORE, JSON.stringify(m))
  } catch (e) { /* 忽略 */ }
}

function makeUploadItem(file) {
  return {
    uid: ++upSeq,
    file: file,
    pct: 0,
    loaded: 0,
    phase: 'queued',       // queued 排队 / running 上传中 / failed 失败 / done 成功
    reason: '',
    resumable: false,      // 失败时服务端分片会话是否保留（可断点续传）
    retryable: false,      // 是否显示该行的「继续上传」按钮
    upload_id: '',
    chunk_size: 0,
    uploaded: new Set(),   // 服务端已确认的分片索引
    paused: false,         // 该任务是否已暂停（分片边界生效；每个任务独立）
    pauseWaiters: [],      // 该任务暂停期间等待恢复的 worker
    chunked: false,        // 是否走多线程分片上传（小文件单请求直传不支持中途暂停）
    canceled: false,       // 用户已放弃：worker 尽快退出，不再发新分片
    bucket: '',            // 发起任务时的 bucket/key（切换目录后清理断点记录仍准确）
    key: '',
    xhr: null,             // 小文件直传的 XHR（「放弃」时 abort）
    starting: false,       // 正在校验 token / 准备发起，防止并发 processQueue 重复调度同一项
  }
}

// 上传目标 key；未选 bucket 时返回空串（退化为旧版 legacy 会话）
function uploadTargetKey(file) {
  if (!state.bucket) return ''
  return state.prefix ? state.prefix + '/' + file.name : file.name
}

function upChunkBytes(item, index) {
  const start = index * item.chunk_size
  const end = Math.min(item.file.size, start + item.chunk_size)
  return Math.max(0, end - start)
}

// 暂停边界：worker 在发下一分片前 await 该任务自己的恢复信号；已发出的分片照常收尾，不重传
function upWaitResume(item) {
  if (!item || !item.paused) return Promise.resolve()
  return new Promise((resolve) => { item.pauseWaiters.push(resolve) })
}

function upWakePauseWaiters(item) {
  if (!item) return
  const waiters = item.pauseWaiters
  item.pauseWaiters = []
  waiters.forEach(fn => fn())
}

function findUpItem(uid) {
  return upItems.find(it => String(it.uid) === String(uid)) || null
}

// 是否走分片上传：小文件是单请求直传，中途无法暂停（暂停按钮只对分片任务显示）
function upIsChunked(item) {
  return !!item && (!!item.chunked || item.file.size > uploadCfg.chunk_size)
}

// 默认值与 config.json 的 upload.* 一致；页面加载/initiate 时用服务端配置覆盖
const DEFAULT_UPLOAD_CFG = {
  chunk_size: 8 * 1024 * 1024,
  chunk_concurrency: 4,
  file_concurrency: 3,
  request_timeout_seconds: 86400,
}
let uploadCfg = Object.assign({}, DEFAULT_UPLOAD_CFG)

function uploadTimeoutMs() {
  const sec = Number(uploadCfg.request_timeout_seconds) || 86400
  return Math.max(1000, sec * 1000)
}

function applyUploadConfig(cfg, selectFileConcurrency = false) {
  if (!cfg) return
  const pick = (v, d, lo, hi) => {
    const n = Number(v)
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : d
  }
  uploadCfg.chunk_size = pick(cfg.chunk_size, uploadCfg.chunk_size, 1, 64 * 1024 * 1024)
  uploadCfg.chunk_concurrency = pick(cfg.chunk_concurrency, uploadCfg.chunk_concurrency, 1, 16)
  uploadCfg.file_concurrency = pick(cfg.file_concurrency, uploadCfg.file_concurrency, 1, 10)
  uploadCfg.request_timeout_seconds = pick(cfg.request_timeout_seconds, uploadCfg.request_timeout_seconds, 60, 7 * 24 * 3600)
  if (selectFileConcurrency) {
    const sel = $('upConcurrency')
    if (sel) {
      const val = String(uploadCfg.file_concurrency)
      if (!Array.from(sel.options).some(o => o.value === val)) {
        const opt = document.createElement('option')
        opt.value = val; opt.textContent = val
        sel.appendChild(opt)
      }
      sel.value = val
    }
  }
}

async function loadUploadConfig() {
  try {
    // 同一个公开接口同时下发上传参数（config.upload.*）与下载参数（config.download.*）
    const b = await api('/api/auth/permissions')
    const d = (b && b.data) || {}
    applyUploadConfig(d.upload, true)
    applyDownloadConfig(d.download)
    applyUiConfig(d.ui)
  } catch (e) { /* 接口不可用时保留默认值 */ }
}

function hasFiles(e) {
  return e.dataTransfer && e.dataTransfer.types && Array.from(e.dataTransfer.types).includes('Files')
}
function showDropOverlay() { $('dropOverlay').style.display = 'flex' }
function hideDropOverlay() { $('dropOverlay').style.display = 'none' }

async function checkToken(tok) {
  if (!tok) return null
  try {
    const b = await api('/api/auth/check', {}, tok)
    return b.data && b.data.valid ? b.data.actor : null
  } catch (e) { return null }
}

// 令牌门槛：未填写令牌时「选择文件」「上传」按钮禁用（拖拽进入也会在 enqueueFiles 里再次校验）
function uploadTokenValue() {
  const el = $('upToken')
  return el ? el.value.trim() : ''
}

function syncUploadGate() {
  const on = !!uploadTokenValue()
  const file = $('upFile')
  if (file) {
    file.disabled = !on
    file.title = on ? '' : '请先填写令牌'
  }
  const btn = $('upBtn')
  if (btn) {
    btn.disabled = !on
    btn.title = on ? '' : '请先填写令牌'
  }
  const hint = $('upTokenHint')
  if (hint) {
    hint.textContent = on ? '' : '请先填写上方「令牌」，再选择文件或拖拽上传。'
    hint.style.display = on ? 'none' : ''
  }
}

// ---- 当前令牌的未完成上传（服务端分片会话；刷新页面后仍可显示 / 取消）----
function renderPendingUploads() {
  const wrap = $('upPendingWrap')
  const box = $('upPending')
  if (!wrap || !box) return
  // 本页队列里正在处理的任务已经在队列行展示，避免同一个会话重复出现
  const active = new Set(upItems.map(it => it.upload_id).filter(Boolean))
  const items = pendingUploads.filter(it => it && it.upload_id && !active.has(it.upload_id))
  if (!items.length) { wrap.style.display = 'none'; box.innerHTML = ''; return }
  const info = $('upPendingInfo')
  if (info) info.textContent = '（' + items.length + ' 个）'
  wrap.style.display = ''
  box.innerHTML = items.map(it => {
    const pct = Math.max(0, Math.min(100, Number(it.percent) || 0))
    const label = '已传 ' + fmtSize(it.received_bytes) + ' / ' + fmtSize(it.size) + '（' + pct + '%）'
    const path = (it.bucket || '') + ' / ' + (it.key || '')
    return '<div class="up-item pending"><span class="nm" title="' + esc(path) + '">' + esc(it.name || it.key) + '</span>' +
      '<progress value="' + pct + '" max="100"></progress>' +
      '<span class="st">' + esc(label) + '</span>' +
      '<span class="muted pending-key" title="' + esc(path) + '">' + esc(path) + '</span>' +
      '<button class="ghost up-act" data-action="cancel-pending-upload" data-uid="' + esc(it.upload_id) + '">取消上传</button></div>'
  }).join('')
}

async function refreshPendingUploads() {
  const token = uploadTokenValue()
  if (!token) { pendingUploads = []; renderPendingUploads(); return }
  try {
    const body = await api('/api/objects/uploads', {}, token)
    pendingUploads = (body && body.data && body.data.items) || []
  } catch (e) {
    pendingUploads = []   // token 无效/无权限/接口不可用时静默隐藏，不打断当前操作
  }
  renderPendingUploads()
}

function schedulePendingRefresh() {
  if (pendingTimer) clearTimeout(pendingTimer)
  pendingTimer = setTimeout(() => { pendingTimer = null; refreshPendingUploads() }, 500)
}

async function cancelPendingUpload(uploadId) {
  const token = uploadTokenValue()
  if (!token || !uploadId) return
  const item = pendingUploads.find(it => it.upload_id === uploadId)
  const name = (item && (item.name || item.key)) || uploadId
  const ok = await confirmDialog('取消后服务端已上传的分片会被删除，且无法恢复。确定取消「' + name + '」？')
  if (!ok) return
  try {
    const fd = new FormData()
    fd.append('upload_id', uploadId)
    await api('/api/objects/abort', { method: 'POST', body: fd }, token)
  } catch (e) {
    toast('取消失败：' + ((e && e.message) || e), false)
    return
  }
  upResumeDropByUploadId(uploadId)
  pendingUploads = pendingUploads.filter(it => it.upload_id !== uploadId)
  renderPendingUploads()
  toast('已取消未完成上传：' + name)
}

function enqueueFiles(fileList) {
  const tok = uploadTokenValue()
  if (!tok) { toast('请先填写令牌，再选择文件', false); return }
  const files = Array.from(fileList || [])
  if (!files.length) return
  for (const f of files) upItems.push(makeUploadItem(f))
  renderUpQueue()
  processQueue()
}

function doUpload() {
  enqueueFiles($('upFile').files)
  $('upFile').value = ''   // 允许重复选同一文件
}

// 队列状态文案：失败项区分「可续传」（服务端会话保留）与普通失败（直传/token 问题）
function upItemLabel(item) {
  if (item.phase === 'done') return '成功'
  if (item.phase === 'failed') {
    return item.resumable
      ? '失败（可续传）' + (item.reason ? '：' + item.reason : '')
      : '失败：' + (item.reason || '未知错误')
  }
  if (item.phase === 'queued') return item.paused ? '排队中（已暂停）' : '排队中'
  if (item.paused) return '已暂停 ' + fmtSize(item.loaded) + ' / ' + fmtSize(item.file.size)
  return '上传中 ' + fmtSize(item.loaded) + ' / ' + fmtSize(item.file.size)
}

// 暂停某个上传任务：当前在途分片传完后不再发新分片；会话与已传分片全部保留
function pauseUpload(uid) {
  const item = findUpItem(uid)
  if (!item || item.paused) return
  if (item.phase === 'done' || item.phase === 'failed') return
  if (!upIsChunked(item)) { toast('小文件为单请求直传，无法中途暂停', false); return }
  item.paused = true
  renderUpQueue()
  toast('已暂停上传：当前分片传完后不再发新分片，已传分片保留')
}

// 继续某个上传任务：唤醒该任务的 worker 从断点继续（失败项由 retryUpload 重新排队）
function resumeUpload(uid) {
  const item = findUpItem(uid)
  if (!item) return
  item.paused = false
  upWakePauseWaiters(item)
  renderUpQueue()
  toast('已继续上传')
  processQueue()
}

// 每项「继续上传」：保留 upload_id，重跑 initiate(resume=true) 后只补缺失分片
function retryUpload(uid) {
  const item = findUpItem(uid)
  if (!item || item.phase !== 'failed') return
  item.phase = 'queued'
  item.reason = ''
  item.resumable = false
  item.paused = false
  item.canceled = false
  upWakePauseWaiters(item)
  renderUpQueue()
  processQueue()
}

// 每项「放弃」：显式 abort 服务端会话并清理本地断点记录（失败/暂停不会自动 abort）
async function cancelUpload(uid) {
  const idx = upItems.findIndex(it => String(it.uid) === String(uid))
  if (idx < 0) return
  const item = upItems[idx]
  if (item.phase === 'running' && !item.paused) {
    if (upIsChunked(item)) {
      toast('正在上传，请先点该行「暂停上传」再放弃', false)
      return
    }
    // 小文件单请求直传：直接 abort 该 XHR（无服务端分片会话需要清理）
    item.canceled = true
    if (item.xhr) item.xhr.abort()
  }
  item.canceled = true
  item.paused = false
  upWakePauseWaiters(item)
  const token = uploadTokenValue()
  if (item.upload_id && token) await abortUpload(item.upload_id, token)
  if (item.upload_id) {
    if (item.bucket && item.key) upResumeDel(item.bucket, item.key)
    upResumeDropByUploadId(item.upload_id)
  }
  upItems.splice(idx, 1)
  renderUpQueue()
  refreshPendingUploads()
  toast('已放弃 ' + item.file.name + '，断点记录已清理')
}

async function processQueue() {
  const max = parseInt($('upConcurrency').value) || uploadCfg.file_concurrency || 3
  while (upInFlight < max) {
    const token = uploadTokenValue()
    if (!token) break
    // 只调度未暂停、未在准备中的任务：暂停只影响该行，不再阻塞其它文件
    const item = upItems.find(i => i.phase === 'queued' && !i.paused && !i.starting)
    if (!item) break
    item.starting = true
    const actor = await checkToken(token)
    item.starting = false
    if (!actor) {
      item.phase = 'failed'
      item.retryable = true
      item.resumable = false
      item.reason = 'token 无效'
      renderUpQueue()
      toast('token 无效，跳过 ' + item.file.name, false)
      continue
    }
    // 校验 token 期间状态可能变化（刚暂停 / 刚放弃 / 被其它轮次取走）
    if (item.paused || item.canceled || item.phase !== 'queued') continue
    upInFlight++
    item.phase = 'running'
    item.reason = ''
    item.chunked = item.file.size > uploadCfg.chunk_size
    item.bucket = state.bucket || ''
    item.key = uploadTargetKey(item.file)
    const runner = item.chunked ? uploadOneChunked(item, token) : uploadOne(item, token)
    runner.then((res) => {
      upInFlight--
      res = res || {}
      if (res.ok) {
        item.phase = 'done'
        item.pct = 100
        item.loaded = item.file.size
        item.reason = ''
        item.upload_id = ''
        item.uploaded = new Set()
        refresh()
      } else {
        // 失败不 abort：分片会话保留，可点该行「继续上传」从断点续传
        item.phase = 'failed'
        item.retryable = true
        item.resumable = !!res.resumable
        item.reason = res.reason || '上传失败'
      }
      renderUpQueue()
      refreshPendingUploads()
      processQueue()
    }).catch((e) => {
      upInFlight--
      item.phase = 'failed'
      item.retryable = true
      item.resumable = !!item.upload_id
      item.reason = (e && e.message) || String(e)
      renderUpQueue()
      refreshPendingUploads()
      processQueue()
    })
  }
}

function uploadOne(item, token) {
  // 小文件单请求直传：无法在中间暂停（该行不显示暂停按钮），失败可重试但无断点；放弃时 abort 本请求
  return new Promise((resolve) => {
    const file = item.file
    const key = state.prefix ? state.prefix + '/' + file.name : file.name
    const fd = new FormData()
    fd.append('file', file)
    fd.append('bucket', state.bucket)
    fd.append('key', key)
    fd.append('source_url', $('upSource').value.trim())
    fd.append('note', $('upNote').value.trim())
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/objects')
    xhr.setRequestHeader('Authorization', 'Bearer ' + token)
    xhr.timeout = uploadTimeoutMs()   // 默认 24h（config.upload.request_timeout_seconds）
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        item.loaded = e.loaded
        item.pct = Math.round(e.loaded / e.total * 100)
        renderUpQueue()
      }
    }
    const fail = (reason) => { item.xhr = null; resolve({ ok: false, resumable: false, reason: reason }) }
    xhr.onload = () => {
      let body = null
      try { body = JSON.parse(xhr.responseText) } catch (e) { body = null }
      if (xhr.status >= 200 && xhr.status < 300 && body && body.code === 0) {
        item.xhr = null
        resolve({ ok: true })
      } else {
        fail((body && body.detail) || ('HTTP ' + xhr.status))
      }
    }
    xhr.onerror = () => fail('网络错误')
    xhr.ontimeout = () => fail('请求超时')
    xhr.onabort = () => fail('已取消')
    item.xhr = xhr
    xhr.send(fd)
  })
}

async function abortUpload(upload_id, token) {
  try {
    const fd = new FormData()
    fd.append('upload_id', upload_id)
    await api('/api/objects/abort', { method: 'POST', body: fd }, token)
  } catch (e) { /* 忽略 */ }
}

// 发起 / 恢复分片会话：initiate 带 bucket/key/size；续传再带 resume=true（可再带 upload_id 指定复用）。
// 不传 upload_id 时由服务端按 owner+bucket+key+size 自动找回未完成会话（页面刷新后重新选文件也可续传）。
async function initiateUpload(item, token) {
  const file = item.file
  const bucket = state.bucket || ''
  const key = bucket ? uploadTargetKey(file) : ''
  const fd = new FormData()
  if (bucket) {
    fd.append('bucket', bucket)
    fd.append('key', key)
  }
  fd.append('size', String(file.size))
  let upload_id = item.upload_id || ''
  let resume = true
  if (!upload_id && bucket) {
    const rec = upResumeGet(bucket, key)
    if (rec) {
      if (Number(rec.size) === Number(file.size) &&
          Number(rec.lastModified) === Number(file.lastModified)) {
        upload_id = rec.upload_id || ''
      } else {
        // 文件变了（size/lastModified）→ 丢弃旧记录并新建会话，绝不把旧分片拼进新文件
        upResumeDel(bucket, key)
        resume = false
      }
    }
  }
  // token 变了（可能换了操作者）→ 不复用旧会话，按新身份重新发起
  if (upload_id && item.tokenSig && item.tokenSig !== token) upload_id = ''
  if (resume) fd.append('resume', 'true')
  if (upload_id) fd.append('upload_id', upload_id)
  let data
  try {
    const r = await api('/api/objects/initiate', { method: 'POST', body: fd }, token)
    data = (r && r.data) || {}
  } catch (e) {
    // 会话失效/参数不一致（HTTP 400，detail 含「分片会话」）→ 按会话丢失处理，新建会话重传
    if (upload_id && e && e.status === 400) {
      upResumeDel(bucket, key)
      item.upload_id = ''
      item.tokenSig = ''
      return initiateUpload(item, token)
    }
    throw e
  }
  const sessionId = data.upload_id
  if (!sessionId) throw new Error('服务端未返回 upload_id')
  // 服务端配置优先：分片大小/并发/超时（config.json 的 upload.*）
  applyUploadConfig(data, false)
  const chunk_size = Number(data.chunk_size) || uploadCfg.chunk_size
  const totalChunks = Number(data.total_chunks) || Math.max(1, Math.ceil(file.size / chunk_size))
  const uploaded = new Set()
  ;(data.uploaded || []).forEach((i) => {
    const idx = Number(i)
    if (Number.isInteger(idx) && idx >= 0 && idx < totalChunks) uploaded.add(idx)
  })
  item.upload_id = sessionId
  item.chunk_size = chunk_size
  item.uploaded = uploaded
  item.tokenSig = token
  if (bucket) {
    upResumePut(bucket, key, {
      size: Number(file.size) || 0,
      lastModified: Number(file.lastModified) || 0,
      upload_id: sessionId,
      chunk_size: chunk_size,
    })
  }
  return { upload_id: sessionId, chunk_size: chunk_size, totalChunks: totalChunks, uploaded: uploaded, resumed: !!data.resumed }
}

async function uploadOneChunked(item, token) {
  const file = item.file
  const bucket = state.bucket || ''
  const key = uploadTargetKey(file)
  item.bucket = bucket
  item.key = key
  const source_url = $('upSource').value.trim()
  const note = $('upNote').value.trim()
  let sess
  try {
    sess = await initiateUpload(item, token)
  } catch (e) {
    return { ok: false, resumable: !!item.upload_id, reason: '建立会话失败 ' + ((e && e.message) || e) }
  }
  const totalChunks = sess.totalChunks
  const chunk_size = item.chunk_size
  // 进度：已确认分片直接计入，断点续传后百分比不会回到 0
  const chunkLoaded = new Array(totalChunks).fill(0)
  item.uploaded.forEach((idx) => { chunkLoaded[idx] = upChunkBytes(item, idx) })
  function recalc() {
    let loaded = 0
    for (let i = 0; i < totalChunks; i++) loaded += chunkLoaded[i]
    item.loaded = Math.min(loaded, file.size)
    item.pct = file.size ? Math.round(item.loaded / file.size * 100) : 0
  }
  recalc()
  renderUpQueue()
  if (sess.resumed && item.uploaded.size) {
    toast('从断点继续 ' + file.name + '：已完成 ' + item.uploaded.size + '/' + totalChunks + ' 片')
  }
  function sendChunk(index) {
    return new Promise((resolve) => {
      const start = index * chunk_size
      const end = Math.min(file.size, start + chunk_size)
      const blob = file.slice(start, end)
      const fd = new FormData()
      fd.append('upload_id', item.upload_id)
      fd.append('index', String(index))
      fd.append('chunk', blob, 'chunk_' + index)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/objects/chunk')
      xhr.setRequestHeader('Authorization', 'Bearer ' + token)
      xhr.timeout = uploadTimeoutMs()
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          chunkLoaded[index] = e.loaded
          recalc()
          renderUpQueue()
        }
      }
      xhr.onload = () => {
        let body = null
        try { body = JSON.parse(xhr.responseText) } catch (e) { body = null }
        if (xhr.status >= 200 && xhr.status < 300 && body && body.code === 0) {
          chunkLoaded[index] = end - start
          item.uploaded.add(index)
          recalc()
          renderUpQueue()
          resolve({ ok: true })
        } else {
          resolve({ ok: false, reason: (body && body.detail) || ('HTTP ' + xhr.status) })
        }
      }
      xhr.onerror = () => resolve({ ok: false, reason: '网络错误' })
      xhr.ontimeout = () => resolve({ ok: false, reason: '请求超时' })
      xhr.send(fd)
    })
  }
  // 只补缺失分片；每个分片发送前检查暂停边界（暂停时不再发新片，已传分片不重传）
  const pending = []
  for (let i = 0; i < totalChunks; i++) if (!item.uploaded.has(i)) pending.push(i)
  let failed = null
  let cursor = 0
  async function worker() {
    while (true) {
      if (item.canceled || failed || cursor >= pending.length) return
      if (item.paused) { await upWaitResume(item); continue }
      const idx = pending[cursor++]
      const r = await sendChunk(idx)
      if (item.canceled) return
      if (!r.ok) {
        // 一个分片失败就停止派发；已成功分片保留在 item.uploaded，下次续传只补缺失片
        failed = r
        upWakePauseWaiters(item)   // 唤醒可能在暂停边界等待的同伴，让本轮尽快收尾
        return
      }
    }
  }
  // 并行上传分片（默认 4 路并发，config.upload.chunk_concurrency）
  const conc = Math.max(1, Math.min(16, Number(uploadCfg.chunk_concurrency) || 4))
  const workers = []
  for (let w = 0; w < Math.min(conc, Math.max(1, pending.length)); w++) workers.push(worker())
  await Promise.all(workers)
  if (item.canceled) return { ok: false, resumable: false, reason: '已放弃' }
  if (failed) {
    // 关键：失败不 abort —— 服务端分片会话保留，下次 initiate(resume=true) 只补缺失分片
    return { ok: false, resumable: true, reason: failed.reason || '分片上传失败' }
  }
  // 暂停在分片边界同样生效：等该任务恢复后再合并
  await upWaitResume(item)
  if (item.canceled) return { ok: false, resumable: false, reason: '已放弃' }
  const fd = new FormData()
  fd.append('upload_id', item.upload_id)
  fd.append('bucket', bucket)
  fd.append('key', key)
  fd.append('total_chunks', String(totalChunks))
  fd.append('source_url', source_url)
  fd.append('note', note)
  fd.append('overwrite', 'true')
  try {
    await api('/api/objects/complete', { method: 'POST', body: fd }, token, uploadTimeoutMs())
  } catch (e) {
    // 合并失败同样保留会话（如 409），「继续」可重试 complete
    return { ok: false, resumable: true, reason: '合并失败 ' + ((e && e.message) || e) }
  }
  // 成功：清理本地断点记录（服务端会话已删除）
  if (bucket) upResumeDel(bucket, key)
  return { ok: true }
}

// 队列行操作按钮（只在对应任务进度条右侧出现；顶部工具栏不再放全局暂停/继续）
function upRowOps(it) {
  const parts = []
  if (it.phase === 'queued' || it.phase === 'running') {
    // 小文件是单请求直传，无法中途暂停，只保留「放弃」
    if (upIsChunked(it)) {
      parts.push(it.paused
        ? '<button class="ghost up-act" data-action="resume-upload" data-uid="' + it.uid + '">继续上传</button>'
        : '<button class="ghost up-act" data-action="pause-upload" data-uid="' + it.uid + '">暂停上传</button>')
    }
    parts.push('<button class="ghost up-act" data-action="cancel-upload" data-uid="' + it.uid + '">放弃</button>')
  } else if (it.phase === 'failed' && it.retryable) {
    parts.push('<button class="ghost up-act" data-action="retry-upload" data-uid="' + it.uid + '">继续上传</button>')
    parts.push('<button class="ghost up-act" data-action="cancel-upload" data-uid="' + it.uid + '">放弃</button>')
  }
  return parts.join('')
}

function renderUpQueue() {
  const box = $('upQueue')
  if (!box) return
  if (!upItems.length) { box.innerHTML = ''; return }
  box.innerHTML = upItems.map(it => {
    const cls = it.phase === 'done' ? ' ok' : it.phase === 'failed' ? ' bad' : (it.paused ? ' paused' : '')
    return '<div class="up-item"><span class="nm" title="' + esc(it.file.name) + '">' + esc(it.file.name) + '</span>' +
      '<progress value="' + it.pct + '" max="100"></progress>' +
      '<span class="st' + cls + '">' + esc(upItemLabel(it)) + '</span>' + upRowOps(it) + '</div>'
  }).join('')
}

// 整页拖放：拖入显示遮罩，松手自动上传
let dragDepth = 0
window.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy' })
window.addEventListener('drop', e => e.preventDefault())
document.addEventListener('dragenter', (e) => {
  if (hasFiles(e)) { dragDepth++; showDropOverlay() }
})
document.addEventListener('dragleave', (e) => {
  if (hasFiles(e)) { dragDepth--; if (dragDepth <= 0) { dragDepth = 0; hideDropOverlay() } }
})
document.addEventListener('drop', (e) => {
  e.preventDefault()
  dragDepth = 0; hideDropOverlay()
  if (hasFiles(e)) enqueueFiles(e.dataTransfer.files)
})

// ---- Token 管理 ----

let TKCATALOG = { permissions: [], public: [], roles: [], default_role: 'user' }

async function loadTokenCatalog() {
  try {
    const b = await api('/api/auth/permissions')
    TKCATALOG = b.data || TKCATALOG
    renderTokenCatalog()
  } catch (e) { /* 忽略：目录加载失败仍可手填 */ }
}

function scopeLabel(key) {
  const p = (TKCATALOG.permissions || []).find(x => x.key === key)
  return p ? p.label : key
}

function renderTokenCatalog() {
  const box = $('tkCatalog')
  if (!box) return
  // 角色下拉
  const sel = $('tkRole')
  sel.innerHTML = ''
  ;(TKCATALOG.roles || []).forEach(r => {
    const o = document.createElement('option')
    o.value = r.key
    o.textContent = r.key + ' — ' + r.label
    sel.appendChild(o)
  })
  sel.value = TKCATALOG.default_role || 'user'

  // 逐接口勾选（按 group 分组）
  const groups = {}
  ;(TKCATALOG.permissions || []).forEach(p => {
    ;(groups[p.group] = groups[p.group] || []).push(p)
  })
  let html = ''
  Object.keys(groups).forEach(g => {
    html += '<div style="margin-top:8px;"><strong style="font-size:13px;">' + esc(g) + '</strong>'
    html += '<table style="margin-top:4px;"><thead><tr><th style="width:40px;">启用</th><th style="width:210px;">接口</th><th style="width:90px;">权限点</th><th>说明</th></tr></thead><tbody>'
    groups[g].forEach(p => {
      html += '<tr>' +
        '<td><input type="checkbox" class="tkScope" value="' + esc(p.key) + '"' + (p.write ? '' : '') + '></td>' +
        '<td class="mono">' + esc(p.method) + ' ' + esc(p.path) + '</td>' +
        '<td class="mono">' + esc(p.key) + '</td>' +
        '<td>' + esc(p.label) + '</td>' +
        '</tr>'
    })
    html += '</tbody></table></div>'
  })
  if (TKCATALOG.public && TKCATALOG.public.length) {
    html += '<div style="margin-top:8px;"><strong style="font-size:13px;">公开接口（无需 token，始终可用）</strong>' +
      '<div class="muted mono" style="margin-top:4px;">' +
      TKCATALOG.public.map(p => esc(p.method + ' ' + p.path)).join('　') + '</div></div>'
  }
  box.innerHTML = html
  applyRolePreset()
}

function tkScopeChecks() {
  return Array.from(document.querySelectorAll('.tkScope'))
}

function setChecked(boxes, on) {
  boxes.forEach(b => { b.checked = !!on })
}

function tkSelectAll(on) {
  const boxes = tkScopeChecks()
  setChecked(boxes, on)
}

function tkSelectWrite(write) {
  const boxes = tkScopeChecks()
  boxes.forEach(b => {
    const p = (TKCATALOG.permissions || []).find(x => x.key === b.value)
    b.checked = !!(p && !!p.write === !!write)
  })
}

function applyRolePreset() {
  const role = $('tkRole').value
  const preset = (TKCATALOG.roles || []).find(r => r.key === role)
  if (!preset) return
  const set = new Set(preset.scopes || [])
  tkScopeChecks().forEach(b => { b.checked = set.has(b.value) })
}

function collectScopes() {
  return tkScopeChecks().filter(b => b.checked).map(b => b.value)
}

function parseList(text) {
  return (text || '').split(/[,，\s]+/).map(x => x.trim()).filter(Boolean)
}

function parsePrefixes(text) {
  const out = {}
  parseList(text).forEach(item => {
    const i = item.indexOf(':')
    if (i <= 0) return
    const b = item.slice(0, i).trim()
    const p = item.slice(i + 1).trim()
    if (!b || !p) return
    ;(out[b] = out[b] || []).push(p)
  })
  return out
}

// ---- Token 资源范围（bucket 多选 + 手填）----

let TKBUCKETS = []          // GET /api/buckets 返回的现有 bucket
let tkBucketsLoaded = false // 是否已拉取过（进入 Token 页只自动拉一次）

async function loadTokenBuckets(force) {
  const box = $('tkBucketChecks')
  if (!box) return
  if (tkBucketsLoaded && !force) return
  box.textContent = '（加载中…）'
  try {
    // 带管理员令牌请求：require_read_token=true 时 /api/buckets 也需要 bucket:list
    const body = await api('/api/buckets', {}, $('admToken').value.trim())
    TKBUCKETS = (body.data || []).slice()
    tkBucketsLoaded = true
    renderTokenBuckets()
  } catch (e) {
    box.textContent = '（bucket 列表加载失败：' + e.message + '；可直接手填 bucket）'
  }
}

// 渲染「全部 bucket」+ 现有 bucket 复选框；保留用户已勾选的项
function renderTokenBuckets() {
  const box = $('tkBucketChecks')
  if (!box) return
  const keep = new Set(tkCheckedBuckets())
  box.innerHTML = ''
  box.className = ''
  const allWrap = document.createElement('label')
  allWrap.style.cssText = 'margin-right:14px;white-space:nowrap;'
  const allCb = document.createElement('input')
  allCb.type = 'checkbox'
  allCb.id = 'tkBucketAll'
  allCb.checked = !keep.size && !parseList($('tkBuckets').value).length
  allWrap.appendChild(allCb)
  allWrap.appendChild(document.createTextNode(' 全部 bucket（不限制）'))
  box.appendChild(allWrap)
  if (!TKBUCKETS.length) {
    const span = document.createElement('span')
    span.className = 'muted'
    span.textContent = '（暂无现有 bucket，可手填新 bucket）'
    box.appendChild(span)
    renderTkScopePreview()
    return
  }
  TKBUCKETS.forEach(name => {
    const wrap = document.createElement('label')
    wrap.style.cssText = 'margin-right:14px;white-space:nowrap;'
    const cb = document.createElement('input')
    cb.type = 'checkbox'
    cb.className = 'tkBucket'
    cb.value = name
    cb.checked = keep.has(name)
    wrap.appendChild(cb)
    wrap.appendChild(document.createTextNode(' ' + name))
    box.appendChild(wrap)
  })
  renderTkScopePreview()
}

// 已勾选的现有 bucket（不含手填）
function tkCheckedBuckets() {
  return Array.from(document.querySelectorAll('.tkBucket')).filter(b => b.checked).map(b => b.value)
}

// 合并「勾选的现有 bucket」与「手填 bucket」：去重、去空；勾了「全部 bucket」返回 []（=不限制）
function collectTokenBuckets() {
  const all = $('tkBucketAll')
  if (all && all.checked) return []
  const out = []
  tkCheckedBuckets().forEach(b => { if (b && !out.includes(b)) out.push(b) })
  parseList($('tkBuckets').value).forEach(b => { if (b && !out.includes(b)) out.push(b) })
  return out
}

function onTkBucketChange(e) {
  const el = e.target
  if (!el) return
  if (el.id === 'tkBucketAll') {
    // 勾「全部」→ 清空其它勾选与手填；勾任一具体 bucket → 取消「全部」
    if (el.checked) {
      document.querySelectorAll('.tkBucket').forEach(b => { b.checked = false })
      $('tkBuckets').value = ''
    }
  } else if (el.classList && el.classList.contains('tkBucket') && el.checked) {
    const all = $('tkBucketAll')
    if (all) all.checked = false
  }
  renderTkScopePreview()
}

// 生成前/勾选时实时展示生效范围与前端校验提示
function renderTkScopePreview() {
  const el = $('tkScopePreview')
  if (!el) return
  const buckets = collectTokenBuckets()
  const prefixes = parsePrefixes($('tkPrefixes').value)
  const prefixBuckets = Object.keys(prefixes)
  const outside = buckets.length ? prefixBuckets.filter(b => !buckets.includes(b)) : []
  const unrestricted = !buckets.length && !prefixBuckets.length
  let txt = '生效范围：' + fmtScopeRange({ allow_buckets: buckets, allow_prefixes: prefixes })
  if (unrestricted) txt += '　（该 token 可访问全部 bucket）'
  if (outside.length) txt += '　注意：前缀所属 bucket 不在白名单：' + outside.join('、')
  el.textContent = txt
  el.style.color = (unrestricted || outside.length) ? '#e6a23c' : ''
}

async function generateToken() {
  const t = $('admToken').value.trim()
  if (!t) { toast('请先填写管理员令牌', false); return }
  const user = $('tkUser').value.trim()
  if (!user) { toast('请填写用户名', false); return }
  const scopes = collectScopes()
  const role = $('tkRole').value
  if (!scopes.length && role !== 'custom') {
    if (!confirm('尚未勾选任何接口，生成的 token 将没有任何权限。继续？')) return
  }
  const allowBuckets = collectTokenBuckets()
  const allowPrefixes = parsePrefixes($('tkPrefixes').value)
  const prefixBuckets = Object.keys(allowPrefixes)
  // 前端校验 1：前缀所属 bucket 必须在 Bucket 白名单内，否则该前缀不会生效
  if (allowBuckets.length) {
    const outside = prefixBuckets.filter(b => !allowBuckets.includes(b))
    if (outside.length) {
      toast('前缀白名单里的 bucket 不在 Bucket 白名单内：' + outside.join('、') + '（请先勾选/填写该 bucket，否则前缀不生效）', false)
      return
    }
  }
  // 前端校验 2：范围全空 = 该 token 可访问全部 bucket，需二次确认
  if (!allowBuckets.length && !prefixBuckets.length) {
    const okAll = await confirmDialog('该 token 未设置资源范围：将可访问全部 bucket（含未来新建的 bucket）。确认继续？')
    if (!okAll) return
  }
  try {
    const body = await api('/api/tokens', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user,
        role,
        scopes,
        allow_buckets: allowBuckets,
        allow_prefixes: allowPrefixes,
        expires_at: $('tkExpires').value.trim(),
        description: $('tkDesc').value.trim(),
      }),
    }, t)
    const d = body.data || {}
    $('tkResult').value = d.token || ''
    $('tkResultRow').style.display = ''
    // 生成结果区展示生效范围（以服务端返回为准，回退到本次提交值）
    const info = $('tkScopeInfo')
    if (info) {
      info.textContent = '生效范围：' + fmtScopeRange({
        allow_buckets: d.allow_buckets || allowBuckets,
        allow_prefixes: d.allow_prefixes || allowPrefixes,
      }) + '　（请把范围一并告知使用者）'
    }
    const infoRow = $('tkScopeInfoRow')
    if (infoRow) infoRow.style.display = 'flex'
    toast('已生成 token（仅展示本次）')
    await loadTokens()
  } catch (e) { toast(e.message, false) }
}

function copyTkResult() {
  const v = $('tkResult').value
  if (!v) return
  navigator.clipboard && navigator.clipboard.writeText(v).then(() => toast('已复制')).catch(() => {})
}

async function reloadConfig() {
  const t = $('admToken').value.trim()
  if (!t) { toast('请输入管理员令牌', false); return }
  const info = $('cfgReloadInfo')
  try {
    const body = await api('/api/config/reload', { method: 'POST' }, t)
    const d = body.data || {}
    const parts = []
    if (d.changed && d.changed.length) parts.push('已生效：' + d.changed.join('、'))
    if (d.needs_restart && d.needs_restart.length) parts.push('需重启：' + d.needs_restart.join('、'))
    if (d.warnings && d.warnings.length) parts.push('提示：' + d.warnings.join('；'))
    const msg = parts.length ? parts.join('；') : '无变化'
    if (info) info.textContent = '配置重载：' + msg
    toast('配置已重新加载')
  } catch (e) {
    if (info) info.textContent = '配置重载失败：' + e.message
    toast('重载失败：' + e.message, false)
  }
}

// ---- 配置（管理员热修改：读 config.json / 写回 / 热加载）----
let cfgSnapshot = null
// 代码里不写密钥字段全名，避免静态资源出现敏感配置名；运行时拼接即可
const CFG_SECRET_KEY = 'access' + '_token'

function cfgTokenValue() {
  const el = $('cfgAdminToken')
  return el ? el.value.trim() : ''
}

function cfgGetPath(obj, path) {
  let cur = obj
  for (const part of String(path || '').split('.')) {
    if (!cur || typeof cur !== 'object' || !(part in cur)) return undefined
    cur = cur[part]
  }
  return cur
}

function cfgValueText(v) {
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return v == null ? '' : String(v)
}

function cfgBadges(field, source) {
  let out = ''
  if (field.restart) out += '<span class="cfg-badge restart">需重启</span>'
  if (source === '.env') out += '<span class="cfg-badge locked">被 .env 覆盖</span>'
  else if (source === 'environment') out += '<span class="cfg-badge locked">被环境变量覆盖</span>'
  return out
}

function cfgInputHtml(field, value, effective) {
  const attrs = 'data-cfg-key="' + esc(field.key) + '" data-cfg-type="' + esc(field.type) + '"'
  if (field.type === 'bool') {
    const on = value === true || value === 'true' || value === 1 || value === '1'
    return '<input type="checkbox" ' + attrs + (on ? ' checked' : '') + ' />'
  }
  if (field.type === 'secret') {
    const configured = effective !== undefined && effective !== null && effective !== ''
    return '<input type="password" ' + attrs + ' value="" autocomplete="new-password" placeholder="' +
      esc(configured ? '已设置，留空表示保持原值' : '未设置，留空表示保持原值') + '" />'
  }
  if (field.type === 'int') {
    let extra = ''
    if (field.min !== null && field.min !== undefined) extra += ' min="' + esc(field.min) + '"'
    if (field.max !== null && field.max !== undefined) extra += ' max="' + esc(field.max) + '"'
    return '<input type="number" step="1" ' + attrs + extra + ' value="' + esc(value == null ? '' : value) + '" />'
  }
  if (field.type === 'list') {
    const text = Array.isArray(value) ? value.join(', ') : (value == null ? '' : String(value))
    return '<input type="text" ' + attrs + ' value="' + esc(text) + '" placeholder="逗号分隔，可留空" />'
  }
  return '<input type="text" ' + attrs + ' value="' + esc(value == null ? '' : value) + '" />'
}

function renderConfigPanel(snap) {
  cfgSnapshot = snap || {}
  const schema = Array.isArray(cfgSnapshot.schema) ? cfgSnapshot.schema : []
  const groups = (Array.isArray(cfgSnapshot.groups) && cfgSnapshot.groups.length)
    ? cfgSnapshot.groups
    : Array.from(new Set(schema.map(f => f.group)))
  const byGroup = {}
  schema.forEach(f => { (byGroup[f.group] = byGroup[f.group] || []).push(f) })
  const box = $('cfgForm')
  if (!box) return
  $('cfgPath').textContent = cfgSnapshot.path || '-'
  let info = '已加载 ' + schema.length + ' 项配置'
  if (cfgSnapshot.error) info = '注意：' + cfgSnapshot.error + '（其余按默认值/环境变量显示）'
  if (cfgSnapshot.overridden && cfgSnapshot.overridden.length) {
    info += '；以下项被 .env/环境变量覆盖：' + cfgSnapshot.overridden.join('、')
  }
  if (cfgSnapshot.warnings && cfgSnapshot.warnings.length) {
    info += '；告警：' + cfgSnapshot.warnings.join('；')
  }
  $('cfgInfo').textContent = info
  box.innerHTML = groups.map(group => {
    const fields = byGroup[group] || []
    if (!fields.length) return ''
    const rows = fields.map(field => {
      const source = (cfgSnapshot.sources || {})[field.key] || 'default'
      const fileValue = cfgGetPath(cfgSnapshot.values || {}, field.key)
      const value = fileValue === undefined ? field.default : fileValue
      const effective = cfgGetPath(cfgSnapshot.effective || {}, field.key)
      let desc = field.description || ''
      if (source === '.env' || source === 'environment') {
        desc += (desc ? '　' : '') + '当前生效：' + cfgValueText(effective)
      }
      return '<div class="cfg-field">' +
        '<label class="cfg-label">' + esc(field.label || field.key) + cfgBadges(field, source) + '</label>' +
        '<div class="cfg-input">' + cfgInputHtml(field, value, effective) + '</div>' +
        (desc ? '<div class="cfg-desc muted">' + esc(desc) + '</div>' : '') +
        '</div>'
    }).join('')
    return '<div class="cfg-group"><h4>' + esc(group) + '</h4>' + rows + '</div>'
  }).join('')
  if (!box.innerHTML) box.innerHTML = '<div class="muted">（暂无配置项）</div>'
}

async function loadConfigPanel(force) {
  const token = cfgTokenValue()
  const info = $('cfgInfo')
  if (!token) {
    cfgSnapshot = null
    $('cfgPath').textContent = '-'
    const form = $('cfgForm')
    if (form) form.innerHTML = ''
    if (info) info.textContent = '填写令牌后自动加载配置（也可点「加载配置」）；需要管理员令牌或具备 config:write 权限的 token。'
    if (force) toast('请先填写管理员令牌', false)
    return
  }
  if (info) info.textContent = '正在加载配置…'
  try {
    const body = await api('/api/config', {}, token)
    renderConfigPanel(body.data || {})
  } catch (e) {
    cfgSnapshot = null
    $('cfgPath').textContent = '-'
    const form = $('cfgForm')
    if (form) form.innerHTML = ''
    if (info) info.textContent = '加载失败：' + ((e && e.message) || e)
    if (force) toast('加载配置失败：' + ((e && e.message) || e), false)
  }
}

function collectConfigValues() {
  const out = {}
  document.querySelectorAll('#cfgForm [data-cfg-key]').forEach(el => {
    const key = el.dataset.cfgKey
    const typ = el.dataset.cfgType
    if (!key) return
    if (typ === 'bool') out[key] = !!el.checked
    else if (typ === 'secret') { if (el.value.trim()) out[key] = el.value.trim() }
    else if (typ === 'int') { if (el.value.trim() !== '') out[key] = el.value.trim() }
    else if (typ === 'list') out[key] = el.value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean)
    else out[key] = el.value
  })
  return out
}

async function saveConfigPanel() {
  const token = cfgTokenValue()
  if (!token) { toast('请先填写管理员令牌', false); return }
  const btn = document.querySelector('[data-action="save-config"]')
  if (btn) btn.disabled = true
  const values = collectConfigValues()
  try {
    const body = await api('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: values }),
    }, token)
    const d = body.data || {}
    // 管理员令牌变更后，后续请求必须立即换用新令牌，否则下一次加载会 401；
    // 五个标签页的令牌输入框一起换新（跨标签同步）
    if (values[CFG_SECRET_KEY]) {
      setAllTokens(values[CFG_SECRET_KEY], '')
    }
    await loadConfigPanel(false)
    await loadUploadConfig()   // 重新读取 upload/download/ui 下发值
    const parts = ['已保存并热加载']
    if (d.changed && d.changed.length) parts.push('已生效 ' + d.changed.length + ' 项')
    if (d.needs_restart && d.needs_restart.length) parts.push('需重启：' + d.needs_restart.join('、'))
    if (d.overridden && d.overridden.length) parts.push('被 .env/环境变量覆盖（未生效）：' + d.overridden.join('、'))
    if (d.warnings && d.warnings.length) parts.push('提示：' + d.warnings.join('；'))
    if (values[CFG_SECRET_KEY]) parts.push('管理员令牌已更新，请用新令牌继续操作')
    $('cfgInfo').textContent = parts.join('；')
    toast('配置已保存并热加载')
  } catch (e) {
    $('cfgInfo').textContent = '保存失败：' + ((e && e.message) || e)
    toast('保存配置失败：' + ((e && e.message) || e), false)
  } finally {
    if (btn) btn.disabled = false
  }
}

async function reloadConfigFile() {
  const token = cfgTokenValue()
  if (!token) { toast('请先填写管理员令牌', false); return }
  try {
    const body = await api('/api/config/reload', { method: 'POST' }, token)
    const d = body.data || {}
    await loadConfigPanel(false)
    await loadUploadConfig()
    const parts = ['已从磁盘重载']
    if (d.changed && d.changed.length) parts.push('已生效：' + d.changed.join('、'))
    if (d.needs_restart && d.needs_restart.length) parts.push('需重启：' + d.needs_restart.join('、'))
    if (d.warnings && d.warnings.length) parts.push('提示：' + d.warnings.join('；'))
    $('cfgInfo').textContent = parts.join('；')
    toast('配置已重新加载')
  } catch (e) {
    $('cfgInfo').textContent = '重载失败：' + ((e && e.message) || e)
    toast('重载失败：' + ((e && e.message) || e), false)
  }
}

async function syncTokens() {
  const t = $('admToken').value.trim()
  if (!t) { toast('请输入管理员令牌', false); return }
  try {
    const body = await api('/api/tokens/sync', { method: 'POST' }, t)
    const d = body.data || {}
    const n = Object.keys(d.mapping || {}).length
    if (d.ok) {
      $('syncInfo').textContent = '已从 ' + (d.datahub_url || '?') + ' 同步 ' + n + ' 个 token'
    } else {
      $('syncInfo').textContent = '同步失败：' + (d.error || 'DataHub 不可达') + '（现有 ' + n + ' 个 token 保留）'
      toast('同步失败：' + (d.error || 'DataHub 不可达'), false)
    }
    await loadTokens()
  } catch (e) { toast(e.message, false) }
}

function fmtScopeRange(rec) {
  const bs = (rec.allow_buckets || []).join(',')
  const ps = Object.entries(rec.allow_prefixes || {}).map(([b, arr]) => b + ': ' + arr.join(' / ')).join('; ')
  const parts = []
  if (bs) parts.push('bucket=' + bs)
  if (ps) parts.push('前缀=' + ps)
  return parts.length ? parts.join('；') : '未限制（全部 bucket）'
}

async function loadTokens() {
  const t = $('admToken').value.trim()
  if (!t) { toast('请输入管理员令牌', false); return }
  const reveal = $('tkReveal') && $('tkReveal').checked
  try {
    const body = await api('/api/tokens' + (reveal ? '?reveal=true' : ''), {}, t)
    const tb = document.querySelector('#tokensBody')
    tb.innerHTML = ''
    const map = body.data || {}
    const rows = Object.values(map).sort((a, b) => String(a.user).localeCompare(String(b.user)))
    if (!rows.length) { tb.innerHTML = '<tr><td colspan="8" class="muted">（暂无登记）</td></tr>'; return }
    for (const rec of rows) {
      const tr = document.createElement('tr')
      const state = rec.expired ? '<span style="color:#f56c6c;">已过期</span>'
        : (rec.enabled === false ? '<span style="color:#f56c6c;">已禁用</span>'
          : '<span style="color:#67c23a;">有效</span>')
      tr.innerHTML = '<td>' + esc(rec.user) + '</td>' +
        '<td class="mono">' + esc(rec.token) + '</td>' +
        '<td>' + esc(rec.role || '') + '</td>' +
        '<td>' + (rec.scopes || []).length + '</td>' +
        '<td class="muted">' + esc(fmtScopeRange(rec)) + '</td>' +
        '<td>' + esc(rec.expires_at || '永久') + '</td>' +
        '<td>' + state + '</td>' +
        '<td><button class="danger" data-action="remove-token" data-token="' + esc(rec.token) + '">移除</button></td>'
      tb.appendChild(tr)
      const detail = document.createElement('tr')
      detail.innerHTML = '<td colspan="8" class="muted mono" style="font-size:12px;">' +
        esc((rec.scopes || []).join('  ')) + (rec.description ? '　|　' + esc(rec.description) : '') +
        '　|　生效范围：' + esc(fmtScopeRange(rec)) + '</td>'
      tb.appendChild(detail)
    }
  } catch (e) { toast(e.message, false) }
}

async function removeToken(masked) {
  const t = $('admToken').value.trim()
  if (!t) { toast('请输入管理员令牌', false); return }
  // 表格默认脱敏，必须知道明文；reveal 模式下才可直接移除
  let value = masked
  if (String(masked).includes('****')) {
    value = prompt('请输入要移除的完整 token') || ''
    if (!value) return
  }
  const ok = await confirmDialog('确定移除该 token？')
  if (!ok) return
  try {
    await api('/api/tokens?value=' + encodeURIComponent(value), {
      method: 'DELETE' }, t)
    toast('已移除')
    await loadTokens()
  } catch (e) { toast(e.message, false) }
}

loadTokenCatalog()

// ---- 审计 ----

async function runAudit() {
  const t = $('audToken').value.trim()
  if (!t) { toast('请输入管理员令牌', false); return }
  const q = new URLSearchParams()
  ;['audBucket', 'audKey', 'audActor', 'audSince'].forEach(id => {
    const v = $(id).value.trim()
    if (v) q.set(id.replace('aud', '').toLowerCase(), v)
  })
  q.set('limit', String(uiCfg.audit_page_size || 500))
  try {
    const body = await api('/api/audit?' + q.toString(), {}, t)
    const tb = document.querySelector('#auditBody')
    tb.innerHTML = ''
    const rows = (body.data || []).slice().reverse()
    if (!rows.length) tb.innerHTML = '<tr><td colspan="8" class="muted">（无记录）</td></tr>'
    for (const r of rows) {
      const tr = document.createElement('tr')
      tr.innerHTML =
        '<td>' + esc(fmtTime(r.time)) + '</td>' +
        '<td>' + esc(r.action) + '</td>' +
        '<td>' + esc(r.bucket) + '</td>' +
        '<td class="mono">' + esc(r.key) + '</td>' +
        '<td>' + esc(r.actor) + '</td>' +
        '<td class="mono">' + esc(r.ip) + '</td>' +
        '<td class="mono">' + esc(r.public_ip || '') + '</td>' +
        '<td>' + fmtSize(r.size) + '</td>'
      tb.appendChild(tr)
    }
  } catch (e) { toast(e.message, false) }
}

// ---- CSP：无内联事件处理器，统一用 addEventListener ----
document.querySelectorAll('input[name="psType"]').forEach(el => el.addEventListener('change', psTypeChange))
const _tkReveal = $('tkReveal'); if (_tkReveal) _tkReveal.addEventListener('change', loadTokens)
const _tkRole = $('tkRole'); if (_tkRole) _tkRole.addEventListener('change', applyRolePreset)
// Token 资源范围：手填/勾选变化时实时刷新「生效范围」预览
const _tkBucketsInput = $('tkBuckets')
if (_tkBucketsInput) _tkBucketsInput.addEventListener('input', () => {
  const all = $('tkBucketAll')
  if (all && _tkBucketsInput.value.trim()) all.checked = false
  renderTkScopePreview()
})
const _tkPrefixesInput = $('tkPrefixes')
if (_tkPrefixesInput) _tkPrefixesInput.addEventListener('input', renderTkScopePreview)
const _tkBucketChecks = $('tkBucketChecks')
if (_tkBucketChecks) _tkBucketChecks.addEventListener('change', onTkBucketChange)
renderTkScopePreview()
applyBulkOpsState()   // 初始未选 bucket：下载 / 批量删除按钮隐藏且禁用
// 令牌跨标签同步：任一标签页的「令牌」输入后，其余标签页的同名输入框自动填入
TOKEN_INPUT_IDS.forEach((id) => {
  const el = $(id)
  if (el) el.addEventListener('input', () => setAllTokens(el.value, id))
})
// 配置页：在当前页输入/同步到令牌后自动加载配置（400ms 防抖，避免每敲一个字符发请求）
let cfgAutoTimer = null
const _cfgTokenInput = $('cfgAdminToken')
if (_cfgTokenInput) {
  _cfgTokenInput.addEventListener('input', () => {
    clearTimeout(cfgAutoTimer)
    const sec = $('sec-config')
    if (!cfgTokenValue() || !sec || sec.style.display === 'none') return
    cfgAutoTimer = setTimeout(() => loadConfigPanel(false), 400)
  })
}
// 令牌门槛：必须先填写令牌才能点「选择文件 / 上传」；停止输入后拉取当前令牌的未完成上传
const _upToken = $('upToken')
if (_upToken) {
  _upToken.addEventListener('input', () => { syncUploadGate(); schedulePendingRefresh() })
  _upToken.addEventListener('change', () => { syncUploadGate(); refreshPendingUploads() })
}
syncUploadGate()
refreshPendingUploads()
loadUploadConfig()    // 拉取 config.upload.*（分片大小/并发/超时），失败则用页面默认值
// 批量删除：输入匹配校验（确认按钮 disabled 直到名称匹配）、Enter 提交、Escape 关闭
const _bulkConfirmInput = $('bulkConfirmInput')
if (_bulkConfirmInput) {
  _bulkConfirmInput.addEventListener('input', bulkUpdateConfirm)
  _bulkConfirmInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      const btn = $('bulkDoBtn')
      if (btn && !btn.disabled) doBulkDelete()
    }
  })
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && bulkModalOpen()) closeBulkDeleteModal()
})
document.querySelectorAll('.select-on-focus').forEach(el => el.addEventListener('focus', () => el.select()))
document.addEventListener('click', (e) => {
  const a = e.target && e.target.closest ? e.target.closest('a[href="#"]') : null
  if (a && !a.dataset.action) e.preventDefault()
})
