[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$ApiPort = 18001
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot ".data\dev\local.env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Local development environment is unavailable."
}
$config = ConvertFrom-StringData (Get-Content -Raw -LiteralPath $EnvFile)
$headers = @{
    Authorization = "Bearer $($config.APP_LOCAL_AUTH_TOKEN)"
    "X-Vault-ID" = $config.LOCAL_VAULT_ID
}
$baseUrl = "http://127.0.0.1:$ApiPort"
$project = Invoke-RestMethod -Uri "$baseUrl/v1/narratives/default" `
    -Method Post -Headers $headers -ContentType "application/json" -Body "{}"

function Invoke-NarrativeGeneration {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$IdempotencyKey
    )
    $requestHeaders = @{
        Authorization = $headers.Authorization
        "X-Vault-ID" = $headers."X-Vault-ID"
        "Idempotency-Key" = $IdempotencyKey
    }
    Invoke-RestMethod -Uri "$baseUrl/v1/narratives/$($project.project_id)/$Path" `
        -Method Post -Headers $requestHeaders -ContentType "application/json" `
        -Body "{}" -TimeoutSec 120
}

$lifeLineKey = [Guid]::NewGuid().ToString()
$lifeLine = Invoke-NarrativeGeneration -Path "life-lines" -IdempotencyKey $lifeLineKey
$lifeLineReplay = Invoke-NarrativeGeneration -Path "life-lines" -IdempotencyKey $lifeLineKey
$chapter = Invoke-NarrativeGeneration -Path "memoir-chapters" -IdempotencyKey ([Guid]::NewGuid().ToString())
[PSCustomObject]@{
    project_id = $project.project_id
    life_line_generation_id = $lifeLine.generation_id
    life_line_run_id = $lifeLine.model_run_id
    life_line_replay_same = ($lifeLineReplay.generation_id -eq $lifeLine.generation_id)
    theme_count = $lifeLine.themes.Count
    memoir_generation_id = $chapter.generation_id
    memoir_run_id = $chapter.model_run_id
    memoir_citation_count = $chapter.citations.Count
} | ConvertTo-Json -Compress
