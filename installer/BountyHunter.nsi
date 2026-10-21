!include "MUI2.nsh"

!define APPNAME "Bounty Hunter"
!define APPEXE "BountyHunter.exe"
!define COMPANY "Bounty Hunter"
!define APPVERSION "0.2.1"

Name "${APPNAME}"
OutFile "..\dist\BountyHunter-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\BountyHunter"
InstallDirRegKey HKCU "Software\BountyHunter" "InstallDir"
RequestExecutionLevel user
Unicode True
SetCompressor /SOLID lzma

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Bounty Hunter Desktop" SEC_MAIN
    SetOutPath "$INSTDIR"
    File "..\dist\BountyHunter.exe"
    WriteRegStr HKCU "Software\BountyHunter" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BountyHunter" "DisplayName" "${APPNAME}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BountyHunter" "DisplayVersion" "${APPVERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BountyHunter" "Publisher" "${COMPANY}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BountyHunter" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BountyHunter" "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BountyHunter" "NoRepair" 1
    CreateDirectory "$SMPROGRAMS\Bounty Hunter"
    CreateShortcut "$SMPROGRAMS\Bounty Hunter\Bounty Hunter.lnk" "$INSTDIR\${APPEXE}"
    CreateShortcut "$SMPROGRAMS\Bounty Hunter\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
    WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\${APPEXE}"
    Delete "$INSTDIR\Uninstall.exe"
    Delete "$SMPROGRAMS\Bounty Hunter\Bounty Hunter.lnk"
    Delete "$SMPROGRAMS\Bounty Hunter\Uninstall.lnk"
    RMDir "$SMPROGRAMS\Bounty Hunter"
    RMDir "$INSTDIR"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BountyHunter"
    DeleteRegKey HKCU "Software\BountyHunter"
    ; Intentionally preserve user earnings databases and settings in AppData.
SectionEnd

