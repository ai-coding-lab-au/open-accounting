"""Tables that live in the master DB (data/master.db).

Only company-registry-level data goes here. Nothing about journals, accounts,
or transactions — those live in the per-company books.db.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from ..db.base import MasterBase


def validate_credential_version(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("credential_version must be an integer or None")
    if value < 0:
        raise ValueError("credential_version must be nonnegative")
    return value


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)


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


class AuthenticatedSubject(MasterBase):
    """Stable authenticated principal identity records for the master DB.

    Only a trusted authentication adapter may create or supply these rows; the
    application never infers identity from company headers, legacy actor strings,
    or display text. Subject identity and credential version are immutable;
    credential_version is trusted adapter metadata rather than untrusted request
    input, and deactivation is permanent for this row.
    """

    __tablename__ = "authenticated_subjects"
    __table_args__ = (
        # SQLite validates the stored integer domain after INTEGER affinity;
        # strict source-language types are enforced at the Python write boundary.
        CheckConstraint(
            "length(trim(subject_id)) > 0",
            name="ck_authenticated_subjects_subject_id_nonblank",
        ),
        CheckConstraint(
            "length(subject_id) <= 128",
            name="ck_authenticated_subjects_subject_id_length",
        ),
        CheckConstraint(
            "authentication_method IN ('internal_adapter', 'trusted_adapter')",
            name="ck_authenticated_subjects_auth_method",
        ),
        CheckConstraint(
            "provider_subject IS NULL OR length(trim(provider_subject)) > 0",
            name="ck_authenticated_subjects_provider_subject",
        ),
        CheckConstraint(
            "credential_version IS NULL OR (typeof(credential_version) = 'integer' AND credential_version >= 0)",
            name="ck_authenticated_subjects_credential_version",
        ),
        CheckConstraint(
            "active IN (0, 1) AND ((active = 1 AND deactivated_at IS NULL) OR (active = 0 AND deactivated_at IS NOT NULL))",
            name="ck_authenticated_subjects_lifecycle_state",
        ),
        Index("ix_authenticated_subjects_active", "active", "subject_id"),
    )

    subject_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    authentication_method: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_subject: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    credential_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))

    @validates("credential_version")
    def _validate_credential_version(self, key: str, value: Any) -> int | None:
        return validate_credential_version(value)


class CompanyMembership(MasterBase):
    """One immutable subject/company/generation relationship.

    Deactivation is permanent for this row. Regranting a revoked relationship
    requires a separately designed lifecycle model and is out of scope.
    """

    __tablename__ = "company_memberships"
    __table_args__ = (
        CheckConstraint(
            "length(trim(company_id)) > 0",
            name="ck_company_memberships_company_id_nonblank",
        ),
        CheckConstraint(
            "company_generation >= 0",
            name="ck_company_memberships_generation_nonnegative",
        ),
        CheckConstraint(
            "valid_from IS NOT NULL",
            name="ck_company_memberships_valid_from_required",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from < valid_until",
            name="ck_company_memberships_valid_window",
        ),
        CheckConstraint(
            "active IN (0, 1) AND ((active = 1 AND deactivated_at IS NULL) OR (active = 0 AND deactivated_at IS NOT NULL))",
            name="ck_company_memberships_lifecycle_state",
        ),
        UniqueConstraint(
            "subject_id",
            "company_id",
            "company_generation",
            name="uq_company_membership_identity",
        ),
        Index(
            "ix_company_memberships_subject_company_generation",
            "subject_id",
            "company_id",
            "company_generation",
        ),
        Index("ix_company_memberships_active_lookup", "company_id", "company_generation", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("authenticated_subjects.subject_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[str] = mapped_column(String(32), nullable=False)
    company_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))


class CompanyCapabilityGrant(MasterBase):
    """One immutable capability grant within a company membership.

    Revocation is permanent for this row. Regranting a revoked capability
    requires a separately designed lifecycle model and is out of scope.
    """

    __tablename__ = "company_capability_grants"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('allocation_review_propose', 'allocation_review_decide', 'allocation_review_execute')",
            name="ck_company_capability_grants_capability",
        ),
        CheckConstraint(
            "valid_from IS NOT NULL",
            name="ck_company_capability_grants_valid_from_required",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from < valid_until",
            name="ck_company_capability_grants_valid_window",
        ),
        CheckConstraint(
            "active IN (0, 1) AND ((active = 1 AND revoked_at IS NULL) OR (active = 0 AND revoked_at IS NOT NULL))",
            name="ck_company_capability_grants_lifecycle_state",
        ),
        UniqueConstraint(
            "membership_id",
            "capability",
            name="uq_company_capability_grant_membership_capability",
        ),
        Index(
            "ix_company_capability_grants_membership_capability",
            "membership_id",
            "capability",
            "active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    membership_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("company_memberships.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
