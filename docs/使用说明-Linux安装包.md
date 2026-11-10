# Linux 安装包与便携包使用说明

Ubuntu 22.04 / x86_64。两套包都由项目源码用 PyInstaller 冻结成独立可执行文件，**不依赖系统 Python，也不需要项目源码**。

## 一句话上手

- 安装版：`sudo dpkg -i rdc-control_1.0.0_amd64.deb`，在应用菜单点「RDC 控制端」；卸载 `sudo dpkg -P rdc-control`。
- 便携版：解压后执行包内 `./启动-RDC控制端.sh`（或双击），配置与日志都在包目录里，删目录即卸载。

## 1. 安装包（.deb）

### 安装

```bash
sudo dpkg -i rdc-control_1.0.0_amd64.deb
```

安装位置与系统注册项：

| 项目 | 路径 |
| --- | --- |
| 程序本体 | `/opt/rdc-control/`（root 所有） |
| 命令入口 | `/usr/bin/rdc-control` |
| 应用菜单项 | `/usr/share/applications/rdc-control.desktop`（`Terminal=false`） |
| 图标 | `/usr/share/icons/hicolor/{16,22,24,32,48,64,128,256}x*/apps/rdc-control.png` |

`Depends` 已声明 Qt/X11 运行库（`libxcb-*`、`libxkbcommon-x11-0`、`libgl1`、`libfontconfig1` 等），在 Ubuntu 22.04 桌面上 `dpkg -i` 即可满足，无需再装 Python。

### 启动

在「显示应用程序」里搜「RDC 控制端」后点击，或执行 `rdc-control`。

菜单项声明了 `Terminal=false`，从菜单或双击启动都不会弹终端窗口（实测截图见证据目录 `deb-menu-launched.png`）。

### 配置与日志

- 配置：`~/.config/rdc-control/`。**本版本不持久化任何设置**（地址每次手填，连接码只在内存中），因此安装后不会生成该目录。
- 日志：`~/.local/state/rdc-control/rdc-control.log`，只记录启动与连接状态事件，**不记录连接码与设备 ID**。

### 卸载与残留

```bash
sudo dpkg -r rdc-control    # 移除程序，保留 dpkg 的配置状态记录
sudo dpkg -P rdc-control    # 彻底清除（推荐）
rm -rf ~/.local/state/rdc-control ~/.config/rdc-control   # 用户日志（可选）
```

`dpkg -r` 之后：`/opt/rdc-control`、`/usr/bin/rdc-control`、菜单项、图标全部移除；`dpkg -l` 显示 `rc` 状态，`/var/lib/dpkg/info/rdc-control.*` 记录仍在。
`dpkg -P` 之后：dpkg 查询无结果，`/var/lib/dpkg/info` 无记录。

用户目录属于用户数据，dpkg 不会删除，卸载后仍存在的是：

- `~/.local/state/rdc-control/rdc-control.log`（约 8 KB）

执行上面第三行 `rm -rf` 后即完全无残留。完整检查输出见 `evidence/linux-packages-20261110/uninstall-check.txt`。

## 2. 便携包（tar.gz）

```bash
tar -xzf rdc-control-1.0.0-linux-x86_64.tar.gz
cd rdc-control-portable
./启动-RDC控制端.sh          # 或双击 rdc-control.desktop
```

- 解压即用，可整体拷到另一台同架构（x86_64）Linux 机器；二进制不链接系统 Python，运行时也不需要项目源码或虚拟环境。
- 配置与日志跟随包目录：`config/`、`logs/rdc-control.log`；删除整个目录即彻底清理。
- `rdc-control.desktop` 已声明 `Terminal=false`，可复制到桌面/`~/.local/share/applications` 使用（复制后需允许启动）。
- 便携版依赖系统 Qt/X11 运行库（Ubuntu 22.04 桌面版默认具备）。在无桌面的最小化系统上需先装 `libxcb-cursor0 libxkbcommon-x11-0 libgl1` 等依赖。

## 3. 实测记录

环境：Ubuntu 22.04 guest（X11），产物由 guest 内 PyInstaller 6.22.3 构建。

| 项目 | 结果 |
| --- | --- |
| .deb 安装 | `Status: install ok installed`，依赖全部满足 |
| 菜单启动 | `gtk-launch rdc-control` 起窗成功，终端窗口数 0 |
| 卸载残留 | `dpkg -P` 后无系统文件、无 dpkg 记录；仅用户日志目录（见上） |
| 便携包 | 在 `/home/wuge/portable-test/` 解压，`env -i` 清空环境启动成功 |
| 分辨率 | 1718x918、1920x1080、1367x918 均收到画面（见下表） |

分辨率实测（每档均自动连接、取首帧、注入按键后截图）：

| 屏幕分辨率 | 实际编码尺寸 | 结果 |
| --- | --- | --- |
| 1718x918 | 1718x918 | 通过，收到 39 帧 |
| 1920x1080 | 1920x1080 | 通过，收到 37 帧 |
| 1367x918 | 1366x918 | 通过，收到 36 帧（H.264 4:2:0 要求偶数宽，被控端向下取偶） |

截图：`evidence/linux-packages-20261110/frame-*/selftest_after_typing.png`。

## 4. 构建与产物位置

- 产物（不在仓库内）：宿主 `D:\RDC-Build\linux\artifacts\`，guest `/home/wuge/rdc/dist-linux/`。
- 构建方式：`pyinstaller --onedir`，入口 `scripts/rdc_control_app.py`，搜索路径含 `scripts`、`src` 与项目根（`poc`）。
- 图标由构建根脚本绘制生成，未引入外部素材。

## 5. 已知限制

1. 仅支持 X11 桌面（采集用 mss，注入用 xdotool 的原生实现）；Wayland 不支持。
2. 便携包仍依赖系统 Qt/X11 运行库；不捆绑这些系统库是为了避免与目标机驱动冲突。
3. H.264 4:2:0 编码要求宽高为偶数，被控端会把奇数分辨率向下取偶（1367 → 1366）。
4. 本版本不持久化任何设置，因此没有「记住上次地址」之类的便利功能。
