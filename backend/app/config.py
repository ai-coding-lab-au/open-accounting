import os
import sys
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @field_validator("data_dir", mode="after")
    @classmethod
    def _resolve_data_dir(cls, v: Path) -> Path:
        resolved = (PROJECT_ROOT / v).resolve() if not v.is_absolute() else v.resolve()

        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError:
            # Path is OUTSIDE the project tree — the intended production state.
            # We warn loudly so the operator notices the path during boot.
            print(
                f"[config] DATA_DIR is outside the project tree: {resolved}\n"
                f"[config] (project root = {PROJECT_ROOT})\n"
                f"[config] This is intentional for production. Make sure backups cover this path.",
                file=sys.stderr,
                flush=True,
            )
            return resolved

        # Path resolved INSIDE the project tree. Reject by default — real
        # customer data must never live where coding agents (Claude, Codex)
        # with project-only sandbox rules can read it.
        #
        # Common ways this happens by accident:
        #   - DATA_DIR unset, falling back to the PROJECT_ROOT/data default
        #   - Windows path like E:\books-data on Linux (backslash is not
        #     a separator, so the whole string becomes a relative path that
        #     gets joined onto PROJECT_ROOT)
        #
        # Escape hatch for pytest / local dev: set ALLOW_UNSAFE_DATA_DIR=1
        # to bypass this check. Production deploys must not set it.
        if os.environ.get("ALLOW_UNSAFE_DATA_DIR") == "1":
            print(
                f"[config] WARNING: DATA_DIR is INSIDE the project tree ({resolved}). "
                f"Allowed because ALLOW_UNSAFE_DATA_DIR=1. Never run real customer "
                f"data under this configuration — coding agents can read it.",
                file=sys.stderr,
                flush=True,
            )
            return resolved

        raise ValueError(
            f"DATA_DIR resolved to {resolved}, which is INSIDE the project tree "
            f"({PROJECT_ROOT}). Real customer accounting data must live OUTSIDE the "
            f"project tree so coding agents cannot read it.\n\n"
            f"Likely cause: DATA_DIR is unset, or set to a Windows-style path "
            f"(e.g. 'E:\\books-data') which on Linux is treated as a relative "
            f"path and joined onto the project root.\n\n"
            f"Fix: set DATA_DIR in backend/.env to an absolute path outside "
            f"{PROJECT_ROOT}. Examples:\n"
            f"  Linux:   DATA_DIR=/opt/accounting_data\n"
            f"  WSL2:    DATA_DIR=/mnt/d/accounting_data\n"
            f"  Windows: DATA_DIR=D:/accounting_data   (use forward slashes)\n\n"
            f"For pytest or local dev where you don't have real customer data, "
            f"set ALLOW_UNSAFE_DATA_DIR=1 to bypass this check."
        )

    @property
    def master_db_path(self) -> Path:
        return self.data_dir / "master.db"

    def company_dir(self, company_id: str) -> Path:
        return self.data_dir / "companies" / company_id

    def company_db_path(self, company_id: str) -> Path:
        return self.company_dir(company_id) / "books.db"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
