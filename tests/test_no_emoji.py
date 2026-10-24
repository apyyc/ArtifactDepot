# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""文档与平台 UI 禁止 emoji（2026-10-25 需求）

扫描范围：
- 用户文档：README.md、CHANGELOG.md、docs/*.md、client/README.md
- 平台 UI：resources/index.html、static/app.js、static/app.css、client/*.py（桌面 GUI）

第三方打包资源（swagger-ui-bundle.js / redoc.standalone.js）与依赖缓存（client/.uv）不在范围内。
emoji 范围覆盖常见符号区（U+2600–U+27BF、U+2B00–U+2BFF）、扩展 pictographs
（U+1F000–U+1FAFF）以及变体选择符 U+FE0F（警示符号由 U+26A0 + U+FE0F 组成）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EMOJI_PATTERN = re.compile(
    "[" + "".join((
        "\u2600-\u27bf",   # 杂项符号 / Dingbats（警示、对勾、叉号等）
        "\u2b00-\u2bff",   # 杂项符号与箭头（星形等）
        "\U0001F000-\U0001FAFF",  # 扩展 pictographs
        "\uFE0F",          # 变体选择符（emoji presentation）
    )) + "]"
)


def _targets():
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "client" / "README.md"]
    files += sorted((ROOT / "docs").glob("*.md"))
    resources = ROOT / "src" / "artifactdepot" / "resources"
    files += [resources / "index.html",
              resources / "static" / "app.js",
              resources / "static" / "app.css"]
    files += sorted((ROOT / "client").glob("*.py"))
    return [f for f in files if f.is_file()]


def test_no_emoji_in_docs_and_ui():
    offenders = []
    for path in _targets():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in EMOJI_PATTERN.finditer(line):
                ch = m.group(0)
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno}: {ch!r} (U+{ord(ch):04X}) -> {line.strip()[:120]}")
    assert not offenders, "文档/UI 中禁止出现 emoji：\n" + "\n".join(offenders[:50])
