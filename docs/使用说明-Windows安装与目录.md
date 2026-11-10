# Windows 安装与目录说明

本文对应 0.2.1 起的 Windows 两套包，所有路径均为默认值，用户可在安装向导中改选。

## 1. 两个包的默认位置

| 形态 | 默认位置 | 产生方式 |
| --- | --- | --- |
| 安装包 `rdc-desktop-0.2.1-windows-x64-setup.exe` | `%LOCALAPPDATA%\Programs\RDC 远程桌面`（即 `C:\Users\<用户名>\AppData\Local\Programs\RDC 远程桌面`） | 双击安装，向导第二页可改 |
| 便携包 `rdc-desktop-0.2.1-windows-x64-portable.zip` | 解压后只有一个顶层文件夹 `RDC 远程桌面` | 右键「全部解压缩」，不把文件散到当前目录 |

两者目录同名，直接解压到同一父目录会因同名触发合并/覆盖，把便携文件混进安装目录。想要两者并列，把便携目录改名即可（安装版位置不动）：

```
%LOCALAPPDATA%\Programs\
├─ RDC 远程桌面\              ← 安装版
└─ RDC 远程桌面-portable\     ← 便携版（解压后改名）
```

或者在解压对话框里直接把目标目录改成另一个父目录，例如 `D:\Tools\RDC 远程桌面`。

默认路径来自 [rdc-installer.nsi](file:///c:/Users/Administrator/Desktop/RemoteDesktop/packaging/windows/rdc-installer.nsi) 的 `InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"`。

## 2. 安装后各文件位置

安装目录 `%LOCALAPPDATA%\Programs\RDC 远程桌面`：

| 文件/目录 | 说明 |
| --- | --- |
| `rdc-desktop.exe` | 主程序，图标已嵌入可执行文件 |
| `rdc-desktop.ico` | 安装包附带的图标文件，用于注册表 `DisplayIcon` 与快捷方式 |
| `Uninstall.exe` | 卸载器 |
| `_internal\` | PyInstaller 运行时依赖（Qt 库、Python 运行时等） |

快捷方式（`SetShellVarContext current`，只影响当前用户）：

- 桌面：`%USERPROFILE%\Desktop\RDC 远程桌面.lnk`
- 开始菜单：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\RDC 远程桌面\RDC 远程桌面.lnk` 与 `卸载 RDC 远程桌面.lnk`

安装包**不带许可证页**（没有 `MUI_PAGE_LICENSE`），也没有额外许可证文件落盘。

## 3. 便携包解压后的结构

```
RDC 远程桌面\
├─ rdc-desktop.exe
├─ rdc-desktop.ico          （安装版才有；便携包用 exe 内嵌图标）
├─ portable.marker          （存在即判定为便携模式）
├─ 使用说明.txt
├─ _internal\
└─ data\                    （首次运行后生成）
   ├─ config.json
   ├─ accounts.db           （SQLite 账号库）
   ├─ accounts.db-wal / -shm
   └─ logs\
      ├─ rdc.log
      └─ fault.log
```

运行时生成的文件全部留在 `data\` 内，不写安装版的用户目录（判定逻辑在 [runtime.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/core/runtime.py)：可执行文件同目录存在 `portable.marker` 即为便携模式）。

## 4. 用户数据位置

| 形态 | 账号库 | 日志 | 崩溃日志 |
| --- | --- | --- | --- |
| 安装版 | `%LOCALAPPDATA%\RDC\accounts.db` | `%LOCALAPPDATA%\RDC\logs\rdc.log` | `%LOCALAPPDATA%\RDC\logs\fault.log` |
| 便携版 | `<包目录>\data\accounts.db` | `<包目录>\data\logs\rdc.log` | `<包目录>\data\logs\fault.log` |

两套形态**不共用**账号库：便携版完全隔离，不会读到安装版的账号，反之亦然。

## 5. 注册表写入

只写当前用户 HKCU（`SetRegView 64`），键为
`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\RDC`：

| 值名 | 内容 |
| --- | --- |
| `DisplayName` | `RDC 远程桌面` |
| `DisplayVersion` | 安装包版本（0.2.1） |
| `Publisher` | `RDC` |
| `InstallLocation` | `$INSTDIR` |
| `DisplayIcon` | `$INSTDIR\rdc-desktop.ico` |
| `UninstallString` | `$INSTDIR\Uninstall.exe` |
| `NoModify` / `NoRepair` | 均为 1 |

不写 App Paths，不注册系统服务。安装包本身不写开机启动项；程序首次启动时按「安装版默认开启」写入
`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` 的 `RDC 远程桌面` 值，卸载时删除。

## 6. 卸载后的残留

`Uninstall.exe` 执行后：

- 删除安装目录整个 `$INSTDIR`（含 `_internal`、exe、图标、卸载器）
- 删除 HKCU 卸载键 `...\Uninstall\RDC`
- 删除 HKCU 开机自启值 `...\CurrentVersion\Run\RDC 远程桌面`（若存在）
- 删除桌面与开始菜单快捷方式（含开始菜单目录本身）
- **保留** `%LOCALAPPDATA%\RDC`（账号库、日志、崩溃日志）

因此卸载后重装不会丢账号；要彻底清除需手动删除 `%LOCALAPPDATA%\RDC`。便携版卸载即删除包目录，账号库随目录一起删除。

## 7. 是否需要管理员

不需要。安装包 `RequestExecutionLevel user`，只写 HKCU 与用户目录，普通用户双击即可安装，不会弹 UAC。

## 8. 从 0.2.0 升级的注意点

0.2.0 及更早版本是 per-machine 安装（`C:\Program Files\RDC 远程桌面` + HKLM 卸载键）。0.2.1 改为 per-user：

- 安装包**不会自动迁移**旧目录文件，会装到 `%LOCALAPPDATA%\Programs\RDC 远程桌面`；
- 检测到 HKLM 旧键时，向导开头会提示旧版本安装目录，建议先在「应用和功能」中卸载旧版本；
- 两版可短暂并存，账号库都在 `%LOCALAPPDATA%\RDC`，升级后账号不丢。

## 9. 实测记录（2026-11-10）

- 便携包解压：顶层只有 `RDC 远程桌面`；子项 `_internal, data, portable.marker, rdc-desktop.exe, 使用说明.txt`；运行 15 秒后 `data\` 生成 `accounts.db`、`config.json`、`logs\rdc.log`、`logs\fault.log`，且 `%LOCALAPPDATA%\RDC\accounts.db` 的修改时间与大小均未变化。
- 安装包静默安装：退出码 0，落在 `%LOCALAPPDATA%\Programs\RDC 远程桌面`，HKCU 键 `DisplayVersion=0.2.1`、`InstallLocation` 正确，`rdc-desktop.exe` 文件版本 0.2.1，启动后日志 `app.start version=0.2.1`。
- 卸载：退出码 0，安装目录、HKCU 键、桌面快捷方式、开始菜单目录全部消失，`%LOCALAPPDATA%\RDC` 保留。
