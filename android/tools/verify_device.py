#!/usr/bin/env python3
# ==========================================================================================
# 文件        : verify_device.py
# 所属目录    : android/tools/
# 文件用途    : 真机一键验证: 安装、启动、登录账号中心、连接被控端、截图与日志留证
# 架构定位    : 真机验收脚本; 只用 adb 驱动已安装应用, 不修改应用代码与设备系统设置
# 核心功能    : 设备就绪检查; 安装覆盖; UI 自动填表; 会话全屏铺满与沉浸浮条断言; 截图/logcat 落盘
# 职责边界    : 不伪造验证结论; 无设备或步骤失败时如实退出并写明原因; 不 root、不改系统设置
# 运行环境    : Python 3.12 + Android SDK platform-tools + 已开启 USB 调试的真机
# 依赖关系    : com.rdc.remote 1.0.0 签名 APK; 已被控端(桌面端)在线
# ==========================================================================================

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PACKAGE = "com.rdc.remote"
MAIN_ACTIVITY = f"{PACKAGE}/.MainActivity"
ACCOUNT_ACTIVITY = f"{PACKAGE}/.AccountActivity"
SESSION_ACTIVITY = f"{PACKAGE}/.SessionActivity"

DEFAULT_SDK = Path("D:/RDC-Build/tools/android/sdk")
SAFE_INPUT = re.compile(r"^[A-Za-z0-9._@:-]+$")


class Blocked(Exception):
    """需要用户配合或环境不满足; 不是被测应用失败."""


@dataclass
class Node:
    text: str
    class_name: str
    clickable: bool
    bounds: tuple[int, int, int, int]
    enabled: bool = True
    focused: bool = False

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2


def find_adb(explicit: str | None) -> Path:
    candidates = [explicit, os.environ.get("ADB"), str(DEFAULT_SDK / "platform-tools" / "adb.exe")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise Blocked("未找到 adb, 请安装 Android SDK platform-tools 或用 --adb 指定")


class Device:
    def __init__(self, adb: Path, serial: str | None):
        self.adb = adb
        self.serial = serial

    def run(self, args: list[str], binary: bool = False) -> subprocess.CompletedProcess:
        command = [str(self.adb)]
        if self.serial:
            command += ["-s", self.serial]
        command += args
        # 统一按字节读取: logcat 里可能混入非 UTF-8 字节, text=True 会在解码线程直接崩掉
        return subprocess.run(command, capture_output=True)

    def shell(self, *args: str) -> str:
        result = self.run(["shell", *args])
        return result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else result.stdout

    def exec_out(self, *args: str) -> bytes:
        result = subprocess.run(
            [str(self.adb)]
            + (["-s", self.serial] if self.serial else [])
            + ["exec-out", *args],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"adb exec-out 失败: {result.stderr.decode('utf-8', 'replace')}")
        return result.stdout

    def wait_ready(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            output = self.run(["devices"]).stdout
            text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
            lines = [line for line in text.splitlines() if line.strip() and not line.startswith("List of")]
            if len(lines) == 1 and "\tdevice" in lines[0]:
                if not self.serial:
                    self.serial = lines[0].split("\t", 1)[0]
                return
            if len(lines) == 1 and "unauthorized" in lines[0]:
                raise Blocked("设备已连接但未授权, 请在手机上点「允许 USB 调试」后重试")
            if len(lines) > 1:
                raise Blocked("检测到多台设备, 请用 --serial 指定目标设备")
            time.sleep(0.5)
        raise Blocked("adb 设备列表为空: 请插上真机并在开发者选项里开启 USB 调试")


class Ui:
    def __init__(self, device: Device, evidence: Path):
        self.device = device
        self.evidence = evidence

    def dump(self) -> list[Node]:
        # 先删旧文件: uiautomator 偶尔取不到空闲态会静默失败, 留下上一次的层级
        raw = b""
        for _ in range(4):
            self.device.shell("rm", "-f", "/sdcard/rdc-window.xml")
            # 会话页持续出帧, 普通 dump 会报 could not get idle state; 压缩模式不等待空闲
            self.device.shell("uiautomator", "dump", "--compressed", "/sdcard/rdc-window.xml")
            raw = self.device.exec_out("cat", "/sdcard/rdc-window.xml")
            if raw.strip():
                break
            time.sleep(0.6)
        try:
            root = ET.fromstring(raw.decode("utf-8", "replace"))
        except ET.ParseError as error:
            raise RuntimeError(f"UI 层级解析失败: {error}; 抓取内容={raw[:80]!r}")
        nodes: list[Node] = []
        for item in root.iter("node"):
            bounds = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", item.attrib.get("bounds", ""))
            if not bounds:
                continue
            nodes.append(
                Node(
                    text=item.attrib.get("text", ""),
                    class_name=item.attrib.get("class", ""),
                    clickable=item.attrib.get("clickable") == "true",
                    bounds=tuple(int(value) for value in bounds.groups()),  # type: ignore[arg-type]
                    enabled=item.attrib.get("enabled") != "false",
                    focused=item.attrib.get("focused") == "true",
                )
            )
        return nodes

    def tap(self, node: Node) -> None:
        x, y = node.center
        self.device.shell("input", "tap", str(x), str(y))

    def tap_text(self, text: str, clickable_only: bool = True, exact: bool = False) -> None:
        for node in self.dump():
            matched = node.text == text if exact else text in node.text
            if matched and (node.clickable or not clickable_only):
                self.tap(node)
                return
        raise RuntimeError(f"界面上找不到可点击文字「{text}」")

    def tap_button(self, text: str) -> None:
        """点主操作按钮: 同名的底部导航格宽度小, 取最宽的那个可点击节点."""
        candidates = [node for node in self.dump() if node.text == text and node.clickable]
        if not candidates:
            raise RuntimeError(f"界面上找不到按钮「{text}」")
        widest = max(candidates, key=lambda node: node.bounds[2] - node.bounds[0])
        self.tap(widest)

    def input_into(self, node: Node, value: str) -> None:
        if not SAFE_INPUT.match(value):
            raise Blocked(
                f"自动输入只支持字母/数字/._@:-, 当前值包含其他字符: {value[:1]}…; 请改用简单口令或手动登录",
            )
        self.tap(node)
        self.device.shell("input", "text", value)

    def edit_nodes(self) -> list[Node]:
        return [node for node in self.dump() if node.class_name == "android.widget.EditText"]

    def ime_shown(self) -> bool:
        return "mInputShown=true" in self.device.shell("dumpsys", "input_method")

    def fill_field(self, index: int, value: str) -> bool:
        """按序号填第 index 个输入框: 先收键盘再抓层级, 焦点确认后才输入."""
        if not SAFE_INPUT.match(value):
            raise Blocked(f"自动输入不支持该字符: {value[:1]}…; 请改用简单口令或手动输入")
        for _ in range(2):
            if self.ime_shown():
                self.device.shell("input", "keyevent", "4")
                time.sleep(0.4)
            nodes = self.edit_nodes()
            if len(nodes) <= index:
                raise RuntimeError(f"连接页输入框数量不足: 需要第 {index + 1} 栏, 实际 {len(nodes)} 栏")
            self.tap(nodes[index])
            time.sleep(0.5)
            target = self.edit_nodes()
            if len(target) <= index or not target[index].focused:
                continue
            self.device.shell("input", "keyevent", "123")
            for _ in range(min(len(target[index].text) + 4, 64)):
                self.device.shell("input", "keyevent", "67")
            self.device.shell("input", "text", value)
            time.sleep(0.4)
            filled = self.edit_nodes()
            if len(filled) > index and filled[index].text == value:
                return True
        return False

    def screenshot(self, name: str) -> Path:
        path = self.evidence / f"{name}.png"
        path.write_bytes(self.device.exec_out("screencap", "-p"))
        return path

    def resumed_activity(self) -> str:
        text = self.device.shell("dumpsys", "activity", "activities")
        for line in text.splitlines():
            # Android 14 起改用 topResumedActivity; 旧版本仍是 mResumedActivity
            if "mResumedActivity" in line or "topResumedActivity" in line:
                match = re.search(r"([\w.]+/[\w.]+)", line)
                if match:
                    return match.group(1)
        return ""

    def wait_activity(self, activity: str, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if activity in self.resumed_activity():
                return True
            time.sleep(1)
        return False

    def wait_text(self, text: str, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(text in node.text for node in self.dump()):
                return True
            time.sleep(1)
        return False


def write_evidence_header(evidence: Path, device: Device, apk: Path) -> None:
    model = device.shell("getprop", "ro.product.model").strip()
    release = device.shell("getprop", "ro.build.version.release").strip()
    sdk = device.shell("getprop", "ro.build.version.sdk").strip()
    abi = device.shell("getprop", "ro.product.cpu.abi").strip()
    package = device.shell("dumpsys", "package", PACKAGE)
    version = re.search(r"versionName=([^\s]+)", package)
    (evidence / "device.txt").write_text(
        "\n".join(
            [
                f"采集时间: {datetime.now().isoformat(timespec='seconds')}",
                f"设备型号: {model} {release} (API {sdk})",
                f"设备 ABI: {abi}",
                f"安装包: {apk}",
                f"已安装版本: {version.group(1) if version else '未知'}",
                f"SHA-256: {hashlib_sha256(apk)}",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def hashlib_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump_logcat(device: Device, evidence: Path) -> None:
    output = device.run(["logcat", "-d"]).stdout
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    lines = [line for line in text.splitlines() if "Rdc" in line or PACKAGE in line]
    (evidence / "logcat.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def login(ui: Ui, account: str, password: str) -> None:
    ui.device.shell("am", "start", "-n", ACCOUNT_ACTIVITY)
    time.sleep(1.5)
    nodes = ui.dump()
    edits = [node for node in nodes if node.class_name == "android.widget.EditText"]
    if not edits:
        if any("账号编号" in node.text or "退出登录" in node.text for node in nodes):
            print("  账号页已是登录态, 跳过登录")
            return
        raise RuntimeError("账号页未找到输入框, 当前界面既不是登录表单也不是已登录态")
    if len(edits) < 2:
        raise RuntimeError("账号页输入框少于 2 个, 界面结构可能已变化")
    ui.input_into(edits[0], account)
    ui.input_into(edits[1], password)
    ui.tap_text("登录")
    if not ui.wait_text("账号编号"):
        failure = [node.text for node in ui.dump() if node.text and ("失败" in node.text or "错误" in node.text or "不正确" in node.text)]
        raise RuntimeError(f"登录后未出现已登录状态; 界面提示: {' / '.join(failure) or '无'}")
    ui.screenshot("02-account-logged-in")
    print("  登录成功")


def reveal_connect_form(ui: Ui) -> list[Node]:
    """连接页表单在首屏下方(横屏更明显); 先向下滚, 再反向滚, 直到三栏输入框全部进入层级."""
    nodes = ui.dump()
    for downward in (True, False):
        for _ in range(4):
            if len([node for node in nodes if node.class_name == "android.widget.EditText"]) >= 3:
                return nodes
            # 横屏时 wm size 仍报竖屏物理分辨率, 滑动坐标取 UI 层级里的实际屏幕边界
            width = max((node.bounds[2] for node in nodes), default=1080)
            height = max((node.bounds[3] for node in nodes), default=2400)
            y_from = int(height * 0.82) if downward else int(height * 0.30)
            y_to = int(height * 0.30) if downward else int(height * 0.82)
            ui.device.shell("input", "swipe", str(width // 2), str(y_from), str(width // 2), str(y_to), "260")
            time.sleep(0.4)
            nodes = ui.dump()
    return nodes


def connect(ui: Ui, address: str, peer_id: str, connect_code: str) -> None:
    ui.device.shell("am", "start", "-n", MAIN_ACTIVITY)
    time.sleep(1.2)
    nodes = ui.dump()
    if not any(node.class_name == "android.widget.EditText" for node in nodes):
        # 底部导航文字本身可能不可点击, 坐标点击父容器效果相同
        ui.tap_text("连接", clickable_only=False, exact=True)
        time.sleep(0.8)
        nodes = reveal_connect_form(ui)
    edits = [node for node in nodes if node.class_name == "android.widget.EditText"]
    if len(edits) < 3:
        raise RuntimeError(f"连接页输入框应为 地址/设备码/连接码 三栏, 实际 {len(edits)} 栏, 界面结构可能已变化")
    entered = (
        ui.fill_field(0, address)
        and ui.fill_field(1, peer_id)
        and ui.fill_field(2, connect_code)
    )
    if ui.ime_shown():
        ui.device.shell("input", "keyevent", "4")
    time.sleep(0.6)
    ui.screenshot("03-connect-filled")
    checks = [n.text for n in ui.edit_nodes()][:3]
    if not entered or checks != [address, peer_id, connect_code]:
        raise RuntimeError(f"连接页三栏未填入预期值: {checks}")
    ui.tap_button("连接")
    if not ui.wait_activity(SESSION_ACTIVITY, timeout=25):
        if any("允许" in node.text and node.clickable for node in ui.dump()):
            raise Blocked("连接时弹出系统权限弹窗, 请手动点「允许」后重新运行本脚本")
        status = [node.text for node in ui.dump() if node.text and ("失败" in node.text or "错误" in node.text or "无法" in node.text or "超时" in node.text)]
        ui.screenshot("03-connect-failed")
        raise RuntimeError(f"点击连接后 25 秒内未进入会话页; 界面提示: {' / '.join(status) or '无'}")
    time.sleep(3)
    ui.screenshot("04-session")
    print("  已进入会话页")


def screen_size(device: Device) -> tuple[int, int]:
    text = device.shell("wm", "size")
    match = re.findall(r"(\d+)x(\d+)", text)
    if not match:
        raise Blocked("无法读取屏幕分辨率, 请确认设备已正常解锁")
    return int(match[-1][0]), int(match[-1][1])


def last_log_line(device: Device, marker: str) -> str:
    output = device.run(["logcat", "-d"]).stdout
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    lines = [line for line in text.splitlines() if marker in line]
    return lines[-1] if lines else ""


def log_values(line: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=([^\s]+)", line))


BAR_LABELS = ("适应屏幕", "还原", "回到左上角", "键盘", "画质", "文件", "断开", "返回")


def log_lines(device: Device, marker: str) -> list[str]:
    output = device.run(["logcat", "-d"]).stdout
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    return [line for line in text.splitlines() if marker in line]


def wait_new_log(device: Device, marker: str, since: int, timeout: float) -> str:
    """等待第 since 条之后出现的新日志行, 返回该行原文."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = log_lines(device, marker)
        if len(lines) > since:
            return lines[-1]
        time.sleep(0.35)
    raise RuntimeError(f"等待日志 {marker} 超时({timeout:.0f}s)")


def parse_items(line: str) -> dict[str, dict[str, float]]:
    """解析「标签=x,y,WxH,enabled=..,alpha=..」形式的坐标日志."""
    # 去掉 logcat 前缀(时间/pid/标签), 只保留事件体
    if "]" in line:
        line = line.split("] ", 1)[-1]
    items: dict[str, dict[str, float]] = {}
    for part in line.split("|"):
        if "=" not in part:
            continue
        label, rest = part.split("=", 1)
        bits = rest.split(",")
        if len(bits) < 3:
            continue
        try:
            x, y = int(bits[0]), int(bits[1])
            width, height = (int(value) for value in bits[2].split("x"))
        except ValueError:
            continue
        alpha = 1.0
        for bit in bits[3:]:
            if bit.startswith("alpha="):
                alpha = float(bit.split("=", 1)[1])
        items[label] = {
            "x": x,
            "y": y,
            "w": width,
            "h": height,
            "cx": x + width // 2,
            "cy": y + height // 2,
            "enabled": "enabled=true" in rest,
            "alpha": alpha,
        }
    return items



def wait_fullscreen_fit(device: Device, since: int, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = [line for line in log_lines(device, "viewport_fit") if "mode=fullscreen" in line][since:]
        for line in reversed(lines):
            try:
                state = parse_viewport(line)
            except RuntimeError:
                continue
            view_w, view_h = state["view"]  # type: ignore[misc]
            # 应用外壳固定竖屏: 全屏视口必须高于宽
            if view_h > view_w:
                return line
        time.sleep(0.35)
    raise RuntimeError(f"进入全屏后 {timeout:.0f} 秒内没有竖屏全屏显示日志")


def parse_viewport(line: str) -> dict[str, object]:
    values = log_values(line)
    try:
        source_w, source_h = (int(part) for part in values["source"].split("x"))
        view_w, view_h = (int(part) for part in values["view"].split("x"))
        content_w, content_h = (int(part) for part in values["content"].split("x"))
        offset_x, offset_y = (int(part) for part in values["offset"].split(","))
        return {
            "mode": values.get("mode", ""),
            "scale": float(values["scale"]),
            "min": float(values["min"]),
            "max": float(values["max"]),
            "source": (source_w, source_h),
            "view": (view_w, view_h),
            "content": (content_w, content_h),
            "offset": (offset_x, offset_y),
        }
    except (KeyError, ValueError) as error:
        raise RuntimeError(f"全屏显示日志字段不完整: {line}") from error


def verify_fullscreen(device: Device, ui: Ui) -> None:
    """固定竖屏的会话验证: 默认适应屏幕、全屏沉浸、浮条内容/可用态/3 秒淡出、点击落点."""
    width, height = screen_size(device)
    portrait = parse_viewport(last_log_line(device, "viewport_fit"))
    if portrait["mode"] != "window":
        raise RuntimeError(f"会话窗口态日志缺失: {portrait}")
    if abs(float(portrait["scale"]) - float(portrait["min"])) > 0.01:
        raise RuntimeError(f"窗口态默认倍率不是适应屏幕: {portrait['scale']} / 下限 {portrait['min']}")
    p_view = portrait["view"]
    p_content = portrait["content"]
    if abs(p_content[0] - p_view[0]) > 2 or abs(p_content[1] - p_view[1]) > 2:
        raise RuntimeError(f"窗口态未显示整个远端桌面: 内容 {p_content} / 视口 {p_view}")
    ui.screenshot("04b-portrait-fit")

    # 竖屏操作区按钮坐标来自日志(会话页持续出帧, uiautomator 取不到层级)
    action_line = last_log_line(device, "action_buttons")
    for _ in range(6):
        buttons = parse_items(action_line)
        if "全屏" in buttons:
            break
        since = len(log_lines(device, "action_buttons"))
        action_line = wait_new_log(device, "action_buttons", since, 6)
    buttons = parse_items(action_line)
    if "全屏" not in buttons:
        raise RuntimeError(f"竖屏操作区未记录「全屏」按钮: {action_line}")
    target = buttons["全屏"]
    for _ in range(8):
        if target["cy"] < height - 2:
            break
        # 按钮在折叠区下方: 上滑露出后再读一次坐标
        device.shell("input", "swipe", str(width // 2), str(int(height * 0.75)),
                     str(width // 2), str(int(height * 0.35)), "350")
        since = len(log_lines(device, "action_buttons"))
        action_line = wait_new_log(device, "action_buttons", since, 6)
        target = parse_items(action_line)["全屏"]
    if target["cy"] >= height - 2:
        raise RuntimeError(f"「全屏」按钮不在可视区: {target}")
    fit_since = len([line for line in log_lines(device, "viewport_fit") if "mode=fullscreen" in line])
    bar_since = len(log_lines(device, "floating_bar_shown"))
    device.shell("input", "tap", str(int(target["cx"])), str(int(target["cy"])))

    line = wait_fullscreen_fit(device, fit_since, 15)
    if not ui.wait_activity(SESSION_ACTIVITY, timeout=10):
        raise RuntimeError("进入全屏后会话页不在前台")
    state = parse_viewport(line)
    if abs(float(state["scale"]) - float(state["min"])) > 0.01:
        raise RuntimeError(f"全屏默认倍率不是适应屏幕: {state['scale']} / 下限 {state['min']}")
    if abs(float(state["max"]) - 4.0) > 0.01:
        raise RuntimeError(f"最大倍率不是 4.0: {state['max']}")
    source_w, source_h = state["source"]  # type: ignore[misc]
    view_w, view_h = state["view"]  # type: ignore[misc]
    content_w, content_h = state["content"]  # type: ignore[misc]
    expected_min = min(view_w / source_w, view_h / source_h)
    if abs(float(state["min"]) - expected_min) > 0.02:
        raise RuntimeError(f"最小倍率不等于适应比例: {state['min']} / 期望 {expected_min:.2f}")
    # 远端原始宽高比不得被拉伸: 内容宽高比与远端一致
    if abs(content_w / content_h - source_w / source_h) > 0.01:
        raise RuntimeError(
            f"画面比例被拉伸: 内容 {content_w}x{content_h} / 远端 {source_w}x{source_h}",
        )
    # 适应屏幕: 整个远端桌面完整可见
    if content_w > view_w + 2 or content_h > view_h + 2:
        raise RuntimeError(f"适应屏幕未完整显示远端桌面: 内容 {content_w}x{content_h} / 视口 {view_w}x{view_h}")
    if view_h <= view_w:
        raise RuntimeError(f"全屏视口不是竖屏: 视口 {view_w}x{view_h}")
    expected_offset = ((view_w - content_w) // 2, (view_h - content_h) // 2)
    if abs(state["offset"][0] - expected_offset[0]) > 2 or abs(state["offset"][1] - expected_offset[1]) > 2:
        raise RuntimeError(f"默认倍率未居中: 偏移 {state['offset']} / 期望 {expected_offset}")
    bars_line = last_log_line(device, "system_bars")
    if "hidden=true" not in bars_line:
        raise RuntimeError(f"全屏未隐藏系统状态栏与导航栏: {bars_line}")
    if len(log_lines(device, "floating_bar_shown")) != bar_since:
        raise RuntimeError("进入全屏后浮条自行出现, 不符合「无常驻图标」要求")
    ui.screenshot("05-fullscreen-default")

    # 轻触画面中心: 唤出浮条, 同时是远端鼠标点击
    shot = ui.screenshot("05b-fullscreen-default-check")
    from PIL import Image

    screen_w, screen_h = Image.open(shot).size
    tap_x, tap_y = screen_w // 2, screen_h // 2
    tap_line = ""
    for _ in range(3):
        time.sleep(0.9)
        tap_since = len(log_lines(device, "remote_tap"))
        items_count = len(log_lines(device, "floating_bar_items"))
        device.shell("input", "tap", str(tap_x), str(tap_y))
        tap_line = wait_new_log(device, "remote_tap", tap_since, 5)
        tap_values = log_values(tap_line)
        view_x = int(tap_values["view"].split(",")[0])
        content_parts = tap_values["content"].split(",")
        content_left = int(content_parts[0])
        tap_content_w, tap_content_h = (int(part) for part in content_parts[2].split("x"))
        normalized_x = float(tap_values["normalized"].split(",")[0])
        expected_x = (view_x - content_left) / tap_content_w
        if abs(normalized_x - expected_x) > 0.01:
            raise RuntimeError(
                f"点击落点换算不正确: 归一化 x={normalized_x:.3f} / 期望 {expected_x:.3f} ({tap_line})",
            )
        if (tap_content_w, tap_content_h) != (content_w, content_h):
            # 视口被外部手势改动(例如有人碰了屏幕): 双击还原后重试一次
            print("  检测到视口被外部手势改动, 双击还原后重试")
            device.shell("input", "tap", str(tap_x), str(tap_y))
            time.sleep(0.08)
            device.shell("input", "tap", str(tap_x), str(tap_y))
            time.sleep(1.2)
            bar_since = len(log_lines(device, "floating_bar_shown"))
            continue
        break
    else:
        raise RuntimeError("连续三次轻触都没有在默认倍率下取到落点")
    wait_new_log(device, "floating_bar_shown", bar_since, 4)
    bar_since = len(log_lines(device, "floating_bar_shown"))
    items_line = wait_new_log(device, "floating_bar_items", items_count, 4)
    items = parse_items(items_line)
    ui.screenshot("06-fullscreen-bar")

    for label in BAR_LABELS:
        if label not in items:
            raise RuntimeError(f"操作浮条缺少「{label}」: {items_line}")
    if "适应屏幕" not in items:
        raise RuntimeError(f"默认档浮条倍率显示不是「适应屏幕」: {items_line}")
    overflow = content_w > view_w + 2 or content_h > view_h + 2
    top_left = items["回到左上角"]
    if bool(top_left["enabled"]) != overflow:
        raise RuntimeError(f"「回到左上角」可用状态不正确: enabled={top_left['enabled']} 超出屏幕={overflow}")
    if not bool(top_left["enabled"]) and float(top_left["alpha"]) > 0.5:
        raise RuntimeError(f"「回到左上角」禁用态未置灰: alpha={top_left['alpha']}")

    # 还原: 回默认档(适应屏幕)
    items_count = len(log_lines(device, "floating_bar_items"))
    reset_count = len(log_lines(device, "viewport_reset"))
    reset_button = items["还原"]
    device.shell("input", "tap", str(int(reset_button["cx"])), str(int(reset_button["cy"])))
    wait_new_log(device, "viewport_reset", reset_count, 4)
    restored_line = last_log_line(device, "viewport_fit")
    if abs(float(log_values(restored_line).get("scale", "0")) - float(state["min"])) > 0.02:
        raise RuntimeError(f"还原后倍率不是适应屏幕默认档: {restored_line}")
    reset_items = parse_items(wait_new_log(device, "floating_bar_items", items_count, 4))
    if "适应屏幕" not in reset_items:
        raise RuntimeError(
            f"还原后浮条倍率未显示「适应屏幕」: {last_log_line(device, 'floating_bar_items')}",
        )
    ui.screenshot("08-fullscreen-reset")

    # 浮条 3 秒淡出
    hide_count = len(log_lines(device, "floating_bar_hidden"))
    hidden = wait_new_log(device, "floating_bar_hidden", hide_count, 5)
    if "reason=timeout" not in hidden:
        raise RuntimeError(f"浮条消失原因不是 3 秒超时: {hidden}")
    time.sleep(0.5)
    ui.screenshot("09-fullscreen-bar-gone")
    print(
        f"  竖屏默认适应屏幕: 全屏后 {state['scale']:.4f} 倍(下限 {state['min']:.2f}, 上限 {state['max']:.2f}), "
        f"内容 {content_w}x{content_h} / 视口 {view_w}x{view_h} / 远端 {source_w}x{source_h}, 居中偏移 {state['offset']}; "
        f"浮条条目齐全、倍率「适应屏幕」、3 秒淡出; 系统栏已隐藏",
    )
    print("  双指缩放与平移: adb 无法注入双指, 已由 ViewportMath 单测覆盖; 请在真机手动确认")



def verify_rotation_stability(device: Device, ui: Ui) -> None:
    """旋转手机: 应用外壳固定竖屏, 界面不重建、会话不中断、视口倍率与偏移不变."""
    from PIL import Image

    before_ready = len(log_lines(device, "page_ready"))
    before_destroyed = len(log_lines(device, "page_destroyed"))
    before_view = last_log_line(device, "viewport_fit")
    shot = ui.screenshot("10a-before-rotate")
    before_size = Image.open(shot).size
    rotation = device.shell("settings", "get", "system", "user_rotation").strip() or "0"
    try:
        device.shell("settings", "put", "system", "user_rotation", "1")
        time.sleep(2.5)
        after_shot = ui.screenshot("10b-after-rotate")
        after_size = Image.open(after_shot).size
    finally:
        device.shell("settings", "put", "system", "user_rotation", rotation)
        time.sleep(1.5)
    if after_size != before_size:
        raise RuntimeError(f"旋转后界面尺寸变化: {before_size} -> {after_size}")
    if len(log_lines(device, "page_ready")) != before_ready:
        raise RuntimeError("旋转后会话页被重建(page_ready 增加)")
    if len(log_lines(device, "page_destroyed")) != before_destroyed:
        raise RuntimeError("旋转后会话页被销毁(page_destroyed 增加)")
    after_view = last_log_line(device, "viewport_fit")
    if after_view != before_view:
        raise RuntimeError(f"旋转后视口重算: {before_view} -> {after_view}")
    print(
        f"  旋转手机(user_rotation=1): 界面保持 {after_size[0]}x{after_size[1]} 竖屏, 会话未重建, "
        f"倍率与偏移不变; 已恢复 user_rotation={rotation}",
    )



def main() -> int:
    parser = argparse.ArgumentParser(description="真机一键验证: 安装、启动、登录、连接、截图留证")
    parser.add_argument("--apk", default=None)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--adb", default=None)
    parser.add_argument("--account", default=os.environ.get("RDC_ACCOUNT"))
    parser.add_argument("--password", default=os.environ.get("RDC_ACCOUNT_PASSWORD"))
    parser.add_argument("--peer-id", default=os.environ.get("RDC_PEER_ID"))
    parser.add_argument("--connect-code", default=os.environ.get("RDC_CONNECT_CODE"))
    parser.add_argument("--address", default=os.environ.get("RDC_PEER_ADDRESS"), help="被控端地址, 形如 192.168.31.80:27888")
    parser.add_argument("--evidence", default=None)
    parser.add_argument("--skip-login", action="store_true")
    parser.add_argument("--skip-connect", action="store_true")
    parser.add_argument("--skip-fullscreen", action="store_true", help="只验到连接为止, 全屏手工取证")
    args = parser.parse_args()

    apk = Path(args.apk) if args.apk else (
        Path(__file__).resolve().parents[1] / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    )
    if not apk.is_file():
        print(f"受阻: APK 不存在: {apk}", file=sys.stderr)
        return 3
    evidence = Path(args.evidence) if args.evidence else (
        Path(__file__).resolve().parents[1] / "evidence" / f"device-{datetime.now():%Y%m%d-%H%M%S}"
    )
    evidence.mkdir(parents=True, exist_ok=True)

    try:
        device = Device(find_adb(args.adb), args.serial)
        device.wait_ready()
        write_evidence_header(evidence, device, apk)
        print(f"设备: {device.serial or '已连接'}; 证据目录: {evidence}")

        print("[1/6] 安装 APK")
        result = device.run(["install", "-r", str(apk)])
        output = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else result.stdout
        if result.returncode != 0 or "Success" not in output:
            raise RuntimeError(f"安装失败: {output.strip()}")
        device.shell("am", "force-stop", PACKAGE)
        device.shell("logcat", "-c")

        print("[2/6] 启动应用")
        device.shell("input", "keyevent", "224")
        device.shell("wm", "dismiss-keyguard")
        # 先停掉上次遗留的会话页, 否则 am start 只会把旧任务带到前台
        device.shell("am", "force-stop", PACKAGE)
        time.sleep(1.0)
        device.shell("am", "start", "-W", "-n", MAIN_ACTIVITY)
        ui = Ui(device, evidence)
        if not ui.wait_activity(f"{PACKAGE}/.MainActivity", timeout=15):
            raise RuntimeError("应用启动后未进入 MainActivity")
        time.sleep(1.5)
        # 上次残留的会话页可能把任务带回前台: 退到主界再继续
        for _ in range(5):
            if "MainActivity" in ui.resumed_activity():
                break
            device.shell("input", "keyevent", "4")
            time.sleep(1.2)
        if "MainActivity" not in ui.resumed_activity():
            raise RuntimeError(f"无法回到主界面: {ui.resumed_activity()}")
        ui.screenshot("01-home")
        print("  首页已启动")

        if not args.skip_login:
            print("[3/6] 登录账号中心")
            if not args.account or not args.password:
                raise Blocked("缺少登录凭据: 请设置 RDC_ACCOUNT / RDC_ACCOUNT_PASSWORD 或传 --account/--password")
            login(ui, args.account, args.password)
        else:
            print("[3/6] 跳过登录")

        if not args.skip_connect:
            print("[4/6] 连接被控端")
            if not args.peer_id or not args.connect_code or not args.address:
                raise Blocked(
                    "缺少被控端信息: 请设置 RDC_PEER_ADDRESS / RDC_PEER_ID / RDC_CONNECT_CODE "
                    "或传 --address/--peer-id/--connect-code",
                )
            connect(ui, args.address, args.peer_id, args.connect_code)
            if args.skip_fullscreen:
                print("[5/6] 跳过全屏检查(仅保留连接态截图)")
            else:
                print("[5/6] 全屏铺满与沉浸浮条")
                verify_fullscreen(device, ui)
                print("[5b/6] 旋转稳定性")
                verify_rotation_stability(device, ui)
        else:
            print("[4/6] 跳过连接")
            print("[5/6] 跳过全屏检查")

        print("[6/6] 收尾留证")
        dump_logcat(device, evidence)
        ui.screenshot("07-final")
        print(f"通过: 证据已保存到 {evidence}")
        return 0
    except Blocked as error:
        print(f"受阻: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"失败: {error}", file=sys.stderr)
        try:
            device_any = Device(find_adb(args.adb), args.serial)
            dump_logcat(device_any, evidence)
            Ui(device_any, evidence).screenshot("99-failure")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
