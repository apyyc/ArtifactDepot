# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""pytest 全局夹具：隔离真实配置文件

测试默认不读取仓库里的 src/artifactdepot/config/.env（里面可能有真实密钥），
需要测 .env 的用例自行 monkeypatch.setenv("ARTIFACT_DEPOT_ENV", ...)。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_config_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_DEPOT_ENV", str(tmp_path / ".env.not-exist"))
    yield
