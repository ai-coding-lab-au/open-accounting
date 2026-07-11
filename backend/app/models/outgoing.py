"""M2 (Documents) per-company tables.

These ORM classes used to live in models/company.py. They were extracted
so that an M1-only build can drop the M2 router without dragging the
M2 schema along: importing this module is what registers the M2 tables
against CompanyBase.metadata, so an M1-standalone build that doesn't
load api.v1.outgoing leaves the metadata clean and create_all() builds
no document_counters / outgoing_documents / outgoing_document_lines.

Contains:
  - DocumentType  / DocumentStatus  (enums)
  - OutgoingDocument                (Receipt — the only outgoing document type)
  - OutgoingIssuerSnapshot          (immutable issuer identity/payment details)
  - OutgoingDocumentLine            (line items)
  - DocumentCounter                 (per-(doc_type, year) numbering counter)

Cross-table FKs:
  - customer_id → contacts.id        (M1 Contact table)
  - client_ref_id → clients.id       (M1 Client table)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import CompanyBase
from .company import MONEY, Contact  # MONEY is the shared Numeric(16, 2) alias


class DocumentType(str, Enum):
    RECEIPT = "receipt"


class DocumentStatus(str, Enum):
    DRAFT = "draft"            # editable, not yet finalised
    ISSUED = "issued"           # PDF generated and sent (or ready to send)
    VOID = "void"


class OutgoingDocument(CompanyBase):
    __tablename__ = "outgoing_documents"
    __table_args__ = (
        UniqueConstraint("doc_type", "doc_number", name="uq_outdoc_type_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_type: Mapped[DocumentType] = mapped_column(String(20), nullable=False, index=True)
    doc_number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Customer block (denormalised — customer addresses change over time, but the
    # PDF must always reflect what was on the document at issue time).
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    client_ref_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="RESTRICT"), index=True)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    customer_address: Mapped[str | None] = mapped_column(String(500))  # multi-line, \n separated
    customer_abn: Mapped[str | None] = mapped_column(String(20))
    customer_email: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str | None] = mapped_column(String(50))

    currency: Mapped[str] = mapped_column(String(3), default="AUD", nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    gst_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))

    status: Mapped[DocumentStatus] = mapped_column(
        String(20), nullable=False, default=DocumentStatus.DRAFT, index=True
    )
    paid_date: Mapped[date | None] = mapped_column(Date)
    payment_method: Mapped[str | None] = mapped_column(String(100))  # e.g. "Bank transfer"

    notes: Mapped[str | None] = mapped_column(String(1000))
    pdf_rel_path: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    customer: Mapped[Contact | None] = relationship()
    lines: Mapped[list["OutgoingDocumentLine"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="OutgoingDocumentLine.order_no"
    )
    issuer_snapshot: Mapped["OutgoingIssuerSnapshot | None"] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        uselist=False,
        single_parent=True,
    )


class OutgoingIssuerSnapshot(CompanyBase):
    """Immutable issuer details captured when a receipt is issued.

    This is a separate table rather than a new column so ``create_all`` can
    add it safely to existing company databases.  Legacy receipts without a
    row retain the old current-company fallback; all newly issued receipts get
    a snapshot and can no longer be rewritten by later profile changes.
    """

    __tablename__ = "outgoing_issuer_snapshots"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("outgoing_documents.id", ondelete="CASCADE"), primary_key=True
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    document: Mapped[OutgoingDocument] = relationship(back_populates="issuer_snapshot")


class OutgoingCreateIdempotencyKey(CompanyBase):
    """Persisted ownership of one receipt-create request."""

    __tablename__ = "outgoing_create_idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("outgoing_documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutgoingDocumentLine(CompanyBase):
    __tablename__ = "outgoing_document_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("outgoing_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("1"), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)

    document: Mapped[OutgoingDocument] = relationship(back_populates="lines")


class DocumentCounter(CompanyBase):
    """Per-(doc_type, year) running counter for document numbering.

    Each combination has its own sequence — e.g. (invoice, 2026) → 42 means the
    next invoice this year is INV-2026-0043. Stored explicitly so we can:
      * survive race conditions (UPDATE…RETURNING under a row lock)
      * let the user adjust the starting value in Settings ("we're already at 0042")
    """

    __tablename__ = "document_counters"

    doc_type: Mapped[DocumentType] = mapped_column(String(20), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
