# ==========================================================================================
# 文件        : smoke_target.py
# 所属目录    : scripts/
# 文件用途    : 冒烟用被控端靶窗口: 真实接收系统键盘/鼠标输入并落状态文件, 供进程级断言
# 架构定位    : 测试装置, 只在冒烟脚本内作为子进程启动, 不进入产品路径
# 核心功能    : Tk 窗口 + 文本框 + 画布; 记录按键时间、鼠标事件与文本内容; 输出窗口坐标
# 职责边界    : 不连网; 不解码; 不实现远程协议; 状态文件只写本地临时目录
# ==========================================================================================

"""冒烟靶窗口.

由 scripts/smoke.py 以子进程启动:
    python scripts/smoke_target.py --ready <ready.json> --state <state.json>

ready 文件: 窗口与控件在屏幕上的绝对坐标, 供注入端定位.
state 文件: 当前文本、按键事件(含接收纳秒时间)、鼠标事件计数.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import time
import tkinter as tk
from pathlib import Path
from typing import Any


def _enable_dpi_awareness() -> None:
    """与注入端保持同一坐标口径; 失败不阻塞(旧系统无该接口)."""
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写: 先写临时文件再替换, 避免注入端读到半截内容."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class TargetWindow:
    """记录真实输入事件的靶窗口."""

    def __init__(self, ready_path: Path, state_path: Path) -> None:
        self.ready_path = ready_path
        self.state_path = state_path
        self.key_events: list[dict[str, Any]] = []
        self.mouse = {"left": 0, "right": 0, "wheel": 0, "motion": 0}
        self.root = tk.Tk()
        self.root.title("RDC Smoke Target")
        self.root.geometry("720x420+160+120")
        self.root.attributes("-topmost", True)

        self.text = tk.Text(self.root, height=6, width=80, font=("Consolas", 12))
        self.text.pack(padx=12, pady=(12, 6), fill=tk.X)
        self.canvas = tk.Canvas(self.root, height=160, background="#1f2430", highlightthickness=0)
        self.canvas.pack(padx=12, pady=6, fill=tk.BOTH, expand=True)
        self.canvas.create_text(
            12, 12, anchor="nw", fill="#c8d0e0", text="鼠标落点区: 左键/右键/滚轮均计数", font=("Microsoft YaHei", 11)
        )
        self.status = tk.Label(self.root, text="就绪", anchor="w")
        self.status.pack(padx=12, pady=(0, 10), fill=tk.X)

        self.text.bind("<KeyRelease>", self._on_key)
        self.canvas.bind("<Button-1>", lambda _event: self._count("left"))
        self.canvas.bind("<Button-3>", lambda _event: self._count("right"))
        self.canvas.bind("<MouseWheel>", lambda _event: self._count("wheel"))
        self.canvas.bind("<Motion>", lambda _event: self._count("motion"))
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _count(self, name: str) -> None:
        self.mouse[name] += 1
        self._flush()

    def _on_key(self, event: tk.Event) -> None:
        self.key_events.append(
            {
                "keysym": event.keysym,
                "char": event.char,
                "state": event.state,
                "received_ns": time.time_ns(),
            }
        )
        if len(self.key_events) > 400:
            del self.key_events[:200]
        self._flush()

    def _snapshot(self) -> dict[str, Any]:
        focused = self.root.focus_get()
        return {
            "text": self.text.get("1.0", "end-1c"),
            "key_events": self.key_events,
            "mouse": self.mouse,
            "focus": str(focused) if focused is not None else None,
            "updated_ns": time.time_ns(),
        }

    def _flush(self) -> None:
        _write_json(self.state_path, self._snapshot())

    def _write_ready(self) -> None:
        self.root.update_idletasks()
        root_x, root_y = self.root.winfo_rootx(), self.root.winfo_rooty()
        text_x, text_y = self.text.winfo_rootx(), self.text.winfo_rooty()
        canvas_x, canvas_y = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        _write_json(
            self.ready_path,
            {
                "hwnd": self.root.winfo_id(),
                "window": {"x": root_x, "y": root_y, "w": self.root.winfo_width(), "h": self.root.winfo_height()},
                "text": {
                    "x": text_x,
                    "y": text_y,
                    "w": self.text.winfo_width(),
                    "h": self.text.winfo_height(),
                },
                "canvas": {
                    "x": canvas_x,
                    "y": canvas_y,
                    "w": self.canvas.winfo_width(),
                    "h": self.canvas.winfo_height(),
                },
            },
        )
        self.status.configure(text=f"就绪 · hwnd={self.root.winfo_id()}")

    def _claim_focus(self) -> None:
        """把窗口拉到前台并聚焦文本框; 冒烟注入前调用一次, 避免后台窗口收不到按键."""
        self.root.deiconify()
        self.root.lift()
        with contextlib.suppress(tk.TclError):
            self.root.focus_force()
        self.text.focus_set()
        self._flush()

    def _close(self) -> None:
        self._flush()
        self.root.destroy()

    def run(self) -> None:
        """启动事件循环; 定时刷新状态文件作为心跳."""
        self.root.after(300, self._write_ready)
        self.root.after(450, self._claim_focus)
        self.root.after(1200, self._claim_focus)

        def heartbeat() -> None:
            self._flush()
            self.root.after(100, heartbeat)

        self.root.after(400, heartbeat)
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冒烟靶窗口")
    parser.add_argument("--ready", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    _enable_dpi_awareness()
    window = TargetWindow(Path(args.ready), Path(args.state))
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
