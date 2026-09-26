# Локальный стенд Mini App с отдельной базой и загрузками.
# .\scripts\local.ps1          # запустить или перезапустить и открыть браузер
# .\scripts\local.ps1 -Reset   # заново залить демо-данные
# .\scripts\local.ps1 -Stop    # остановить локальный API
[CmdletBinding()]
param(
    [switch]$Reset,
    [switch]$Stop,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtime = Join-Path $projectRoot 'data\runtime'
$pidFile = Join-Path $runtime 'local-review.pid'
$stdout = Join-Path $runtime 'local-review.out.log'
$stderr = Join-Path $runtime 'local-review.err.log'
$url = 'http://127.0.0.1:8081/?tg_id=1'

function Get-LocalProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $savedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)) {
        return $null
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    if ($processInfo -and $processInfo.CommandLine -match 'app\.main\s+--api' -and
        $processInfo.ExecutablePath -ieq $python) {
        return $processInfo
    }
    return $null
}

function Stop-LocalProcess($processInfo) {
    Stop-Process -Id $processInfo.ProcessId
    Wait-Process -Id $processInfo.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    if (Get-Process -Id $processInfo.ProcessId -ErrorAction SilentlyContinue) {
        throw 'Локальный API не остановился. Закройте его перед повторным запуском.'
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$running = Get-LocalProcess
if ($Stop) {
    if ($running) {
        Stop-LocalProcess $running
        Write-Host 'Локальный API остановлен.'
    } else {
        Write-Host 'Локальный API уже остановлен.'
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    return
}

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Не найден .venv\Scripts\python.exe. Создайте окружение и установите проект: pip install -e ".[dev]"'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    throw 'Не найден .env с настройками проекта.'
}
if ($running) {
    Write-Host 'Перезапускаю локальный API...'
    Stop-LocalProcess $running
}
Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue

# Настройки процесса имеют приоритет над .env. Ключи LLM и ASR остаются из .env,
# а бот не запускается и локальный браузер входит по ?tg_id=1.
$overrides = @{
    BOT_TOKEN = ''
    MINIAPP_URL = ''
    API_HOST = '127.0.0.1'
    API_PORT = '8081'
    DATABASE_URL = 'sqlite:///./data/runtime/local-review.db'
    UPLOAD_DIR = './data/uploads-local'
    PUBLIC_BASE_URL = 'http://127.0.0.1:8081'
    PYTHONIOENCODING = 'utf-8'
}
$originalEnv = @{}
foreach ($name in $overrides.Keys) {
    $originalEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $overrides[$name], 'Process')
}

Push-Location $projectRoot
try {
    if ($Reset -or -not (Test-Path -LiteralPath (Join-Path $runtime 'local-review.db'))) {
        Write-Host 'Заливаю демонстрационные операции в отдельную локальную базу...'
        & $python -m scripts.seed --reset
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить локальную базу.' }
    }

    Write-Host 'Запускаю локальный API...'
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'app.main', '--api') `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Set-Content -LiteralPath $pidFile -Value $process.Id

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($process.HasExited) { break }
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8081/healthz' -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id }
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
        throw "Локальный API не запустился. Проверьте $stderr (возможно, порт 8081 занят)."
    }
    Write-Host "Готово: $url"
    Write-Host 'Остановить: .\scripts\local.ps1 -Stop'
    if (-not $NoBrowser) { Start-Process $url }
}
finally {
    Pop-Location
    foreach ($name in $overrides.Keys) {
        [Environment]::SetEnvironmentVariable($name, $originalEnv[$name], 'Process')
    }
}
