# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""第 4 批可观测性回归测试

- /health/live、/health/ready
- /metrics 返回 Prometheus 文本格式，含 HTTP/领域指标
- X-Request-ID 透传/生成
- 上传/下载/删除计数递增
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ADMIN = "test-admin-token-0123456789"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    depot = tmp_path / "depot"; depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "meta_dir": "", "access_token": ADMIN,
        "datahub_url": "", "ui_enabled": True,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c
    config._CONFIG = None


def _metric(text, name):
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_health_endpoints(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/live").json()["status"] == "alive"
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_request_id_echoed_and_generated(client):
    r = client.get("/health", headers={"X-Request-ID": "trace-xyz-1"})
    assert r.headers.get("x-request-id") == "trace-xyz-1"
    r2 = client.get("/health")
    assert r2.headers.get("x-request-id")


def test_metrics_prometheus_format(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    for name in ("artifactdepot_http_requests_total",
                 "artifactdepot_http_request_duration_seconds_bucket",
                 "artifactdepot_objects_uploaded_total",
                 "artifactdepot_tokens_total",
                 "artifactdepot_disk_used_bytes"):
        assert name in text, name
    assert "# TYPE artifactdepot_http_requests_total counter" in text


def test_object_counters_increment(client):
    before = _metric(client.get("/metrics").text, "artifactdepot_objects_uploaded_total")
    client.post("/api/objects", params={"token": ADMIN},
                data={"bucket": "b", "key": "k"}, files={"file": ("k", b"x")})
    after = _metric(client.get("/metrics").text, "artifactdepot_objects_uploaded_total")
    assert after == before + 1

    d0 = _metric(client.get("/metrics").text, "artifactdepot_objects_downloaded_total")
    client.get("/api/objects/download", params={"token": ADMIN, "bucket": "b", "key": "k"})
    d1 = _metric(client.get("/metrics").text, "artifactdepot_objects_downloaded_total")
    assert d1 == d0 + 1

    x0 = _metric(client.get("/metrics").text, "artifactdepot_objects_deleted_total")
    client.delete("/api/objects", params={"token": ADMIN, "bucket": "b", "key": "k"})
    x1 = _metric(client.get("/metrics").text, "artifactdepot_objects_deleted_total")
    assert x1 == x0 + 1


def test_metrics_can_be_disabled(tmp_path, monkeypatch):
    depot = tmp_path / "depot"; depot.mkdir()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "depot_dir": str(depot), "access_token": ADMIN, "datahub_url": "",
        "metrics_enabled": False,
    }), encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_DEPOT_CONFIG", str(cfg))
    import artifactdepot.config as config
    config._CONFIG = None
    from artifactdepot.main import app
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 404
    config._CONFIG = None
