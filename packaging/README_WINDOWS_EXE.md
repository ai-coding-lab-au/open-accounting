# Windows portable build

This is the first packaging target for Open Accounting. It creates a portable
folder containing `OpenAccounting.exe` plus its runtime files.

## Build

From the repository root:

```powershell
.\packaging\Build-WindowsPortable.ps1
```

If local PowerShell execution policy blocks scripts, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\Build-WindowsPortable.ps1
```

The script:

1. Before installing or building anything, validates one release identity
   across the portable README, backend, FastAPI OpenAPI, frontend lockfile, and
   Windows version resource. Official builds require a clean worktree and an
   annotated `vMAJOR.MINOR.PATCH` tag pointing exactly at `HEAD`.
2. Installs the backend package and PyInstaller into the active Python environment.
3. Runs `npm ci` and `npm run build` in `frontend/`.
4. Builds a versioned PyInstaller onedir bundle (`dist\OpenAccounting\`).
5. Runs `packaging\smoke_portable.py` against the built exe (health, version,
   frontend, API 404, receipt + report PDF rendering) — a build that lost
   reportlab or frontend-dist fails here.
6. Stages `release\OpenAccounting-portable-vX.Y.Z-windows-x64.zip`: the exe
   folder plus `LICENSE.txt`, `README_FIRST.txt` (from
   `README_PORTABLE_USER.txt`), `BUILD_INFO.txt`, and a freshly generated
   `THIRD-PARTY-NOTICES.txt`, then generates a sibling `.sha256` file.

Distribute the **zip**, never the bare exe (`_internal\` must stay next to it).

Before building a release, update every product version plus the `Version:` /
`Built from:` lines at the top of `README_PORTABLE_USER.txt`, commit them, and
create an annotated tag. The build refuses a dirty tree, a split version, a
lightweight/missing tag, or a tag that does not point at `HEAD`; the AGPL §6
source offer in the zip therefore identifies the exact source version.
Official mode also rejects every `-Skip*` switch, checks each native command's
exit code, and revalidates the clean HEAD/tag after the runtime gate and again
after archiving.

For local smoke work only, an uncommitted build can be requested explicitly:

```powershell
.\packaging\Build-WindowsPortable.ps1 -AllowUncommittedBuild
```

Its folder and ZIP contain `candidate` plus the current commit in the name and
must not be distributed.

To validate release identity without installing or building anything:

```powershell
.\packaging\Build-WindowsPortable.ps1 -PreflightOnly
```

## PDF rendering in the portable build

The portable build intentionally ships the **ReportLab** renderer only.
Playwright is excluded in the spec (bundling the Python package without a
browser is dead weight, and shipping Chromium would add ~150 MB). The
Chromium HTML renderer remains available for source installs via
`pip install -e ".[pdf]"`.

## Third-party notices

`generate_third_party_notices.py` resolves the backend dependency closure
from the interpreter that built the bundle and copies each package's license
files, plus the frontend packages bundled by Vite and static notices for the
Python runtime / OpenSSL / MSVC runtime / PyInstaller bootloader /
pdfium.dll. If you add a backend dependency that PyInstaller pulls in via an
optional import (not reachable from `pyproject.toml` requirements), add it to
`BACKEND_EXTRA_DISTS`; new frontend production deps go in
`FRONTEND_PACKAGES`.

## Runtime behavior

Double-clicking `OpenAccounting.exe` starts the local FastAPI server on:

```text
http://127.0.0.1:8787
```

It then opens the default browser. Accounting data is stored under:

```text
%LOCALAPPDATA%\OpenAccounting\data
```

That keeps real user data outside the source tree.

## Useful options

```powershell
OpenAccounting.exe --no-browser
OpenAccounting.exe --port 8790
OpenAccounting.exe --data-dir D:\OpenAccountingData
```

## Distribution notes

AGPL-3.0-or-later obligations for distributing the binary zip:

- The zip must carry `LICENSE.txt`, `THIRD-PARTY-NOTICES.txt`, `BUILD_INFO.txt`, and a
  `README_FIRST.txt` that names the exact source version (tag/commit) and a
  working public link to it (AGPL §6 Corresponding Source).
- Publish the generated `.sha256` file alongside the zip and verify it again
  after upload.
- The tagged source must be public **no later than** the binary, and must
  actually contain everything the binary was built from — never build a
  release zip from uncommitted changes.
- The exe is not code-signed; README_FIRST.txt tells users what the
  SmartScreen prompt looks like and why.
