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
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit(5000)
    }
    Remove-Item -LiteralPath $pidFile -Force
}

if (Test-Path -LiteralPath $EnvFile) {
    docker compose --env-file $EnvFile -f $ComposeFile stop postgres
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL stop failed." }
}
Write-Host "Local API, frontend, and PostgreSQL are stopped; the database volume is preserved."
