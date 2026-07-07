param(
    [switch]$SkipInstall,
    [switch]$SkipFrontendBuild,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (-not $SkipInstall) {
    # No [pdf] extra: the portable build deliberately ships the ReportLab
    # renderer (bundling playwright without a browser is dead weight, and the
    # spec excludes it either way).
    python -m pip install -e "$root\backend"
    python -m pip install pyinstaller
}

if (-not $SkipFrontendBuild) {
    Push-Location "$root\frontend"
    try {
        npm.cmd ci
        npm.cmd run build
    }
    finally {
        Pop-Location
    }
}

Push-Location $root
try {
    python -m PyInstaller --clean --noconfirm packaging\open-accounting.spec
}
finally {
    Pop-Location
}

$output = Join-Path $root "dist\OpenAccounting"

# Post-build smoke: boots the exe against a throwaway data dir and exercises
# health, frontend, API 404, and PDF rendering. A build that silently lost
# reportlab or frontend-dist fails here, not on a user's machine.
if (-not $SkipSmoke) {
    python "$root\packaging\smoke_portable.py" "$output\OpenAccounting.exe"
    if ($LASTEXITCODE -ne 0) { throw "Portable smoke test failed" }
}

# Stage the release zip: exe folder + LICENSE + user README + third-party
# notices. These compliance files are REQUIRED in every distributed zip
# (AGPL source offer + bundled third-party license texts) - keep this step
# scripted so they can't be forgotten.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stageRoot = Join-Path $root "release"
$stage = Join-Path $stageRoot "OpenAccounting-portable-$stamp"
New-Item -ItemType Directory -Force $stage | Out-Null

Copy-Item -Recurse $output (Join-Path $stage "OpenAccounting")
Copy-Item (Join-Path $root "LICENSE") (Join-Path $stage "LICENSE.txt")
Copy-Item (Join-Path $root "packaging\README_PORTABLE_USER.txt") (Join-Path $stage "README_FIRST.txt")
python "$root\packaging\generate_third_party_notices.py" --output (Join-Path $stage "THIRD-PARTY-NOTICES.txt")
if ($LASTEXITCODE -ne 0) { throw "Third-party notices generation failed" }

# Python zipfile, not Compress-Archive: PS 5.1's archiver intermittently
# fails mid-stream on large trees (BinaryReader exception), leaving no zip.
$zip = "$stage.zip"
python -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" ($zip -replace '\.zip$', '') $stage
if ($LASTEXITCODE -ne 0) { throw "Zip creation failed" }

Write-Host ""
Write-Host "Portable build staged at: $stage"
Write-Host "Release zip:              $zip"
Write-Host "Distribute the ZIP (never the bare exe - _internal must stay next to it)."
