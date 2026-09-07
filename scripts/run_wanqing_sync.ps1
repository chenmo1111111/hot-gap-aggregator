$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$stateDir = if ($env:WANQING_STATE_DIR) { $env:WANQING_STATE_DIR } else { Join-Path $env:LOCALAPPDATA "hot-gap-aggregator\wanqing" }
$profileDir = if ($env:WANQING_PROFILE_DIR) { $env:WANQING_PROFILE_DIR } else { Join-Path $stateDir "browser-profile" }
$qiuzhaoSnapshot = Join-Path $stateDir "qiuzhao_wanqing.json"
$gongkaoSnapshot = Join-Path $stateDir "gongkao_sheet.json"
$sshKey = if ($env:HOT_GAP_DEPLOY_KEY) { $env:HOT_GAP_DEPLOY_KEY } else { Join-Path $env:USERPROFILE ".ssh\hotgap_deploy" }
$sshHost = if ($env:HOT_GAP_DEPLOY_HOST) { $env:HOT_GAP_DEPLOY_HOST } else { "120.48.78.40" }
$sshUser = if ($env:HOT_GAP_DEPLOY_USER) { $env:HOT_GAP_DEPLOY_USER } else { "deploy" }
$logPath = Join-Path $stateDir "wanqing-sync.log"

New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $sshKey)) { throw "SSH key not found: $sshKey" }

function Write-Log([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Invoke-Capture([string]$label, [string]$module, [string]$snapshot, [string]$incoming) {
    Write-Log "starting $label capture"
    Push-Location $projectRoot
    try {
        $captureOutput = & $python -m $module --output $snapshot --profile-dir $profileDir --state-dir $stateDir 2>&1
        $captureCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $captureOutput | ForEach-Object { Write-Log ([string]$_) }
    if ($captureCode -ne 0) {
        Write-Log "$label capture FAILED with code $captureCode; server will retain its previous snapshot"
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

try {
    $qiuzhaoOk = Invoke-Capture "Wanqing Qiuzhao" "app.capture_wanqing" $qiuzhaoSnapshot "/home/deploy/.qiuzhao_wanqing.json.incoming"
    $gongkaoOk = Invoke-Capture "Feishu Sheet Gongkao" "app.capture_gongkao_sheet" $gongkaoSnapshot "/home/deploy/.gongkao_sheet.json.incoming"

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
} catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
