# Обновление уже установленного бота новым кодом:
#   1) распакуйте StreamMonitor-update.zip ПОВЕРХ папки C:\ (заменить файлы — «Да для всех»).
#      В update-архиве только код: .env, база эфиров и логи на сервере не трогаются;
#   2) выполните: powershell -ExecutionPolicy Bypass -File C:\Monitor-Python\deploy\update_windows.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Output "== Останавливаю бота"
Stop-ScheduledTask -TaskName StreamMonitorBot -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*Monitor-Python*main.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }

# Установка идемпотентна: обновит зависимости, проверит ffmpeg и vMix, перерегистрирует автозапуск и запустит бота
& powershell -NoProfile -ExecutionPolicy Bypass -File "$root\deploy\setup_windows.ps1"

Start-Sleep -Seconds 10
Write-Output "== Последние строки лога:"
Get-Content "$root\logs\bot.log" -Encoding UTF8 -Tail 3
Write-Output "== Должно быть: Run polling for bot @Alert_Checker_RealTime_bot"
