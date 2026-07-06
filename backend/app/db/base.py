from sqlalchemy.orm import DeclarativeBase


class MasterBase(DeclarativeBase):
    """ORM base for the master database (company registry, app-wide settings)."""


class CompanyBase(DeclarativeBase):
    """ORM base for per-company databases (books, journals, contacts, etc.)."""
