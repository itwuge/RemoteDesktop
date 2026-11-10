"""开机自启回归: Linux XDG 文件、Windows Run 键、命令层回读与首次默认策略."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rdc.commands import settings as settings_commands
from rdc.core import autostart, runtime
from rdc.core.result import CommandResult
from rdc.ui.app import should_start_in_tray

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_TEST_RUN_KEY = r"Software\RDC 测试\Autostart"


def test_linux_desktop_path_follows_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """XDG_CONFIG_HOME 为绝对路径时优先, 相对路径按规范忽略."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert autostart.linux_desktop_path() == tmp_path / "xdg" / "autostart" / "rdc-desktop.desktop"

    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert autostart.linux_config_home(home=tmp_path) == tmp_path / ".config"


def test_linux_entry_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """写入后可读出同一命令行, 删除后回到未启用."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = autostart.linux_desktop_path(home=tmp_path)
    assert not path.exists()
    assert autostart.read_linux_entry(path) is None

    command = autostart.launch_command()
    autostart.write_linux_entry(path, command)
    try:
        assert path.exists()
        assert autostart.read_linux_entry(path) == command
        text = path.read_text(encoding="utf-8")
        assert "X-GNOME-Autostart-enabled=true" in text
        assert "Terminal=false" in text
        assert autostart.AUTOSTART_FLAG in text
    finally:
        autostart.remove_linux_entry(path)
    assert not path.exists()
    assert autostart.read_linux_entry(path) is None


def test_linux_entry_disabled_flag_is_not_enabled(tmp_path: Path) -> None:
    """desktop 文件存在但被显式禁用时, 真实状态必须是关闭."""
    path = tmp_path / autostart.LINUX_DESKTOP_NAME
    path.write_text(
        '[Desktop Entry]\nType=Application\nExec="/opt/rdc/rdc-desktop" --autostart\nX-GNOME-Autostart-enabled=false\n',
        encoding="utf-8",
    )
    assert autostart.read_linux_entry(path) is None


def test_linux_state_and_toggle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 自启开关走文件存在性, 开与关都立即回读."""
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_portable", lambda: False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    state = autostart.current_state(platform="linux", home=tmp_path)
    assert state.supported
    assert not state.enabled
    assert str(tmp_path) in state.target

    opened = autostart.set_enabled(True, platform="linux", home=tmp_path)
    assert opened.enabled
    path = autostart.linux_desktop_path(home=tmp_path)
    assert path.exists()

    closed = autostart.set_enabled(False, platform="linux", home=tmp_path)
    assert not closed.enabled
    assert not path.exists()


def test_development_state_is_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """开发态不注册系统自启, 状态明确说明原因."""
    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    state = autostart.current_state(platform="linux")
    assert not state.supported
    assert not state.enabled
    assert "开发态" in state.detail


@pytest.mark.skipif(sys.platform != "win32", reason="HKCU Run 键只在 Windows 验证")
def test_windows_run_value_roundtrip() -> None:
    """HKCU Run 键写读删全过程; 结束后清理测试键."""
    import winreg

    value_name = "rdc-autostart-test"
    command = '"C:\\Program Files\\RDC\\rdc-desktop.exe" --autostart'
    autostart.delete_windows_value(value_name, WINDOWS_TEST_RUN_KEY)
    assert autostart.read_windows_value(value_name, WINDOWS_TEST_RUN_KEY) is None
    autostart.write_windows_value(command, value_name, WINDOWS_TEST_RUN_KEY)
    try:
        assert autostart.read_windows_value(value_name, WINDOWS_TEST_RUN_KEY) == command
    finally:
        autostart.delete_windows_value(value_name, WINDOWS_TEST_RUN_KEY)
    assert autostart.read_windows_value(value_name, WINDOWS_TEST_RUN_KEY) is None
    for key in (WINDOWS_TEST_RUN_KEY, r"Software\RDC 测试"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            continue


@pytest.mark.skipif(sys.platform != "win32", reason="HKCU Run 键只在 Windows 验证")
def test_windows_state_and_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 自启开关落到 HKCU Run 值, 关闭后值被删除."""
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_portable", lambda: False)
    value_name = "rdc-autostart-state"
    autostart.delete_windows_value(value_name, WINDOWS_TEST_RUN_KEY)
    try:
        state = autostart.current_state(platform="windows", run_key=WINDOWS_TEST_RUN_KEY, value_name=value_name)
        assert state.supported
        assert not state.enabled

        opened = autostart.set_enabled(True, platform="windows", run_key=WINDOWS_TEST_RUN_KEY, value_name=value_name)
        assert opened.enabled
        assert opened.command == autostart.launch_command()
        assert autostart.read_windows_value(value_name, WINDOWS_TEST_RUN_KEY) == autostart.launch_command()

        closed = autostart.set_enabled(False, platform="windows", run_key=WINDOWS_TEST_RUN_KEY, value_name=value_name)
        assert not closed.enabled
        assert autostart.read_windows_value(value_name, WINDOWS_TEST_RUN_KEY) is None
    finally:
        autostart.delete_windows_value(value_name, WINDOWS_TEST_RUN_KEY)


def test_get_autostart_status_reads_real_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """状态命令直接呈现系统读取结果与目标位置."""
    state = autostart.AutostartState(
        supported=True,
        enabled=True,
        target="HKCU\\...\\Run\\RDC 远程桌面",
        command='"C:\\RDC\\rdc-desktop.exe" --autostart',
        detail="已登记",
        portable=False,
    )
    monkeypatch.setattr(autostart, "current_state", lambda: state)
    result = settings_commands.get_autostart_status()
    assert result.ok
    assert result.data is not None
    assert result.data["enabled"] is True
    assert result.data["target"] == state.target


def test_set_autostart_mismatch_reports_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    """写入后回读不一致必须报失败并给出实际状态."""
    actual = autostart.AutostartState(
        supported=True,
        enabled=False,
        target="HKCU\\...\\Run\\RDC 远程桌面",
        command="",
        detail="未登记在 HKCU 启动项",
        portable=False,
    )
    monkeypatch.setattr(autostart, "set_enabled", lambda enabled: actual)
    result = settings_commands.set_autostart_enabled(True)
    assert not result.ok
    assert "回读为关闭" in result.message
    assert result.data is not None
    assert result.data["requested"] is True


def test_set_autostart_failure_reports_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """写入抛错时回读实际状态并把系统原因带回界面."""

    def refuse(enabled: bool) -> autostart.AutostartState:
        raise PermissionError(f"拒绝访问（请求值 {enabled}）")

    actual = autostart.AutostartState(
        supported=True,
        enabled=False,
        target="HKCU\\...\\Run\\RDC 远程桌面",
        command="",
        detail="未登记在 HKCU 启动项",
        portable=False,
    )
    monkeypatch.setattr(autostart, "set_enabled", refuse)
    monkeypatch.setattr(autostart, "current_state", lambda: actual)
    result = settings_commands.set_autostart_enabled(True)
    assert not result.ok
    assert "拒绝访问" in result.message
    assert "未登记在 HKCU 启动项" in result.message


@pytest.mark.parametrize("value", ["true", 1, None])
def test_set_autostart_rejects_non_bool(value: Any) -> None:
    """开关参数只接受布尔值, 防止界面误传字符串."""
    result: CommandResult = settings_commands.set_autostart_enabled(value)
    assert not result.ok
    assert "布尔值" in result.message


def test_ensure_default_installed_enables_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """安装版首次启动写入自启并记录标记, 第二次启动不重复写."""
    config: dict[str, Any] = {}
    calls: list[bool] = []
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_portable", lambda: False)
    monkeypatch.setattr(runtime, "load_config", lambda: dict(config))
    monkeypatch.setattr(runtime, "save_config", lambda payload: config.update(payload))
    monkeypatch.setattr(
        autostart,
        "current_state",
        lambda: autostart.AutostartState(True, False, "target", "", "未登记", False),
    )

    def enable(enabled: bool) -> autostart.AutostartState:
        calls.append(enabled)
        return autostart.AutostartState(True, enabled, "target", "cmd", "已登记", False)

    monkeypatch.setattr(autostart, "set_enabled", enable)
    first = settings_commands.ensure_autostart_default()
    assert first.ok
    assert calls == [True]
    assert config["autostart_initialized"] is True
    second = settings_commands.ensure_autostart_default()
    assert second.ok
    assert calls == [True]


def test_ensure_default_portable_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """便携版首次启动不写自启, 只记录已初始化, 允许后续手动开启."""
    config: dict[str, Any] = {}
    calls: list[bool] = []
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_portable", lambda: True)
    monkeypatch.setattr(runtime, "load_config", lambda: dict(config))
    monkeypatch.setattr(runtime, "save_config", lambda payload: config.update(payload))
    monkeypatch.setattr(autostart, "set_enabled", lambda enabled: calls.append(enabled))
    result = settings_commands.ensure_autostart_default()
    assert result.ok
    assert "便携版" in result.message
    assert calls == []
    assert config["autostart_initialized"] is True


def test_ensure_default_development_does_not_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """开发态既不写自启也不落标记, 避免影响后续安装版首启."""
    config: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime, "load_config", lambda: dict(config))
    monkeypatch.setattr(runtime, "save_config", lambda payload: config.update(payload))
    result = settings_commands.ensure_autostart_default()
    assert result.ok
    assert config == {}


def test_nsis_uninstall_removes_autostart_value() -> None:
    """卸载脚本必须删除 Run 键值, 且值名与程序写入一致."""
    script = (PROJECT_ROOT / "packaging" / "windows" / "rdc-installer.nsi").read_text(encoding="utf-8-sig")
    assert '!define APP_RUN_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Run"' in script
    assert '!define APP_RUN_VALUE "RDC 远程桌面"' in script
    assert 'DeleteRegValue HKCU "${APP_RUN_KEY}" "${APP_RUN_VALUE}"' in script


def test_should_start_in_tray_by_flag() -> None:
    """只有带 --autostart 的启动才静默进托盘."""
    assert should_start_in_tray(["rdc-desktop.exe", "--autostart"])
    assert not should_start_in_tray(["rdc-desktop.exe"])
