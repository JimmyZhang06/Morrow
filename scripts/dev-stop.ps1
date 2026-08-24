[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $RepoRoot ".data\dev"
$EnvFile = Join-Path $StateDir "local.env"
$ComposeFile = Join-Path $RepoRoot "compose.local.yml"

foreach ($name in @("frontend", "api")) {
    $pidFile = Join-Path $StateDir "$name.pid.json"
    if (-not (Test-Path -LiteralPath $pidFile)) { continue }
    $record = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        $actualExecutable = $process.Path
        $expectedExecutable = [System.IO.Path]::GetFullPath([string]$record.executable)
        if ([System.IO.Path]::GetFullPath($actualExecutable) -ne $expectedExecutable) {
            throw "Refusing to stop PID $($record.pid): executable identity changed."
        }
        $expectedStart = ([datetime]$record.startedUtc).ToUniversalTime()
        $actualStart = $process.StartTime.ToUniversalTime()
        if ([Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 1) {
            throw "Refusing to stop PID $($record.pid): process start identity changed."
        }
        # The bundled virtual-environment launcher owns a child interpreter on
        # Windows, so stop the verified process tree instead of orphaning uvicorn.
        & taskkill.exe /PID $process.Id /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to stop managed $name process tree." }
    }
    Remove-Item -LiteralPath $pidFile -Force
}

if (Test-Path -LiteralPath $EnvFile) {
    docker compose --env-file $EnvFile -f $ComposeFile stop postgres
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL stop failed." }
}
Write-Host "Local API, frontend, and PostgreSQL are stopped; the database volume is preserved."
