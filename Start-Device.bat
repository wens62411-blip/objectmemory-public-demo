@echo off
cd /d "%~dp0"
echo ObjectMemory device setup: enabling PIN-protected local-network access.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -Lan
pause
