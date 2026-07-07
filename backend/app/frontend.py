from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


def _candidate_frontend_dirs() -> list[Path]:
    candidates: list[Path] = []

    configured = os.environ.get("OPEN_ACCOUNTING_FRONTEND_DIR")
    if configured:
        candidates.append(Path(configured))

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / "frontend-dist")

    project_root = Path(__file__).resolve().parents[2]
    candidates.append(project_root / "frontend" / "dist")

    return candidates


def find_frontend_dist() -> Path | None:
    for candidate in _candidate_frontend_dirs():
        index = candidate / "index.html"
        if candidate.exists() and index.is_file():
            return candidate
    return None


def configure_frontend(app: FastAPI) -> None:
    frontend_dir = find_frontend_dist()
    if frontend_dir is None:
        return

    index_file = frontend_dir / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        requested = (frontend_dir / full_path).resolve()
        try:
            requested.relative_to(frontend_dir.resolve())
        except ValueError:
            return FileResponse(index_file)

        if requested.is_file():
            return FileResponse(requested)
        return FileResponse(index_file)
