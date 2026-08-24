[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$ApiPort = 18000
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot ".data\dev\local.env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Local configuration is missing; run scripts/dev-start.ps1 first."
}
$Config = ConvertFrom-StringData (Get-Content -Raw -LiteralPath $EnvFile)
$baseUrl = "http://127.0.0.1:$ApiPort"
$authHeaders = @{
    Authorization = "Bearer $($Config.APP_LOCAL_AUTH_TOKEN)"
    "X-Vault-ID" = $Config.LOCAL_VAULT_ID
}

function Send-ApiJson {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][hashtable]$Headers,
        [Parameter()][object]$Body
    )
    $arguments = @{
        Uri = "$baseUrl$Path"
        Method = $Method
        Headers = $Headers
        TimeoutSec = 15
        SkipHttpErrorCheck = $true
    }
    if ($null -ne $Body) {
        $arguments.ContentType = "application/json"
        $arguments.Body = $Body | ConvertTo-Json -Depth 8 -Compress
    }
    $response = Invoke-WebRequest @arguments
    $payload = if ($response.Content) { $response.Content | ConvertFrom-Json } else { $null }
    return @{ response = $response; payload = $payload }
}

function Require-Status {
    param([hashtable]$Result, [int[]]$Allowed, [string]$Stage)
    if ($Result.response.StatusCode -notin $Allowed) {
        throw "$Stage failed with HTTP $($Result.response.StatusCode)."
    }
}

$entryKey = [Guid]::NewGuid().ToString()
$entry = Send-ApiJson -Method POST -Path "/v1/entries" -Headers (
    $authHeaders + @{ "Idempotency-Key" = $entryKey }
) -Body @{
    content = "我希望本周每天留出十分钟整理产品闭环，并在周五回顾一次。"
    captured_at = [DateTimeOffset]::UtcNow.ToString("O")
    memory_policy = "default"
    client_id = $entryKey
    source_type = "note"
    data_class = "sensitive"
}
Require-Status $entry @(201) "entry creation"

$candidateKey = [Guid]::NewGuid().ToString()
$candidateHeaders = $authHeaders + @{
    "Idempotency-Key" = $candidateKey
    "If-Match" = '"1"'
}
$candidate = Send-ApiJson -Method POST `
    -Path "/v1/entries/$($entry.payload.id)/candidate-insights" `
    -Headers $candidateHeaders -Body @{}
Require-Status $candidate @(200) "candidate generation"
if ($candidate.payload.status -ne "succeeded" -or -not $candidate.payload.memory_id) {
    throw "Candidate generation did not return a durable Memory."
}
$candidateReplay = Send-ApiJson -Method POST `
    -Path "/v1/entries/$($entry.payload.id)/candidate-insights" `
    -Headers $candidateHeaders -Body @{}
Require-Status $candidateReplay @(200) "candidate idempotent replay"
if ($candidateReplay.payload.run_id -ne $candidate.payload.run_id -or
    $candidateReplay.payload.memory_id -ne $candidate.payload.memory_id) {
    throw "Candidate idempotency contract failed."
}

$memory = Send-ApiJson -Method GET `
    -Path "/v1/memories/$($candidate.payload.memory_id)" `
    -Headers $authHeaders
Require-Status $memory @(200) "Memory detail"
if (-not $memory.payload.evidence -or -not $memory.payload.evidence[0].id) {
    throw "Candidate Memory has no authoritative evidence."
}
$excerpt = Send-ApiJson -Method GET `
    -Path "/v1/memories/$($candidate.payload.memory_id)/evidence/$($memory.payload.evidence[0].id)/excerpt" `
    -Headers $authHeaders
Require-Status $excerpt @(200) "Evidence excerpt"
if (-not $excerpt.payload.excerpt) {
    throw "Evidence excerpt is empty."
}

$memoryEtag = [string]$memory.response.Headers.ETag
$confirmed = Send-ApiJson -Method POST `
    -Path "/v1/memories/$($candidate.payload.memory_id)/verdicts" `
    -Headers ($authHeaders + @{ "If-Match" = $memoryEtag }) `
    -Body @{ verdict = "confirm" }
Require-Status $confirmed @(201) "Memory confirmation"
if ($confirmed.payload.state -ne "active") {
    throw "Confirmed Memory did not become active."
}

$actionKey = [Guid]::NewGuid().ToString()
$action = Send-ApiJson -Method POST `
    -Path "/v1/memories/$($candidate.payload.memory_id)/actions" `
    -Headers ($authHeaders + @{ "Idempotency-Key" = $actionKey }) -Body @{}
Require-Status $action @(201) "action creation"
if ($action.payload.state -ne "proposed" -or -not $action.payload.is_reversible) {
    throw "Action was not created as a reversible proposal."
}

$actionId = $action.payload.action_id
$actionEtag = [string]$action.response.Headers.ETag
foreach ($transition in @("accept", "complete", "revoke")) {
    $outcome = Send-ApiJson -Method POST -Path "/v1/actions/$actionId/verdicts" `
        -Headers ($authHeaders + @{
            "If-Match" = $actionEtag
            "Idempotency-Key" = [Guid]::NewGuid().ToString()
        }) -Body @{ verdict = $transition }
    Require-Status $outcome @(200) "action $transition"
    $actionEtag = [string]$outcome.response.Headers.ETag
}
$authoritativeAction = Send-ApiJson -Method GET -Path "/v1/actions/$actionId" `
    -Headers $authHeaders
Require-Status $authoritativeAction @(200) "authoritative action read"
if ($authoritativeAction.payload.state -ne "revoked") {
    throw "Action did not reach the final revoked state."
}

Write-Host "PASS: entry -> idempotent evidence-backed candidate -> evidence excerpt -> confirm -> reversible action."
Write-Host "Candidate status: $($candidate.payload.status); action transitions: proposed -> accepted -> completed -> revoked."
