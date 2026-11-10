# UI-V2 冻结后桌面端确认性复核 · 实施规格（收窄版）

## 1. 目标与边界

- 基准：`docs/design/ui-v2/桌面原型.html` + `design-tokens.json` + `界面说明.md`。
- 只修正「换成 ui-v2 后产生的确定漂移」：页头眉标、导航文案、侧栏账号块、页标题、屏幕墙文案/徽标。
- 不重排设置、审批、剪贴板、会话、文件传输的页面骨架；这些列为残余差异并写明理由，不删除任何现有功能与命令调用。
- 不改命令层签名、契约正文；门禁 6/6。

## 2. 确定的漂移与改动

| 位置 | 现状 | ui-v2 | 改动 |
| --- | --- | --- | --- |
| 页头眉标 | 全部「页面 · 已登录」 | 页面专属眉标 | 按页映射；首页/设备保留登录态后缀 |
| 导航文案 | 首页 / 连接、设备列表、消息中心、分组「账号」 | 首页、设备、消息、账号与系统 | 改文案与分组 |
| 侧栏底部 | 「账号 · 已登录」 | 账号名 + 状态 + 账号编号 | 用 `get_account_state` 的 `account_name/account_id/logged_in` 呈现 |
| 消息页标题 | 消息 | 消息中心 | 改标题 |
| 屏幕墙 | 「N 格」、无在线数/布局说明 | 「N 路」「x / y 路在线」「布局：a × b」 | 改文案并补徽标与布局说明 |

实施文件：

1. `src/rdc/ui/main_window.py`
   - 新增 `PAGE_EYEBROWS`（trust「信任设备 · 免确认直连」、messages「消息 · 审计时间线」、account「账号与安全」、settings「设置 · 本机与服务端」、wall「屏幕墙 · 多设备监看」、session「会话窗口 · 远程控制」、transfer「文件传输 · 会话内通道」、clipboard「剪贴板 · 双向同步」、approvals「申请与审批 · 临时授权」；home/devices 为「首页」「设备」+ 登录态）。
   - `NAV_GROUPS` 文案改「首页」「设备」「消息」「账号与系统」；`PAGE_NAMES["messages"] = "消息中心"`。
   - `switch_page` 按 `PAGE_EYEBROWS` 设置眉标，home/devices 追加「 · 已登录/未登录」。
   - `_refresh`（现状 304 行附近）把侧栏底部改为账号名 + 状态点 + 账号编号；主题按钮位置不变。
2. `src/rdc/ui/pages/wall.py`
   - 布局按钮「N 格」改「N 路」；工具栏补「x / y 路在线」徽标与「布局：a × b」；空态文案改「屏幕墙当前没有画面」「创建屏幕墙」。
   - 不改网格、按钮回调与命令调用。
3. `tests/test_ui_smoke.py`：仅当断言命中改动词案时同步更新。

## 3. 残余差异（不改，逐条写理由）

1. 设置页仍为单列分组，未改 ui-v2 的两列四卡：现有分组含「账号与免确认」等契约字段，重排会牵动字段归属与冒烟断言；列为待下一版 UI 改版处理。
2. 审批页仍用下拉框分组，未改胶囊：交互等价，改动会牵动筛选逻辑与测试。
3. 剪贴板仍为单列，未改两列卡片：控件与命令完整，列布局不影响取值与操作。
4. 文件传输的任务详情为行内文本，未包成独立卡片：信息项一致，仅容器不同。
5. 会话窗口信息区在画面下方全宽，未改右侧信息列：右侧列会压缩远端画面，且信息区含可编辑参数与复制按钮。
6. 更新仍为设置页内面板，未新增导航第 12 页「更新面板」：命令入口与契约不变。
7. 原型中的命令标签、状态切换器、演示数值、占位品牌名、40px 自绘标题栏不进入实机。
8. 传输/剪贴板在未建立会话时显示「需要先建立会话」，原型未建模该状态。

## 4. 打包资源路径清单

| 来源 | 冻结包去向 | 用途 | Windows | Linux |
| --- | --- | --- | --- | --- |
| `src/rdc/ui/assets/`（`icons/*.svg` 55 个、`icons/LICENSE-lucide.txt`） | `rdc/ui/assets/` | 界面图标与许可 | `--add-data` | `--add-data` |
| `deploy/relay/ca.crt` | `rdc/ui/assets/ca.crt`、`rdc/assets/ca.crt` | 中继 TLS 校验 | 两份 `--add-data` | 两份 `--add-data` |
| `assets/app-icon/rdc.ico` | exe 与安装包图标 | 图标 | `--icon`、NSIS `ICON_FILE` | 不适用 |
| `packaging/linux/icons/hicolor/*/apps/rdc-desktop.png` | `/usr/share/icons/hicolor/<尺寸>/apps/rdc-desktop.png` | 桌面图标 | 不适用 | deb |
| `packaging/linux/rdc-desktop.desktop` | `/usr/share/applications/` | 启动项 | 不适用 | deb |

核对：冻结目录必须存在 `rdc/ui/assets/icons/*.svg`（55 个）、`rdc/ui/assets/ca.crt`、`rdc/assets/ca.crt`；Linux 另查全部 hicolor 尺寸与 `.desktop`。清单追加到 `docs/发布物清单-v1.0.0.md`。

## 5. 证据与验收

- 原型截图：`docs/design/ui-v2/桌面原型.html`，12 页 × 深/浅，窗口元素 1308×820。
- 实机截图：`MainWindow` 1308×820，深/浅 11 页 + 更新面板 + 登录前状态。
- 输出：`evidence/ui/align-<时间戳>-uiv2/{proto,actual,compare}/`，并排图左原型右实机。
- 残余差异清单：`evidence/ui/align-<时间戳>-uiv2/残余差异清单.md`，逐条写页面/差异/是否已改/文件/理由，0 条也写明。
- 门禁：`py -3 scripts/gate.py` 6/6，证据落 `D:\RDC-Build\evidence\gate\<时间戳>`。

## 6. 风险

- 眉标/导航文案改动可能命中 `tests/test_ui_smoke.py` 断言，需同步并复跑。
- 屏幕墙新增徽标只读 `list_devices` 与当前布局，不新增命令。
