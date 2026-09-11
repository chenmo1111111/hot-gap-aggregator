$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$stateDir = if ($env:WANQING_STATE_DIR) { $env:WANQING_STATE_DIR } else { Join-Path $env:LOCALAPPDATA "hot-gap-aggregator\wanqing" }
$profileDir = if ($env:WANQING_PROFILE_DIR) { $env:WANQING_PROFILE_DIR } else { Join-Path $stateDir "browser-profile" }
$qiuzhaoSnapshot = Join-Path $stateDir "qiuzhao_wanqing.json"
$gongkaoSnapshot = Join-Path $stateDir "gongkao_sheet.json"
$qiuzhaoCandidate = Join-Path $stateDir "qiuzhao_wanqing.candidate.json"
$gongkaoCandidate = Join-Path $stateDir "gongkao_sheet.candidate.json"
$sshKey = if ($env:HOT_GAP_DEPLOY_KEY) { $env:HOT_GAP_DEPLOY_KEY } else { Join-Path $env:USERPROFILE ".ssh\hotgap_deploy" }
$sshHost = if ($env:HOT_GAP_DEPLOY_HOST) { $env:HOT_GAP_DEPLOY_HOST } else { "120.48.78.40" }
$sshUser = if ($env:HOT_GAP_DEPLOY_USER) { $env:HOT_GAP_DEPLOY_USER } else { "deploy" }
$logPath = Join-Path $stateDir "wanqing-sync.log"
$disappearanceLog = Join-Path $stateDir "disappeared-items.jsonl"
$syncState = Join-Path $stateDir "sync-state.json"
$barkSender = Join-Path $env:USERPROFILE ".bark\bark-send.ps1"

New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $sshKey)) { throw "SSH key not found: $sshKey" }

function Write-Log([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Output $line
}

# Windows PowerShell 5.1 treats a UTF-8 file without a BOM as the system ANSI
# code page.  Keep this runner ASCII-only and decode user-facing Chinese text
# at runtime so the scheduled task parses reliably on every Windows locale.
function ConvertFrom-Utf8Base64([string]$value) {
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value))
}

function Invoke-Capture([string]$label, [string]$kind, [string]$module, [string]$candidate, [string]$snapshot, [string]$incoming, [string[]]$extraArgs = @()) {
    Write-Log "starting $label capture"
    if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Force }
    Push-Location $projectRoot
    try {
        $captureOutput = & $python -m $module --output $candidate --profile-dir $profileDir --state-dir $stateDir @extraArgs 2>&1
        $captureCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $captureOutput | ForEach-Object { Write-Log ([string]$_) }
    if ($captureCode -ne 0) {
        Write-Log "$label capture FAILED with code $captureCode; server will retain its previous snapshot"
        return $false
    }

    Push-Location $projectRoot
    try {
        $validationOutput = & $python -m app.capture_monitor validate --kind $kind --candidate $candidate --stable $snapshot --disappearance-log $disappearanceLog 2>&1
        $validationCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $validationOutput | ForEach-Object { Write-Log "$label validation $_" }
    if ($validationCode -ne 0) {
        Write-Log "$label validation FAILED; previous snapshot retained"
        return $false
    }

    $remote = "$sshUser@$sshHost"
    & scp -q -i $sshKey -o BatchMode=yes $snapshot "${remote}:$incoming"
    if ($LASTEXITCODE -ne 0) {
        Write-Log "$label upload FAILED with code $LASTEXITCODE; server will retain its previous snapshot"
        return $false
    }
    Write-Log "$label capture and upload completed"
    return $true
}

function Send-FailureAlert([string]$reason) {
    Push-Location $projectRoot
    try {
        $stateJson = & $python -m app.capture_monitor state --path $syncState --reason $reason
        $state = $stateJson | ConvertFrom-Json
        $lastSuccess = if ($state.last_success) { [string]$state.last_success } else { ConvertFrom-Utf8Base64 "5LuO5pyq5oiQ5Yqf" }
        $title = if ([int]$state.consecutive_failures -ge 2) {
            ConvertFrom-Utf8Base64 "4pqg77iP5pWw5o2u5Y+v6IO95bey6L+H5pyf"
        } else {
            ConvertFrom-Utf8Base64 "6LSt5Lmw6KGo6Ieq5Yqo5oqT5Y+W5aSx6LSl"
        }
        $separator = ConvertFrom-Utf8Base64 "77yb6L+e57ut5aSx6LSlIA=="
        $lastSuccessLabel = ConvertFrom-Utf8Base64 "IOasoe+8m+S4iuasoeaIkOWKn++8mg=="
        $body = "$reason$separator$($state.consecutive_failures)$lastSuccessLabel$lastSuccess"
        $alertJson = & $python -m app.capture_monitor alert --title $title --message $body
        $alertJson | ForEach-Object { Write-Log "alert $_" }
        $localFeishuSent = $false
        try { $localFeishuSent = [bool](($alertJson | ConvertFrom-Json).feishu_sent) } catch {}
        if (-not $localFeishuSent) {
            $alertPath = Join-Path $stateDir "capture-alert.json"
            $alertPayload = @{ title = $title; message = $body } | ConvertTo-Json -Compress
            [IO.File]::WriteAllText($alertPath, $alertPayload, (New-Object Text.UTF8Encoding($false)))
            $remote = "$sshUser@$sshHost"
            & scp -q -i $sshKey -o BatchMode=yes $alertPath "${remote}:/home/deploy/.capture-alert.json.incoming"
            if ($LASTEXITCODE -eq 0) {
                & ssh -i $sshKey -o BatchMode=yes $remote "sudo /usr/local/sbin/hot-gap-feishu-refresh" | ForEach-Object { Write-Log "remote alert $_" }
            }
            if ($LASTEXITCODE -ne 0) { Write-Log "remote Feishu alert relay FAILED with code $LASTEXITCODE" }
        }
        if (Test-Path -LiteralPath $barkSender) {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $barkSender -Source Codex -Kind needs-input -Title $title -Body $body | Out-Null
            Write-Log "Bark alert requested"
        } else {
            Write-Log "Bark alert helper missing: $barkSender"
        }
    } finally {
        Pop-Location
    }
}

try {
    $env:CAPTURE_RUNNER_MANAGED = "1"
    $qiuzhaoOk = Invoke-Capture "Wanqing Qiuzhao" "qiuzhao" "app.capture_wanqing" $qiuzhaoCandidate $qiuzhaoSnapshot "/home/deploy/.qiuzhao_wanqing.json.incoming"
    $gongkaoOk = Invoke-Capture "Feishu Sheet Gongkao" "gongkao" "app.capture_gongkao_sheet" $gongkaoCandidate $gongkaoSnapshot "/home/deploy/.gongkao_sheet.json.incoming"

    if (-not $qiuzhaoOk -and -not $gongkaoOk) {
        throw "both purchased-table captures failed; no server refresh attempted"
    }

    $remote = "$sshUser@$sshHost"
    & ssh -i $sshKey -o BatchMode=yes $remote "sudo /usr/local/sbin/hot-gap-feishu-refresh"
    if ($LASTEXITCODE -ne 0) { throw "remote refresh exited with code $LASTEXITCODE" }
    Write-Log "server export and Feishu refresh completed"

    if (-not $qiuzhaoOk -or -not $gongkaoOk) {
        throw "one or more sources failed; successful sources were refreshed and failed sources retained their previous snapshots"
    }
    Push-Location $projectRoot
    try {
        & $python -m app.capture_monitor state --path $syncState --success | ForEach-Object { Write-Log "state $_" }
    } finally {
        Pop-Location
    }
} catch {
    $reason = [string]$_.Exception.Message
    Write-Log "FAILED: $reason"
    Send-FailureAlert $reason
    exit 1
}
