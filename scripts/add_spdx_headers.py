#!/usr/bin/env python3
# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0
"""源文件 SPDX 署名头工具（幂等，无第三方依赖）。

用途
  给本项目第一方源文件加/查 Apache-2.0 的 SPDX 双行署名头：

      # Copyright 2026 apyyc
      # SPDX-License-Identifier: Apache-2.0

  约定见 README「许可证与支持」；细则文档 docs/LICENSE-HEADER（本地留存，不进 git）。

用法
  python3 scripts/add_spdx_headers.py --check        # 只检查，缺头则退出码 1（可用于 CI）
  python3 scripts/add_spdx_headers.py                # 自动发现第一方文件并补头
  python3 scripts/add_spdx_headers.py --dry-run      # 预览将改哪些文件，不落盘
  python3 scripts/add_spdx_headers.py a.py b.sh      # 只处理指定文件

安全设计
  * 幂等：已含 SPDX 的文件跳过，可反复运行
  * shebang(#!) 永远留在第 1 行，署名头插其后
  * 注释符号按扩展名选择（.py/.sh/Dockerfile → #，.js → //，.css → /* */）
  * VENDORED 里的第三方压缩包不加头：那是别人的版权，加了等于错误署名
  * 不碰 JSON / .conf / .env：这类格式不支持注释
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

YEAR = "2026"
OWNER = "apyyc"
SPDX = "Apache-2.0"
MARKER = "SPDX-License-Identifier"

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 第三方 vendored 资产：绝不加署名头
VENDORED = frozenset(
    {
        "src/artifactdepot/resources/static/swagger-ui-bundle.js",
        "src/artifactdepot/resources/static/swagger-ui.css",
        "src/artifactdepot/resources/static/redoc.standalone.js",
    }
)

# 第一方源码的扩展名/文件名白名单
INCLUDE_SUFFIXES = (".py", ".sh", ".js", ".css")
INCLUDE_NAMES = ("Dockerfile",)


def _rel_arg(rel: str) -> str:
    """把入参统一成相对项目根的路径（绝对路径、../ 写法都可用）。"""
    path = Path(rel)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(PROJECT_ROOT))
        except ValueError:
            return str(path)  # 项目外的文件：保留原样，apply_one 会报 MISSING
    return rel


def comment_block(rel: str) -> list[str]:
    """按文件类型返回署名头的行（不含前后空行）。"""
    if rel.endswith(".css"):
        return [
            f"/* Copyright {YEAR} {OWNER}",
            f" * {MARKER}: {SPDX}",
            " */",
        ]
    if rel.endswith(".js"):
        prefix = "//"
    else:  # .py / .sh / Dockerfile / 无扩展名
        prefix = "#"
    return [
        f"{prefix} Copyright {YEAR} {OWNER}",
        f"{prefix} {MARKER}: {SPDX}",
    ]


def discover() -> list[str]:
    """找第一方源文件：已跟踪 + 未跟踪但未忽略的文件；非 git 环境退化为 rglob。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        tracked = out.splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        tracked = [
            str(p.relative_to(PROJECT_ROOT))
            for p in PROJECT_ROOT.rglob("*")
            if p.is_file()
        ]
    picked: list[str] = []
    for rel in tracked:
        if rel in VENDORED:
            continue
        name = Path(rel).name
        if name in INCLUDE_NAMES or name.endswith(INCLUDE_SUFFIXES):
            picked.append(rel)
    return sorted(set(picked))


def has_header(text: str) -> bool:
    return MARKER in text


def apply_one(rel: str, *, dry_run: bool) -> str:
    path = PROJECT_ROOT / rel
    if not path.is_file():
        return "MISSING"
    raw = path.read_text(encoding="utf-8")
    if has_header(raw):
        return "ok"
    block = "\n".join(comment_block(rel))
    if raw.startswith("#!"):
        shebang, _, rest = raw.partition("\n")
        new = f"{shebang}\n\n{block}\n\n{rest}"
    else:
        new = f"{block}\n\n{raw}"
    if dry_run:
        return "would-edit"
    path.write_text(new, encoding="utf-8")
    return "edited"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="指定文件（相对项目根）；省略则自动发现")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不改文件")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查：有缺头的文件就退出码 1，适合 CI / pre-commit",
    )
    args = parser.parse_args(argv)

    targets = args.files or discover()
    targets = [_rel_arg(f) for f in targets]

    missing: list[str] = []
    edited: list[str] = []
    for rel in targets:
        if rel in VENDORED:
            print(f"skip-vendored        {rel}")
            continue
        if args.check:
            path = PROJECT_ROOT / rel
            if path.is_file() and not has_header(path.read_text(encoding="utf-8")):
                missing.append(rel)
                print(f"missing-header       {rel}")
            continue
        status = apply_one(rel, dry_run=args.dry_run)
        if status in ("edited", "would-edit"):
            edited.append(rel)
        print(f"{status:20} {rel}")

    if args.check:
        print(f"\n检查 {len(targets)} 个文件，缺署名头 {len(missing)} 个")
        return 1 if missing else 0
    verb = "将补头" if args.dry_run else "已补头"
    print(f"\n处理 {len(targets)} 个文件，{verb} {len(edited)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
