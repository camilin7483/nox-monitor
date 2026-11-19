Unicode True
!include "MUI2.nsh"
!include "x64.nsh"

Name "Nox Monitor"
OutFile "${OUT}"
InstallDir "$LOCALAPPDATA\Programs\Nox Monitor"
InstallDirRegKey HKCU "Software\NoxMonitor" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "NOX MONITOR     /     ESCRITORIO"
VIProductVersion "1.0.1.0"
VIAddVersionKey "ProductName" "Nox Monitor"
VIAddVersionKey "FileDescription" "Asistente de instalación de Nox Monitor"
VIAddVersionKey "ProductVersion" "1.0.1"
VIAddVersionKey "FileVersion" "1.0.1"
VIAddVersionKey "LegalCopyright" "Nox Monitor"

!define MUI_ICON "${ART}/nox.ico"
!define MUI_UNICON "${ART}/nox.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${ART}/wizard.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${ART}/wizard.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP "${ART}/header.bmp"
!define MUI_HEADERIMAGE_RIGHT
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "Bienvenido a Nox Monitor"
!define MUI_WELCOMEPAGE_TEXT "Conecta WhatsApp desde un navegador privado y configura la IA a tu ritmo.$\r$\n$\r$\nAl abrir Nox, escanea el código QR para vincular tu cuenta. La sesión se guarda en tu perfil de usuario.$\r$\n$\r$\nConexión a internet necesaria para WhatsApp Web."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "Nox Monitor está instalado"
!define MUI_FINISHPAGE_TEXT "Puedes abrir Nox desde el menú Inicio. En el primer arranque, Nox abrirá WhatsApp en una ventana privada."
!define MUI_FINISHPAGE_RUN "$INSTDIR\Nox Monitor.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Abrir Nox Monitor"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Spanish"

Function .onInit
  SetShellVarContext current
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Esta edición de Nox requiere Windows de 64 bits."
    Abort
  ${EndIf}
FunctionEnd

Section "Aplicación" Main
  IfFileExists "$INSTDIR\*.*" 0 clean
  IfFileExists "$INSTDIR\.nox-installation" clean
  MessageBox MB_ICONSTOP "La carpeta de destino ya contiene otros archivos. Nox se instala en la ruta de aplicaciones de tu usuario."
  Abort
  clean:
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}/*"
  FileOpen $0 "$INSTDIR\.nox-installation" w
  FileWrite $0 "Nox Monitor"
  FileClose $0
  WriteUninstaller "$INSTDIR\Desinstalar.exe"
  WriteRegStr HKCU "Software\NoxMonitor" "InstallDir" "$INSTDIR"
  CreateShortcut "$SMPROGRAMS\Nox Monitor.lnk" "$INSTDIR\Nox Monitor.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor" "DisplayName" "Nox Monitor"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor" "DisplayVersion" "1.0.1"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor" "DisplayIcon" "$INSTDIR\Nox Monitor.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  IfFileExists "$INSTDIR\.nox-installation" confirmed
  Abort
  confirmed:
  Delete "$SMPROGRAMS\Nox Monitor.lnk"
  Delete "$INSTDIR\Nox Monitor.exe"
  Delete "$INSTDIR\LEEME.txt"
  Delete "$INSTDIR\Desinstalar.exe"
  Delete "$INSTDIR\.nox-installation"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\assets"
  RMDir /r "$INSTDIR\packaging"
  RMDir /r "$INSTDIR\licencias"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\NoxMonitor"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoxMonitor"
SectionEnd
