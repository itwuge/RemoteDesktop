# macOS 构建与签名说明

## 1. 本版状态（v0.2.1）

v0.2.1 **不提供 macOS 产物**：本机与构建机都没有 macOS，无法生成、签名或验证 `.app`/`.dmg`，也没有 Apple Developer ID 证书。
因此 `update-manifest.json` 的 `platforms.macos-arm64` 明确标注为 `unavailable`，`assets` 中不含 macOS 条目；macOS 客户端检查更新会得到「缺少当前平台产物」的明确失败，不会误装其他平台包。

仓库只提供构建脚本 [build_macos.sh](file:///c:/Users/Administrator/Desktop/RemoteDesktop/scripts/build_macos.sh)，由具备 macOS 机器的同事在本地出包，**不伪造、不冒用他人签名产物**。

## 2. 前置条件

1. macOS 13+（Apple Silicon 出 `macos-arm64`，Intel 出 `macos-x64`）。
2. Xcode Command Line Tools：`xcode-select --install`。
3. Python 3.12，并安装锁定依赖：
   ```bash
   python3 -m venv ~/rdc-venv
   source ~/rdc-venv/bin/activate
   pip install PySide6==6.11.2 cryptography==50.0.1 pyinstaller==6.10.0
   ```
4. 可选签名：钥匙串中已有 `Developer ID Application` 证书（付费 Apple Developer 账号）。

## 3. 构建

```bash
bash scripts/build_macos.sh ~/rdc-build
```

产物（命名与 Windows/Linux 保持同一规范，发布脚本可自动识别）：

| 形态 | 文件名 |
|---|---|
| 安装包 | `rdc-desktop-<版本>-macos-arm64.dmg` |
| 便携包 | `rdc-desktop-<版本>-macos-arm64-portable.zip` |

脚本依次完成：PyInstaller `--windowed --onedir` 冻结 → 可选 `codesign --options runtime` → 便携 zip（含 `portable.marker`，配置与日志跟随包目录）→ dmg（应用 + `/Applications` 软链）→ 两个 `.sha256` 文件。

## 4. 签名与公证

```bash
CODESIGN_IDENTITY="Developer ID Application: 公司名 (TEAMID)" \
  bash scripts/build_macos.sh ~/rdc-build
```

签名只解决“可执行”；要让 Gatekeeper 在联网机器上不再拦截，还需要公证：

```bash
xcrun notarytool submit rdc-desktop-0.2.1-macos-arm64.dmg \
  --keychain-profile "rdc-notary" --wait
xcrun stapler staple rdc-desktop-0.2.1-macos-arm64.dmg
```

`notarytool` 的凭据用 `xcrun notarytool store-credentials` 存进钥匙串，**不写进仓库、不写进脚本、不随产物分发**。

## 5. 未签名产物的用户提示

未提供 `CODESIGN_IDENTITY` 时脚本会打印「未签名」。这类产物首次打开会被 Gatekeeper 拦截，用户需要：

- 右键点击 `rdc-desktop.app` → 打开 → 再确认一次；或
- 系统设置 → 隐私与安全性 → 仍要打开。

发布时必须在 Release 说明里写明签名状态，不能把未签名包说成已签名。

## 6. 发布清单如何标注

用发布脚本时追加：

```bash
python scripts/publish_release.py \
  --platform-unavailable "macos-arm64=未提供签名产物: 无 macOS 构建机与 Developer ID 证书, 仅提供 scripts/build_macos.sh"
```

清单会得到：

```json
"platforms": {
  "macos-arm64": {"status": "unavailable", "reason": "未提供签名产物: ..."}
}
```

客户端解析只看 `assets`，`platforms` 是给人看的可用性声明，不影响既有更新逻辑。
