param(
    [switch]$SkipInstall,
    [switch]$SkipFrontendBuild,
    [switch]$SkipSmoke,
    [switch]$AllowUncommittedBuild,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Invoke-Git {
    param([string[]]$Arguments)

    $output = & git -C $root @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)"
    }
    return $output
}

function Require-Match {
    param(
        [string]$Label,
        [string]$Content,
        [string]$Pattern
    )

    if ($Content -notmatch $Pattern) {
        throw "$Label is not aligned with release version $releaseVersion."
    }
}

function Assert-OfficialIdentity {
    param(
        [string]$ExpectedHead,
        [string]$ExpectedTag
    )

    $currentHead = ((Invoke-Git @('rev-parse', 'HEAD')) |
        Select-Object -First 1).Trim()
    $currentStatus = @(Invoke-Git @('status', '--porcelain', '--untracked-files=normal'))
    if ($currentHead -ne $ExpectedHead -or $currentStatus.Count -gt 0) {
        throw (
            "Official build identity changed during the release process. " +
            "Expected clean $ExpectedHead; found HEAD $currentHead with " +
            "$($currentStatus.Count) worktree change(s)."
        )
    }
    $tagType = ((Invoke-Git @('cat-file', '-t', $ExpectedTag)) |
        Select-Object -First 1).Trim()
    if ($tagType -ne 'tag') {
        throw "$ExpectedTag must exist locally as an annotated tag."
    }
    $tagCommit = ((Invoke-Git @('rev-parse', "$ExpectedTag^{commit}")) |
        Select-Object -First 1).Trim()
    if ($tagCommit -ne $ExpectedHead) {
        throw "$ExpectedTag points to $tagCommit, but the release commit is $ExpectedHead."
    }
}

# Release identity preflight. This deliberately runs before pip/npm/build writes
# so a dirty tree, stale source offer, or split version cannot produce an
# official-looking archive.
$portableReadmePath = Join-Path $root "packaging\README_PORTABLE_USER.txt"
$portableReadme = Get-Content -LiteralPath $portableReadmePath -Raw
$versionMatch = [regex]::Match(
    $portableReadme,
    '(?m)^Version:\s*(v\d+\.\d+\.\d+)\s*$'
)
if (-not $versionMatch.Success) {
    throw "README_PORTABLE_USER.txt must contain 'Version: vMAJOR.MINOR.PATCH'."
}
$releaseTag = $versionMatch.Groups[1].Value
$releaseVersion = $releaseTag.Substring(1)
$versionParts = $releaseVersion.Split('.')
$expectedSource = "https://github.com/ai-coding-lab-au/open-accounting/tree/$releaseTag"
$sourceMatch = [regex]::Match($portableReadme, '(?m)^Built from:\s*(\S+)\s*$')
if (-not $sourceMatch.Success -or $sourceMatch.Groups[1].Value -ne $expectedSource) {
    throw "README source offer must be exactly: Built from: $expectedSource"
}

$escapedVersion = [regex]::Escape($releaseVersion)
$backendProject = Get-Content -LiteralPath (Join-Path $root "backend\pyproject.toml") -Raw
$backendMain = Get-Content -LiteralPath (Join-Path $root "backend\app\main.py") -Raw
$frontendPackage = Get-Content -LiteralPath (Join-Path $root "frontend\package.json") -Raw |
    ConvertFrom-Json
$frontendLockPath = Join-Path $root "frontend\package-lock.json"
$lockVersions = @(& python -c (
    "import json, pathlib, sys; " +
    "data=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); " +
    "print(data['version']); print(data['packages']['']['version'])"
) $frontendLockPath)
if ($LASTEXITCODE -ne 0 -or $lockVersions.Count -ne 2) {
    throw "Could not read the two root versions from frontend/package-lock.json."
}
$windowsVersionPath = Join-Path $root "packaging\windows_version_info.txt"
$windowsVersion = Get-Content -LiteralPath $windowsVersionPath -Raw
$pyInstallerSpec = Get-Content -LiteralPath (Join-Path $root "packaging\open-accounting.spec") -Raw

Require-Match "backend/pyproject.toml" $backendProject "(?m)^version\s*=\s*`"$escapedVersion`"\s*$"
Require-Match "backend/app/main.py" $backendMain "version\s*=\s*`"$escapedVersion`""
if ($frontendPackage.version -ne $releaseVersion) {
    throw "frontend/package.json version must be $releaseVersion."
}
if ($lockVersions[0] -ne $releaseVersion -or $lockVersions[1] -ne $releaseVersion) {
    throw "frontend/package-lock.json root versions must both be $releaseVersion."
}
Require-Match "Windows FileVersion" $windowsVersion (
    "StringStruct\(u`"FileVersion`",\s*u`"$escapedVersion\.0`"\)"
)
Require-Match "Windows ProductVersion" $windowsVersion (
    "StringStruct\(u`"ProductVersion`",\s*u`"$escapedVersion`"\)"
)
$fixedVersionPattern = (
    "$($versionParts[0]),\s*$($versionParts[1]),\s*" +
    "$($versionParts[2]),\s*0"
)
Require-Match "Windows fixed file version" $windowsVersion (
    "filevers=\($fixedVersionPattern\)"
)
Require-Match "Windows fixed product version" $windowsVersion (
    "prodvers=\($fixedVersionPattern\)"
)
if ($pyInstallerSpec -notmatch 'windows_version_info\.txt') {
    throw "open-accounting.spec must embed packaging/windows_version_info.txt."
}

if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess) {
    throw "The windows-x64 release must be built in 64-bit Windows PowerShell."
}
$architecture = @(& python -c (
    "import platform, struct; " +
    "print(platform.machine()); print(struct.calcsize('P') * 8)"
))
if ($LASTEXITCODE -ne 0 -or $architecture.Count -ne 2) {
    throw "Could not determine the Python build architecture."
}
$pythonMachine = $architecture[0].Trim()
$pythonPointerBits = $architecture[1].Trim()
if ($pythonPointerBits -ne '64' -or $pythonMachine -notmatch '^(AMD64|x86_64)$') {
    throw (
        "This script names the artifact windows-x64 and therefore requires " +
        "64-bit x86 Python; found $pythonMachine/$pythonPointerBits-bit."
    )
}

$head = ((Invoke-Git @('rev-parse', 'HEAD')) | Select-Object -First 1).Trim()
$headShort = $head.Substring(0, 12)
$statusLines = @(Invoke-Git @('status', '--porcelain', '--untracked-files=normal'))
$worktreeDirty = $statusLines.Count -gt 0

if ($AllowUncommittedBuild) {
    Write-Warning (
        "Building an UNPUBLISHABLE local candidate from commit $headShort with " +
        "uncommitted changes. The artifact name will include 'candidate'."
    )
}
else {
    if ($SkipInstall -or $SkipFrontendBuild -or $SkipSmoke) {
        throw "Official portable builds cannot use -SkipInstall, -SkipFrontendBuild, or -SkipSmoke."
    }
    if ($worktreeDirty) {
        throw (
            "Official portable builds require a clean Git worktree. " +
            "Commit the release metadata and all source changes first, or use " +
            "-AllowUncommittedBuild for an explicitly unpublishable candidate."
        )
    }
    Assert-OfficialIdentity $head $releaseTag
}

Write-Host "Release identity: $releaseTag ($head)"
$modeLabel = if ($AllowUncommittedBuild) { 'LOCAL CANDIDATE' } else { 'OFFICIAL' }
Write-Host "Build mode:       $modeLabel"
if ($PreflightOnly) {
    Write-Host "Release preflight passed; no install or build steps were run."
    return
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stageRoot = Join-Path $root "release"
$artifactName = if ($AllowUncommittedBuild) {
    "OpenAccounting-portable-$releaseTag-windows-x64-candidate-$headShort-$stamp"
}
else {
    "OpenAccounting-portable-$releaseTag-windows-x64"
}
$stage = Join-Path $stageRoot $artifactName
$zip = "$stage.zip"
$checksumPath = "$zip.sha256"
if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $zip) -or
    (Test-Path -LiteralPath $checksumPath)) {
    throw "Release output already exists for $artifactName; move or remove it before rebuilding."
}

if (-not $SkipInstall) {
    # No [pdf] extra: the portable build deliberately ships the ReportLab
    # renderer (bundling playwright without a browser is dead weight, and the
    # spec excludes it either way).
    python -m pip install -e "$root\backend"
    if ($LASTEXITCODE -ne 0) { throw "Backend package installation failed." }
    python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }
}

if (-not $SkipFrontendBuild) {
    $frontendDist = Join-Path $root "frontend\dist"
    if (Test-Path -LiteralPath $frontendDist) {
        Remove-Item -LiteralPath $frontendDist -Recurse -Force
    }
    Push-Location "$root\frontend"
    try {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend production build failed." }
    }
    finally {
        Pop-Location
    }
}

$output = Join-Path $root "dist\OpenAccounting"
if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Recurse -Force
}
Push-Location $root
try {
    python -m PyInstaller --clean --noconfirm packaging\open-accounting.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
finally {
    Pop-Location
}

$exePath = Join-Path $output "OpenAccounting.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "PyInstaller did not produce $exePath."
}
$exeVersion = (Get-Item -LiteralPath $exePath).VersionInfo
if ($exeVersion.FileVersion -ne "$releaseVersion.0" -or
    $exeVersion.ProductVersion -ne $releaseVersion) {
    throw (
        "Built EXE version mismatch: expected $releaseVersion.0/$releaseVersion, " +
        "found $($exeVersion.FileVersion)/$($exeVersion.ProductVersion)."
    )
}

# Post-build smoke: boots the exe against a throwaway data dir and exercises
# health, frontend, API 404, and PDF rendering. A build that silently lost
# reportlab or frontend-dist fails here, not on a user's machine.
if (-not $SkipSmoke) {
    python "$root\packaging\smoke_portable.py" $exePath --expected-version $releaseVersion
    if ($LASTEXITCODE -ne 0) { throw "Portable smoke test failed" }
}

if (-not $AllowUncommittedBuild) {
    Assert-OfficialIdentity $head $releaseTag
}

# Stage the release zip: exe folder + LICENSE + user README + third-party
# notices. These compliance files are REQUIRED in every distributed zip
# (AGPL source offer + bundled third-party license texts) - keep this step
# scripted so they can't be forgotten.
New-Item -ItemType Directory -Force $stage | Out-Null

Copy-Item -Recurse $output (Join-Path $stage "OpenAccounting")
Copy-Item (Join-Path $root "LICENSE") (Join-Path $stage "LICENSE.txt")
Copy-Item (Join-Path $root "packaging\README_PORTABLE_USER.txt") (Join-Path $stage "README_FIRST.txt")
if ($AllowUncommittedBuild) {
    $candidateReadme = Join-Path $stage "README_FIRST.txt"
    $candidateWarning = @(
        "UNPUBLISHABLE LOCAL CANDIDATE - DO NOT DISTRIBUTE"
        "Built from a worktree without official clean-tag enforcement."
        ""
    )
    $candidateWarning + (Get-Content -LiteralPath $candidateReadme) |
        Set-Content -LiteralPath $candidateReadme -Encoding UTF8
}
python "$root\packaging\generate_third_party_notices.py" --output (Join-Path $stage "THIRD-PARTY-NOTICES.txt")
if ($LASTEXITCODE -ne 0) { throw "Third-party notices generation failed" }

$pythonVersion = (& python --version 2>&1 | Select-Object -First 1)
if ($LASTEXITCODE -ne 0) { throw "Could not record the Python version." }
$pyInstallerVersion = (& python -m PyInstaller --version 2>&1 | Select-Object -First 1)
if ($LASTEXITCODE -ne 0) { throw "Could not record the PyInstaller version." }
$nodeVersion = (& node --version 2>&1 | Select-Object -First 1)
if ($LASTEXITCODE -ne 0) { throw "Could not record the Node version." }
$npmVersion = (& npm.cmd --version 2>&1 | Select-Object -First 1)
if ($LASTEXITCODE -ne 0) { throw "Could not record the npm version." }
$buildMode = if ($AllowUncommittedBuild) { "LOCAL CANDIDATE - DO NOT PUBLISH" } else { "OFFICIAL" }
$buildInfo = @(
    "Open Accounting portable build information"
    "Version: $releaseTag"
    "Commit: $head"
    "Build mode: $buildMode"
    "Worktree clean at preflight: $(-not $worktreeDirty)"
    "Built UTC: $([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    "Python: $pythonVersion"
    "PyInstaller: $pyInstallerVersion"
    "Architecture: $pythonMachine/$pythonPointerBits-bit"
    "Node: $nodeVersion"
    "npm: $npmVersion"
)
$buildInfo | Set-Content -LiteralPath (Join-Path $stage "BUILD_INFO.txt") -Encoding UTF8

$requiredFiles = @(
    "LICENSE.txt",
    "README_FIRST.txt",
    "THIRD-PARTY-NOTICES.txt",
    "BUILD_INFO.txt",
    "OpenAccounting\OpenAccounting.exe"
)
foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $stage $requiredFile))) {
        throw "Required release file is missing: $requiredFile"
    }
}

# Python zipfile, not Compress-Archive: PS 5.1's archiver intermittently
# fails mid-stream on large trees (BinaryReader exception), leaving no zip.
python -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" ($zip -replace '\.zip$', '') $stage
if ($LASTEXITCODE -ne 0) { throw "Zip creation failed" }
if (-not $AllowUncommittedBuild) {
    Assert-OfficialIdentity $head $releaseTag
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash
"$hash  $(Split-Path -Leaf $zip)" |
    Set-Content -LiteralPath $checksumPath -Encoding ASCII

Write-Host ""
Write-Host "Portable build staged at: $stage"
Write-Host "Release zip:              $zip"
Write-Host "SHA-256:                  $hash"
Write-Host "Checksum file:            $checksumPath"
Write-Host "Distribute the ZIP (never the bare exe - _internal must stay next to it)."
