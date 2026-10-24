# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""流式 ZIP 生成（目录一键下载）：后台线程生产 + 有界队列 + 异步消费。

为什么不用「先写临时 zip 再返回」：
- 大目录的 ZIP 可能远大于磁盘余量（宿主 /tmp 只有几 GB），且临时文件会带来
  「下载完才删」的清理问题；
- 同步生成会阻塞事件循环，整个服务在该目录打包期间无法响应其他请求。

本模块的做法：
- zipfile 在独立线程里逐条把条目写入一个**非 seekable** 的队列 writer
  （ZIP_STORED + allowZip64，zipfile 自动使用 data descriptor）；
- 异步生成器从**有界队列**取块 yield 给 StreamingResponse，队列满时生产线程
  阻塞在 put 上形成背压，内存占用恒定（约 READ_CHUNK * QUEUE_MAX）；
- 读文件固定 256 KiB 块，绝不 `read()` 全量；
- 客户端中断（异步生成器被关闭 / 请求取消）时置位 threading.Event，
  生产线程在下一次 put 时收到 _Cancelled 并退出；审计通过 `finish_once()`
  保证恰好写一次（中断场景由生产线程兜底，正常场景由消费端写）。
"""
import asyncio
import io
import logging
import queue
import re
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Callable
from urllib.parse import quote

# 每次从磁盘读取的块大小（大文件禁止 read() 全量）
READ_CHUNK = 256 * 1024
# 有界队列容量（块数）：背压上限约 READ_CHUNK * QUEUE_MAX
QUEUE_MAX = 8

_SENTINEL = object()
_ERROR_TAG = "__zipstream_error__"

logger = logging.getLogger("artifactdepot")


def content_disposition(filename: str) -> str:
    """生成 ZIP 下载的 Content-Disposition：ASCII 回退 + RFC 5987 `filename*`。

    `filename="..."` 只放 ASCII 安全字符（不支持 filename* 的旧客户端可用），
    真实文件名（可能是中文）放 `filename*=UTF-8''<percent-encoded>`。
    """
    full = filename + ".zip"
    stem = filename.encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "download"
    return f'attachment; filename="{stem}.zip"; filename*=UTF-8\'\'{quote(full, safe="")}'


class _Cancelled(Exception):
    """客户端中断 / 流被关闭：生产线程用它快速退出。"""


def _put(q: "queue.Queue", item, cancel: threading.Event) -> None:
    """有界入队；队列满时循环重试，并始终响应 cancel（避免中断后线程卡死）。"""
    while True:
        if cancel.is_set():
            raise _Cancelled()
        try:
            q.put(item, timeout=0.2)
            return
        except queue.Full:
            continue


class _QueueWriter(io.RawIOBase):
    """把 zipfile 的写调用转成队列里的字节块（非 seekable）。"""

    def __init__(self, q: "queue.Queue", cancel: threading.Event):
        super().__init__()
        self._q = q
        self._cancel = cancel

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        _put(self._q, bytes(b), self._cancel)
        return len(b)


def _zip_date_time(ts) -> tuple:
    """epoch 秒 → ZIP 的 DOS 时间六元组；ZIP 最早只支持 1980 年，越界钳到 1980-01-01。"""
    try:
        lt = time.localtime(float(ts)) if ts is not None else time.localtime()
    except (TypeError, ValueError, OverflowError, OSError):
        lt = time.localtime()
    if lt.tm_year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)


def _zip_info(name: str, ts, is_dir: bool = False) -> zipfile.ZipInfo:
    """构造带源文件 mtime 的 ZipInfo（否则 zipfile.open(...,'w') 默认时间为 1980-01-01）。"""
    info = zipfile.ZipInfo(name)
    info.date_time = _zip_date_time(ts)
    info.compress_type = zipfile.ZIP_STORED
    if is_dir:
        info.external_attr = (0o40755 << 16) | 0x10   # Unix dir + MS-DOS directory 标记
    return info


def _produce(q: "queue.Queue", cancel: threading.Event, entries: list,
             root_name: str, state: dict, finish_once: Callable[[], None]) -> None:
    """后台线程：生成 ZIP 字节块放入队列；正常/异常/中断都能收尾并通知消费端。"""
    try:
        writer = _QueueWriter(q, cancel)
        with zipfile.ZipFile(writer, mode="w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            # 顶层文件夹条目（即使当前目录为空也已有内容，预检已排除空目录）
            zf.writestr(_zip_info(root_name + "/", None, is_dir=True), b"")
            for ent in entries:
                if cancel.is_set():
                    raise _Cancelled()
                name = ent["name"]
                if ent["is_dir"]:
                    zf.writestr(_zip_info(f"{root_name}/{name}/", ent.get("mtime"),
                                          is_dir=True), b"")
                    state["dirs"] += 1
                    continue
                try:
                    src = open(ent["path"], "rb")
                except FileNotFoundError:
                    continue                      # 扫描后被并发删除：跳过，不破坏 ZIP
                except OSError:
                    continue                      # 不可读文件：跳过该条目
                with src, zf.open(_zip_info(f"{root_name}/{name}", ent.get("mtime")),
                                  mode="w") as dst:
                    while chunk := src.read(READ_CHUNK):
                        dst.write(chunk)
                state["files"] += 1
    except _Cancelled:
        state["aborted"] = True
    except BaseException as exc:                  # noqa: BLE001 - 线程内异常必须上报消费端
        state["error"] = exc
        try:
            q.put((_ERROR_TAG, exc), timeout=0.2)
        except queue.Full:
            pass                                  # 消费端已不在，错误只能丢弃
    finally:
        state["done"] = True
        try:
            q.put(_SENTINEL, timeout=0.2)
        except queue.Full:
            pass
        if state["aborted"] or state["error"] is not None:
            # 客户端中断时异步生成器可能来不及执行 finally：这里兜底写审计
            finish_once()


async def zip_stream(entries: list, root_name: str,
                     on_finish: Callable[[int, int, int, bool], None]
                     ) -> AsyncIterator[bytes]:
    """异步生成器：把 entries（scan 结果，目录在前）打包成 ZIP 字节流。

    `on_finish(sent_bytes, files, dirs, aborted)` 在流结束（正常/中断/异常）时
    恰好调用一次，供调用方写审计。
    """
    q: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAX)
    cancel = threading.Event()
    lock = threading.Lock()
    state = {"files": 0, "dirs": 0, "sent": 0, "done": False,
             "aborted": False, "error": None, "finished": False}

    def finish_once() -> None:
        with lock:
            if state["finished"]:
                return
            state["finished"] = True
            aborted = bool(state["aborted"] or state["error"] is not None)
            sent = int(state["sent"])
            files = int(state["files"])
            dirs = int(state["dirs"])
        try:
            on_finish(sent, files, dirs, aborted)
        except Exception:                          # 审计失败不得影响下载（沿用 storage.audit 行为）
            logger.warning("批量下载审计写入失败", exc_info=True)

    thread = threading.Thread(target=_produce,
                              args=(q, cancel, entries, root_name, state, finish_once),
                              name="bulk-zip-producer", daemon=True)
    thread.start()
    completed = False
    try:
        while True:
            try:
                # 带超时的阻塞取值：即使消费端被取消、队列里不再有 sentinel，
                # 执行器线程也会在 0.5s 内自行返回（不会永久卡在 q.get 上）。
                item = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                completed = True
                break
            if isinstance(item, tuple) and item and item[0] == _ERROR_TAG:
                raise RuntimeError("ZIP 生成失败") from item[1]
            state["sent"] += len(item)
            yield item
    finally:
        cancel.set()
        if not completed:
            # 消费端提前停止（客户端中断/取消）：无论生产线程是否已写完，都标记 aborted。
            # 生产线程每 0.2s 检查一次 cancel，这里最多等 1s 确保其收尾并兜底写审计；
            # 只有中断路径才会同步等待，正常下载不受影响。
            state["aborted"] = True
            thread.join(1.0)
        finish_once()
