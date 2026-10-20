// Copyright 2026 apyyc
// SPDX-License-Identifier: Apache-2.0

const $ = (id) => document.getElementById(id)
let state = { bucket: '', prefix: '', limit: 200, offset: 0, items: [], total: 0, truncated: false }
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
  ;['browse', 'links', 'tokens', 'audit'].forEach(s => {
    $('sec-' + s).style.display = s === t ? '' : 'none'
  })
  // 进入 Token 页时拉取现有 bucket，渲染「资源范围」多选框（拉过一次后复用，「刷新现有 bucket」可强制刷新）
  if (t === 'tokens') loadTokenBuckets()
}

function authHeaders(token, extra) {
  const h = Object.assign({}, extra || {})
  if (token) h['Authorization'] = 'Bearer ' + token
  return h
}

// token 优先走 Authorization 头，避免出现在 URL query（访问日志/历史泄露）
async function api(url, opts = {}, token = '') {
  opts = Object.assign({}, opts)
  opts.headers = authHeaders(token, opts.headers)
  const r = await fetch(url, opts)
  let body = null
  try { body = await r.json() } catch (e) { body = null }
  if (!r.ok) throw new Error((body && body.detail) || ('HTTP ' + r.status))
  return body
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
  else if (a === 'open-mkdir') openMkdirModal()
  else if (a === 'do-mkdir') doMkdir()
  else if (a === 'close-mkdir') closeMkdirModal()
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

// URL 携带 token/bucket 进入（从协作平台跳转自动认证）
// 从 query 读 token 填入输入框，并立即清除地址栏里的 token（防进历史/Referer 泄露）
const urlParams = new URLSearchParams(location.search)
const urlToken = urlParams.get('token')
const urlBucket = urlParams.get('bucket')
if (urlToken) {
  $('upToken').value = urlToken   // 浏览/上传/下载通用令牌
  $('lkToken').value = urlToken   // 签名链接页令牌
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
      const dl = '/api/objects/download?bucket=' + encodeURIComponent(state.bucket) + '&key=' + encodeURIComponent(it.key) + '&token=' + encodeURIComponent($('upToken').value.trim())
      tr.innerHTML =
        '<td>' + esc(it.name) + '</td>' +
        '<td>' + fmtSize(it.size) + '</td>' +
        '<td class="muted">' + esc(fmtTime(it.mtime)) + '</td>' +
        '<td class="muted" title="' + esc(it.source_url) + '">' + esc((it.source_url || '').slice(0, 28)) + '</td>' +
        '<td class="muted">' + esc(it.uploader || '-') + '</td>' +
        '<td><a href="' + dl + '" referrerpolicy="no-referrer">下载</a> <a href="#" data-action="presign" data-key="' + esc(it.key) + '">签名链接</a> <a href="#" data-action="del-file" data-key="' + esc(it.key) + '" style="color:#f56c6c;">删除</a></td>'
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

// 上传（拖拽 / 多选 / 先审核 token / 并发 / 大文件分片）
const upItems = []   // { file, pct, status }
let upInFlight = 0
const UP_CHUNK_SIZE = 8 * 1024 * 1024  // 8 MB

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

function enqueueFiles(fileList) {
  const tok = $('upToken').value.trim()
  if (!tok) { toast('请先填写你的 api_token', false); return }
  const files = Array.from(fileList || [])
  if (!files.length) return
  for (const f of files) upItems.push({ file: f, pct: 0, status: '排队中' })
  renderUpQueue()
  processQueue()
}

function doUpload() {
  enqueueFiles($('upFile').files)
  $('upFile').value = ''   // 允许重复选同一文件
}

async function processQueue() {
  const max = parseInt($('upConcurrency').value) || 3
  while (upInFlight < max) {
    const token = $('upToken').value.trim()
    if (!token) break
    const item = upItems.find(i => i.status === '排队中')
    if (!item) break
    const actor = await checkToken(token)
    if (!actor) {
      item.status = '失败：token 无效'
      renderUpQueue()
      toast('token 无效，跳过 ' + item.file.name, false)
      continue
    }
    upInFlight++
    item.status = '上传中'
    const isBig = item.file.size > UP_CHUNK_SIZE
    const runner = isBig ? uploadOneChunked(item, token) : uploadOne(item, token)
    runner.then(ok => {
      item.pct = ok ? 100 : 0
      item.status = ok ? '成功' : '失败'
      upInFlight--
      renderUpQueue()
      if (ok) refresh()
      processQueue()
    })
  }
}

function uploadOne(item, token) {
  return new Promise((resolve) => {
    const file = item.file
    const key = state.prefix ? state.prefix + '/' + file.name : file.name
    const fd = new FormData()
    fd.append('file', file)
    fd.append('bucket', state.bucket)
    fd.append('key', key)
    fd.append('source_url', $('upSource').value.trim())
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/objects')
    xhr.setRequestHeader('Authorization', 'Bearer ' + token)
    xhr.timeout = 600000   // 10 分钟（大文件）
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        item.pct = Math.round(e.loaded / e.total * 100)
        item.status = '上传中 ' + fmtSize(e.loaded) + ' / ' + fmtSize(e.total)
        renderUpQueue()
      }
    }
    xhr.onload = () => {
      let body = null
      try { body = JSON.parse(xhr.responseText) } catch (e) { body = null }
      resolve(xhr.status >= 200 && xhr.status < 300 && body && body.code === 0)
    }
    xhr.onerror = () => resolve(false)
    xhr.ontimeout = () => resolve(false)
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

async function uploadOneChunked(item, token) {
  // 1. initiate
  let upload_id, chunk_size
  try {
    const r = await api('/api/objects/initiate', { method: 'POST' }, token)
    const d = r.data || {}
    upload_id = d.upload_id
    chunk_size = d.chunk_size || UP_CHUNK_SIZE
    if (!upload_id) throw new Error('服务端未返回 upload_id')
  } catch (e) {
    item.status = '失败：' + e.message
    return false
  }
  const file = item.file
  const totalChunks = Math.max(1, Math.ceil(file.size / chunk_size))
  const bucket = state.bucket
  const key = state.prefix ? state.prefix + '/' + file.name : file.name
  const source_url = $('upSource').value.trim()
  const chunkProgress = new Array(totalChunks).fill(0)
  function updatePct() {
    const loaded = chunkProgress.reduce((a, b) => a + b, 0)
    item.pct = Math.round(loaded / file.size * 100)
    item.status = '上传中 ' + fmtSize(loaded) + ' / ' + fmtSize(file.size)
    renderUpQueue()
  }
  function sendChunk(index) {
    return new Promise((resolve) => {
      const start = index * chunk_size
      const end = Math.min(file.size, start + chunk_size)
      const blob = file.slice(start, end)
      const fd = new FormData()
      fd.append('upload_id', upload_id)
      fd.append('index', String(index))
      fd.append('chunk', blob, 'chunk_' + index)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/objects/chunk')
      xhr.setRequestHeader('Authorization', 'Bearer ' + token)
      xhr.timeout = 600000
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          chunkProgress[index] = e.loaded
          updatePct()
        }
      }
      xhr.onload = () => {
        let body = null
        try { body = JSON.parse(xhr.responseText) } catch (e) { body = null }
        if (xhr.status >= 200 && xhr.status < 300 && body && body.code === 0) {
          chunkProgress[index] = (end - start)
          updatePct()
          resolve(true)
        } else {
          resolve(false)
        }
      }
      xhr.onerror = () => resolve(false)
      xhr.ontimeout = () => resolve(false)
      xhr.send(fd)
    })
  }
  // 2. 并行上传分片（4 路并发）
  let aborted = false
  const CHUNK_CONCURRENCY = 4
  const workers = []
  let nextIdx = 0
  for (let w = 0; w < Math.min(CHUNK_CONCURRENCY, totalChunks); w++) {
    workers.push(new Promise(async (resolve) => {
      while (nextIdx < totalChunks) {
        const idx = nextIdx++
        const ok = await sendChunk(idx)
        if (!ok) {
          nextIdx = totalChunks
          aborted = true
          resolve(false)
          return
        }
      }
      resolve(true)
    }))
  }
  try {
    await Promise.all(workers)
  } catch (e) {
    aborted = true
  }
  if (aborted) {
    await abortUpload(upload_id, token)
    item.status = '失败：分片上传失败'
    return false
  }
  // 3. complete
  const fd = new FormData()
  fd.append('upload_id', upload_id)
  fd.append('bucket', bucket)
  fd.append('key', key)
  fd.append('total_chunks', String(totalChunks))
  fd.append('source_url', source_url)
    fd.append('overwrite', 'true')
  try {
    await api('/api/objects/complete', { method: 'POST', body: fd }, token)
    return true
  } catch (e) {
    await abortUpload(upload_id, token)
    item.status = '失败：合并失败 ' + e.message
    return false
  }
}

function renderUpQueue() {
  const box = $('upQueue')
  if (!upItems.length) { box.innerHTML = ''; return }
  box.innerHTML = upItems.map(it =>
    '<div class="up-item"><span class="nm" title="' + esc(it.file.name) + '">' + esc(it.file.name) + '</span>' +
    '<progress value="' + it.pct + '" max="100"></progress>' +
    '<span class="st' + (it.status === '成功' ? ' ok' : it.status.startsWith('失败') ? ' bad' : '') + '">' + esc(it.status) + '</span></div>'
  ).join('')
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
  if (outside.length) txt += '　⚠️ 前缀所属 bucket 不在白名单：' + outside.join('、')
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
      $('syncInfo').textContent = '⚠️ 同步失败：' + (d.error || 'DataHub 不可达') + '（现有 ' + n + ' 个 token 保留）'
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
  return parts.length ? parts.join('；') : '⚠️ 全部 bucket'
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
document.querySelectorAll('.select-on-focus').forEach(el => el.addEventListener('focus', () => el.select()))
document.addEventListener('click', (e) => {
  const a = e.target && e.target.closest ? e.target.closest('a[href="#"]') : null
  if (a && !a.dataset.action) e.preventDefault()
})
