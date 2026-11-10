; ==========================================================================================
; 文件        : rdc-installer.nsi
; 所属目录    : packaging/windows/
; 文件用途    : Windows 安装包脚本; 安装到当前用户目录或用户选择目录, 建快捷方式, 可卸载
; 架构定位    : 发布配置; 由 scripts/build_installer.py 参数化调用
; 核心功能    : 复制无控制台产物; 开始菜单与桌面快捷方式; 卸载注册项; 静默安装支持
; 职责边界    : 不修改系统服务; 不写开机启动; 不删除用户配置与日志
; ==========================================================================================

Unicode true
!include "MUI2.nsh"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef SOURCE_DIR
  !error "缺少 SOURCE_DIR, 请通过 /DSOURCE_DIR= 指定 PyInstaller 产物目录"
!endif
!ifndef OUT_FILE
  !error "缺少 OUT_FILE, 请通过 /DOUT_FILE= 指定安装包输出路径"
!endif

!define APP_NAME "RDC 远程桌面"
!define APP_EXE "rdc-desktop.exe"
!define APP_PUBLISHER "RDC"
!define APP_REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\RDC"
!define APP_MUTEX "RDC.Desktop.Setup"

; 图标由 scripts/build_installer.py 通过 /DICON_FILE= 传入, 与 exe 内嵌图标同源
!ifdef ICON_FILE
  !define MUI_ICON "${ICON_FILE}"
  !define MUI_UNICON "${ICON_FILE}"
!endif

Name "${APP_NAME} ${VERSION}"
OutFile "${OUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${APP_REG_KEY}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "${APP_NAME} ${VERSION}"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=2052 "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME} 安装程序"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Copyright (C) 2026 RDC"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${VERSION}"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "启动 ${APP_NAME}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; 0.2.0 及更早版本装在 Program Files 并写 HKLM; 本版改为当前用户目录, 不自动迁移旧文件
Function .onInit
  ReadRegStr $0 HKLM "${APP_REG_KEY}" "InstallLocation"
  StrCmp $0 "" done
  IfSilent done
  MessageBox MB_OK|MB_ICONINFORMATION "检测到旧版本安装目录:$\r$\n$0$\r$\n$\r$\n本版默认安装到当前用户目录, 不会自动迁移旧文件; 如不再需要旧版本, 请先在「应用和功能」中卸载."
  done:
FunctionEnd

Section "主程序" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "${SOURCE_DIR}\*.*"
  !ifdef ICON_FILE
    File "/oname=$INSTDIR\rdc-desktop.ico" "${ICON_FILE}"
  !endif

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  SetRegView 64
  WriteRegStr HKCU "${APP_REG_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${APP_REG_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${APP_REG_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${APP_REG_KEY}" "InstallLocation" "$INSTDIR"
  !ifdef ICON_FILE
    WriteRegStr HKCU "${APP_REG_KEY}" "DisplayIcon" "$INSTDIR\rdc-desktop.ico"
  !else
    WriteRegStr HKCU "${APP_REG_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  !endif
  WriteRegStr HKCU "${APP_REG_KEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegDWORD HKCU "${APP_REG_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${APP_REG_KEY}" "NoRepair" 1

  SetShellVarContext current
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  SetRegView 64
  DeleteRegKey HKCU "${APP_REG_KEY}"
  RMDir /r "$INSTDIR"
SectionEnd
