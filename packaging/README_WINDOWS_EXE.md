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

1. Installs the backend package and PyInstaller into the active Python environment.
2. Runs `npm ci` and `npm run build` in `frontend/`.
3. Builds a PyInstaller onedir bundle (`dist\OpenAccounting\`).
4. Runs `packaging\smoke_portable.py` against the built exe (health, frontend,
   API 404, receipt + report PDF rendering) — a build that lost reportlab or
   frontend-dist fails here.
5. Stages the release zip under `release\OpenAccounting-portable-<stamp>\`:
   the exe folder plus `LICENSE.txt`, `README_FIRST.txt` (from
   `README_PORTABLE_USER.txt`) and a freshly generated
   `THIRD-PARTY-NOTICES.txt`, then compresses it to
   `release\OpenAccounting-portable-<stamp>.zip`.

Distribute the **zip**, never the bare exe (`_internal\` must stay next to it).

Before building a release: tag the commit and update the `Version:` /
`Built from:` lines at the top of `README_PORTABLE_USER.txt` to match — the
AGPL §6 source offer in the zip must identify the exact source version.

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

- The zip must carry `LICENSE.txt`, `THIRD-PARTY-NOTICES.txt`, and a
  `README_FIRST.txt` that names the exact source version (tag/commit) and a
  working public link to it (AGPL §6 Corresponding Source).
- The tagged source must be public **no later than** the binary, and must
  actually contain everything the binary was built from — never build a
  release zip from uncommitted changes.
- The exe is not code-signed; README_FIRST.txt tells users what the
  SmartScreen prompt looks like and why.
