# Dev launcher (Windows): backend on :8787 + frontend on :5173, each in its own window.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendPython = Join-Path $root "backend\.venv\Scripts\python.exe"

if (-not (Test-Path $backendPython)) {
    Write-Error "Missing backend virtualenv. Run: cd backend; python -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m pip install -e '.[dev]'"
    exit 1
}

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle = 'Open Accounting - backend :8787'; " +
    "cd '$root\backend'; & '$backendPython' -m uvicorn app.main:app --port 8787 --reload"
)

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle = 'Open Accounting - frontend :5173'; " +
    "cd '$root\frontend'; npm run dev"
)

Write-Host "Backend  -> http://127.0.0.1:8787  (API docs at /docs)"
Write-Host "Frontend -> http://127.0.0.1:5173"
