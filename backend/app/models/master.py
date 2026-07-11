"""Tables that live in the master DB (data/master.db).

Only company-registry-level data goes here. Nothing about journals, accounts,
or transactions — those live in the per-company books.db.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import MasterBase


class Company(MasterBase):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # short slug, used as folder name
    # A slug can be re-used after deletion, but a generation id never can.  It
    # lets the API distinguish an old browser tab for company A1 from a newly
    # created company A2 that happens to use the same folder slug.
    generation_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid4()), nullable=False, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    abn: Mapped[str | None] = mapped_column(String(20))  # Australian Business Number
    country: Mapped[str] = mapped_column(String(2), default="AU", nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), default="AUD", nullable=False)
    fy_start_month: Mapped[int] = mapped_column(default=7, nullable=False)  # AU FY = Jul-Jun
    gst_registered: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Show Chinese label translations alongside English on outgoing-document
    # PDFs (services/doc_labels.py). server_default so the additive schema
    # sync can ALTER existing master DBs with a NOT NULL column.
    bilingual_labels: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default="0"
    )

    # Contact/address block — printed on outgoing documents (Invoice / Payment Request / Receipt).
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    suburb: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(20))
    postcode: Mapped[str | None] = mapped_column(String(10))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(200))

    # Bank/payment details — printed in the "PAYMENT METHOD" block.
    bank_account_name: Mapped[str | None] = mapped_column(String(200))
    bank_name: Mapped[str | None] = mapped_column(String(100))
    bank_bsb: Mapped[str | None] = mapped_column(String(10))
    bank_account_number: Mapped[str | None] = mapped_column(String(30))
    bank_swift: Mapped[str | None] = mapped_column(String(20))

    # Operating (general business) account, e.g. a partner paying the firm a
    # referral fee. Printed on partner documents; the bank fields above are
    # printed on client invoices / SAs.
    operating_bank_account_name: Mapped[str | None] = mapped_column(String(200))
    operating_bank_name: Mapped[str | None] = mapped_column(String(100))
    operating_bank_bsb: Mapped[str | None] = mapped_column(String(10))
    operating_bank_account_number: Mapped[str | None] = mapped_column(String(30))
    operating_bank_swift: Mapped[str | None] = mapped_column(String(20))

    default_payment_terms_days: Mapped[int] = mapped_column(default=28, nullable=False)

    # Accounting periods are closed monotonically.  Every dated write path
    # rejects dates on or before this boundary; keeping the boundary in the
    # master registry lets the company identity dependency provide one fresh,
    # authoritative policy snapshot for each request.
    books_locked_through: Mapped[date | None] = mapped_column()

    acn: Mapped[str | None] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
