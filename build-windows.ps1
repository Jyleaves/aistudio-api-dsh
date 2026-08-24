# Build the Windows desktop app and its selectable-location installer.
[CmdletBinding()]
param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$projectVersion = [regex]::Match((Get-Content (Join-Path $root "pyproject.toml") -Raw), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $projectVersion) { throw "Unable to read project version from pyproject.toml" }
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
    # Remove only legacy generated runtime folders from older builds. Keeping
    # the staging name aligned with the product avoids exposing the old project
    # name in direct, unpacked releases.
    $distRoot = [IO.Path]::GetFullPath((Join-Path $root "dist"))
    foreach ($legacyRelativePath in @("dist\aistudio-api", "dist\aistudio-api-update")) {
        $legacyOutput = [IO.Path]::GetFullPath((Join-Path $root $legacyRelativePath))
        if (-not $legacyOutput.StartsWith($distRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove build output outside dist: $legacyOutput"
        }
        if (Test-Path -LiteralPath $legacyOutput) {
            Remove-Item -LiteralPath $legacyOutput -Recurse -Force
        }
    }

    # Keep PyInstaller inside the virtualenv. A locked user-site directory on
    # some Windows machines otherwise causes a WinError 5 during dependency
    # scanning.
    $env:PYTHONNOUSERSITE = "1"
    & $python -m PyInstaller --noconfirm --clean aistudio-api.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    # Ship only the full Playwright Chromium browser. Keep the build source
    # copy in the project directory so builds do not depend on a user cache.
    $projectBrowserRoot = Join-Path $root "cloakbrowser-chromium"
    $bundledBrowserRoot = Join-Path $root "dist\Asteria\cloakbrowser-chromium"
    $projectChrome = Get-ChildItem $projectBrowserRoot -Recurse -Filter "chrome.exe" -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $projectChrome) {
        $browserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
        $chromium = Get-ChildItem $browserRoot -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
        if (-not $chromium) {
            throw "Playwright Chromium not found. Run: $python -m playwright install chromium"
        }
        if (Test-Path $projectBrowserRoot) { Remove-Item -LiteralPath $projectBrowserRoot -Recurse -Force }
        New-Item -ItemType Directory -Path $projectBrowserRoot -Force | Out-Null
        Get-ChildItem -LiteralPath $chromium.FullName -Force |
            Copy-Item -Destination $projectBrowserRoot -Recurse -Force
    }
    if (Test-Path $bundledBrowserRoot) { Remove-Item -LiteralPath $bundledBrowserRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $bundledBrowserRoot -Force | Out-Null
    Get-ChildItem -LiteralPath $projectBrowserRoot -Force |
        Copy-Item -Destination $bundledBrowserRoot -Recurse -Force

    # Build a second staging directory for existing users. It contains the
    # application only and intentionally reuses the installed browser.
    $updateRoot = Join-Path $root "dist\Asteria-update"
    if (Test-Path $updateRoot) { Remove-Item -LiteralPath $updateRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $updateRoot -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $root "dist\Asteria") -Force |
        Where-Object { $_.Name -ne "cloakbrowser-chromium" } |
        Copy-Item -Destination $updateRoot -Recurse -Force

    if (-not $SkipInstaller) {
    if (-not (Test-Path $iscc)) {
        throw "Inno Setup compiler not found. Install Inno Setup 7 or use -SkipInstaller."
    }
    Write-Host "Using Inno Setup: $iscc"
        & $iscc "/DMyAppVersion=$projectVersion" (Join-Path $root "installer.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
        & $iscc "/DMyAppVersion=$projectVersion" (Join-Path $root "installer-update.iss")
        if ($LASTEXITCODE -ne 0) { throw "Incremental Inno Setup failed with exit code $LASTEXITCODE" }

        foreach ($package in @(
            (Join-Path $root "dist\Asteria-setup-$projectVersion.exe"),
            (Join-Path $root "dist\Asteria-update-$projectVersion.exe")
        )) {
            if (-not (Test-Path $package)) { throw "Expected installer was not created: $package" }
            $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToLowerInvariant()
            Set-Content -LiteralPath "$package.sha256" -Value "$hash *$([IO.Path]::GetFileName($package))" -Encoding ascii
        }
    }

    Get-ChildItem (Join-Path $root "dist") -File |
        Where-Object { $_.Name -like "Asteria-setup-*.exe" -or $_.Name -like "Asteria-update-*.exe" } |
        Select-Object Name, Length, LastWriteTime
} finally {
    Pop-Location
}
