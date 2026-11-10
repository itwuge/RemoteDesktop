# Linux 安装包与便携包使用说明

对应 `rdc-desktop` 0.2.1，两个架构各一套：

| 平台标识 | 产物 | 构建方式 | 验证状态 |
| --- | --- | --- | --- |
| `linux-x64` | `rdc-desktop-0.2.1-linux-x64.deb`、`rdc-desktop-0.2.1-linux-x64-portable.tar.gz` | Ubuntu 22.04 虚机（x86_64）本地 PyInstaller 构建 | 已装、已跑、已卸，零残留 |
| `linux-arm64` | `rdc-desktop-0.2.1-linux-arm64.deb`、`rdc-desktop-0.2.1-linux-arm64-portable.tar.gz` | GitHub Actions `ubuntu-24.04-arm` runner（`linux-arm64.yml`） | **未在 arm64 真机启动验证**，仅校验 deb `Architecture: arm64` 与构建成功 |

两套包都由项目源码冻结成独立可执行文件，不依赖系统 Python，也不需要项目源码。

## 1. 安装包（.deb）

```bash
sudo dpkg -i rdc-desktop-0.2.1-linux-x64.deb    # 或 ...-linux-arm64.deb
```

安装位置与系统注册项：

| 项目 | 路径 |
| --- | --- |
| 程序本体 | `/opt/rdc-desktop/` |
| 命令入口 | `/usr/bin/rdc-desktop` |
| 应用菜单项 | `/usr/share/applications/rdc-desktop.desktop` |
| 图标 | `/usr/share/icons/hicolor/{16,24,32,48,64,128,256}x*/apps/rdc-desktop.png` |

`Depends` 已声明 Qt/X11 运行库（`libxcb-cursor0`、`libxkbcommon-x11-0`、`libgl1`、`libfontconfig1` 等），Ubuntu 22.04 桌面版 `dpkg -i` 后即可满足。

启动：应用菜单搜「RDC 远程桌面」，或命令行执行 `rdc-desktop`。

### 用户数据

| 项目 | 路径 |
| --- | --- |
| 账号库 | `~/.local/share/rdc/accounts.db`（SQLite，含 `-wal`/`-shm`） |
| 日志 | `~/.local/share/rdc/logs/rdc.log` |
| 崩溃日志 | `~/.local/share/rdc/logs/fault.log` |

### 卸载与残留

```bash
sudo dpkg -P rdc-desktop
```

`dpkg -P` 之后：`/opt/rdc-desktop`、`/usr/bin/rdc-desktop`、菜单项、图标、`/var/lib/dpkg/info/rdc-desktop.*` 全部移除，`dpkg -l` 无记录。用户数据 `~/.local/share/rdc` 保留（重装不丢账号），要彻底清除需手动 `rm -rf ~/.local/share/rdc`。

## 2. 便携包（tar.gz）

```bash
tar -xzf rdc-desktop-0.2.1-linux-x64-portable.tar.gz
cd rdc-desktop-0.2.1-linux-x64
./rdc-desktop            # 或 ./rdc-desktop.sh
```

- 顶层目录为产物名，解压不散落；包内 340 个文件。
- `portable.marker` 存在即进入便携模式：配置、账号库、日志全部写到包内 `data/`（`config.json`、`accounts.db`、`logs/`），不碰 `~/.local/share/rdc`；删除整个目录即卸载。
- 依赖系统 Qt/X11 运行库；无桌面环境的最小化系统需先装 `libxcb-cursor0 libxkbcommon-x11-0 libgl1` 等。
- 仅支持 X11；Wayland 不支持。

## 3. 实测记录（2026-11-10，干净 HEAD 重打）

环境：Ubuntu 22.04 虚机，Python 3.12.16，PyInstaller 6.10.0，X11。

| 项目 | 结果 |
| --- | --- |
| deb 安装 | `dpkg -i` 退出码 0，`dpkg -s` 状态 `install ok installed` |
| 启动 | 存活 15 秒，窗口标题 `RDC 远程桌面 · V1`；日志 `app.start version=0.2.1`、`python=3.12.16` |
| 账号库 | 首次启动生成 `~/.local/share/rdc/accounts.db`（含 wal/shm） |
| 卸载 | `dpkg -P` 退出码 0；`/opt/rdc-desktop`、`/usr/bin/rdc-desktop`、desktop 文件全部 REMOVED，图标与 dpkg info 无残留，仅保留 `~/.local/share/rdc` |
| 便携包 | 解压 340 文件，`portable.marker` 存在；启动存活 15 秒，`data/` 内生成 `accounts.db`、`config.json`、`logs/rdc.log` |
| arm64 | CI 构建成功（run 42788481738），artifact `linux-arm64`；deb `Architecture: arm64`；**未在 arm64 真机验证** |

## 4. 构建

```bash
python scripts/build_linux.py --build-root /path/to/build-root
```

- deb 的 `Architecture` 由构建机 `platform.machine()` 决定（`x86_64` → `amd64`，`aarch64` → `arm64`），产物名带 `linux-x64` / `linux-arm64`。
- arm64 不在 x86 机器上交叉编译，统一走 `.github/workflows/linux-arm64.yml`（`ubuntu-24.04-arm` runner）；该工作流只在 tag 或手动触发时运行，不影响 Windows 与 x64 构建。

## 5. 已知限制

1. 仅支持 X11 桌面；Wayland 不支持。
2. 便携包仍依赖系统 Qt/X11 运行库，不捆绑系统库以避免驱动冲突。
3. arm64 产物由 CI 构建，未在真机启动验证，状态以本文表格为准。
