#Requires -Version 5.1

[CmdletBinding()]
param(
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"
$root = (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "[aistudio-api] Project: $root" -ForegroundColor DarkGray
Write-Host "[aistudio-api] Checking port $Port ..." -ForegroundColor DarkGray

function Get-ListenerProcessIds {
    $ids = @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($ids.Count -eq 0) {
        # Get-NetTCPConnection can return no data for a listener owned by a
        # different elevation context. netstat is a compatible fallback.
        foreach ($line in @(netstat -ano -p tcp 2>$null)) {
            if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                $ids += [int]$matches[1]
            }
        }
    }
    @($ids | Sort-Object -Unique)
}

function Get-ProcessTree([int[]]$ids) {
    $all = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    foreach ($id in $ids) {
        if ($id -gt 0 -and $all.Add($id)) { $queue.Enqueue($id) }
    }
    while ($queue.Count -gt 0) {
        $id = $queue.Dequeue()
        try {
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction Stop)
        } catch {
            $children = @()
        }
        foreach ($child in $children) {
            $childId = [int]$child.ProcessId
            if ($all.Add($childId)) { $queue.Enqueue($childId) }
        }
    }
    return @($all)
}

function Get-ProcessInfo([int[]]$ids) {
    foreach ($id in $ids) {
        try {
            $info = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction Stop
        } catch {
            $info = $null
        }
        if ($info) { $info }
    }
}

$listenerIds = Get-ListenerProcessIds
if ($listenerIds.Count -gt 0) {
    Write-Host "[aistudio-api] Port $Port is already in use; inspecting the existing process ..." -ForegroundColor Yellow
    $treeIds = Get-ProcessTree $listenerIds
    $processes = @(Get-ProcessInfo $treeIds)
    $ownProcess = $processes | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like "*$root*" -and
        $_.CommandLine -like "*main.py*" -and
        $_.CommandLine -like "*server*" -and
        $_.CommandLine -like "*--port $Port*"
    }

    if (-not $ownProcess) {
        Write-Host "Port $Port is already occupied by another process." -ForegroundColor Red
        Write-Host "The process details are unavailable or it is not an aistudio-api instance." -ForegroundColor Yellow
        $processes | Select-Object ProcessId,Name,CommandLine | Format-Table -Wrap
        Write-Host "The script will not stop an unrelated process."
        exit 1
    }

    Write-Host "This aistudio-api instance is already running on port $Port." -ForegroundColor Yellow
    $answer = Read-Host "Stop it and start a new instance? [Y/N]"
    if ($answer -notmatch '^(?i:y|yes)$') {
        Write-Host "No changes made."
        exit 0
    }

    foreach ($id in $treeIds) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 20; $i++) {
        if ((Get-ListenerProcessIds).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    if ((Get-ListenerProcessIds).Count -gt 0) {
        Write-Host "The old instance did not release port $Port." -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Virtual environment not found: $python" -ForegroundColor Red
    exit 1
}

# 后台浏览器探测：找不到任何可用浏览器时给出安装指引（不自动下载，
# 避免需要代理的下载卡住启动）。
$probeCode = "import sys; sys.path.insert(0, r'$root\src'); from aistudio_api.infrastructure.browser.browser_engine import detect_background_browser as d; f = d(); print(('OK ' + f['kind']) if f else 'MISSING')"
$probeResult = (& $python -c $probeCode) 2>$null
if ($LASTEXITCODE -ne 0 -or -not $probeResult -or $probeResult -eq "MISSING") {
    Write-Host "[aistudio-api] No usable browser found." -ForegroundColor Yellow
    Write-Host "  Recommended (stable Chromium, ~130MB):" -ForegroundColor Yellow
    Write-Host "    .\.venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Cyan
    Write-Host "  Or install Google Chrome, or use the system Edge. Starting anyway..." -ForegroundColor Yellow
} else {
    Write-Host "[aistudio-api] Browser: $probeResult" -ForegroundColor DarkGray
}

Set-Location -LiteralPath $root
Write-Host "Starting aistudio-api on http://127.0.0.1:$Port ..." -ForegroundColor Green
Write-Host "Close this window or press Ctrl+C to stop the service."
& $python (Join-Path $root "main.py") server --port $Port
exit $LASTEXITCODE
