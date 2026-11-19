Unicode True
Name "Nox Monitor"
OutFile "${OUT}"
Icon "${ART}/nox.ico"
SilentInstall silent
RequestExecutionLevel user
VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName" "Nox Monitor"
VIAddVersionKey "FileDescription" "Nox Monitor"
VIAddVersionKey "ProductVersion" "1.0.0"
VIAddVersionKey "FileVersion" "1.0.0"
VIAddVersionKey "LegalCopyright" "Nox Monitor"
Section
  Exec '"$EXEDIR\runtime\python\pythonw.exe" "$EXEDIR\packaging\launcher.py"'
SectionEnd
