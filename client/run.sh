#!/usr/bin/env bash
# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0
#
# ArtifactDepot 上传客户端启动脚本（Linux / macOS）
#
# 做的事：
#   1. 检测系统 Python（需要 3.9+）
#   2. 如果系统没有 uv，自动安装到本目录 .uv/（不修改 shell 配置）
#   3. GUI 缺 tkinter 时，自动解包 Debian Tk 8.6 到 .tk86/（Xft，中文清晰）
#   4. 用 uv 在本目录创建/复用 .venv 虚拟环境
#   5. 启动 artifactdepot_upload.py，并把所有参数透传（如 --file/--bucket/--path）
#
# 用法：
#   ./run.sh                     # 图形界面
#   ./run.sh --help              # 查看客户端参数
#   ./run.sh --server ... --token ... --bucket ... --path ... --file ...
#
# 说明：
#   - 客户端本身没有第三方依赖；创建 venv 是为了隔离 Python 环境，
#     也方便以后在 client/requirements.txt 里加依赖（脚本会自动安装）。
#   - uv 默认优先使用 PATH 中已有的；没有才装到本目录 .uv/。
#   - Windows 请直接用 `python artifactdepot_upload.py`（或见 README）。

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
CLIENT="$SCRIPT_DIR/artifactdepot_upload.py"
VENV="$SCRIPT_DIR/.venv"
UV_DIR="$SCRIPT_DIR/.uv"
TK86_ROOT="$SCRIPT_DIR/.tk86"
UV_BIN=""

say()  { printf '\033[36m[启动]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[提示]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. 检测 Python 3.9+
# ---------------------------------------------------------------------------
PYTHON=""
PYTHON_VERSION=""
for cand in python3 python; do
    path="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$path" ] || continue
    if "$path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        PYTHON="$path"
        PYTHON_VERSION="$("$path" -c 'import sys; print(sys.version.split()[0])')"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    die "未找到 Python 3.9+。请先安装 Python 3.9 或更新版本，并确保 python3 在 PATH 中。"
fi
if [ ! -f "$CLIENT" ]; then
    die "找不到客户端文件：$CLIENT"
fi
say "Python：$PYTHON（$PYTHON_VERSION）"
say "目录：$SCRIPT_DIR"
PY_VER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
TK86_MULTIARCH="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_config_var("MULTIARCH") or "")')"

# ---------------------------------------------------------------------------
# 2. 查找 / 安装 uv
# ---------------------------------------------------------------------------
find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for p in "$UV_DIR/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$p" ]; then
            printf '%s\n' "$p"
            return 0
        fi
    done
    return 1
}

UV_BIN="$(find_uv || true)"
if [ -z "$UV_BIN" ]; then
    say "未找到 uv，自动安装到 $UV_DIR（首次需要联网）…"
    mkdir -p "$UV_DIR"
    # 让官方安装器装到本目录，并且不要改 ~/.bashrc / ~/.zshrc。
    # uv 自己的配置/缓存/数据也放本目录：某些机器的 ~/.config 不可写，
    # 安装器会因此返回非零（但二进制通常已经装好），下面兼容这种情况。
    export UV_INSTALL_DIR="$UV_DIR"
    export UV_NO_MODIFY_PATH=1
    export XDG_CONFIG_HOME="$UV_DIR/config"
    export XDG_CACHE_HOME="$UV_DIR/cache"
    export XDG_DATA_HOME="$UV_DIR/data"
    mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"

    install_script="https://astral.sh/uv/install.sh"
    install_rc=0
    if command -v curl >/dev/null 2>&1; then
        set +e
        curl -LsSf "$install_script" | sh
        install_rc=$?
        set -e
    elif command -v wget >/dev/null 2>&1; then
        set +e
        wget -qO- "$install_script" | sh
        install_rc=$?
        set -e
    else
        die "需要 curl 或 wget 才能自动安装 uv；请先安装其一，或手动安装 uv。"
    fi

    if [ -x "$UV_DIR/uv" ]; then
        UV_BIN="$UV_DIR/uv"
    else
        UV_BIN="$(find_uv || true)"
    fi
    if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
        die "uv 安装失败（安装器 exit=$install_rc）。可手动安装后重试：https://docs.astral.sh/uv/"
    fi
    if [ "$install_rc" -ne 0 ]; then
        warn "uv 安装器返回 $install_rc（通常是收据/配置目录写入告警），但 uv 已就位，继续。"
    fi
fi
say "uv：$UV_BIN（$("$UV_BIN" --version 2>/dev/null | head -n 1)）"

# 使用本目录的 uv 时（不是系统 PATH 里的），配置/缓存也放本目录：
# 某些环境 ~/.config 不可写，uv 创建 venv 时也会因此失败。
if [ "$UV_BIN" = "$UV_DIR/uv" ]; then
    export XDG_CONFIG_HOME="$UV_DIR/config"
    export XDG_CACHE_HOME="$UV_DIR/cache"
    export XDG_DATA_HOME="$UV_DIR/data"
    export UV_CACHE_DIR="$UV_DIR/cache"
    mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$UV_CACHE_DIR"
fi

# Tk 9（uv 托管 Python）在部分 X11 环境只使用 X core 字体，中文没有抗锯齿。
# Debian/Ubuntu 的 Tk 8.6 通常启用 Xft，可直接用 Noto Sans CJK；这里把官方包
# 解包到 client/.tk86，不需要 root，也不修改系统。
ensure_tk86() {
    local root="$TK86_ROOT"
    if ls "$root/usr/lib/python${PY_VER}"/lib-dynload/_tkinter*.so >/dev/null 2>&1; then
        return 0
    fi
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
        return 1
    fi
    local tmp
    tmp="$(mktemp -d "$SCRIPT_DIR/.tk86-tmp.XXXXXX" 2>/dev/null)" || return 1
    mkdir -p "$tmp/pkgs" "$tmp/root"
    local pkgs=( "python${PY_VER}-tk" libtk8.6 libtcl8.6 tcl8.6 tk8.6 libxft2 libxss1 )
    if ! ( cd "$tmp/pkgs" && apt-get download "${pkgs[@]}" >/dev/null 2>&1 ); then
        rm -rf "$tmp"
        return 1
    fi
    local deb
    for deb in "$tmp"/pkgs/*.deb; do
        if ! dpkg-deb -x "$deb" "$tmp/root" >/dev/null 2>&1; then
            rm -rf "$tmp"
            return 1
        fi
    done
    if ! ls "$tmp/root/usr/lib/python${PY_VER}"/lib-dynload/_tkinter*.so >/dev/null 2>&1; then
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$root"
    mv "$tmp/root" "$root"
    rm -rf "$tmp"
    say "已准备 Tk 8.6（Xft 抗锯齿字体）：$root"
    return 0
}

activate_tk86() {
    local libdir="$TK86_ROOT/usr/lib/$TK86_MULTIARCH"
    [ -d "$libdir" ] || libdir="$TK86_ROOT/usr/lib"
    export PYTHONPATH="$TK86_ROOT/usr/lib/python${PY_VER}/lib-dynload:$TK86_ROOT/usr/lib/python${PY_VER}${PYTHONPATH:+:$PYTHONPATH}"
    export LD_LIBRARY_PATH="$libdir:$TK86_ROOT/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export TCL_LIBRARY="$TK86_ROOT/usr/share/tcltk/tcl8.6"
    export TK_LIBRARY="$TK86_ROOT/usr/share/tcltk/tk8.6"
}

# ---------------------------------------------------------------------------
# 3. 选择解释器：GUI 需要 tkinter；优先系统 Tk 8.6，其次 uv 托管 Python
# ---------------------------------------------------------------------------
GUI_MODE=1
for arg in "$@"; do
    case "$arg" in
        --file|--file=*|--help|-h) GUI_MODE=0 ;;
    esac
done

SYS_HAS_TK=0
if "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
    SYS_HAS_TK=1
fi

PYTHON_FOR_VENV="$PYTHON"
PYTHON_FOR_VENV_DESC="系统 Python $PYTHON_VERSION"
TK86_ACTIVE=0

if [ "$GUI_MODE" -eq 1 ] && [ "$SYS_HAS_TK" -eq 0 ]; then
    if ensure_tk86; then
        activate_tk86
        TK86_ACTIVE=1
        PYTHON_FOR_VENV="$PYTHON"
        PYTHON_FOR_VENV_DESC="系统 Python $PYTHON_VERSION + 本地 Tk 8.6（Xft 抗锯齿）"
        say "GUI 使用 Tk 8.6 + Xft（中文 Noto Sans CJK）"
    elif [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import tkinter' >/dev/null 2>&1; then
        say "复用已有带 tkinter 的虚拟环境（Tk 9；中文可能有锯齿）"
        warn "未能准备 Tk 8.6（需要 apt-get 下载约 2MB 包）；如中文有锯齿可安装 python3-tk。"
    else
        say "系统 Python 没有 tkinter；使用 uv 安装托管 Python 3.12（Tk 9，中文可能有锯齿）…"
        export UV_PYTHON_INSTALL_DIR="$UV_DIR/python"
        mkdir -p "$UV_PYTHON_INSTALL_DIR"
        if ! "$UV_BIN" python install 3.12; then
            die "无法安装 uv 托管 Python 3.12。可改用系统包 python3-tk，或使用 CLI 参数 --file。"
        fi
        PYTHON_FOR_VENV="3.12"
        PYTHON_FOR_VENV_DESC="uv 托管 Python 3.12（Tk 9；中文可能有锯齿）"
    fi
fi

# ---------------------------------------------------------------------------
# 4. 创建 / 复用 .venv
# ---------------------------------------------------------------------------
VENV_REUSE=0
if [ -x "$VENV/bin/python" ] && \
   "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    if [ "$GUI_MODE" -eq 0 ]; then
        VENV_REUSE=1
    elif [ "$TK86_ACTIVE" -eq 1 ]; then
        # 必须使用与系统 Python 相同版本的 venv，Tk 8.6 的 _tkinter 才能加载
        if "$VENV/bin/python" -c "import sys, tkinter; raise SystemExit(0 if tkinter.TkVersion == 8.6 and '.'.join(map(str, sys.version_info[:2])) == '$PY_VER' else 1)" >/dev/null 2>&1; then
            VENV_REUSE=1
        fi
    elif "$VENV/bin/python" -c 'import tkinter' >/dev/null 2>&1; then
        VENV_REUSE=1
    fi
fi

if [ "$VENV_REUSE" -eq 1 ]; then
    say "复用虚拟环境：$VENV"
else
    say "创建虚拟环境：$VENV（$PYTHON_FOR_VENV_DESC）"
    rm -rf "$VENV"
    "$UV_BIN" venv --python "$PYTHON_FOR_VENV" "$VENV"
fi

if [ ! -x "$VENV/bin/python" ]; then
    die "虚拟环境创建失败：$VENV/bin/python 不存在"
fi
say "虚拟环境 Python：$("$VENV/bin/python" -c 'import sys; print(sys.version.split()[0])')"

# 以后如果 client/requirements.txt 出现，自动安装（当前客户端无第三方依赖）
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    say "安装 requirements.txt …"
    "$UV_BIN" pip install --python "$VENV/bin/python" -r "$SCRIPT_DIR/requirements.txt"
fi

# GUI 模式：安装可选的拖拽支持（失败不影响文件选择上传）
if [ "$GUI_MODE" -eq 1 ]; then
    if ! "$VENV/bin/python" -c 'import tkinterdnd2' >/dev/null 2>&1; then
        say "安装拖拽上传支持（tkinterdnd2）…"
        if ! "$UV_BIN" pip install --python "$VENV/bin/python" tkinterdnd2 >/dev/null 2>&1; then
            warn "tkinterdnd2 安装失败：拖拽不可用，仍可选择文件上传。"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 5. 环境检查与启动
# ---------------------------------------------------------------------------
HAS_TK=0
if "$VENV/bin/python" -c 'import tkinter' >/dev/null 2>&1; then
    HAS_TK=1
else
    if [ "$GUI_MODE" -eq 1 ]; then
        warn "当前虚拟环境没有 tkinter：图形界面不可用，仍可用 CLI 模式。"
        warn "Debian/Ubuntu（有 sudo 时）：sudo apt install python3-tk；Windows/macOS 官方 Python 自带。"
    fi
fi

if [ "$GUI_MODE" -eq 1 ] && [ "$HAS_TK" -eq 1 ] && [ "$(uname -s 2>/dev/null || echo unknown)" = "Linux" ]; then
    if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        warn "没有检测到图形会话（DISPLAY/WAYLAND_DISPLAY 为空），GUI 可能无法启动；"
        warn "可改用 CLI，例如：./run.sh --file /path/to/file --bucket <bucket> --path <目录> --token <token>"
    fi
fi

if [ "$GUI_MODE" -eq 1 ] && [ "$HAS_TK" -eq 0 ]; then
    die "GUI 需要 tkinter。可安装 python3-tk，或使用 CLI 参数（--file ...）。"
fi

cd "$SCRIPT_DIR"
exec "$VENV/bin/python" "$CLIENT" "$@"
