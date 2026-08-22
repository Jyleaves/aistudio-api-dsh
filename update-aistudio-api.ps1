#Requires -Version 5.1

[CmdletBinding()]
param(
    [int]$Port = 8090,
    [switch]$Restart,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$root = (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"
$startScript = Join-Path $root "start-aistudio-api.ps1"

function Get-OwnListenerIds {
    $ids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($id in $ids) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        if ($process.CommandLine -and $process.CommandLine -like "*$root*" -and
            $process.CommandLine -like "*main.py*" -and $process.CommandLine -like "*server*") {
            $id
        }
    }
}

Set-Location -LiteralPath $root
if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) {
    throw "This directory is not a Git checkout: $root"
}

$dirty = @(git status --porcelain)
if ($dirty.Count -gt 0) {
    Write-Host "Uncommitted changes found; update stopped to avoid overwriting them." -ForegroundColor Yellow
    $dirty | Select-Object -First 20 | ForEach-Object { Write-Host "  $_" }
    Write-Host "Commit or back up the changes, then run the update again. .env, data and .venv are not tracked by Git."
    exit 2
}

$current = (git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null)
if (-not $current) {
    throw "The current branch has no upstream Git branch."
}

if ($CheckOnly) {
    Write-Host "Update check passed: $current" -ForegroundColor Green
    exit 0
}

$listenerIds = @(Get-OwnListenerIds)
if ($listenerIds.Count -gt 0 -and -not $Restart) {
    throw "The proxy is running. Stop it first, or use -Restart to restart it automatically."
}
if ($listenerIds.Count -gt 0) {
    $listenerIds | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}

Write-Host "Fetching updates..." -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "Git update failed." }

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found: $python. Install it according to README first."
}

Write-Host "Syncing virtual-environment dependencies..." -ForegroundColor Cyan
& $python -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency update failed." }

Write-Host "Update complete; .env, data and browser configuration were preserved." -ForegroundColor Green
if ($Restart) {
    & $startScript -Port $Port
}
