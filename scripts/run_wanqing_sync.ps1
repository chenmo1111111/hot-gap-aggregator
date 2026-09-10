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

function Invoke-Capture([string]$label, [string]$kind, [string]$module, [string]$candidate, [string]$snapshot, [string]$incoming) {
    Write-Log "starting $label capture"
    if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Force }
    Push-Location $projectRoot
    try {
        $captureOutput = & $python -m $module --output $candidate --profile-dir $profileDir --state-dir $stateDir 2>&1
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
        $lastSuccess = if ($state.last_success) { [string]$state.last_success } else { "从未成功" }
        $title = if ([int]$state.consecutive_failures -ge 2) { "⚠️数据可能已过期" } else { "购买表自动抓取失败" }
        $body = "$reason；连续失败 $($state.consecutive_failures) 次；上次成功：$lastSuccess"
        & $python -m app.capture_monitor alert --title $title --message $body | ForEach-Object { Write-Log "alert $_" }
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
        throw "both captures failed; no server refresh attempted"
    }

    $remote = "$sshUser@$sshHost"
    & ssh -i $sshKey -o BatchMode=yes $remote "sudo /usr/local/sbin/hot-gap-feishu-refresh"
    if ($LASTEXITCODE -ne 0) { throw "remote refresh exited with code $LASTEXITCODE" }
    Write-Log "server export and Feishu refresh completed"

    if (-not $qiuzhaoOk -or -not $gongkaoOk) {
        throw "one source failed; the successful source was refreshed and the failed source retained its previous snapshot"
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
