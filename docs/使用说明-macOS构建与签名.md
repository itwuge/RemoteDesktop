# macOS 构建、CI 与首次打开说明

## 1. 当前状态（v0.2.1）

- v0.2.1 的 Release **没有 macOS 产物**：发布时没有 macOS 构建机，清单 `platforms.macos-arm64` 标注 `unavailable`。
- 已加入 GitHub Actions 流水线 [macos-release.yml](file:///c:/Users/Administrator/Desktop/RemoteDesktop/.github/workflows/macos-release.yml)，用 GitHub 提供的 macOS runner 出包，不需要自购 Mac。
- **流水线尚未在 GitHub 上实跑过，产物状态一律记为「未验证」**；首个 `v*` 标签或手动触发跑通后才会产出可下载的 dmg/zip，从下一版开始在清单里补 darwin 条目。
- 全部 macOS 产物**未签名、未公证**。仓库、Release 说明与本文档都不会声称「已签名」或「已公证」。

## 2. 架构与产物

两个架构**分别出包，不做 universal2 合并**：

| 架构 | Runner | 安装包 | 便携包 |
|---|---|---|---|
| Apple Silicon | `macos-14` | `rdc-desktop-<版本>-macos-arm64.dmg` | `rdc-desktop-<版本>-macos-arm64-portable.zip` |
| Intel | `macos-15-intel` | `rdc-desktop-<版本>-macos-x64.dmg` | `rdc-desktop-<版本>-macos-x64-portable.zip` |

`macos-13`（Intel）已被 GitHub 退役，Intel 包改用官方接棒的 `macos-15-intel`，产物仍为 x86_64。文件名带架构后缀，用户按自己的芯片下载，避免下错。

## 3. CI 触发方式

- **推 `v*` 标签**：自动构建两个架构，构建完把 dmg/zip/sha256 上传到同 tag 的 Release，并合并 `update-manifest.json`。
- **手动 `workflow_dispatch`**：
  - `tag`：要构建的标签，留空用当前 ref；
  - `publish`：是否上传到 Release，默认关（只跑构建、产物留作 Actions Artifacts）；
  - `sign`：是否启用签名/公证，默认关。

流水线只跑 macOS runner，不改动 Windows/Linux 的构建与发布流程。

## 4. 发布顺序注意事项

macOS 流水线在 tag 推送时就触发，可能早于 Windows/Linux 发布脚本。建议：

1. 推 tag 后先跑 Windows/Linux 发布（`scripts/publish_release.py` + 上传资产）；
2. 再手动 `workflow_dispatch` 选 `publish=true` 重跑 macOS 合并步骤；或
3. 若 Release 里还没有 `update-manifest.json`，CI 会改传 `update-manifest-macos.json`（fragment），等主清单生成后再合并。

合并逻辑在 [merge_macos_manifest.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/scripts/merge_macos_manifest.py)：按 `(平台, 形态)` 合并，新增/覆盖 `macos-arm64`、`macos-x64` 条目，并生成按架构分组的 `darwin` 字段（各自带 SHA-256、大小与下载地址）。

## 5. 首次打开被 Gatekeeper 拦住的放行步骤

未签名包双击会提示「无法打开，因为 Apple 无法检查其是否包含恶意软件」。按顺序操作：

1. 在「访达」里找到 `rdc-desktop.app`（dmg 已拖入「应用程序」）；
2. **右键点击**（或按住 Control 点按）图标，选「打开」；
3. 弹窗里再点一次「打开」，应用即启动；之后双击不再拦截。
4. 如果第 2 步没有「打开」按钮：打开「系统设置」→「隐私与安全性」→ 下滑到「安全性」→ 找到被拦截的 RDC 条目 → 点「仍要打开」→ 输入登录密码。
5. 仍被拦（例如从浏览器下载被打了隔离标记）时，在「终端」执行：
   ```bash
   xattr -d com.apple.quarantine /Applications/rdc-desktop.app
   ```
   再回到第 2 步打开。

dmg 里的 `.app` 可以直接拖进「应用程序」；便携 zip 解压后同样按上述步骤放行。

## 6. 本地构建（可选）

没有 Mac 的用户用 CI 即可；有 Mac 的同事可以本地出包：

```bash
python3 -m venv ~/rdc-venv && source ~/rdc-venv/bin/activate
pip install PySide6==6.11.2 cryptography==50.0.1 pyinstaller==6.10.0
bash scripts/build_macos.sh ~/rdc-build
```

脚本会调用 PyInstaller（图标为 [rdc.icns](file:///c:/Users/Administrator/Desktop/RemoteDesktop/assets/app-icon/rdc.icns)）产出 `.app`，再封装 dmg 与便携 zip，并写 `.sha256`。

## 7. 签名/公证开关（保留但默认关闭）

用户已决定不上架 App Store、不购买 Apple 开发者账号，因此**不启用**签名与公证。流水线保留了开关与 secrets 注入方式，未来需要时再配置：

| Secret | 用途 |
|---|---|
| `MACOS_CERT_P12` | Developer ID Application 证书（base64） |
| `MACOS_CERT_PASSWORD` | 证书密码 |
| `MACOS_KEYCHAIN_PASSWORD` | 临时钥匙串密码 |
| `MACOS_SIGN_IDENTITY` | `codesign` 使用的身份名 |
| `APPLE_ID` / `APPLE_APP_PASSWORD` / `APPLE_TEAM_ID` | `notarytool` 公证账号 |

启用方式：手动触发并勾选 `sign=true`。没有配置证书时步骤自动跳过，产物保持未签名。密钥只从 secrets 读取，不写入工作流明文。
