#!/usr/bin/env bash
# 在 Ubuntu 虚机里启动被控端。默认使用桌面会话的 DISPLAY=:0。
# 用法: bash scripts/start-host.sh [--port 27888] [--fps 15]
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [ -n "${PYTHON:-}" ]; then
    PYTHON_BIN="$PYTHON"
elif [ -x "$HOME/rdc/venv/bin/python" ]; then
    PYTHON_BIN="$HOME/rdc/venv/bin/python"
else
    PYTHON_BIN="python3"
fi

export DISPLAY="${DISPLAY:-:0}"
if [ -z "${XAUTHORITY:-}" ]; then
    for candidate in "/run/user/$(id -u)/gdm/Xauthority" "$HOME/.Xauthority"; do
        if [ -f "$candidate" ]; then
            export XAUTHORITY="$candidate"
            break
        fi
    done
fi

export PYTHONPATH="$APP_DIR/src:$APP_DIR:${PYTHONPATH:-}"

exec "$PYTHON_BIN" scripts/rdc_host_linux.py "$@"
