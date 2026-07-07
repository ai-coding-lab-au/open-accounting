"""Generate THIRD-PARTY-NOTICES.txt for the Windows portable build.

The portable zip redistributes compiled copies of the backend's Python
dependencies (PyInstaller onedir), the built frontend bundle (which embeds
React and friends), the Python runtime, OpenSSL DLLs, and pdfium.dll. Their
licenses require (or make customary) shipping the license texts with copies,
so the build script runs this to produce a single notices file at the zip
root.

Run with the SAME interpreter the build uses (Build-WindowsPortable.ps1 runs
it in place), after the backend deps are installed:

    python packaging/generate_third_party_notices.py --output <path>

The backend list is resolved from the app's declared dependencies via
importlib.metadata (so it tracks the versions actually bundled), plus a few
packages PyInstaller pulls in through optional imports. The frontend list is
the production dependencies plus the transitives Vite bundles. A missing
package or missing license text is a hard error — fix the list rather than
ship an incomplete notices file.
"""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Roots = backend runtime dependencies (pyproject [project.dependencies]).
# The full closure is resolved recursively below.
BACKEND_ROOT_DISTS = [
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "pydantic",
    "pydantic-settings",
    "httpx",
    "python-multipart",
    "pypdf",
    "pdfplumber",
    "openpyxl",
    "reportlab",
    "Pillow",
]

# uvicorn[standard] extras + packages PyInstaller pulls in via optional
# imports (observed in the bundle) that requires() resolution won't reach.
BACKEND_EXTRA_DISTS = [
    "watchfiles",
    "websockets",
    "httptools",
    "python-dotenv",
    "PyYAML",
    "colorama",
    "cryptography",
    "cffi",
    "pycparser",
]

# Frontend production dependencies (package.json) plus the transitive
# packages Vite bundles into the shipped JS.
FRONTEND_PACKAGES = [
    "@tanstack/react-query",
    "axios",
    "react",
    "react-dom",
    "react-router-dom",
    "zustand",
    "scheduler",
    "@remix-run/router",
    "use-sync-external-store",
    "loose-envify",
    "js-tokens",
]

# Components bundled by PyInstaller / the Python distribution that have no
# dist-info of their own.
STATIC_NOTICES = """\
================================================================================
Python (CPython runtime, bundled as python DLL + standard library)
License: Python Software Foundation License Version 2 (PSF-2.0)
https://docs.python.org/3/license.html

================================================================================
OpenSSL (libcrypto / libssl DLLs bundled with the CPython distribution)
License: Apache License 2.0 (OpenSSL 3.x)
https://www.openssl.org/source/license.html

================================================================================
SQLite (sqlite3 DLL bundled with the CPython distribution)
License: Public Domain
https://www.sqlite.org/copyright.html

================================================================================
Microsoft Visual C++ Runtime (vcruntime / msvcp DLLs)
Redistributed under the Microsoft Visual Studio redistributable license.
https://learn.microsoft.com/visualstudio/releases/2022/redistribution

================================================================================
PyInstaller bootloader (OpenAccounting.exe launcher stub)
License: GPL 2.0 with a special exception which allows bundling any-license
applications; the bundled application is not affected by the bootloader's
license. https://pyinstaller.org/en/stable/license.html
"""


def _license_label(dist: metadata.Distribution) -> str:
    md = dist.metadata
    label = md.get("License-Expression") or ""
    if not label:
        classifiers = [
            c.split("::")[-1].strip()
            for c in md.get_all("Classifier", [])
            if c.startswith("License ::")
        ]
        label = " / ".join(classifiers)
    if not label:
        lic = (md.get("License") or "").strip()
        label = lic if lic and len(lic) < 120 else ""
    return label or "(see license text below)"


def _license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """(relative-name, text) for every license/notice file in the dist-info."""
    texts: list[tuple[str, str]] = []
    base = getattr(dist, "_path", None)  # the *.dist-info directory
    if base is None:
        return texts
    base = Path(base)
    candidates: list[Path] = []
    licenses_dir = base / "licenses"
    if licenses_dir.is_dir():
        candidates.extend(p for p in sorted(licenses_dir.rglob("*")) if p.is_file())
    for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE.rst",
                 "LICENSE.APACHE", "LICENSE.BSD", "COPYING", "COPYING.txt",
                 "NOTICE", "NOTICE.txt", "AUTHORS"):
        p = base / name
        if p.is_file():
            candidates.append(p)
    for p in candidates:
        if p.suffix.lower() in {".json", ".whl"} or p.name == "RECORD":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            texts.append((str(p.relative_to(base)), text))
    return texts


def _resolve_backend_closure() -> list[metadata.Distribution]:
    seen: dict[str, metadata.Distribution] = {}
    queue = list(BACKEND_ROOT_DISTS) + list(BACKEND_EXTRA_DISTS)
    missing: list[str] = []
    while queue:
        name = queue.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        seen[key] = dist
        for req in dist.requires or []:
            # Skip extras-only requirements; keep unconditional ones.
            if ";" in req and "extra ==" in req.split(";", 1)[1]:
                continue
            dep = (
                req.split(";")[0]
                .split("[")[0]
                .split("(")[0]
                .split("==")[0]
                .split(">=")[0]
                .split("<")[0]
                .split("!=")[0]
                .split("~=")[0]
                .strip()
            )
            if dep:
                queue.append(dep)
    required_missing = [m for m in missing if m in BACKEND_ROOT_DISTS]
    if required_missing:
        raise SystemExit(
            f"Backend dependencies not installed in this interpreter: "
            f"{required_missing}. Run the build script's install step first."
        )
    if missing:
        print(f"note: optional packages not installed, skipped: {missing}")
    return sorted(seen.values(), key=lambda d: d.metadata["Name"].lower())


def _frontend_sections() -> list[str]:
    node_modules = REPO_ROOT / "frontend" / "node_modules"
    if not node_modules.is_dir():
        raise SystemExit(
            "frontend/node_modules not found — run `npm ci` in frontend/ first."
        )
    sections: list[str] = []
    missing: list[str] = []
    for name in FRONTEND_PACKAGES:
        pkg_dir = node_modules / Path(*name.split("/"))
        pkg_json = pkg_dir / "package.json"
        if not pkg_json.is_file():
            missing.append(name)
            continue
        import json

        meta = json.loads(pkg_json.read_text(encoding="utf-8"))
        version = meta.get("version", "?")
        license_id = meta.get("license", "(unspecified)")
        text = ""
        for lic_name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE-MIT.txt", "license"):
            p = pkg_dir / lic_name
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                break
        if not text:
            raise SystemExit(f"No license file found for frontend package {name}")
        sections.append(
            "=" * 80 + f"\n{name} {version}\nLicense: {license_id}\n\n{text}\n"
        )
    if missing:
        raise SystemExit(f"Frontend packages not found in node_modules: {missing}")
    return sections


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    parts: list[str] = [
        "THIRD-PARTY NOTICES for the Open Accounting Windows portable build\n"
        "===================================================================\n\n"
        "Open Accounting itself is licensed under AGPL-3.0-or-later (see\n"
        "LICENSE.txt). This build additionally redistributes the third-party\n"
        "components below, under their own licenses.\n\n"
    ]

    parts.append("PYTHON BACKEND DEPENDENCIES\n" + "=" * 80 + "\n\n")
    for dist in _resolve_backend_closure():
        name = dist.metadata["Name"]
        header = f"{name} {dist.version}\nLicense: {_license_label(dist)}"
        texts = _license_texts(dist)
        body = "\n\n".join(
            f"--- {rel} ---\n{text}" for rel, text in texts
        ) or "(no license file shipped in the wheel; see the project's homepage)"
        parts.append("=" * 80 + f"\n{header}\n\n{body}\n\n")

    parts.append("\nFRONTEND DEPENDENCIES (bundled into frontend-dist JS)\n" + "=" * 80 + "\n\n")
    parts.extend(_frontend_sections())

    parts.append("\nRUNTIME COMPONENTS\n" + "=" * 80 + "\n\n" + STATIC_NOTICES)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(parts), encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
