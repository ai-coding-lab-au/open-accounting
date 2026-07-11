"""Per-company tables (books.db).

M1 ships Account (Chart of Accounts).
Invoice import phase adds: Contact, Invoice, InvoiceLine, Attachment.
Later milestones will add: JournalEntry/JournalLine, BankAccount, BankTransaction,
BankRule, etc. Importing this module registers all mappings against
CompanyBase.metadata so create_all() builds the full schema.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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


# Decimal precision used throughout: 2 decimal places, up to 14 integer digits.
MONEY = Numeric(16, 2)


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
