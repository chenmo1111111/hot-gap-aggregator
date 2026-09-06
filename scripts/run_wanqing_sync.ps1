$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$stateDir = if ($env:WANQING_STATE_DIR) { $env:WANQING_STATE_DIR } else { Join-Path $env:LOCALAPPDATA "hot-gap-aggregator\wanqing" }
$profileDir = if ($env:WANQING_PROFILE_DIR) { $env:WANQING_PROFILE_DIR } else { Join-Path $stateDir "browser-profile" }
$snapshot = Join-Path $stateDir "qiuzhao_wanqing.json"
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

try {
    Write-Log "starting Wanqing capture"
    Push-Location $projectRoot
    try {
        $captureOutput = & $python -m app.capture_wanqing --output $snapshot --profile-dir $profileDir --state-dir $stateDir 2>&1
        $captureCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $captureOutput | ForEach-Object { Write-Log ([string]$_) }
    if ($captureCode -ne 0) { throw "capture exited with code $captureCode" }

    $remote = "$sshUser@$sshHost"
    $incoming = "/home/deploy/.qiuzhao_wanqing.json.incoming"
    & scp -q -i $sshKey -o BatchMode=yes $snapshot "${remote}:$incoming"
    if ($LASTEXITCODE -ne 0) { throw "scp exited with code $LASTEXITCODE" }
    & ssh -i $sshKey -o BatchMode=yes $remote "sudo /usr/local/sbin/hot-gap-feishu-refresh"
    if ($LASTEXITCODE -ne 0) { throw "remote refresh exited with code $LASTEXITCODE" }
    Write-Log "capture, upload and Feishu refresh completed"
} catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
