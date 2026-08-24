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
        TimeoutSec = 70
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

$notes = @(
    @{
        label = "2025-01"
        captured_at = "2025-01-12T09:20:00+08:00"
        content = "【虚拟长期测试 1/6】最近连续几周，只要早上先关掉消息，用九十分钟独立梳理产品问题，我就会觉得思路很清楚；被会议切碎时反而很焦躁。"
    },
    @{
        label = "2025-02"
        captured_at = "2025-02-23T20:10:00+08:00"
        content = "【虚拟长期测试 2/6】周末取消了一场临时聚会，独自散步和阅读后恢复得很快。我发现热闹并不总能让我充电，留白时间更重要。"
    },
    @{
        label = "2025-04"
        captured_at = "2025-04-05T18:40:00+08:00"
        content = "【虚拟长期测试 3/6】这次带新人拆解需求让我很满足。比起替别人给答案，我更喜欢帮助对方自己看见问题。"
    },
    @{
        label = "2025-05"
        captured_at = "2025-05-18T22:15:00+08:00"
        content = "【虚拟长期测试 4/6】同时答应三个项目后又开始拖延。把本周唯一要推进的结果写下来以后，压力明显下降，也终于开始行动。"
    },
    @{
        label = "2025-06"
        captured_at = "2025-06-29T16:30:00+08:00"
        content = "【虚拟长期测试 5/6】今天把一个小功能真正交给用户，比继续扩展宏大方案更有成就感。我希望保持每两周完成一个可验证闭环。"
    },
    @{
        label = "2025-08"
        captured_at = "2025-08-10T21:05:00+08:00"
        content = "【虚拟长期测试 6/6】回看半年记录，最稳定的状态仍然来自安静的上午、清晰的单一目标和能看到真实反馈的工作。会议多又目标模糊时，我很容易消耗。"
    }
)

# Reuse exact synthetic notes on retries, then create every missing Source first so
# all later model runs share the final Source generation.
$entryPage = Send-ApiJson -Method GET -Path "/v1/entries?limit=100" -Headers $authHeaders
Require-Status $entryPage @(200) "entry listing"
$created = foreach ($note in $notes) {
    $existing = @($entryPage.payload.items | Where-Object { $_.content -eq $note.content }) |
        Select-Object -First 1
    if ($null -ne $existing) {
        [PSCustomObject]@{
            label = $note.label
            content = $note.content
            entry_id = $existing.id
            revision = $existing.revision
        }
        continue
    }
    $entryKey = [Guid]::NewGuid().ToString()
    $entry = Send-ApiJson -Method POST -Path "/v1/entries" -Headers (
        $authHeaders + @{ "Idempotency-Key" = $entryKey }
    ) -Body @{
        content = $note.content
        captured_at = $note.captured_at
        memory_policy = "default"
        client_id = $entryKey
        source_type = "note"
        data_class = "sensitive"
    }
    Require-Status $entry @(201) "entry creation $($note.label)"
    [PSCustomObject]@{
        label = $note.label
        content = $note.content
        entry_id = $entry.payload.id
        revision = $entry.payload.revision
    }
}

# Index any already-succeeded canary Memories by their authoritative Source
# document so rerunning after an upstream failure does not call the model again.
$existingMemoryByDocument = @{}
$memoryPage = Send-ApiJson -Method GET -Path "/v1/memory-inbox?limit=100" -Headers $authHeaders
Require-Status $memoryPage @(200) "Memory inbox listing"
foreach ($memoryItem in $memoryPage.payload.items) {
    $detail = Send-ApiJson -Method GET `
        -Path "/v1/memories/$($memoryItem.memory_id)" -Headers $authHeaders
    Require-Status $detail @(200) "existing Memory detail"
    foreach ($anchor in $detail.payload.evidence) {
        $documentId = [string]$anchor.source_document_id
        if (-not $existingMemoryByDocument.ContainsKey($documentId)) {
            $existingMemoryByDocument[$documentId] = $detail
        }
    }
}

$results = foreach ($item in $created) {
    $existingMemory = $existingMemoryByDocument[[string]$item.entry_id]
    if ($null -ne $existingMemory) {
        $anchor = $existingMemory.payload.evidence[0]
        $excerpt = Send-ApiJson -Method GET `
            -Path "/v1/memories/$($existingMemory.payload.memory_id)/evidence/$($anchor.id)/excerpt" `
            -Headers $authHeaders
        Require-Status $excerpt @(200) "existing Evidence excerpt $($item.label)"
        $verbatim = $item.content.Contains([string]$excerpt.payload.excerpt)
        if (-not $verbatim) {
            throw "Existing Evidence excerpt is not verbatim Source text for $($item.label)."
        }
        [PSCustomObject]@{
            period = $item.label
            memory_id = $existingMemory.payload.memory_id
            statement = $existingMemory.payload.version.statement
            confidence = $existingMemory.payload.version.confidence_band
            evidence = $excerpt.payload.excerpt
            evidence_count = @($existingMemory.payload.evidence).Count
            idempotent_replay = $true
            verbatim_evidence = $verbatim
            provider_attempts = 0
            status = "succeeded-existing"
        }
        continue
    }
    $candidate = $null
    $candidateHeaders = $null
    $providerAttempts = 0
    while ($providerAttempts -lt 2) {
        $providerAttempts += 1
        $candidateKey = [Guid]::NewGuid().ToString()
        $revisionTag = '"' + $item.revision + '"'
        $candidateHeaders = $authHeaders + @{
            "Idempotency-Key" = $candidateKey
            "If-Match" = $revisionTag
        }
        $candidate = Send-ApiJson -Method POST `
            -Path "/v1/entries/$($item.entry_id)/candidate-insights" `
            -Headers $candidateHeaders -Body @{}
        if ($candidate.response.StatusCode -eq 200) { break }
        if ($candidate.response.StatusCode -ne 500) { break }
    }
    if ($candidate.response.StatusCode -ne 200) {
        [PSCustomObject]@{
            period = $item.label
            memory_id = $null
            statement = "<provider failure>"
            confidence = $null
            evidence = $null
            evidence_count = 0
            idempotent_replay = $false
            verbatim_evidence = $false
            provider_attempts = $providerAttempts
            status = "failed-http-$($candidate.response.StatusCode)"
        }
        continue
    }
    if ($candidate.payload.status -ne "succeeded" -or -not $candidate.payload.memory_id) {
        throw "Candidate generation did not return a durable Memory for $($item.label)."
    }

    $replay = Send-ApiJson -Method POST `
        -Path "/v1/entries/$($item.entry_id)/candidate-insights" `
        -Headers $candidateHeaders -Body @{}
    Require-Status $replay @(200) "candidate replay $($item.label)"
    $idempotent = (
        $replay.payload.run_id -eq $candidate.payload.run_id -and
        $replay.payload.memory_id -eq $candidate.payload.memory_id
    )
    if (-not $idempotent) {
        throw "Candidate idempotency contract failed for $($item.label)."
    }

    $memory = Send-ApiJson -Method GET `
        -Path "/v1/memories/$($candidate.payload.memory_id)" -Headers $authHeaders
    Require-Status $memory @(200) "Memory detail $($item.label)"
    if (-not $memory.payload.evidence -or -not $memory.payload.evidence[0].id) {
        throw "Candidate Memory has no authoritative evidence for $($item.label)."
    }
    $excerpt = Send-ApiJson -Method GET `
        -Path "/v1/memories/$($candidate.payload.memory_id)/evidence/$($memory.payload.evidence[0].id)/excerpt" `
        -Headers $authHeaders
    Require-Status $excerpt @(200) "Evidence excerpt $($item.label)"
    $verbatim = $item.content.Contains([string]$excerpt.payload.excerpt)
    if (-not $verbatim) {
        throw "Evidence excerpt is not verbatim Source text for $($item.label)."
    }

    [PSCustomObject]@{
        period = $item.label
        memory_id = $candidate.payload.memory_id
        statement = $memory.payload.version.statement
        confidence = $memory.payload.version.confidence_band
        evidence = $excerpt.payload.excerpt
        evidence_count = @($memory.payload.evidence).Count
        idempotent_replay = $idempotent
        verbatim_evidence = $verbatim
        provider_attempts = $providerAttempts
        status = "succeeded"
    }
}

$succeeded = @($results | Where-Object { $_.status -like "succeeded*" })
$failed = @($results | Where-Object { $_.status -notlike "succeeded*" })
$uniqueMemories = @($succeeded | Select-Object -ExpandProperty memory_id -Unique).Count
$results | Select-Object period, status, statement, confidence, evidence, evidence_count, provider_attempts, idempotent_replay, verbatim_evidence | Format-Table -Wrap
if ($failed.Count -gt 0) {
    throw "$($failed.Count) longitudinal candidate(s) failed after bounded retry."
}
Write-Host "PASS: $($created.Count) historical synthetic notes -> $($succeeded.Count) real candidates."
Write-Host "Unique Memories: $uniqueMemories; every replay and Evidence excerpt passed."
