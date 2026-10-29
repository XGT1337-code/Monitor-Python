# Установка бота на новый Windows-компьютер (например, сервер с vMix) одной командой:
#   powershell -ExecutionPolicy Bypass -File deploy\setup_windows.ps1
# Что делает: venv + зависимости, ffmpeg (winget), пути к ffmpeg и VMIX_HOST в .env,
# автозапуск через Планировщик заданий и запуск бота.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Output "== Папка проекта: $root"

# 1. Python 3.11+
$py = $null
foreach ($v in @("3.14", "3.13", "3.12", "3.11")) {
    try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break } } catch {}
}
if (-not $py) { throw "Python 3.11+ не найден. Установите: winget install Python.Python.3.12" }
Write-Output "== Python: $($py -join ' ')"

# 2. Виртуальное окружение и зависимости
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py[0] $py[1] -m venv .venv }
& .venv\Scripts\python.exe -m pip install -q --upgrade pip
& .venv\Scripts\python.exe -m pip install -q -r requirements.txt
Write-Output "== Зависимости установлены"

# 3. ffmpeg
$ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
if (-not $ffprobe) {
    $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) {
        Write-Output "== Устанавливаю ffmpeg (winget)…"
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent | Out-Null
        $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if (-not $found) { throw "ffmpeg не найден после установки — поставьте вручную и пропишите FFPROBE_PATH/FFMPEG_PATH в .env" }
    $ffprobe = $found.FullName
}
$ffmpeg = Join-Path (Split-Path $ffprobe) "ffmpeg.exe"
Write-Output "== ffmpeg: $ffmpeg"

# 4. .env: пути к ffmpeg и vMix на этом же компьютере
if (-not (Test-Path ".env")) { throw ".env не найден — скопируйте его со старого компьютера" }
$envText = [IO.File]::ReadAllText("$root\.env")
function Set-EnvVar([string]$name, [string]$value, [bool]$overwrite) {
    $pattern = "(?m)^$name=.*$"
    if ($script:envText -match $pattern) {
        if ($overwrite -or ($script:envText -match "(?m)^$name=\s*$")) {
            $script:envText = [regex]::Replace($script:envText, $pattern, "$name=$value")
        }
    } else {
        $script:envText = $script:envText.TrimEnd("`r", "`n") + "`n$name=$value`n"
    }
}
Set-EnvVar "FFPROBE_PATH" $ffprobe $true
Set-EnvVar "FFMPEG_PATH" $ffmpeg $true

# Новые настройки из обновления (deploy\env_defaults.txt): добавляются, только если их ещё нет / они пустые
$defaults = Join-Path $root "deploy\env_defaults.txt"
if (Test-Path $defaults) {
    foreach ($line in Get-Content $defaults -Encoding UTF8) {
        if ($line -match '^\s*([A-Z_]+)=(.*)$') {
            Set-EnvVar $Matches[1] $Matches[2].Trim() $false
            Write-Output "   .env: $($Matches[1]) — проверено"
        }
    }
}

# 5. Поиск vMix API: vMix может слушать не 127.0.0.1, а сетевой IP компьютера
$candidates = @("127.0.0.1") + @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } |
    Select-Object -ExpandProperty IPAddress)
$vmixHost = $null
foreach ($ip in $candidates) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://${ip}:8088/api"
        if ($r.Content -like "*<vmix>*") { $vmixHost = $ip; break }
    } catch {}
}
if ($vmixHost) {
    Set-EnvVar "VMIX_HOST" $vmixHost $true
    Write-Output "== vMix API найден: http://${vmixHost}:8088 ✓"
} else {
    Write-Output "!! vMix API не найден. Откройте vMix → Settings → Web Controller → Enable (порт 8088),"
    Write-Output "   затем запустите этот скрипт ещё раз (или впишите VMIX_HOST в .env вручную)."
}
[IO.File]::WriteAllText("$root\.env", $envText)
Write-Output "== .env обновлён (FFPROBE_PATH, FFMPEG_PATH, VMIX_HOST)"

# 6. Автозапуск и старт
& powershell -NoProfile -ExecutionPolicy Bypass -File "$root\deploy\install_autostart.ps1"
Start-ScheduledTask -TaskName StreamMonitorBot
Write-Output "== Бот запущен. Лог: $root\logs\bot.log"
Write-Output "!! На СТАРОМ компьютере бота нужно остановить: Unregister-ScheduledTask StreamMonitorBot -Confirm:`$false"
