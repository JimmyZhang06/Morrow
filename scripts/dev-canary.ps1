[CmdletBinding()]
param(
    [ValidateRange(0, 65535)][int]$ApiPort = 0
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot ".data\dev\local.env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Local configuration is missing; run scripts/dev-start.ps1 first."
}
$Config = ConvertFrom-StringData (Get-Content -Raw -LiteralPath $EnvFile)
$activeApiPort = if ($ApiPort -eq 0) { $Config.LOCAL_API_PORT } else { $ApiPort }
$baseUrl = "http://127.0.0.1:$activeApiPort"
$headers = @{
    Authorization = "Bearer $($Config.APP_LOCAL_AUTH_TOKEN)"
    "X-Vault-ID" = $Config.LOCAL_VAULT_ID
}

$live = Invoke-RestMethod -Uri "$baseUrl/health/live" -TimeoutSec 5
$ready = Invoke-RestMethod -Uri "$baseUrl/health/ready" -TimeoutSec 5
$capabilities = Invoke-RestMethod -Uri "$baseUrl/health/capabilities" -TimeoutSec 5
if ($live.status -ne "live" -or $ready.status -ne "ready") {
    throw "Health contract failed."
}
if (-not $capabilities.features.entries -or -not $capabilities.features.memory_review) {
    throw "Protected Source and Memory APIs are not mounted."
}
$entries = Invoke-RestMethod -Uri "$baseUrl/v1/entries?limit=1" -Headers $headers -TimeoutSec 8
if ($null -eq $entries.items) {
    throw "Authenticated Vault-scoped Source request failed."
}

Write-Host "PASS: PostgreSQL, migrations, non-owner runtime, local authentication, membership, and Source API."
Write-Host "Vault ID: $($Config.LOCAL_VAULT_ID)"
