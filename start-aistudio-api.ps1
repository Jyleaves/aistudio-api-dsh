#Requires -Version 5.1

[CmdletBinding()]
param(
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"
$root = (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"

function Get-ListenerProcessIds {
    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-ProcessTree([int[]]$ids) {
    $all = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    foreach ($id in $ids) {
        if ($id -gt 0 -and $all.Add($id)) { $queue.Enqueue($id) }
    }
    while ($queue.Count -gt 0) {
        $id = $queue.Dequeue()
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue)
        foreach ($child in $children) {
            $childId = [int]$child.ProcessId
            if ($all.Add($childId)) { $queue.Enqueue($childId) }
        }
    }
    return @($all)
}

function Get-ProcessInfo([int[]]$ids) {
    foreach ($id in $ids) {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        if ($info) { $info }
    }
}

$listenerIds = Get-ListenerProcessIds
if ($listenerIds.Count -gt 0) {
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

Set-Location -LiteralPath $root
Write-Host "Starting aistudio-api on http://127.0.0.1:$Port ..." -ForegroundColor Green
Write-Host "Close this window or press Ctrl+C to stop the service."
& $python (Join-Path $root "main.py") server --port $Port
exit $LASTEXITCODE
