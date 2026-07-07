# Windows exe packaging lessons learned

Date: 2026-07-07

## Target chosen

The first packaging target is a Windows portable onedir build:

```text
dist\OpenAccounting\
  OpenAccounting.exe
  _internal\
```

This fits the current architecture better than a single-file exe or a full
installer because Open Accounting is a FastAPI backend plus a React frontend.

## Architecture decisions

- Build the React app first with `npm.cmd run build`.
- Bundle `frontend/dist` into the PyInstaller package as `frontend-dist`.
- Let the packaged FastAPI app serve both `/api/v1/...` and the React app.
- Use a catch-all frontend fallback for browser routes such as `/dashboard`.
- Do not let the frontend fallback swallow unknown `/api/...` routes.
- Store user accounting data outside the source tree by default:

```text
%LOCALAPPDATA%\OpenAccounting\data
```

## Files added

```text
backend/app/frontend.py
backend/app/desktop.py
packaging/open-accounting.spec
packaging/Build-WindowsPortable.ps1
packaging/README_WINDOWS_EXE.md
packaging/README_PORTABLE_USER.txt
```

## Windows issues encountered

- PowerShell blocks `npm.ps1` under the default execution policy.
  Use `npm.cmd` inside build scripts.
- PowerShell may also block local `.ps1` scripts.
  Use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\Build-WindowsPortable.ps1
```

- PyInstaller installed successfully but its scripts directory was not on PATH.
  Calling it as `python -m PyInstaller` avoids PATH problems.

## Verification completed

- `python -m compileall backend\app\frontend.py backend\app\desktop.py`
- `npm.cmd run build`
- FastAPI TestClient smoke:
  - `/` returns 200
  - `/dashboard` returns 200
  - `/api/nope` returns 404
  - `/health` returns 200
- PyInstaller build completed and produced `dist\OpenAccounting`.
- Foreground exe run started Uvicorn successfully.

## Known limitations

- This is not yet an installer.
- There is no app icon yet.
- There is no code signing certificate yet, so Windows SmartScreen may warn.
- It is a folder-based portable build. Users must keep `_internal` next to the exe.
- Playwright/Chromium PDF rendering is not yet validated inside the exe bundle.
- The background automated exe smoke was unreliable in the sandbox, although
  foreground startup worked.

## Recommended next steps

1. Add an `.ico` app icon and wire it into the PyInstaller spec.
2. Validate PDF rendering from the packaged exe.
3. Create an Inno Setup installer after the portable build is stable.
4. Add a release checklist and checksum generation.
5. Publish the zip with the AGPL license and source-code link.
