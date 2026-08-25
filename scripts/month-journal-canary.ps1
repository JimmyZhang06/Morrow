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
        TimeoutSec = 90
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
        $detail = if ($Result.payload.detail) { [string]$Result.payload.detail } else { "no safe detail" }
        throw "$Stage failed with HTTP $($Result.response.StatusCode): $detail"
    }
}

$contents = @(
    "周一同时摆着三个任务，我一早就在切换，忙了一上午却没有真正推进。午后把它们写下来，只保留「完成访谈提纲」这一件，心里终于安静了一点。",
    "今天八点半到公司后没有先看消息，连续写了七十分钟。那段时间思路很顺，十点后的两个会议虽然密集，但至少最重要的东西已经向前走了。",
    "评审会上我其实不同意把所有功能都塞进第一版，但开口时还是说得很委婉。会后有点懊恼，我担心直接表达会让气氛变僵。",
    "午饭后绕园区走了二十分钟，回来处理文档时明显清醒。以前我总觉得运动要完整安排才算数，今天这种短暂活动似乎也有用。",
    "晚上九点又看到工作群的新问题，我忍不住立刻回复。事情不大，但睡前脑子一直停不下来；也许我需要给晚上的消息设一个边界。",
    "原本答应参加周末聚会，早上醒来却很想独处。最后只去了一个小时，回来做饭、听音乐，恢复得比继续待在人群里快。",
    "一周结束时回看，真正完成的是访谈提纲和一个小原型，临时答应的杂事反而占了很多注意力。下周想先决定唯一交付，再安排会议。",
    "今天访谈了一位真实用户。她指出我们一直担心的问题并不是她最在意的，虽然推翻了一部分设想，但我反而很兴奋，因为终于有了具体反馈。",
    "上午又想扩展原型的设置页，做到一半才发现核心流程还没让人试过。我停下来把可演示版本发给同事，收到第一条反馈后比继续独自完善更踏实。",
    "数据看板连续两天没有变化，我开始怀疑这周的工作是不是没有价值。和同事聊完才意识到，我们甚至还没定义这次小实验要观察什么。",
    "和导师聊了半小时。他没有替我给答案，只问我最害怕放弃的是哪一部分。我发现自己不是不知道优先级，而是不愿承认有些想法本月做不完。",
    "傍晚跑了三公里，速度不快。回家后那种一直压着的烦躁松了一些；我不一定需要追求训练成绩，规律地动一动可能更重要。",
    "今天拒绝了一个临时帮忙的请求，刚说出口时有点内疚。对方其实很平静，我也守住了已经答应团队的交付，边界似乎没有我想得那么伤人。",
    "晚上和家人吃饭，没有谈工作。听他们讲一些日常小事时，我突然发现自己最近总把注意力放在「还没完成什么」，很少感受已经拥有的稳定关系。",
    "半个月回看，状态好的日子通常都有一个清晰的小目标、安静的开始和真实反馈。状态差时则常常是任务太多、消息太碎，又迟迟没有把东西交出去。",
    "早上本来计划写方案，却先处理了十几条消息。到中午一直有种被推着走的感觉；明天想试试九点半之前不开聊天软件。",
    "今天开会前先用十分钟写清楚自己的判断和两个依据。讨论时虽然还是紧张，但我表达得比上次直接，也没有出现我担心的冲突。",
    "有封需要说明延期的邮件拖了两天。今晚只给自己十分钟写第一版，真正开始后并没有想象中难，最消耗我的可能是反复回避。",
    "同事说原型的信息层级还是太复杂，我第一反应是想解释。忍住之后重新看了一遍，发现他说得有道理；被指出问题不舒服，但并不等于之前的工作没有价值。",
    "周六去了小型展览，一个人慢慢看了两个小时。没有完成任何任务，但脑子里一些纠缠的想法自然松开了，留白不是浪费。",
    "昨晚睡得很晚，今天即使列了计划也很难集中。下午取消了一个非必要安排，提早回家休息；精力不足时继续逼自己，效果并不好。",
    "把新版本演示给三个人看，其中两个人都在同一步停住。这个反馈很明确，我知道下周该改什么，也第一次觉得缩小范围不是退让，而是在加快学习。",
    "陪新人梳理需求时，我没有直接给方案，而是让他把用户、场景和假设写出来。看到他自己找到问题，我感到很满足，这种帮助方式很像我想成为的协作者。",
    "今天四个项目同时来问进度，我又开始焦躁。把所有承诺列出来后，我决定只保证一个本周结果，并主动告诉其他人新的时间，压力下降了很多。",
    "我很在意工作的自主感，也在意交付质量，但过去常把质量理解成「继续完善」。最近越来越觉得，真正尊重质量也包括让用户尽早看见并纠正方向。",
    "连续第三天把上午第一小时留给最重要的任务。不是每天都很高效，但至少不会到晚上才发现真正重要的事完全没碰。",
    "讨论资源分配时我直接说出了不同意见，也认真听了对方的限制。最后没有完全按我的方案走，但决定更清楚；表达分歧并不必然破坏关系。",
    "今天没有安排社交，下午散步、整理房间、看了一会儿书。晚上精力恢复得很好，我需要的休息可能更多是安静和不被要求回应。",
    "回看这些天，反复出现的不是「我要更自律」，而是我在单一目标、连续时间和快速反馈下更容易行动。任务越抽象、承诺越模糊，我越容易拖延。",
    "这个月我完成了两次小交付，也更敢拒绝临时请求。下个月不想制定更大的计划，只想保持三个做法：上午先做最重要的一件事、尽早让真实用户看见、每周留一次安静回顾。"
)
$hours = @(21, 9, 20, 14, 22, 18, 21, 19, 17, 20, 21, 19, 18, 21, 20, 12, 18, 22, 19, 17, 20, 18, 21, 20, 22, 11, 19, 20, 21, 22)
$start = [DateTimeOffset]::Parse("2026-07-27T00:00:00+08:00")
$entries = @()
$existingPage = Send-ApiJson -Method GET -Path "/v1/entries?limit=100" -Headers $authHeaders
Require-Status $existingPage @(200) "initial entry listing"

for ($index = 0; $index -lt $contents.Count; $index++) {
    $capturedAt = $start.AddDays($index).AddHours($hours[$index]).ToString("O")
    $existing = @($existingPage.payload.items | Where-Object { $_.content -eq $contents[$index] }) |
        Select-Object -First 1
    if ($null -ne $existing) {
        $entries += [PSCustomObject]@{
            day = $index + 1
            id = [string]$existing.id
            revision = [int]$existing.revision
            captured_at = [string]$existing.captured_at
            content = $contents[$index]
        }
        continue
    }
    $entryKey = [Guid]::NewGuid().ToString()
    $entry = Send-ApiJson -Method POST -Path "/v1/entries" -Headers (
        $authHeaders + @{ "Idempotency-Key" = $entryKey }
    ) -Body @{
        content = $contents[$index]
        captured_at = $capturedAt
        memory_policy = "default"
        client_id = $entryKey
        source_type = "note"
        data_class = "sensitive"
    }
    Require-Status $entry @(201) "day $($index + 1) entry creation"
    $entries += [PSCustomObject]@{
        day = $index + 1
        id = [string]$entry.payload.id
        revision = [int]$entry.payload.revision
        captured_at = $capturedAt
        content = $contents[$index]
    }
}

$existingMemoryByDocument = @{}
$existingMemoryPage = Send-ApiJson -Method GET -Path "/v1/memory-inbox?limit=100" -Headers $authHeaders
Require-Status $existingMemoryPage @(200) "initial Memory inbox listing"
foreach ($item in $existingMemoryPage.payload.items) {
    $detail = Send-ApiJson -Method GET -Path "/v1/memories/$($item.memory_id)" -Headers $authHeaders
    Require-Status $detail @(200) "existing Memory detail"
    $anchors = @($detail.payload.evidence) + @($detail.payload.counterevidence) + @($detail.payload.contextual_evidence)
    foreach ($anchor in $anchors) {
        $documentId = [string]$anchor.source_document_id
        if (-not $existingMemoryByDocument.ContainsKey($documentId)) {
            $existingMemoryByDocument[$documentId] = [string]$item.memory_id
        }
    }
}

function New-Candidate {
    param([Parameter(Mandatory)]$Entry)
    if ($existingMemoryByDocument.ContainsKey([string]$Entry.id)) {
        return [PSCustomObject]@{
            day = $Entry.day
            entry = $Entry
            memory_id = $existingMemoryByDocument[[string]$Entry.id]
            run_id = "existing"
        }
    }
    $result = $null
    $headers = $null
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $key = [Guid]::NewGuid().ToString()
        $headers = $authHeaders + @{
            "Idempotency-Key" = $key
            "If-Match" = '"' + $Entry.revision + '"'
        }
        $result = Send-ApiJson -Method POST `
            -Path "/v1/entries/$($Entry.id)/candidate-insights" `
            -Headers $headers -Body @{}
        if ($result.response.StatusCode -eq 200) { break }
        if ($result.response.StatusCode -lt 500) { break }
    }
    Require-Status $result @(200) "day $($Entry.day) candidate generation"
    if ($result.payload.status -ne "succeeded" -or -not $result.payload.memory_id) {
        throw "Day $($Entry.day) did not produce a durable candidate Memory."
    }
    $replay = Send-ApiJson -Method POST `
        -Path "/v1/entries/$($Entry.id)/candidate-insights" `
        -Headers $headers -Body @{}
    Require-Status $replay @(200) "day $($Entry.day) candidate replay"
    if ($replay.payload.run_id -ne $result.payload.run_id -or
        $replay.payload.memory_id -ne $result.payload.memory_id) {
        throw "Day $($Entry.day) candidate idempotency failed."
    }
    return [PSCustomObject]@{
        day = $Entry.day
        entry = $Entry
        memory_id = [string]$result.payload.memory_id
        run_id = [string]$result.payload.run_id
    }
}

function Get-Memory {
    param([Parameter(Mandatory)][string]$MemoryId)
    $memory = Send-ApiJson -Method GET -Path "/v1/memories/$MemoryId" -Headers $authHeaders
    Require-Status $memory @(200) "Memory $MemoryId detail"
    return $memory
}

function Set-MemoryVerdict {
    param(
        [Parameter(Mandatory)]$Candidate,
        [Parameter(Mandatory)][string]$Verdict,
        [string]$Correction
    )
    $memory = Get-Memory -MemoryId $Candidate.memory_id
    $body = @{ verdict = $Verdict }
    if ($Verdict -eq "correct") {
        $body.replacement = @{
            statement = $Correction
            mode = "interpretation_error"
            confidence_band = "medium"
        }
    }
    $outcome = Send-ApiJson -Method POST `
        -Path "/v1/memories/$($Candidate.memory_id)/verdicts" `
        -Headers ($authHeaders + @{ "If-Match" = [string]$memory.response.Headers.ETag }) `
        -Body $body
    Require-Status $outcome @(201) "day $($Candidate.day) $Verdict verdict"
}

# Correction creates a new Source generation. Do it first, then generate all
# remaining candidates so their evidence snapshots stay current.
$candidateByDay = @{}
$expectedVerdictByDay = @{
    2 = "confirm"
    6 = "reject"
    8 = "confirm"
    13 = "correct"
    17 = "pending"
    23 = "snooze"
    27 = "pending"
    30 = "confirm"
}
$corrected = New-Candidate -Entry $entries[12]
$candidateByDay[13] = $corrected
$correction = "我正在练习按照精力和优先级做选择；拒绝额外请求不是冷漠，而是在保护已经答应的事情。"
Set-MemoryVerdict -Candidate $corrected -Verdict "correct" -Correction $correction

foreach ($day in @(2, 6, 8, 17, 23, 27, 30)) {
    $candidate = New-Candidate -Entry $entries[$day - 1]
    $candidateByDay[$day] = $candidate
    $verdict = $expectedVerdictByDay[$day]
    if ($verdict -ne "pending") {
        Set-MemoryVerdict -Candidate $candidate -Verdict $verdict
    }
}

$entryPage = Send-ApiJson -Method GET -Path "/v1/entries?limit=100" -Headers $authHeaders
Require-Status $entryPage @(200) "final entry listing"
$memoryPage = Send-ApiJson -Method GET -Path "/v1/memory-inbox?limit=100" -Headers $authHeaders
Require-Status $memoryPage @(200) "final Memory inbox listing"

$monthEntries = @($entryPage.payload.items | Where-Object { $_.content -in $contents })
$uniqueDates = @($monthEntries | ForEach-Object {
    [DateTimeOffset]::Parse($_.captured_at).ToOffset([TimeSpan]::FromHours(8)).Date
} | Sort-Object -Unique)
if ($monthEntries.Count -ne 30 -or $uniqueDates.Count -ne 30) {
    throw "Month continuity failed: $($monthEntries.Count) entries across $($uniqueDates.Count) unique dates."
}

$candidateResults = foreach ($day in @(2, 6, 8, 13, 17, 23, 27, 30)) {
    $candidate = $candidateByDay[$day]
    $memory = Get-Memory -MemoryId $candidate.memory_id
    $expectedVerdict = $expectedVerdictByDay[$day]
    if ($expectedVerdict -ne "pending" -and @($memory.payload.verdicts).Count -lt 1) {
        throw "Day $day is missing its persisted $expectedVerdict verdict."
    }
    $anchors = @($memory.payload.evidence) + @($memory.payload.counterevidence) + @($memory.payload.contextual_evidence)
    if ($anchors.Count -lt 1) {
        throw "Day $day candidate has no current Source evidence."
    }
    $excerpt = Send-ApiJson -Method GET `
        -Path "/v1/memories/$($candidate.memory_id)/evidence/$($anchors[0].id)/excerpt" `
        -Headers $authHeaders
    Require-Status $excerpt @(200) "day $day Evidence excerpt"
    $isVerbatim = $candidate.entry.content.Contains([string]$excerpt.payload.excerpt) -or
        $correction.Contains([string]$excerpt.payload.excerpt)
    if (-not $isVerbatim) {
        throw "Day $day Evidence excerpt is not verbatim Source text."
    }
    [PSCustomObject]@{
        day = $day
        verdict = $expectedVerdict
        statement = [string]$memory.payload.version.statement
        evidence = [string]$excerpt.payload.excerpt
        evidence_count = $anchors.Count
    }
}

if (@($memoryPage.payload.items).Count -ne 2) {
    throw "Memory inbox should contain exactly the 2 unresolved candidates."
}

$candidateResults | Format-Table day, verdict, statement, evidence_count -Wrap
Write-Host "PASS: 30 synthetic entries cover 30 consecutive days through the Source API."
Write-Host "PASS: 8 real governed-model candidates have current verbatim evidence and idempotent replay."
Write-Host "Verdicts: 3 confirm, 1 correct, 1 reject, 1 snooze, 2 pending."
