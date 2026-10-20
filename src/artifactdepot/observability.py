# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""可观测性（无第三方依赖）：请求 ID、结构化访问日志、Prometheus 指标。

- ``ObservabilityMiddleware``：为每个 HTTP 请求生成/透传 ``X-Request-ID``，
  统计耗时与状态码，写一条 JSON 访问日志，并更新 HTTP 指标；
- ``render_metrics()``：输出 Prometheus 文本格式（text/plain; version=0.0.4）；
- 领域指标：上传/下载/删除计数、token 数、文件系统磁盘用量。

说明：这里实现的是「够用就好」的极简指标，避免引入 prometheus-client 依赖；
如需直方图分位数/多进程聚合等完整能力，可平滑替换为 prometheus-client。
"""
import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 极简 Prometheus 指标注册表
# ---------------------------------------------------------------------------

_LABEL_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"))


def _esc(value) -> str:
    text = str(value)
    for a, b in _LABEL_ESCAPES:
        text = text.replace(a, b)
    return text


def _labels(names, values) -> str:
    if not names:
        return ""
    parts = [f'{n}="{_esc(v)}"' for n, v in zip(names, values)]
    return "{" + ",".join(parts) + "}"


class _Metric:
    def __init__(self, name, help_text, label_names=()):
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)
        self._lock = threading.Lock()

    def _key(self, labels):
        labels = tuple(labels or ())
        if len(labels) != len(self.label_names):
            raise ValueError(f"{self.name} 需要 {len(self.label_names)} 个标签，收到 {len(labels)}")
        return labels


class Counter(_Metric):
    type_name = "counter"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._values = {}

    def inc(self, labels=(), value=1):
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + value

    def _render(self):
        for key, val in sorted(self._values.items()):
            yield f"{self.name}{_labels(self.label_names, key)} {val}"


class Gauge(_Metric):
    type_name = "gauge"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._values = {}

    def set(self, value, labels=()):
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def _render(self):
        for key, val in sorted(self._values.items()):
            yield f"{self.name}{_labels(self.label_names, key)} {val}"


class Histogram(_Metric):
    type_name = "histogram"

    def __init__(self, name, help_text, label_names=(), buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)):
        super().__init__(name, help_text, label_names)
        self.buckets = tuple(sorted(buckets))
        self._data = {}

    def observe(self, value, labels=()):
        key = self._key(labels)
        with self._lock:
            entry = self._data.setdefault(key, {"buckets": [0] * len(self.buckets), "sum": 0.0, "count": 0})
            for i, b in enumerate(self.buckets):
                if value <= b:
                    entry["buckets"][i] += 1
            entry["sum"] += value
            entry["count"] += 1

    def _render(self):
        for key, entry in sorted(self._data.items()):
            base = self.name + _labels(self.label_names, key)
            # 每个 bucket 的 le 标签需要追加到已有标签后面
            for b, cnt in zip(self.buckets, entry["buckets"]):
                le = _labels(self.label_names, key)
                le = (le[:-1] + f',le="{b}"' + "}") if le else f'{{le="{b}"}}'
                yield f"{self.name}_bucket{le} {cnt}"
            le = _labels(self.label_names, key)
            le = (le[:-1] + ',le="+Inf"' + "}") if le else '{le="+Inf"}'
            yield f"{self.name}_bucket{le} {entry['count']}"
            yield f"{base}_sum {entry['sum']}"
            yield f"{base}_count {entry['count']}"


class MetricsRegistry:
    def __init__(self):
        self._metrics = []

    def counter(self, name, help_text, label_names=()):
        m = Counter(name, help_text, label_names); self._metrics.append(m); return m

    def gauge(self, name, help_text, label_names=()):
        m = Gauge(name, help_text, label_names); self._metrics.append(m); return m

    def histogram(self, name, help_text, label_names=(), buckets=None):
        m = Histogram(name, help_text, label_names, buckets or (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
        self._metrics.append(m); return m

    def render(self) -> str:
        lines = []
        for m in self._metrics:
            lines.append(f"# HELP {m.name} {m.help}")
            lines.append(f"# TYPE {m.name} {m.type_name}")
            lines.extend(m._render())
        return "\n".join(lines) + "\n"


REGISTRY = MetricsRegistry()

HTTP_REQUESTS = REGISTRY.counter(
    "artifactdepot_http_requests_total", "HTTP 请求总数（按方法/路由/状态码）",
    ("method", "route", "status"))
HTTP_DURATION = REGISTRY.histogram(
    "artifactdepot_http_request_duration_seconds", "HTTP 请求耗时（秒）",
    ("method", "route"))
UPLOADS = REGISTRY.counter("artifactdepot_objects_uploaded_total", "成功上传对象数")
DOWNLOADS = REGISTRY.counter("artifactdepot_objects_downloaded_total", "成功下载对象数")
DELETES = REGISTRY.counter("artifactdepot_objects_deleted_total", "成功删除对象/目录数")
TOKENS = REGISTRY.gauge("artifactdepot_tokens_total", "已登记 token 数")
DISK_USED = REGISTRY.gauge("artifactdepot_disk_used_bytes", "depot 所在文件系统已用字节")
DISK_FREE = REGISTRY.gauge("artifactdepot_disk_free_bytes", "depot 所在文件系统可用字节")
DISK_TOTAL = REGISTRY.gauge("artifactdepot_disk_total_bytes", "depot 所在文件系统总字节")


def render_metrics() -> str:
    """在返回指标前刷新需要即时计算的 gauge（token 数、磁盘用量）。"""
    from artifactdepot import storage
    import shutil
    try:
        TOKENS.set(len(storage.load_tokens()))
    except Exception:
        pass
    try:
        usage = shutil.disk_usage(storage.get_root())
        DISK_USED.set(usage.used)
        DISK_FREE.set(usage.free)
        DISK_TOTAL.set(usage.total)
    except OSError:
        pass
    return REGISTRY.render()


# ---------------------------------------------------------------------------
# 请求 ID + 结构化访问日志
# ---------------------------------------------------------------------------

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

_LOGGER = logging.getLogger("artifactdepot")
_LOGGING_INSTALLED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_data", None)
        if isinstance(extra, dict):
            data.update(extra)
        rid = request_id_var.get("")
        if rid and "request_id" not in data:
            data["request_id"] = rid
        return json.dumps(data, ensure_ascii=False)


def install_observability() -> None:
    """安装结构化日志 handler（幂等）。"""
    global _LOGGING_INSTALLED
    if _LOGGING_INSTALLED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.addHandler(handler)
    _LOGGER.propagate = False
    _LOGGING_INSTALLED = True


def _route_of(scope) -> str:
    route = scope.get("route")
    return getattr(route, "path", None) or scope.get("path", "unknown")


class ObservabilityMiddleware:
    """纯 ASGI 中间件：不缓冲响应体（对 Range/大文件下载友好）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        rid = ""
        for key, value in scope.get("headers", []):
            if key == b"x-request-id":
                rid = value.decode("latin-1")[:64]
                break
        if not rid:
            rid = uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                if not any(k == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration = time.perf_counter() - start
            method = scope.get("method", "")
            route = _route_of(scope)
            HTTP_REQUESTS.inc((method, route, "500"))
            HTTP_DURATION.observe(duration, (method, route))
            _LOGGER.info("request", extra={"extra_data": {
                "method": method, "path": scope.get("path", ""), "route": route,
                "status": 500, "duration_ms": round(duration * 1000, 2),
                "client": (scope.get("client") or ["", 0])[0], "request_id": rid}})
            raise
        finally:
            request_id_var.reset(token)

        duration = time.perf_counter() - start
        method = scope.get("method", "")
        route = _route_of(scope)
        status = str(status_holder["status"])
        HTTP_REQUESTS.inc((method, route, status))
        HTTP_DURATION.observe(duration, (method, route))
        _LOGGER.info("request", extra={"extra_data": {
            "method": method, "path": scope.get("path", ""), "route": route,
            "status": status_holder["status"], "duration_ms": round(duration * 1000, 2),
            "client": (scope.get("client") or ["", 0])[0], "request_id": rid}})
