; Ridea Windows Installer
; Uses NSIS to create a proper installer from the packaged Electron app

Unicode True

!define APP_NAME "Ridea"
!define APP_VERSION "1.0.12"
!define APP_EXE "Ridea.exe"
!define APP_DIR "dist14\Ridea-win32-x64"
!define INSTALL_DIR "$PROGRAMFILES64\Ridea"
!define UNINSTALL_REG "Software\Microsoft\Windows\CurrentVersion\Uninstall\Ridea"

Name "${APP_NAME}"
OutFile "Ridea-Setup-${APP_VERSION}.exe"
InstallDir "${INSTALL_DIR}"
InstallDirRegKey HKLM "${UNINSTALL_REG}" "InstallLocation"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

; Modern UI
!include "MUI2.nsh"
!define MUI_ABORTWARNING
!define MUI_ICON "assets\icon.ico"
!define MUI_UNICON "assets\icon.ico"

; Pages
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ─── Install ───────────────────────────────────────────────────────────────────
Section "Ridea" SecMain
  SectionIn RO
  SetOutPath "${INSTALL_DIR}"

  ; Copy all app files
  File /r "${APP_DIR}\*.*"

  ; Write uninstaller
  WriteUninstaller "${INSTALL_DIR}\Uninstall.exe"

  ; Registry: Add/Remove Programs entry
  WriteRegStr HKLM "${UNINSTALL_REG}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "${UNINSTALL_REG}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "${UNINSTALL_REG}" "Publisher" "Dajana Rodriguez"
  WriteRegStr HKLM "${UNINSTALL_REG}" "InstallLocation" "${INSTALL_DIR}"
  WriteRegStr HKLM "${UNINSTALL_REG}" "UninstallString" '"${INSTALL_DIR}\Uninstall.exe"'
  WriteRegDWORD HKLM "${UNINSTALL_REG}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_REG}" "NoRepair" 1

  ; Copy icon to install dir root for easy shortcut reference
  File "assets\icon.ico"

  ; Add/Remove Programs display icon
  WriteRegStr HKLM "${UNINSTALL_REG}" "DisplayIcon" "${INSTALL_DIR}\icon.ico"

  ; Desktop shortcut with Ridea icon
  CreateShortcut "$DESKTOP\Ridea.lnk" "${INSTALL_DIR}\${APP_EXE}" "" "${INSTALL_DIR}\icon.ico" 0

  ; Start Menu shortcut with Ridea icon
  CreateDirectory "$SMPROGRAMS\Ridea"
  CreateShortcut "$SMPROGRAMS\Ridea\Ridea.lnk" "${INSTALL_DIR}\${APP_EXE}" "" "${INSTALL_DIR}\icon.ico" 0
  CreateShortcut "$SMPROGRAMS\Ridea\Odinštalovať Ridea.lnk" "${INSTALL_DIR}\Uninstall.exe"

  ; Refresh Windows icon cache
  Exec '"$SYSDIR\ie4uinit.exe" -show'

SectionEnd

; ─── Uninstall ─────────────────────────────────────────────────────────────────
Section "Uninstall"
  ; Remove all installed files
  RMDir /r "${INSTALL_DIR}"

  ; Remove shortcuts
  Delete "$DESKTOP\Ridea.lnk"
  RMDir /r "$SMPROGRAMS\Ridea"

  ; Remove registry entry
  DeleteRegKey HKLM "${UNINSTALL_REG}"
SectionEnd
