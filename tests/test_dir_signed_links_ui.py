# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""目录签名链接网页入口回归（静态标记）

网页脚本无法在 pytest 里执行 DOM，这里只做「入口/接口/元素」存在性断言，
避免后续重构把目录行入口、进度弹窗或轮询接口删掉。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dir_link_ui_markers():
    base = ROOT / "src" / "artifactdepot" / "resources"
    html = (base / "index.html").read_text(encoding="utf-8")
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    # 目录行：下载 / 签名链接（kind=dir）/ 删除
    assert 'data-action="download-dir" data-key=' in js
    assert 'data-action="presign" data-key=' in js and 'data-kind="dir"' in js
    # 打包进度：复用下载进度弹窗，含取消打包 / 后台进行
    assert "pkgTrack(" in js and "pkgPoll(" in js
    assert "/api/objects/signed-links/" in js and "/package" in js
    assert "/package/cancel" in js
    assert 'id="dlTitle"' in html
    assert 'id="pkgCancelBtn"' in html and 'id="pkgBgBtn"' in html
    # 签名链接弹窗的目录提示 / 打包状态提示
    assert 'id="psKindHint"' in html and 'id="psPackageNote"' in html
    assert "package_state" in js and "目录 ZIP" in js
