# UI-V2 对齐复核 · 验收检查单

## 一、漂移修正

- [x] 页头眉标按 `PAGE_EYEBROWS` 逐页映射，首页/设备保留「 · 已登录/未登录」后缀；`PAGE_NAMES` 已移除无残留。
- [x] 侧栏导航文案与原型一致：首页 / 设备 / 消息 / 账号与系统。
- [x] 侧栏分组为「工作台 / 空间 / 账号与系统」，顺序与原型一致。
- [x] 侧栏底部显示账号名 + 状态点 + 账号编号；编号超 18 字符省略并把完整值放入工具提示。
- [x] 消息页标题为「消息中心」。
- [x] 屏幕墙使用「N 路」「x / y 路在线」「布局：a × b」，刷新在工具条、关闭在页头，空态为「屏幕墙当前没有画面」「创建屏幕墙」。
- [x] 界面文案无新增英文；命令层签名与契约正文未改（结构检查 50 个命令全覆盖）。

## 二、证据

- [x] 原型截图 `evidence/ui/align-20261111-uiv2/proto/dark|light/` 各 33 张。
- [x] 实机截图 `evidence/ui/align-20261111-uiv2/actual/` 34 张（深浅 11 页 + 更新面板 + 登录前状态）。
- [x] 并排对照图 `evidence/ui/align-20261111-uiv2/compare/` 28 张，左原型右实机，尺寸统一 1308×820。
- [x] 残余差异清单落盘，已修正 5 项 + 残余 12 条，逐条写文件与不采纳理由；含条数结论。

## 三、打包线

- [x] `docs/发布物清单-v1.0.0.md` §3.5 列明界面资源路径：`src/rdc/ui/assets/icons/`（55 个 svg + `LICENSE-lucide.txt`）、`deploy/relay/ca.crt` 两处落点、`assets/app-icon/rdc.ico`、Linux hicolor 9 个尺寸与 `.desktop`、便携包生成文件。
- [x] 清单自带 PowerShell 核对命令（svg 数、许可、两处 ca.crt）与 `dpkg-deb -c` 核对要求。
- [x] 源路径实测存在：55 个 svg + 许可、`deploy/relay/ca.crt`、`assets/app-icon/rdc.ico`、hicolor 9 个尺寸、`rdc-desktop.desktop`。

## 四、门禁

- [x] `py -3 scripts\gate.py` 6/6 全绿，用时 72.1s，证据 `D:\RDC-Build\evidence\gate\20261111-002719`。
- [x] pytest 606 项通过 606、失败 0；ruff / 格式 / 禁止写法 / 结构 / mypy 均 0 违规。

## 五、口径

- [x] 本轮只做冻结后对齐，未扩大范围；页面骨架级差异（设置/审批/剪贴板/传输/会话/更新/账号）明确列入残余。
- [x] 安卓底部导航 r1–r3 图标结论已写入 `docs/design/ui-v2/变更记录.md`：不作为实现基准，以现有 app 视觉为准，原型仅保留参考。
