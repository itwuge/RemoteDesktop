# deferred-capture（暂缓的 Android 被控采集代码）

本目录不在任何 source set 里，**不参与构建、不进 APK**。

## 为什么放在这里

Android 端的产品定位是「只做控制端，不做被控」（见设计书 v1.2 与需求基线）。
早期骨架误按采集侧实现，`CaptureService.kt` / `CaptureContract.kt` 属于被控版本的能力，
与当前定位冲突，因此从 `app/src/main` 移出，保留代码供后续「被控版本」立项时评估复用。

## 约束

- 不得把本目录加回 `sourceSets`，除非产品基线明确恢复「Android 被控」。
- 若恢复，Manifest 需同时加回 `FOREGROUND_SERVICE_MEDIA_PROJECTION` 权限与 `mediaProjection` 类型前台服务声明。
- 本目录代码未经控制端里程碑验证，不得计入任何验收结论。
