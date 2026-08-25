[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [ValidateRange(1, 65535)][int]$ApiPort = 8000,
    [ValidateRange(1, 65535)][int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $RepoRoot ".data\dev"
$EnvFile = Join-Path $StateDir "local.env"
$ComposeFile = Join-Path $RepoRoot "compose.local.yml"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Node = (Get-Command node -ErrorAction Stop).Source

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing .venv Python runtime. Install the backend development environment first."
}
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null

if (-not (Test-Path -LiteralPath $EnvFile)) {
    $adminPassword = "A$([Guid]::NewGuid().ToString('N'))"
    $runtimePassword = "R$([Guid]::NewGuid().ToString('N'))"
    $localToken = "T$([Guid]::NewGuid().ToString('N'))"
    $sourceHmac = "H$([Guid]::NewGuid().ToString('N'))"
    $contentKey = "C$([Guid]::NewGuid().ToString('N'))"
    $modelRunHmac = "M$([Guid]::NewGuid().ToString('N'))"
    $values = @(
        "LOCAL_POSTGRES_PORT=55432"
        "LOCAL_POSTGRES_ADMIN_PASSWORD=$adminPassword"
        "LOCAL_RUNTIME_DATABASE_ROLE=life_coach_runtime"
        "LOCAL_RUNTIME_DATABASE_PASSWORD=$runtimePassword"
        "LOCAL_ADMIN_DATABASE_URL=postgresql+asyncpg://life_coach_owner:$adminPassword@127.0.0.1:55432/life_coach"
        "APP_DATABASE_URL=postgresql+asyncpg://life_coach_runtime:$runtimePassword@127.0.0.1:55432/life_coach"
        "APP_ENV=development"
        "APP_SOURCE_API_ENABLED=true"
        "APP_MEMORY_API_ENABLED=true"
        "APP_SOURCE_API_HMAC_KEY=$sourceHmac"
        "APP_LOCAL_SOURCE_CONTENT_KEY=$contentKey"
        "APP_MODEL_PROVIDER=deterministic-fake"
        "APP_CANDIDATE_ASYNC_ENABLED=true"
        "APP_MODEL_RUN_HMAC_KEY=$modelRunHmac"
        "APP_LOCAL_AUTH_ENABLED=true"
        "APP_LOCAL_AUTH_PRINCIPAL_ID=11111111-1111-4111-8111-111111111111"
        "APP_LOCAL_AUTH_TOKEN=$localToken"
        "LOCAL_VAULT_ID=22222222-2222-4222-8222-222222222222"
        "LOCAL_API_PORT=8000"
        "LOCAL_FRONTEND_PORT=5173"
    )
    Set-Content -LiteralPath $EnvFile -Value $values -Encoding utf8NoBOM
}

$existingConfig = ConvertFrom-StringData (Get-Content -Raw -LiteralPath $EnvFile)
if (-not $existingConfig.ContainsKey("APP_MODEL_PROVIDER")) {
    Add-Content -LiteralPath $EnvFile -Value "APP_MODEL_PROVIDER=deterministic-fake" -Encoding UTF8
}
if (-not $existingConfig.ContainsKey("APP_MODEL_RUN_HMAC_KEY")) {
    $modelRunHmac = "M$([Guid]::NewGuid().ToString('N'))"
    Add-Content -LiteralPath $EnvFile -Value "APP_MODEL_RUN_HMAC_KEY=$modelRunHmac" -Encoding UTF8
}
if (-not $existingConfig.ContainsKey("APP_CANDIDATE_ASYNC_ENABLED")) {
    Add-Content -LiteralPath $EnvFile -Value "APP_CANDIDATE_ASYNC_ENABLED=true" -Encoding UTF8
}

$Config = ConvertFrom-StringData (Get-Content -Raw -LiteralPath $EnvFile)
foreach ($item in $Config.GetEnumerator()) {
    Set-Item -Path "Env:$($item.Key)" -Value $item.Value
}
$env:LOCAL_API_PORT = [string]$ApiPort
$env:LOCAL_FRONTEND_PORT = [string]$FrontendPort
$env:VITE_API_BASE_URL = "http://127.0.0.1:$ApiPort"
$env:VITE_DEV_AUTH_TOKEN = $Config.APP_LOCAL_AUTH_TOKEN
$env:VITE_VAULT_ID = $Config.LOCAL_VAULT_ID

docker compose --env-file $EnvFile -f $ComposeFile up -d --wait postgres
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL did not become healthy." }

Push-Location $RepoRoot
try {
    $runtimeDatabaseUrl = $env:APP_DATABASE_URL
    $env:APP_DATABASE_URL = $env:LOCAL_ADMIN_DATABASE_URL
    & $Python -m alembic -c alembic/alembic.ini upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Alembic migration failed." }
    & $Python scripts/seed_local.py
    if ($LASTEXITCODE -ne 0) { throw "Local identity seed failed." }
    $env:APP_DATABASE_URL = $runtimeDatabaseUrl

    function Start-ManagedProcess {
        param(
            [Parameter(Mandatory)][string]$Name,
            [Parameter(Mandatory)][string]$Executable,
            [Parameter(Mandatory)][string[]]$Arguments,
            [Parameter(Mandatory)][string]$ReadyUrl
        )
        $pidFile = Join-Path $StateDir "$Name.pid.json"
        if (Test-Path -LiteralPath $pidFile) {
            $record = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
            $existing = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
            if ($null -ne $existing) {
                try {
                    Invoke-RestMethod -Uri $ReadyUrl -TimeoutSec 2 | Out-Null
                    return
                } catch {
                    throw "$Name process exists but is not healthy; run scripts/dev-stop.ps1."
                }
            }
            Remove-Item -LiteralPath $pidFile -Force
        }
        $stdout = Join-Path $StateDir "$Name.log"
        $stderr = Join-Path $StateDir "$Name.err.log"
        $process = Start-Process -FilePath $Executable -ArgumentList $Arguments `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        @{
            pid = $process.Id
            executable = $Executable
            startedUtc = $process.StartTime.ToUniversalTime().ToString("O")
        } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8NoBOM
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            Start-Sleep -Milliseconds 500
            if ($process.HasExited) { throw "$Name exited during startup. See $stderr" }
            try {
                Invoke-RestMethod -Uri $ReadyUrl -TimeoutSec 2 | Out-Null
                return
            } catch { }
        }
        throw "$Name did not become ready. See $stderr"
    }

    Start-ManagedProcess -Name "api" -Executable $Python -Arguments @(
        "-m", "uvicorn", "life_coach.main:app", "--host", "127.0.0.1",
        "--port", $env:LOCAL_API_PORT, "--no-access-log"
    ) -ReadyUrl "http://127.0.0.1:$($env:LOCAL_API_PORT)/health/ready"

    if (-not $SkipFrontend) {
        $vite = Join-Path $RepoRoot "apps\desktop\node_modules\vite\bin\vite.js"
        if (-not (Test-Path -LiteralPath $vite)) {
            throw "Frontend dependencies are missing. Run npm install in apps/desktop once."
        }
        Start-ManagedProcess -Name "frontend" -Executable $Node -Arguments @(
            $vite, "apps\desktop", "--config", "apps\desktop\vite.config.ts",
            "--host", "127.0.0.1", "--port", $env:LOCAL_FRONTEND_PORT,
            "--strictPort"
        ) -ReadyUrl "http://127.0.0.1:$($env:LOCAL_FRONTEND_PORT)"
    }
} finally {
    Pop-Location
}

Write-Host "Local stack is ready."
Write-Host "API:      http://127.0.0.1:$($env:LOCAL_API_PORT)"
if (-not $SkipFrontend) {
    Write-Host "Frontend: http://127.0.0.1:$($env:LOCAL_FRONTEND_PORT)"
}
Write-Host "Vault ID: $($env:LOCAL_VAULT_ID)"
Write-Host "Token and generated local credentials remain in ignored .data/dev/local.env."
