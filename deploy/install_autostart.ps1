# Автозапуск бота при входе в Windows (Планировщик заданий).
# Запуск без окна (pythonw), задержка 30 с после входа (ждём сеть),
# перезапуск при падении каждую минуту, только один экземпляр.
# Удалить: Unregister-ScheduledTask -TaskName StreamMonitorBot -Confirm:$false

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$main = Join-Path $root "main.py"
if (-not (Test-Path $pythonw)) { throw "Не найден $pythonw — сначала создайте venv и установите зависимости" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$main`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Priority 7
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "StreamMonitorBot" -Description "Telegram-бот мониторинга эфиров (@Alert_Checker_RealTime_bot)" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Задача StreamMonitorBot создана"
