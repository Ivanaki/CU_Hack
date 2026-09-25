# Выкладка одного проверенного Git-коммита на боевой стенд с Windows.
# .\deploy\deploy.ps1               # HEAD, только при чистой рабочей копии
# .\deploy\deploy.ps1 -Ref <SHA>   # конкретный коммит с этим сценарием
# .\deploy\deploy.ps1 -Ref HEAD -DryRun
[CmdletBinding()]
param(
    [string]$Ref = '',
    [string]$Target = 'egor@87.120.84.226',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$scp = Join-Path $env:WINDIR 'System32\OpenSSH\scp.exe'
$curl = Join-Path $env:WINDIR 'System32\curl.exe'

function Invoke-Native([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable завершился с кодом $LASTEXITCODE"
    }
}

Push-Location $projectRoot
try {
    if (-not $Ref) {
        $changes = @(git status --porcelain)
        if ($changes.Count -gt 0) {
            throw 'Есть незакоммиченные изменения. Закоммитьте их или укажите -Ref <SHA>, чтобы выложить только этот коммит.'
        }
        $Ref = 'HEAD'
    }
    $resolvedRef = & git rev-parse --verify "$Ref`^{commit}"
    if ($LASTEXITCODE -ne 0 -or -not $resolvedRef) {
        throw "Не удалось найти коммит: $Ref"
    }
    $sha = $resolvedRef.Trim()
    if ($sha -notmatch '^[0-9a-f]{40}$') { throw "Не удалось найти коммит: $Ref" }
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Нет .venv\Scripts\python.exe. Создайте окружение и установите зависимости проекта с [dev].'
    }

    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("honest-month-" + [guid]::NewGuid().ToString('N'))
    $sourceDir = Join-Path $tempDir 'source'
    New-Item -ItemType Directory -Path $sourceDir -Force | Out-Null
    $archive = Join-Path $tempDir 'release.tar'
    try {
        Write-Host "Упаковываю коммит $sha (рабочие файлы в архив не попадут)"
        Invoke-Native 'git' @('archive', '--format=tar', "--output=$archive", $sha)
        Invoke-Native 'tar.exe' @('-xf', $archive, '-C', $sourceDir)
        foreach ($required in @('deploy\remote_release.sh', 'deploy\compose.release.yml', 'tests\miniapp_viewport.test.cjs', 'miniapp\dist\index.html')) {
            if (-not (Test-Path -LiteralPath (Join-Path $sourceDir $required))) {
                throw "В выбранном коммите нет $required; сначала закоммитьте сценарий выкладки."
            }
        }
        $indexHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $sourceDir 'miniapp\dist\index.html')).Hash.ToLowerInvariant()

        Write-Host 'Проверяю тесты на содержимом коммита'
        Push-Location $sourceDir
        try {
            Invoke-Native 'node' @('--test', 'tests/miniapp_viewport.test.cjs')
            Invoke-Native $python @('-m', 'pytest', '-q')
        }
        finally { Pop-Location }

        if ($DryRun) {
            Write-Host "Проверка пройдена. Релиз $sha готов к отправке."
            return
        }

        $remoteArchive = "honest-month-release-$sha.tar"
        Write-Host "Передаю архив на $Target"
        Invoke-Native $scp @('-o', 'ConnectTimeout=15', $archive, "${Target}:$remoteArchive")
        $remoteCommand = "umask 077 && mkdir -p honest-month-releases/$sha && tar -xf $remoteArchive -C honest-month-releases/$sha && rm -f $remoteArchive && bash honest-month-releases/$sha/deploy/remote_release.sh $sha $indexHash"
        Invoke-Native $ssh @('-o', 'ConnectTimeout=15', $Target, $remoteCommand)

        # Проверка внешнего маршрута показывает, дошёл ли релиз до домена.
        # При её сбое сервер уже может работать на новом образе: не выдаём успех.
        $publicIndex = Join-Path $tempDir 'public-index.html'
        Invoke-Native $curl @('-fLsS', '--max-time', '15', '-H', 'Cache-Control: no-cache', '--output', $publicIndex, 'https://grosh.cu3rd.ru/')
        $publicHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $publicIndex).Hash.ToLowerInvariant()
        if ($publicHash -ne $indexHash) {
            throw "На публичном адресе другой index.html (SHA256 $publicHash); ожидается $indexHash. Состояние сервера проверьте через SSH."
        }
        Write-Host "Публичный Mini App совпадает с коммитом $sha"
    } finally {
        # Удаляем только созданный этим запуском временный каталог.
        $resolved = [System.IO.Path]::GetFullPath($tempDir)
        $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $resolved)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
} finally {
    Pop-Location
}
