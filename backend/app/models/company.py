"""Per-company tables (books.db).

M1 ships Account (Chart of Accounts).
Invoice import phase adds: Contact, Invoice, InvoiceLine, Attachment.
Later milestones will add: JournalEntry/JournalLine, BankAccount, BankTransaction,
BankRule, etc. Importing this module registers all mappings against
CompanyBase.metadata so create_all() builds the full schema.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import CompanyBase


# ---------------------------------------------------------------------------
# Chart of Accounts
# ---------------------------------------------------------------------------


class AccountType(str, Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    COST_OF_SALES = "COST_OF_SALES"


class Account(CompanyBase):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[AccountType] = mapped_column(String(20), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    is_gst: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    parent: Mapped["Account | None"] = relationship(remote_side="Account.id", backref="children")


# ---------------------------------------------------------------------------
# Contacts (customers / suppliers)
# ---------------------------------------------------------------------------


class ContactKind(str, Enum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    BOTH = "both"


class Contact(CompanyBase):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[ContactKind] = mapped_column(String(10), nullable=False, default=ContactKind.SUPPLIER)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    abn: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(String(500))  # printed as "Bill To" on documents
    notes: Mapped[str | None] = mapped_column(String(1000))
    # server_default so DB-level inserts that omit the column (raw SQL, legacy
    # rows backfilled by schema_sync's ALTER) still get a value.
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="contact")


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


class InvoiceDirection(str, Enum):
    AP = "AP"  # Accounts Payable — supplier bills (purchase invoices)
    AR = "AR"  # Accounts Receivable — invoices we issue to customers


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    AUTHORISED = "authorised"
    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"
    VOID = "void"


class InvoiceSource(str, Enum):
    MANUAL = "manual"
    PDF = "pdf"
    EXCEL = "excel"


class CreditNoteStatus(str, Enum):
    DRAFT = "draft"
    AUTHORISED = "authorised"
    VOID = "void"


class CreditNoteApplicationStatus(str, Enum):
    ACTIVE = "active"
    REVERSED = "reversed"


class CreditNoteRefundStatus(str, Enum):
    ACTIVE = "active"
    REVERSED = "reversed"


# Decimal precision used throughout: 2 decimal places, up to 14 integer digits.
MONEY = Numeric(16, 2)
GST_ADJUSTMENT_MAX_CENTS = 999_999_999_999_999
_GST_ADJUSTMENT_MONEY_INPUT = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?\Z")


def gst_adjustment_money_to_cents(value: Decimal | int | str) -> int:
    """Validate a decimal money input and convert it to exact integer cents."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ValueError("GST adjustment money must be a Decimal, integer, or decimal string")
    raw = str(value)
    if _GST_ADJUSTMENT_MONEY_INPUT.fullmatch(raw) is None:
        raise ValueError("GST adjustment money must use plain decimal notation with at most two places")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("GST adjustment money must be a finite decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("GST adjustment money must be finite and nonnegative")
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        raise ValueError("GST adjustment money cannot contain fractional cents")
    cents = int(scaled)
    if cents > GST_ADJUSTMENT_MAX_CENTS:
        raise ValueError("GST adjustment money exceeds the supported maximum")
    return cents


def gst_adjustment_cents_to_money(value: int) -> Decimal:
    """Convert validated integer cents to a two-decimal Decimal value."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > GST_ADJUSTMENT_MAX_CENTS
    ):
        raise ValueError("GST adjustment cents are outside supported limits")
    return Decimal(value).scaleb(-2)


class Invoice(CompanyBase):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("direction", "contact_id", "invoice_number", name="uq_invoice_dir_contact_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    direction: Mapped[InvoiceDirection] = mapped_column(String(2), nullable=False, index=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contact_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    contact_abn_snapshot: Mapped[str | None] = mapped_column(String(20))
    contact_address_snapshot: Mapped[str | None] = mapped_column(String(500))
    contact_email_snapshot: Mapped[str | None] = mapped_column(String(200))
    contact_phone_snapshot: Mapped[str | None] = mapped_column(String(50))
    invoice_number: Mapped[str] = mapped_column(String(80), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="AUD", nullable=False)

    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gst_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gst_inclusive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(
        String(10), nullable=False, default=InvoiceStatus.DRAFT, index=True
    )
    authorised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    paid_date: Mapped[date | None] = mapped_column(Date)

    notes: Mapped[str | None] = mapped_column(String(1000))
    source: Mapped[InvoiceSource] = mapped_column(String(10), default=InvoiceSource.MANUAL, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    contact: Mapped[Contact] = relationship(back_populates="invoices")
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="invoice")
    payment_allocations: Mapped[list["InvoicePaymentAllocation"]] = relationship(
        back_populates="invoice"
    )
    credit_applications: Mapped[list["CreditNoteApplication"]] = relationship(
        back_populates="invoice"
    )


class InvoiceLine(CompanyBase):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("1"), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.10"), nullable=False)
    line_subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    line_gst: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Stable BAS treatment for this economic line.  A zero GST amount alone
    # cannot distinguish GST-free, input-taxed, capital, and out-of-scope
    # supplies, so payment-time reporting must not infer the category from GST.
    tax_code: Mapped[str] = mapped_column(
        String(20), nullable=False, default="gst_free", server_default="gst_free"
    )

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    account: Mapped[Account | None] = relationship()


class CreditNote(CompanyBase):
    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint(
            "direction",
            "contact_id",
            "credit_note_number",
            name="uq_credit_note_dir_contact_no",
        ),
        CheckConstraint("direction IN ('AR', 'AP')", name="ck_credit_note_direction"),
        CheckConstraint("currency = 'AUD'", name="ck_credit_note_currency_aud"),
        CheckConstraint(
            "status IN ('draft', 'authorised', 'void')",
            name="ck_credit_note_status_allowed",
        ),
        CheckConstraint("subtotal >= 0", name="ck_credit_note_subtotal_nonneg"),
        CheckConstraint("gst_amount >= 0", name="ck_credit_note_gst_nonneg"),
        CheckConstraint("total >= 0", name="ck_credit_note_total_nonneg"),
        CheckConstraint("total > 0", name="ck_credit_note_total_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    credit_note_number: Mapped[str] = mapped_column(String(80), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="AUD")
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gst_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gst_inclusive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[CreditNoteStatus] = mapped_column(
        String(10), nullable=False, default=CreditNoteStatus.DRAFT, index=True
    )
    notes: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    source_invoice: Mapped[Invoice] = relationship()
    contact: Mapped[Contact] = relationship()
    lines: Mapped[list["CreditNoteLine"]] = relationship(
        back_populates="credit_note", cascade="all, delete-orphan"
    )
    applications: Mapped[list["CreditNoteApplication"]] = relationship(
        back_populates="credit_note", cascade="all, delete-orphan"
    )
    refunds: Mapped[list["CreditNoteRefund"]] = relationship(
        back_populates="credit_note", cascade="all, delete-orphan"
    )


class CreditNoteLine(CompanyBase):
    __tablename__ = "credit_note_lines"
    __table_args__ = (
        UniqueConstraint(
            "credit_note_id",
            "source_invoice_line_id",
            name="uq_credit_note_line_note_source_line",
        ),
        CheckConstraint("quantity > 0", name="ck_credit_note_line_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_credit_note_line_unit_price_nonneg"),
        CheckConstraint("line_subtotal >= 0", name="ck_credit_note_line_subtotal_nonneg"),
        CheckConstraint("line_gst >= 0", name="ck_credit_note_line_gst_nonneg"),
        CheckConstraint("line_total >= 0", name="ck_credit_note_line_total_nonneg"),
        CheckConstraint("line_subtotal > 0", name="ck_credit_note_line_subtotal_positive"),
        CheckConstraint("line_total > 0", name="ck_credit_note_line_total_positive"),
        CheckConstraint("line_gst <= line_total", name="ck_credit_note_line_gst_within"),
        CheckConstraint(
            "tax_code IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none')",
            name="ck_credit_note_line_tax_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credit_note_id: Mapped[int] = mapped_column(
        ForeignKey("credit_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_invoice_line_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_lines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    line_subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    line_gst: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_code: Mapped[str] = mapped_column(String(20), nullable=False)

    credit_note: Mapped[CreditNote] = relationship(back_populates="lines")
    source_invoice_line: Mapped[InvoiceLine] = relationship()
    account: Mapped[Account] = relationship()


class CreditNoteApplication(CompanyBase):
    __tablename__ = "credit_note_applications"
    __table_args__ = (
        Index(
            "ix_credit_note_application_active_pair",
            "credit_note_id",
            "invoice_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        CheckConstraint("amount > 0", name="ck_credit_note_application_amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credit_note_id: Mapped[int] = mapped_column(
        ForeignKey("credit_notes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    application_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[CreditNoteApplicationStatus] = mapped_column(
        String(10), nullable=False, default=CreditNoteApplicationStatus.ACTIVE, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    credit_note: Mapped[CreditNote] = relationship(back_populates="applications")
    invoice: Mapped[Invoice] = relationship(back_populates="credit_applications")
    reversal: Mapped["CreditNoteApplicationReversal | None"] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        uselist=False,
    )


class CreditNoteApplicationReversal(CompanyBase):
    __tablename__ = "credit_note_application_reversals"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            name="uq_credit_note_application_reversal_application",
        ),
        CheckConstraint("reversal_date IS NOT NULL", name="ck_credit_note_application_reversal_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey(
            "credit_note_applications.id", ondelete="RESTRICT"
        ),
        nullable=False,
        unique=True,
        index=True,
    )
    reversal_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    application: Mapped[CreditNoteApplication] = relationship(
        back_populates="reversal"
    )


class CreditNoteApplicationIdempotencyKey(CompanyBase):
    __tablename__ = "credit_note_application_idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            name="uq_credit_note_application_idempotency_application",
        ),
    )

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    application_id: Mapped[int] = mapped_column(
        ForeignKey(
            "credit_note_applications.id", ondelete="CASCADE"
        ),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CreditNoteRefund(CompanyBase):
    __tablename__ = "credit_note_refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_credit_note_refund_amount_positive"),
        CheckConstraint(
            "status IN ('active', 'reversed')",
            name="ck_credit_note_refund_status",
        ),
        UniqueConstraint(
            "credit_note_id",
            "bank_transaction_id",
            name="uq_credit_note_refund_txn",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credit_note_id: Mapped[int] = mapped_column(
        ForeignKey("credit_notes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    journal_entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    refund_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[CreditNoteRefundStatus] = mapped_column(
        String(10), nullable=False, default=CreditNoteRefundStatus.ACTIVE, index=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_date: Mapped[date | None] = mapped_column(Date)
    reversal_bank_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="RESTRICT"),
        unique=True,
    )
    reversal_journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    credit_note: Mapped[CreditNote] = relationship(back_populates="refunds")
    bank_transaction: Mapped[BankTransaction] = relationship(
        foreign_keys=[bank_transaction_id]
    )
    journal_entry: Mapped[JournalEntry] = relationship(
        foreign_keys=[journal_entry_id]
    )
    reversal_bank_transaction: Mapped[BankTransaction | None] = relationship(
        foreign_keys=[reversal_bank_transaction_id]
    )
    reversal_journal_entry: Mapped[JournalEntry | None] = relationship(
        foreign_keys=[reversal_journal_entry_id]
    )


class CreditNoteRefundIdempotencyKey(CompanyBase):
    __tablename__ = "credit_note_refund_idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refund_id: Mapped[int] = mapped_column(
        ForeignKey("credit_note_refunds.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    refund: Mapped[CreditNoteRefund] = relationship()


class GSTAdjustmentEvent(CompanyBase):
    """Append-only record of a policy-versioned GST adjustment."""

    __tablename__ = "gst_adjustment_events"
    __table_args__ = (
        CheckConstraint("source_direction IN ('AR', 'AP')", name="ck_gst_adjustment_event_source_direction"),
        CheckConstraint(
            "adjustment_direction IN ('increasing', 'decreasing')",
            name="ck_gst_adjustment_event_adjustment_direction",
        ),
        CheckConstraint("projection_box IN ('1A', '1B')", name="ck_gst_adjustment_event_projection_box"),
        CheckConstraint(
            f"typeof(amount_cents) = 'integer' AND amount_cents BETWEEN 1 AND {GST_ADJUSTMENT_MAX_CENTS}",
            name="ck_gst_adjustment_event_amount_cents_positive",
        ),
        CheckConstraint(
            f"typeof(gst_amount_cents) = 'integer' AND gst_amount_cents BETWEEN 0 AND amount_cents "
            f"AND gst_amount_cents <= {GST_ADJUSTMENT_MAX_CENTS}",
            name="ck_gst_adjustment_event_gst_cents_within",
        ),
        CheckConstraint(
            "event_type IN ('agreement', 'refund_repayment', 'adjustment_note', 'reversal')",
            name="ck_gst_adjustment_event_type",
        ),
        CheckConstraint(
            "tax_code IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none', 'mixed')",
            name="ck_gst_adjustment_event_tax_code",
        ),
        CheckConstraint(
            "gst_amount_cents > 0 OR tax_code IN ('gst_free', 'input_taxed', 'none', 'mixed')",
            name="ck_gst_adjustment_event_zero_gst_code",
        ),
        CheckConstraint("policy_version IN ('F-02-v1')", name="ck_gst_adjustment_event_policy_version"),
        CheckConstraint(
            "source_record_type IS NOT NULL AND source_record_type IN ('credit_note') "
            "AND length(trim(source_record_type, char(9) || char(10) || char(13) || ' ')) > 0",
            name="ck_gst_adjustment_event_source_record_type",
        ),
        CheckConstraint(
            "typeof(source_record_id) = 'integer' AND source_record_id > 0",
            name="ck_gst_adjustment_event_source_record_id",
        ),
        CheckConstraint(
            "manual_review_status IN ('not_required', 'pending', 'approved', 'rejected')",
            name="ck_gst_adjustment_event_manual_review_status",
        ),
        CheckConstraint(
            "source_record_type IS NOT NULL AND source_record_id IS NOT NULL",
            name="ck_gst_adjustment_event_source_record_pair",
        ),
        CheckConstraint(
            "(event_type = 'reversal' AND reversal_of_event_id IS NOT NULL) OR "
            "(event_type != 'reversal' AND reversal_of_event_id IS NULL)",
            name="ck_gst_adjustment_event_reversal_type_link",
        ),
        Index("ix_gst_adjustment_events_effective_date", "effective_date"),
        Index(
            "ix_gst_adjustment_events_source_record",
            "source_record_type",
            "source_record_id",
        ),
        Index(
            "uq_gst_adjustment_events_reversal_once",
            "reversal_of_event_id",
            unique=True,
            sqlite_where=text("reversal_of_event_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_direction: Mapped[str] = mapped_column(String(2), nullable=False)
    adjustment_direction: Mapped[str] = mapped_column(String(10), nullable=False)
    projection_box: Mapped[str] = mapped_column(String(2), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_code: Mapped[str] = mapped_column(String(20), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_type: Mapped[str | None] = mapped_column(String(50))
    source_record_id: Mapped[int | None] = mapped_column(Integer)
    lifecycle_operation_type: Mapped[str | None] = mapped_column(String(50))
    lifecycle_operation_id: Mapped[int | None] = mapped_column(Integer)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    awareness_date: Mapped[date | None] = mapped_column(Date)
    agreement_date: Mapped[date | None] = mapped_column(Date)
    refund_repayment_date: Mapped[date | None] = mapped_column(Date)
    adjustment_note_reference: Mapped[str | None] = mapped_column(String(200))
    adjustment_note_held_date: Mapped[date | None] = mapped_column(Date)
    manual_review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_required", server_default=text("'not_required'")
    )
    reversal_of_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("gst_adjustment_events.id", ondelete="RESTRICT")
    )
    tax_slice_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GSTAdjustmentTaxSlice(CompanyBase):
    """Tax-code allocation for an event with mixed-tax components."""

    __tablename__ = "gst_adjustment_tax_slices"
    __table_args__ = (
        UniqueConstraint("event_id", "tax_code", name="uq_gst_adjustment_tax_slice_code"),
        CheckConstraint(
            f"typeof(amount_cents) = 'integer' AND amount_cents BETWEEN 1 AND {GST_ADJUSTMENT_MAX_CENTS}",
            name="ck_gst_adjustment_tax_slice_amount_cents_positive",
        ),
        CheckConstraint(
            f"typeof(gst_amount_cents) = 'integer' AND gst_amount_cents BETWEEN 0 AND amount_cents "
            f"AND gst_amount_cents <= {GST_ADJUSTMENT_MAX_CENTS}",
            name="ck_gst_adjustment_tax_slice_gst_cents_within",
        ),
        CheckConstraint(
            "tax_code IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none')",
            name="ck_gst_adjustment_tax_slice_tax_code",
        ),
        CheckConstraint(
            "gst_amount_cents > 0 OR tax_code IN ('gst_free', 'input_taxed', 'none')",
            name="ck_gst_adjustment_tax_slice_zero_gst_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("gst_adjustment_events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tax_code: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)


class GSTAdjustmentFinalization(CompanyBase):
    """Append-only report-eligibility record for a reconciled event allocation."""

    __tablename__ = "gst_adjustment_finalizations"
    __table_args__ = (
        CheckConstraint("tax_slice_count > 0", name="ck_gst_adjustment_finalization_slice_count_positive"),
        CheckConstraint(
            f"typeof(amount_cents) = 'integer' AND amount_cents BETWEEN 1 AND {GST_ADJUSTMENT_MAX_CENTS}",
            name="ck_gst_adjustment_finalization_amount_cents_positive",
        ),
        CheckConstraint(
            f"typeof(gst_amount_cents) = 'integer' AND gst_amount_cents BETWEEN 0 AND amount_cents "
            f"AND gst_amount_cents <= {GST_ADJUSTMENT_MAX_CENTS}",
            name="ck_gst_adjustment_finalization_gst_cents_within",
        ),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("gst_adjustment_events.id", ondelete="RESTRICT"), primary_key=True
    )
    tax_slice_count: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GSTAdjustmentEvidence(CompanyBase):
    """Append-only evidence references retained with an adjustment event."""

    __tablename__ = "gst_adjustment_evidence"
    __table_args__ = (
        CheckConstraint("evidence_type IN ('document')", name="ck_gst_adjustment_evidence_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("gst_adjustment_events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False)
    evidence_reference: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GSTAdjustmentManualReview(CompanyBase):
    """Append-only manual-review decision history for an adjustment event."""

    __tablename__ = "gst_adjustment_manual_reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_gst_adjustment_manual_review_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewed_at IS NOT NULL)",
            name="ck_gst_adjustment_manual_review_timestamp",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("gst_adjustment_events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Attachments (files on disk; DB only stores metadata + relative path)
# ---------------------------------------------------------------------------


class Attachment(CompanyBase):
    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    rel_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[Invoice | None] = relationship(back_populates="attachments")


# ---------------------------------------------------------------------------
# Outgoing documents: Invoice / Payment Request / Receipt that we issue to customers.
# Distinct from `Invoice` above which models supplier bills + (legacy) manual AR.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module 2 (Documents) tables — OutgoingDocument, OutgoingDocumentLine,
# DocumentCounter, DocumentType, DocumentStatus — moved to
# backend/app/models/outgoing.py so an M1-standalone build can drop M2
# entirely (don't import api.v1.outgoing → models.outgoing never loads
# → tables aren't registered with CompanyBase.metadata → create_all
# builds an M1-only schema).
#
# The legacy ServiceAgreement model (a parallel pre-OutgoingDocument
# table) was removed in an earlier commit.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Clients & banking
#
# Domain split:
#   - Client    : a person/entity we provide services to. Independent table
#                 from Contact.
#   - Contact   : suppliers / providers we pay (kept in `contacts` table above;
#                 customer-kind rows are legacy and will be migrated to Client).
#
# Money layers:
#   - BankAccount       : the company's bank account. One per company,
#                         seeded on company init.
#   - BankTransaction   : real cash movement on the BankAccount.
# ---------------------------------------------------------------------------


class Client(CompanyBase):
    """A client we provide services to."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(String(500))
    client_ref: Mapped[str | None] = mapped_column(String(50), index=True)  # firm's own client code
    notes: Mapped[str | None] = mapped_column(String(1000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BankAccount(CompanyBase):
    __tablename__ = "bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    bsb: Mapped[str | None] = mapped_column(String(20))
    account_number: Mapped[str | None] = mapped_column(String(50))
    opening_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    ledger_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), unique=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    transactions: Mapped[list["BankTransaction"]] = relationship(back_populates="bank_account")


class BankTxnDirection(str, Enum):
    IN = "in"
    OUT = "out"


class TaxCode(str, Enum):
    """GST treatment for a money event. Determines which BAS box(es) it lands in.

    STANDARD     — taxable supply / acquisition at 10% (default for GST-registered)
    GST_FREE     — explicitly GST-free supply (basic food, education, exports) → G3/G14
    INPUT_TAXED  — input-taxed supply (financial, residential rent) → G4
    CAPITAL      — capital acquisition (asset purchase >= GST-claim threshold) → G10
    NONE         — outside BAS scope: owner draws, inter-account transfer, trust legs
    """

    STANDARD = "standard"
    GST_FREE = "gst_free"
    INPUT_TAXED = "input_taxed"
    CAPITAL = "capital"
    NONE = "none"


class BankTransaction(CompanyBase):
    """A real cash movement on a real bank account. Amount is always positive;
    direction tells you the sign."""

    __tablename__ = "bank_transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_bank_txn_amount_positive"),
        CheckConstraint("gst_amount >= 0", name="ck_bank_txn_gst_nonneg"),
        CheckConstraint("gst_amount <= amount", name="ck_bank_txn_gst_within"),
        CheckConstraint(
            "unapplied_amount >= 0 AND unapplied_amount <= amount",
            name="ck_bank_txn_unapplied_within",
        ),
        CheckConstraint(
            "(unapplied_amount = 0 AND unapplied_account_id IS NULL) OR "
            "(unapplied_amount > 0 AND unapplied_account_id IS NOT NULL)",
            name="ck_bank_txn_unapplied_account",
        ),
        Index(
            "uq_bank_txn_dedup",
            "bank_account_id",
            "dedup_key",
            unique=True,
            sqlite_where=text("dedup_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_account_id: Mapped[int] = mapped_column(
        ForeignKey("bank_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    direction: Mapped[BankTxnDirection] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)  # > 0
    occurred_at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    memo: Mapped[str | None] = mapped_column(String(500))
    counter_party_name: Mapped[str | None] = mapped_column(String(200))

    # Categorisation: which CoA account this movement hits (drives P&L).
    # Optional because a freshly-imported txn may be uncategorised.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    # Optional GST split — left at 0 while the firm is not GST-registered, but
    # the column exists so BAS can compute as soon as GST is turned on.
    gst_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )
    # GST treatment classification. Defaults to STANDARD so existing rows
    # keep behaving as before; the BAS report uses this to split into the
    # correct boxes (G1/G2/G3/G10/G11 etc).
    tax_code: Mapped[TaxCode] = mapped_column(
        String(20), nullable=False, default=TaxCode.STANDARD
    )

    # When a real cash movement exceeds the invoices it settles, the remainder
    # is still part of this one bank row.  Store its explicit balance-sheet
    # destination instead of forcing users to fabricate a second cash row or
    # double-count income/expense.
    unapplied_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    unapplied_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )

    # Stable hash of (bank_account_id, direction, amount, occurred_at, memo)
    # used to detect duplicates when re-importing a bank statement CSV.
    # Optional because manual entries don't need it; set automatically by the
    # import pipeline.
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    bank_account: Mapped[BankAccount] = relationship(back_populates="transactions")
    invoice_allocations: Mapped[list["InvoicePaymentAllocation"]] = relationship(
        back_populates="bank_transaction",
        cascade="all, delete-orphan",
    )
    unapplied_account: Mapped[Account | None] = relationship(
        foreign_keys=[unapplied_account_id]
    )


class BankTransactionIdempotencyKey(CompanyBase):
    """Durable ownership of one manual-bank-create idempotency key."""

    __tablename__ = "bank_transaction_idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "bank_transaction_id",
            name="uq_bank_transaction_idempotency_transaction",
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Keep a tombstone when the transaction is deleted so a delayed retry cannot
    # silently resurrect cash that the operator intentionally removed.
    bank_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InvoicePaymentAllocation(CompanyBase):
    """Explicit accounting link between one cash movement and one invoice.

    Memo/contact text may suggest a match, but it is never accounting fact.
    This row is the fact used to derive invoice paid/outstanding status.
    """

    __tablename__ = "invoice_payment_allocations"
    __table_args__ = (
        UniqueConstraint(
            "bank_transaction_id",
            "invoice_id",
            name="uq_invoice_payment_txn_invoice",
        ),
        CheckConstraint("amount > 0", name="ck_invoice_payment_amount_positive"),
        CheckConstraint(
            "gst_amount >= 0 AND gst_amount <= amount",
            name="ck_invoice_payment_gst_within",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gst_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    bank_transaction: Mapped[BankTransaction] = relationship(
        back_populates="invoice_allocations"
    )
    invoice: Mapped[Invoice] = relationship(back_populates="payment_allocations")
    tax_components: Mapped[list["InvoicePaymentTaxComponent"]] = relationship(
        back_populates="allocation",
        cascade="all, delete-orphan",
    )


class InvoicePaymentTaxComponent(CompanyBase):
    """Cash-basis tax composition captured from immutable posted invoice lines."""

    __tablename__ = "invoice_payment_tax_components"
    __table_args__ = (
        UniqueConstraint(
            "allocation_id",
            "tax_code",
            name="uq_invoice_payment_tax_component",
        ),
        CheckConstraint("gross_amount > 0", name="ck_invoice_payment_tax_gross_positive"),
        CheckConstraint(
            "gst_amount >= 0 AND gst_amount <= gross_amount",
            name="ck_invoice_payment_tax_gst_within",
        ),
        CheckConstraint(
            "tax_code IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none')",
            name="ck_invoice_payment_tax_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    allocation_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_payment_allocations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tax_code: Mapped[str] = mapped_column(String(20), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gst_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )

    allocation: Mapped[InvoicePaymentAllocation] = relationship(
        back_populates="tax_components"
    )


class AllocationHistorySnapshot(CompanyBase):
    """Immutable, integer-cent allocation state finalized with tax components."""

    __tablename__ = "allocation_history_snapshots"
    __table_args__ = (
        CheckConstraint("source_kind IN ('invoice', 'bill')", name="ck_allocation_snapshot_source_kind"),
        CheckConstraint("gross_cents > 0", name="ck_allocation_snapshot_gross_positive"),
        CheckConstraint(
            "gst_cents >= 0 AND gst_cents <= gross_cents",
            name="ck_allocation_snapshot_gst_within",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False
    )
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AllocationHistoryTaxComponent(CompanyBase):
    """Tax composition for one allocation snapshot, represented exactly in cents."""

    __tablename__ = "allocation_history_tax_components"
    __table_args__ = (
        Index(
            "uq_allocation_history_tax_component",
            "snapshot_id",
            "tax_code",
            unique=True,
        ),
        CheckConstraint("gross_cents > 0", name="ck_allocation_component_gross_positive"),
        CheckConstraint(
            "gst_cents >= 0 AND gst_cents <= gross_cents",
            name="ck_allocation_component_gst_within",
        ),
        CheckConstraint(
            "tax_code IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none')",
            name="ck_allocation_component_tax_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_history_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    tax_code: Mapped[str] = mapped_column(String(20), nullable=False)
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_cents: Mapped[int] = mapped_column(Integer, nullable=False)


class AllocationHistoryFinalization(CompanyBase):
    """One immutable proof that a snapshot's components reconcile exactly."""

    __tablename__ = "allocation_history_finalizations"

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_history_snapshots.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AllocationHistoryBaselineBinding(CompanyBase):
    """Observed legacy allocation row bound to its unknown-history marker."""

    __tablename__ = "allocation_history_baseline_bindings"
    __table_args__ = (
        Index(
            "uq_allocation_history_baseline_allocation",
            "baseline_event_id",
            "allocation_row_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    baseline_event_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_history_events.id", ondelete="RESTRICT"), nullable=False
    )
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    allocation_row_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str | None] = mapped_column(String(10))
    invoice_or_bill_id: Mapped[int | None] = mapped_column(Integer)
    allocated_gross_cents: Mapped[int | None] = mapped_column(Integer)
    allocated_gross_source: Mapped[str | None] = mapped_column(Text)
    allocated_gst_cents: Mapped[int | None] = mapped_column(Integer)
    allocated_gst_source: Mapped[str | None] = mapped_column(Text)
    allocation_created_at: Mapped[str | None] = mapped_column(Text)


class AllocationHistoryBaselineTaxBinding(CompanyBase):
    """Observed legacy tax-component row bound to one allocation binding."""

    __tablename__ = "allocation_history_baseline_tax_bindings"
    __table_args__ = (
        Index(
            "uq_allocation_history_baseline_component",
            "allocation_binding_id",
            "source_component_row_id",
            unique=True,
        ),
        Index(
            "uq_allocation_history_baseline_component_row",
            "source_component_row_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    allocation_binding_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_history_baseline_bindings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_component_row_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_code: Mapped[str | None] = mapped_column(String(20))
    component_gross_cents: Mapped[int | None] = mapped_column(Integer)
    component_gross_source: Mapped[str | None] = mapped_column(Text)
    component_gst_cents: Mapped[int | None] = mapped_column(Integer)
    component_gst_source: Mapped[str | None] = mapped_column(Text)


class AllocationHistoryBaselineFinalization(CompanyBase):
    """Immutable completion state for a legacy allocation observation."""

    __tablename__ = "allocation_history_baseline_finalizations"
    __table_args__ = (
        Index(
            "uq_allocation_history_baseline_finalization_transaction",
            "bank_transaction_id",
            unique=True,
        ),
        CheckConstraint(
            "status IN ('complete', 'incomplete')",
            name="ck_allocation_history_baseline_finalization_status",
        ),
    )

    baseline_event_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_history_events.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    observed_payment_date: Mapped[str | None] = mapped_column(String(10))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_allocation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_allocation_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    integrity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    incomplete_reason_codes: Mapped[str] = mapped_column(Text, nullable=False)


class BASPeriodLodgmentMarker(CompanyBase):
    """Internal assertion only; this is not authenticated ATO lodgment evidence."""

    __tablename__ = "bas_period_lodgment_markers"
    __table_args__ = (
        Index("uq_bas_period_lodgment_period", "period_id", unique=True),
        CheckConstraint("lodged_status = 1", name="ck_bas_period_lodged_assertion"),
        CheckConstraint(
            "evidence_scope = 'internal_assertion_not_ato_lodgment_evidence'",
            name="ck_bas_period_evidence_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_id: Mapped[str] = mapped_column(String(100), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    lodged_status: Mapped[bool] = mapped_column(Boolean, nullable=False)
    internally_asserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_placeholder: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_scope: Mapped[str] = mapped_column(
        String(80), nullable=False, default="internal_assertion_not_ato_lodgment_evidence"
    )


class AllocationPeriodCorrectionApproval(CompanyBase):
    """Append-only approval keyed to a planned correction's idempotency key."""

    __tablename__ = "allocation_period_correction_approvals"
    __table_args__ = (
        Index(
            "uq_allocation_period_correction_approval",
            "period_marker_id",
            "approved_idempotency_key",
            unique=True,
        ),
        CheckConstraint("length(trim(reason)) > 0", name="ck_allocation_approval_reason_nonblank"),
        CheckConstraint(
            "length(trim(evidence_reference)) > 0",
            name="ck_allocation_approval_evidence_nonblank",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_marker_id: Mapped[int] = mapped_column(
        ForeignKey("bas_period_lodgment_markers.id", ondelete="RESTRICT"), nullable=False
    )
    allocation_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_payment_allocations.id", ondelete="RESTRICT"), nullable=False
    )
    approved_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_placeholder: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    evidence_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AllocationHistoryEvent(CompanyBase):
    """Append-only correction, reversal, or deliberately incomplete legacy marker."""

    __tablename__ = "allocation_history_events"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('allocation_correction', 'allocation_reversal', 'legacy_history_unknown')",
            name="ck_allocation_history_operation",
        ),
        CheckConstraint(
            "(operation = 'legacy_history_unknown' AND snapshot_id IS NULL "
            "AND version IS NULL AND effective_date IS NULL AND recorded_at IS NULL "
            "AND idempotency_key IS NULL AND expected_prior_version IS NULL "
            "AND actor_placeholder IS NULL AND reason IS NULL "
            "AND evidence_reference IS NULL AND evidence_sha256 IS NULL "
            "AND linked_event_id IS NULL AND linked_allocation_id IS NULL "
            "AND approval_id IS NULL) OR "
            "(operation IN ('allocation_correction', 'allocation_reversal') "
            "AND snapshot_id IS NOT NULL AND version > 0 AND effective_date IS NOT NULL "
            "AND recorded_at IS NOT NULL AND idempotency_key IS NOT NULL "
            "AND expected_prior_version >= 0 AND actor_placeholder IS NOT NULL "
            "AND reason IS NOT NULL AND evidence_reference IS NOT NULL "
            "AND ((linked_event_id IS NOT NULL) != (linked_allocation_id IS NOT NULL)))",
            name="ck_allocation_history_operation_fields",
        ),
        Index(
            "uq_allocation_history_idempotency",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_allocation_history_version",
            "allocation_id",
            "version",
            unique=True,
            sqlite_where=text("version IS NOT NULL"),
        ),
        Index(
            "uq_allocation_history_legacy_unknown",
            "bank_transaction_id",
            unique=True,
            sqlite_where=text("operation = 'legacy_history_unknown'"),
        ),
        Index(
            "uq_allocation_history_reversed_event",
            "linked_event_id",
            unique=True,
            sqlite_where=text("operation = 'allocation_reversal' AND linked_event_id IS NOT NULL"),
        ),
        Index(
            "uq_allocation_history_reversed_allocation",
            "linked_allocation_id",
            unique=True,
            sqlite_where=text("operation = 'allocation_reversal' AND linked_allocation_id IS NOT NULL"),
        ),
        Index(
            "uq_allocation_history_snapshot",
            "snapshot_id",
            unique=True,
            sqlite_where=text("snapshot_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    allocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_payment_allocations.id", ondelete="RESTRICT")
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("allocation_history_snapshots.id", ondelete="RESTRICT")
    )
    version: Mapped[int | None] = mapped_column(Integer)
    effective_date: Mapped[date | None] = mapped_column(Date)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    expected_prior_version: Mapped[int | None] = mapped_column(Integer)
    actor_placeholder: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(String(1000))
    evidence_reference: Mapped[str | None] = mapped_column(String(500))
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    linked_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("allocation_history_events.id", ondelete="RESTRICT")
    )
    linked_allocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_payment_allocations.id", ondelete="RESTRICT")
    )
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("allocation_period_correction_approvals.id", ondelete="RESTRICT")
    )


class AllocationBatch(CompanyBase):
    """Immutable transaction-wide after-state for allocation changes."""

    __tablename__ = "allocation_batches"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('allocation_correction', 'allocation_reversal', 'allocation_reallocation')",
            name="ck_allocation_batch_operation",
        ),
        CheckConstraint(
            "transaction_version > 0 AND expected_prior_version >= 0 "
            "AND transaction_version = expected_prior_version + 1",
            name="ck_allocation_batch_version_step",
        ),
        CheckConstraint(
            "manual_review_status IN ('not_required', 'required', 'approved', 'rejected')",
            name="ck_allocation_batch_manual_review_status",
        ),
        CheckConstraint(
            "residual_amount_cents >= 0 AND "
            "((residual_amount_cents = 0 AND residual_account_id IS NULL) OR "
            "(residual_amount_cents > 0 AND residual_account_id IS NOT NULL))",
            name="ck_allocation_batch_residual_account",
        ),
        UniqueConstraint(
            "bank_transaction_id", "transaction_version",
            name="uq_allocation_batch_transaction_version",
        ),
        UniqueConstraint("idempotency_key", name="uq_allocation_batch_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    transaction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_prior_version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    evidence_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    actor_placeholder: Mapped[str] = mapped_column(String(200), nullable=False)
    linked_prior_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("allocation_batches.id", ondelete="RESTRICT")
    )
    internal_correction_approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("allocation_period_correction_approvals.id", ondelete="RESTRICT")
    )
    manual_review_status: Mapped[str] = mapped_column(String(20), nullable=False)
    residual_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    residual_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    baseline_finalization_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_history_baseline_finalizations.baseline_event_id", ondelete="RESTRICT"),
        nullable=False,
    )


class AllocationBatchLine(CompanyBase):
    """One allocation in a complete transaction-wide batch after-state."""

    __tablename__ = "allocation_batch_lines"
    __table_args__ = (
        CheckConstraint("source_kind IN ('invoice', 'bill')", name="ck_allocation_batch_line_source_kind"),
        CheckConstraint("line_order >= 0", name="ck_allocation_batch_line_order"),
        CheckConstraint("gross_cents > 0", name="ck_allocation_batch_line_gross_positive"),
        CheckConstraint(
            "gst_cents >= 0 AND gst_cents <= gross_cents",
            name="ck_allocation_batch_line_gst_within",
        ),
        UniqueConstraint("batch_id", "line_order", name="uq_allocation_batch_line_order"),
        UniqueConstraint(
            "batch_id", "source_kind", "invoice_or_bill_id",
            name="uq_allocation_batch_line_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_batches.id", ondelete="RESTRICT"), nullable=False
    )
    line_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    invoice_or_bill_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False
    )
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_cents: Mapped[int] = mapped_column(Integer, nullable=False)


class AllocationBatchTaxComponent(CompanyBase):
    """Complete immutable tax composition for one batch allocation line."""

    __tablename__ = "allocation_batch_tax_components"
    __table_args__ = (
        CheckConstraint(
            "tax_code IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none')",
            name="ck_allocation_batch_component_tax_code",
        ),
        CheckConstraint("gross_cents > 0", name="ck_allocation_batch_component_gross_positive"),
        CheckConstraint(
            "gst_cents >= 0 AND gst_cents <= gross_cents",
            name="ck_allocation_batch_component_gst_within",
        ),
        UniqueConstraint(
            "allocation_batch_line_id", "tax_code",
            name="uq_allocation_batch_component_tax_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    allocation_batch_line_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_batch_lines.id", ondelete="RESTRICT"), nullable=False
    )
    tax_code: Mapped[str] = mapped_column(String(20), nullable=False)
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    gst_cents: Mapped[int] = mapped_column(Integer, nullable=False)


class AllocationBatchFinalization(CompanyBase):
    """Immutable reconciliation proof that makes one allocation batch usable."""

    __tablename__ = "allocation_batch_finalizations"

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("allocation_batches.id", ondelete="RESTRICT"), primary_key=True
    )
    allocation_line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_allocated_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    total_gst_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    residual_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    bank_cash_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_integrity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaymentReconciliationEvent(CompanyBase):
    """Immutable record of a fail-safe legacy payment repair at upgrade."""

    __tablename__ = "payment_reconciliation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), index=True
    )
    bank_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Manual journal entries (M2.1)
#
# Used for the things Invoice / Outgoing / Bank don't cover:
#   - opening balances at company creation
#   - period-end adjustments (accruals, prepayments)
#   - depreciation
#   - bad debt write-offs
#   - bank interest / fees not tied to a categorised transaction
#   - any other manual correction
#
# Per the M2.1 design (C-route): journal entries do NOT mirror activity
# from the other modules. They are an additive ledger that complements
# them. Reports add the two sources together.
# ---------------------------------------------------------------------------


class JournalEntrySource(str, Enum):
    MANUAL = "manual"
    INVOICE_AR = "invoice_ar"
    INVOICE_AP = "invoice_ap"
    INVOICE_REVERSAL = "invoice_reversal"
    CREDIT_NOTE_AR = "credit_note_ar"
    CREDIT_NOTE_AP = "credit_note_ap"
    CREDIT_NOTE_VOID_AR = "credit_note_void_ar"
    CREDIT_NOTE_VOID_AP = "credit_note_void_ap"
    REFUND_AR = "refund_ar"
    REFUND_AP = "refund_ap"
    REFUND_REVERSAL_AR = "refund_reversal_ar"
    REFUND_REVERSAL_AP = "refund_reversal_ap"


class JournalEntry(CompanyBase):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    memo: Mapped[str] = mapped_column(String(500), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))  # e.g. cheque number, doc ref
    source_type: Mapped[JournalEntrySource] = mapped_column(
        String(20), nullable=False, default=JournalEntrySource.MANUAL, index=True
    )
    source_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reverses_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalLine.id",
    )
    reverses_entry: Mapped["JournalEntry | None"] = relationship(
        remote_side="JournalEntry.id",
        foreign_keys=[reverses_entry_id],
    )


class JournalIdempotencyKey(CompanyBase):
    """Persisted ownership of one manual-journal create request.

    The opaque client key is company-local because every company has its own
    database.  Keeping the parsed-payload hash lets a safe retry return the
    original entry while rejecting accidental key reuse for different money.
    """

    __tablename__ = "journal_idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    entry: Mapped[JournalEntry] = relationship()


class BankRule(CompanyBase):
    """Per-company auto-categorisation rule for imported bank transactions.

    On import the rules are evaluated in `priority` ascending order; the
    first one whose match clauses all pass wins and sets account_id +
    tax_code on the new transaction. The user can still override after.

    Matching is conjunctive: every non-null match_* must pass. A rule
    with all match_* null fires on every row (useful for "all OUT goes
    to misc" sweepers).
    """

    __tablename__ = "bank_rules"
    __table_args__ = (
        CheckConstraint(
            "match_amount_min IS NULL OR match_amount_max IS NULL "
            "OR match_amount_min <= match_amount_max",
            name="ck_bank_rule_amount_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)

    match_direction: Mapped[str | None] = mapped_column(String(3))   # 'in' | 'out' | None
    match_amount_min: Mapped[Decimal | None] = mapped_column(MONEY)
    match_amount_max: Mapped[Decimal | None] = mapped_column(MONEY)
    match_memo_regex: Mapped[str | None] = mapped_column(String(500))
    match_counter_party_regex: Mapped[str | None] = mapped_column(String(500))

    set_account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    set_tax_code: Mapped[TaxCode] = mapped_column(
        String(20), nullable=False, default=TaxCode.STANDARD
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    set_account: Mapped[Account] = relationship()


class JournalLine(CompanyBase):
    """A single debit or credit posting against one account.

    Sign convention: debit_amount and credit_amount are both >= 0 and
    exactly one of them is > 0 (enforced by CHECK). The service layer
    additionally enforces sum(debit) == sum(credit) across all lines of
    an entry — SQLite can't express a row-aggregate CHECK.
    """

    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit_amount >= 0", name="ck_journal_line_debit_nonneg"),
        CheckConstraint("credit_amount >= 0", name="ck_journal_line_credit_nonneg"),
        CheckConstraint(
            "(debit_amount > 0 AND credit_amount = 0) "
            "OR (credit_amount > 0 AND debit_amount = 0)",
            name="ck_journal_line_one_sided",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    debit_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    credit_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    description: Mapped[str | None] = mapped_column(String(500))

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()


class StaffMember(CompanyBase):
    """A firm staff member selectable as a document signer.

    A staff member may have a MARA registration (MARN), be a legal
    practitioner (LPN), or have no registration number at all.
    """

    __tablename__ = "staff_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # "mara" | "lpn" | "none"
    registration_type: Mapped[str] = mapped_column(
        String(10), default="none", nullable=False
    )
    registration_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
