# Build the Windows desktop app and its selectable-location installer.
[CmdletBinding()]
param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$iscc = if ($env:ISCC_PATH -and (Test-Path $env:ISCC_PATH)) {
    $env:ISCC_PATH
} elseif (Get-Command ISCC.exe -ErrorAction SilentlyContinue) {
    (Get-Command ISCC.exe).Source
} else {
    @(
    "C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 7\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not (Test-Path $python)) {
    throw "Python virtual environment not found: $python"
}

Push-Location $root
try {
    # Keep PyInstaller inside the virtualenv. A locked user-site directory on
    # some Windows machines otherwise causes a WinError 5 during dependency
    # scanning.
    $env:PYTHONNOUSERSITE = "1"
    & $python -m PyInstaller --noconfirm --clean aistudio-api.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    # Ship only the full Playwright Chromium browser. Do not include the
    # headless shell, ffmpeg, or other browser downloads.
    $browserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
    $chromium = Get-ChildItem $browserRoot -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $chromium) {
        throw "Playwright Chromium not found at $browserRoot. Run: $python -m playwright install chromium"
    }
    $bundledBrowserRoot = Join-Path $root "dist\aistudio-api\playwright-browsers\chromium-bundled"
    if (Test-Path $bundledBrowserRoot) { Remove-Item -LiteralPath $bundledBrowserRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $bundledBrowserRoot -Force | Out-Null
    Get-ChildItem -LiteralPath $chromium.FullName -Force |
        Copy-Item -Destination $bundledBrowserRoot -Recurse -Force

    if (-not $SkipInstaller) {
    if (-not (Test-Path $iscc)) {
        throw "Inno Setup compiler not found. Install Inno Setup 7 or use -SkipInstaller."
    }
    Write-Host "Using Inno Setup: $iscc"
        & $iscc installer.iss
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
    }

    Get-ChildItem (Join-Path $root "dist") -File |
        Where-Object { $_.Name -like "Asteria-setup-*.exe" } |
        Select-Object Name, Length, LastWriteTime
} finally {
    Pop-Location
}
