# Build the Windows desktop app and its selectable-location installer.
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$UpdateOnly
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

    if (-not $UpdateOnly) {
        # Ship the pinned CloakBrowser runtime exactly as supplied. Google login
        # compatibility depends on this browser build, so never substitute a
        # Playwright-downloaded Chromium or prune files from the runtime.
    $projectBrowserRoot = Join-Path $root "cloakbrowser-chromium"
    $bundledBrowserRoot = Join-Path $root "dist\Asteria\cloakbrowser-chromium"
    $projectBrowserFullPath = [IO.Path]::GetFullPath($projectBrowserRoot)
    $projectRootFullPath = [IO.Path]::GetFullPath($root)
    if (-not $projectBrowserFullPath.StartsWith($projectRootFullPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to read browser runtime outside project: $projectBrowserFullPath"
    }
    $browserLockPath = Join-Path $root "browser-runtime.json"
    if (-not (Test-Path -LiteralPath $browserLockPath)) {
        throw "Pinned CloakBrowser metadata not found: $browserLockPath"
    }
    $browserLock = Get-Content -LiteralPath $browserLockPath -Raw | ConvertFrom-Json
    if (-not $browserLock.chrome_version -or -not $browserLock.chrome_sha256) {
        throw "Pinned CloakBrowser metadata is incomplete: $browserLockPath"
    }
    $projectChromePath = Join-Path $projectBrowserRoot "chrome.exe"
    if (-not (Test-Path -LiteralPath $projectChromePath -PathType Leaf)) {
        throw "Pinned CloakBrowser chrome.exe not found: $projectChromePath"
    }
    $projectChrome = Get-Item -LiteralPath $projectChromePath
    $actualBrowserVersion = $projectChrome.VersionInfo.ProductVersion
    $actualBrowserHash = (Get-FileHash -LiteralPath $projectChromePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualBrowserVersion -ne $browserLock.chrome_version -or $actualBrowserHash -ne $browserLock.chrome_sha256) {
        throw "CloakBrowser runtime does not match browser-runtime.json (version=$actualBrowserVersion sha256=$actualBrowserHash)"
    }
    if (Test-Path $bundledBrowserRoot) { Remove-Item -LiteralPath $bundledBrowserRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $bundledBrowserRoot -Force | Out-Null
    Get-ChildItem -LiteralPath $projectBrowserRoot -Force |
        Copy-Item -Destination $bundledBrowserRoot -Recurse -Force
    }

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
        $packages = @()
        if (-not $UpdateOnly) {
            & $iscc "/DMyAppVersion=$projectVersion" (Join-Path $root "installer.iss")
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
            $packages += Join-Path $root "dist\Asteria-setup-$projectVersion.exe"
        }
        & $iscc "/DMyAppVersion=$projectVersion" (Join-Path $root "installer-update.iss")
        if ($LASTEXITCODE -ne 0) { throw "Incremental Inno Setup failed with exit code $LASTEXITCODE" }
        $packages += Join-Path $root "dist\Asteria-update-$projectVersion.exe"

        foreach ($package in $packages) {
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
