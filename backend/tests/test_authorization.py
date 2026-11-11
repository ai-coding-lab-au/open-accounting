from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import MasterBase
from app.db import master as master_db
from app.db.errors import DataRecoveryRequiredError
from app.db.schema_sync import MASTER_AUTHORIZATION_INDEX_SIGNATURES, sqlite_index_matches
from app.models.master import (
    AuthenticatedSubject,
    CompanyCapabilityGrant,
    CompanyMembership,
)
from app.services.authorization import (
    AmbiguousAuthorizationStateError,
    AuthenticatedPrincipal,
    CapabilityGrantExpiredError,
    CapabilityGrantNotYetValidError,
    CapabilityRequiredError,
    CompanyContext,
    CompanyGenerationMismatchError,
    CompanyMembershipRequiredError,
    InactiveAuthenticatedSubjectError,
    InactiveCapabilityGrantError,
    InactiveCompanyMembershipError,
    InvalidAuthenticatedPrincipalError,
    InvalidAuthorizationTimestampError,
    MissingAuthenticatedPrincipalError,
    SelfApprovalForbiddenError,
    UnknownAuthenticatedSubjectError,
    require_allocation_review_capability,
    require_allocation_review_policy,
    require_distinct_allocation_reviewer,
    resolve_company_principal,
)


@pytest.fixture
def master_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'master.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    MasterBase.metadata.create_all(engine)
    master_db._repair_master_authorization_schema(engine)
    session = Session(engine, autoflush=False, autocommit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def principal():
    return AuthenticatedPrincipal(
        subject_id="subject-123",
        authentication_method="internal_adapter",
        authenticated_at=datetime(2026, 1, 1, tzinfo=UTC),
        credential_version=2,
    )


@pytest.fixture
def company_context():
    return CompanyContext(company_id="c1", generation=7)


def _add_subject(
    session: Session,
    *,
    subject_id: str,
    active: bool = True,
    credential_version: int | None = 2,
    created_at: datetime | None = None,
):
    subject = AuthenticatedSubject(
        subject_id=subject_id,
        authentication_method="internal_adapter",
        provider_subject=subject_id,
        active=True,
        credential_version=credential_version,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        deactivated_at=None,
    )
    session.add(subject)
    session.commit()
    if not active:
        subject.active = False
        subject.deactivated_at = datetime(2026, 1, 2, tzinfo=UTC)
        session.commit()
    return subject


def _add_membership(
    session: Session,
    *,
    subject_id: str,
    company_id: str,
    generation: int,
    active: bool = True,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    created_at: datetime | None = None,
):
    membership = CompanyMembership(
        subject_id=subject_id,
        company_id=company_id,
        company_generation=generation,
        active=True,
        valid_from=valid_from or datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=valid_until,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        deactivated_at=None,
    )
    session.add(membership)
    session.commit()
    if not active:
        membership.active = False
        membership.deactivated_at = datetime(2026, 1, 2, tzinfo=UTC)
        session.commit()
    return membership


def _add_grant(
    session: Session,
    *,
    membership_id: int,
    capability: str,
    active: bool = True,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    created_at: datetime | None = None,
):
    grant = CompanyCapabilityGrant(
        membership_id=membership_id,
        capability=capability,
        active=True,
        valid_from=valid_from or datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=valid_until,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        revoked_at=None,
    )
    session.add(grant)
    session.commit()
    if not active:
        grant.active = False
        grant.revoked_at = datetime(2026, 1, 2, tzinfo=UTC)
        session.commit()
    return grant


@pytest.fixture
def production_master(tmp_path, monkeypatch):
    data_dir = tmp_path / "isolated-master-data"
    data_dir.mkdir()
    db_path = data_dir / "master.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    monkeypatch.setattr(master_db, "settings", SimpleNamespace(data_dir=data_dir, master_db_path=db_path))
    monkeypatch.setattr(master_db, "master_engine", engine)
    monkeypatch.setattr(master_db, "_master_initialised", False)
    yield engine, db_path
    engine.dispose()


def test_valid_immutable_principal(principal):
    assert principal.subject_id == "subject-123"
    assert principal.authentication_method == "internal_adapter"
    assert principal.credential_version == 2


def test_principal_is_immutable(principal):
    with pytest.raises(FrozenInstanceError):
        principal.subject_id = "other-subject"


@pytest.mark.parametrize("credential_version", [None, 0, 1, 2, 99])
def test_principal_accepts_optional_nonnegative_integer_credential_versions(credential_version):
    principal = AuthenticatedPrincipal(
        "subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), credential_version
    )
    assert principal.credential_version == credential_version


@pytest.mark.parametrize(
    "credential_version",
    [-1, True, False, 1.0, Decimal("1"), "1", b"1", object()],
)
def test_principal_rejects_non_integer_or_invalid_credential_versions(credential_version):
    with pytest.raises(InvalidAuthenticatedPrincipalError):
        AuthenticatedPrincipal(
            "subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), credential_version
        )


def test_authorization_datetimes_are_stored_as_canonical_utc(master_session):
    local_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    master_session.add(
        AuthenticatedSubject(
            subject_id="subject-utc-storage",
            authentication_method="internal_adapter",
            provider_subject="subject-utc-storage",
            active=True,
            credential_version=0,
            created_at=local_time,
            deactivated_at=None,
        )
    )
    master_session.commit()
    stored = master_session.execute(
        text("SELECT created_at FROM authenticated_subjects WHERE subject_id='subject-utc-storage'")
    ).scalar_one()
    assert stored == "2026-01-01 10:00:00.000000"
    assert master_session.get(AuthenticatedSubject, "subject-utc-storage").created_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("credential_version", [None, 0, 1, 2])
def test_database_accepts_valid_credential_versions(master_session, credential_version):
    _add_subject(
        master_session,
        subject_id=f"subject-version-{credential_version}",
        credential_version=credential_version,
    )


@pytest.mark.parametrize(
    "credential_version",
    [-1, True, False, 1.0, Decimal("1"), "1", b"1", object()],
)
def test_orm_rejects_invalid_credential_version_before_flush(
    master_session, credential_version
):
    with pytest.raises((TypeError, ValueError)):
        AuthenticatedSubject(
            subject_id="subject-invalid-version",
            authentication_method="internal_adapter",
            provider_subject="subject-invalid-version",
            active=True,
            credential_version=credential_version,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            deactivated_at=None,
        )
    assert master_session.execute(select(AuthenticatedSubject)).scalars().all() == []


def test_sqlite_stored_domain_rejects_negative_credential_version(master_session):
    with pytest.raises(IntegrityError):
        master_session.execute(
            text(
                "INSERT INTO authenticated_subjects "
                "(subject_id, authentication_method, provider_subject, active, "
                "credential_version, created_at) "
                "VALUES ('subject-negative-version', 'internal_adapter', "
                "'subject-negative-version', 1, -1, '2026-01-01 00:00:00.000000')"
            )
        )
    master_session.rollback()


@pytest.mark.parametrize(
    ("source_kind", "credential_version"),
    [("bool", True), ("float", 1.0), ("string", "1")],
)
def test_sqlite_integer_affinity_validates_stored_domain_not_source_type(
    master_session, source_kind, credential_version
):
    """INTEGER affinity may coerce host-language inputs before CHECK evaluation."""
    master_session.execute(
        text(
            "INSERT INTO authenticated_subjects "
            "(subject_id, authentication_method, provider_subject, active, "
            "credential_version, created_at) "
            "VALUES (:subject_id, 'internal_adapter', :provider_subject, 1, "
            ":credential_version, '2026-01-01 00:00:00.000000')"
        ),
        {
            "subject_id": f"subject-affinity-{source_kind}",
            "provider_subject": f"subject-affinity-{source_kind}",
            "credential_version": credential_version,
        },
    )
    stored_type, stored_value = master_session.execute(
        text(
            "SELECT typeof(credential_version), credential_version FROM authenticated_subjects "
            "WHERE subject_id=:subject_id"
        ),
        {"subject_id": f"subject-affinity-{source_kind}"},
    ).one()
    assert stored_type == "integer"
    assert stored_value == 1
    master_session.commit()


def test_principal_cannot_be_created_from_dict():
    with pytest.raises((TypeError, InvalidAuthenticatedPrincipalError)):
        AuthenticatedPrincipal({"subject_id": "x"}, "internal_adapter", datetime.now(UTC), None)


def test_missing_principal_raises():
    with pytest.raises(MissingAuthenticatedPrincipalError):
        resolve_company_principal(None, None, CompanyContext("c1", 7), "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_blank_subject_id_raises():
    with pytest.raises(InvalidAuthenticatedPrincipalError):
        AuthenticatedPrincipal("   ", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)


def test_inactive_subject_raises(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123", active=False)
    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    with pytest.raises(InactiveAuthenticatedSubjectError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_unknown_subject_raises(master_session, company_context, principal):
    with pytest.raises(UnknownAuthenticatedSubjectError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_valid_active_membership_and_capability_passes(master_session, company_context):
    subject = _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id=subject.subject_id, company_id="c1", generation=7)
    _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose")

    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    result = resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))
    assert result.subject_id == "subject-123"
    assert result.company_id == "c1"
    assert result.company_generation == 7
    assert result.capability == "allocation_review_propose"
    assert result.membership_id == membership.id


@pytest.mark.parametrize("credential_version", [0, 1])
def test_authorization_resolution_accepts_persisted_credential_versions(
    master_session, company_context, credential_version
):
    subject = _add_subject(
        master_session,
        subject_id=f"subject-resolve-version-{credential_version}",
        credential_version=credential_version,
    )
    membership = _add_membership(
        master_session,
        subject_id=subject.subject_id,
        company_id=company_context.company_id,
        generation=company_context.generation,
    )
    _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose")
    result = resolve_company_principal(
        master_session,
        AuthenticatedPrincipal(subject.subject_id, "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), credential_version),
        company_context,
        "allocation_review_propose",
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert result.subject_id == subject.subject_id


def test_missing_membership_raises(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    with pytest.raises(CompanyMembershipRequiredError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_inactive_membership_raises(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7, active=False)
    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    with pytest.raises(InactiveCompanyMembershipError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_company_generation_mismatch_raises(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=8)
    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    with pytest.raises(CompanyGenerationMismatchError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_missing_capability_raises(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    with pytest.raises(CapabilityRequiredError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_inactive_capability_raises(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose", active=False)
    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    with pytest.raises(InactiveCapabilityGrantError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_self_review_is_rejected():
    proposer = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    reviewer = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    with pytest.raises(SelfApprovalForbiddenError):
        require_distinct_allocation_reviewer(proposer, reviewer)


def test_require_distinct_allocation_reviewer_accepts_different_subjects():
    proposer = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    reviewer = AuthenticatedPrincipal("subject-456", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    require_distinct_allocation_reviewer(proposer, reviewer)


def test_company_context_rejects_invalid_generation():
    with pytest.raises(TypeError):
        CompanyContext(company_id="c1", generation=True)


@pytest.mark.parametrize(
    "company_id",
    [None, 1, True, b"c1", {"id": "c1"}, ["c1"], object(), "", "   ", " c1", "c1 ", "a" * 33],
)
def test_company_context_rejects_noncanonical_company_ids(company_id):
    with pytest.raises((TypeError, ValueError)):
        CompanyContext(company_id=company_id, generation=7)


@pytest.mark.parametrize(
    "capability",
    ["allocation_review_propose", "allocation_review_decide", "allocation_review_execute"],
)
def test_require_allocation_review_capability_uses_required_capability(
    master_session, company_context, capability
):
    _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    _add_grant(master_session, membership_id=membership.id, capability=capability)
    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    result = require_allocation_review_capability(master_session, principal, company_context, capability, datetime(2026, 1, 2, tzinfo=UTC))
    assert result.capability == capability


def test_membership_future_and_expired_validity_rejected(master_session, company_context):
    _add_subject(master_session, subject_id="subject-future")
    future = _add_membership(
        master_session,
        subject_id="subject-future",
        company_id="c1",
        generation=7,
        valid_from=datetime(2027, 1, 1, tzinfo=UTC),
        valid_until=datetime(2027, 2, 1, tzinfo=UTC),
    )
    _add_grant(master_session, membership_id=future.id, capability="allocation_review_propose")
    _add_subject(master_session, subject_id="subject-expired")
    expired = _add_membership(
        master_session,
        subject_id="subject-expired",
        company_id="c1",
        generation=7,
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        valid_until=datetime(2025, 2, 1, tzinfo=UTC),
    )
    _add_grant(master_session, membership_id=expired.id, capability="allocation_review_propose")

    future_principal = AuthenticatedPrincipal("subject-future", "internal_adapter", datetime(2026, 1, 2, tzinfo=UTC), None)
    with pytest.raises(CapabilityGrantNotYetValidError):
        resolve_company_principal(master_session, future_principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))

    expired_principal = AuthenticatedPrincipal("subject-expired", "internal_adapter", datetime(2026, 1, 2, tzinfo=UTC), None)
    with pytest.raises(InactiveCompanyMembershipError):
        resolve_company_principal(master_session, expired_principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_grant_valid_from_boundary_and_valid_until_boundary(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    _add_grant(
        master_session,
        membership_id=membership.id,
        capability="allocation_review_propose",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 1, 10, tzinfo=UTC),
    )

    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    result = resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 1, tzinfo=UTC))
    assert result.capability == "allocation_review_propose"

    with pytest.raises(CapabilityGrantExpiredError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 10, tzinfo=UTC))


def test_membership_validity_boundaries(master_session, company_context):
    _add_subject(master_session, subject_id="subject-membership-boundary")
    membership = _add_membership(
        master_session,
        subject_id="subject-membership-boundary",
        company_id=company_context.company_id,
        generation=company_context.generation,
        valid_from=datetime(2026, 1, 2, tzinfo=UTC),
        valid_until=datetime(2026, 1, 4, tzinfo=UTC),
    )
    _add_grant(
        master_session,
        membership_id=membership.id,
        capability="allocation_review_propose",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 1, 10, tzinfo=UTC),
    )
    principal = AuthenticatedPrincipal(
        "subject-membership-boundary", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None
    )

    for operation_at in (
        datetime(2026, 1, 2, tzinfo=UTC),
        datetime(2026, 1, 4, tzinfo=UTC) - timedelta(microseconds=1),
    ):
        resolve_company_principal(
            master_session,
            principal,
            company_context,
            "allocation_review_propose",
            operation_at,
        )
    with pytest.raises(CapabilityGrantNotYetValidError):
        resolve_company_principal(
            master_session,
            principal,
            company_context,
            "allocation_review_propose",
            datetime(2026, 1, 2, tzinfo=UTC) - timedelta(microseconds=1),
        )
    with pytest.raises(InactiveCompanyMembershipError):
        resolve_company_principal(
            master_session,
            principal,
            company_context,
            "allocation_review_propose",
            datetime(2026, 1, 4, tzinfo=UTC),
        )


def test_future_and_expired_grants_are_rejected(master_session, company_context):
    _add_subject(master_session, subject_id="subject-future-grant")
    future_membership = _add_membership(
        master_session,
        subject_id="subject-future-grant",
        company_id="c1",
        generation=7,
    )
    _add_grant(
        master_session,
        membership_id=future_membership.id,
        capability="allocation_review_propose",
        valid_from=datetime(2027, 1, 1, tzinfo=UTC),
    )
    _add_subject(master_session, subject_id="subject-expired-grant")
    expired_membership = _add_membership(
        master_session,
        subject_id="subject-expired-grant",
        company_id="c1",
        generation=7,
    )
    _add_grant(
        master_session,
        membership_id=expired_membership.id,
        capability="allocation_review_propose",
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        valid_until=datetime(2025, 2, 1, tzinfo=UTC),
    )

    future_principal = AuthenticatedPrincipal("subject-future-grant", "internal_adapter", datetime(2026, 1, 2, tzinfo=UTC), None)
    with pytest.raises(CapabilityGrantNotYetValidError):
        resolve_company_principal(master_session, future_principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))
    expired_principal = AuthenticatedPrincipal("subject-expired-grant", "internal_adapter", datetime(2026, 1, 2, tzinfo=UTC), None)
    with pytest.raises(CapabilityGrantExpiredError):
        resolve_company_principal(master_session, expired_principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


@pytest.mark.parametrize(
    ("record_type", "field"),
    [
        ("membership", "valid_from"),
        ("membership", "valid_until"),
        ("membership", "deactivated_at"),
        ("grant", "valid_from"),
        ("grant", "valid_until"),
        ("grant", "revoked_at"),
        ("subject", "deactivated_at"),
    ],
)
def test_malformed_authorization_timestamp_fields_fail_closed(
    master_session, company_context, record_type, field
):
    _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    grant = _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose")
    subject = master_session.get(AuthenticatedSubject, "subject-123")
    record = {"subject": subject, "membership": membership, "grant": grant}[record_type]
    setattr(record, field, "not-a-date")

    principal = AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 2, tzinfo=UTC), None)
    with pytest.raises(InvalidAuthorizationTimestampError):
        resolve_company_principal(master_session, principal, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))


def test_ambiguous_memberships_and_grants_fail_closed(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    with pytest.raises(IntegrityError):
        master_session.add(
            CompanyMembership(
                subject_id="subject-123",
                company_id="c1",
                company_generation=7,
                active=True,
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                deactivated_at=None,
            )
        )
        master_session.commit()
    master_session.rollback()

    membership = master_session.execute(select(CompanyMembership).where(CompanyMembership.subject_id == "subject-123")).scalar_one()
    _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose")
    with pytest.raises(IntegrityError):
        master_session.add(
            CompanyCapabilityGrant(
                membership_id=membership.id,
                capability="allocation_review_propose",
                active=True,
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                revoked_at=None,
            )
        )
        master_session.commit()
    master_session.rollback()


def test_duplicate_membership_and_grant_constraints(master_session):
    _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    with pytest.raises(IntegrityError):
        master_session.add(
            CompanyMembership(
                subject_id="subject-123",
                company_id="c1",
                company_generation=7,
                active=True,
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                deactivated_at=None,
            )
        )
        master_session.commit()
    master_session.rollback()

    _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose")
    with pytest.raises(IntegrityError):
        master_session.add(
            CompanyCapabilityGrant(
                membership_id=membership.id,
                capability="allocation_review_propose",
                active=True,
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                revoked_at=None,
            )
        )
        master_session.commit()
    master_session.rollback()


def test_concurrent_duplicate_membership_and_grant_creation_is_constrained(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth-dup.db'}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _handle(conn, _):
        conn.execute("PRAGMA foreign_keys = ON")

    MasterBase.metadata.create_all(engine)
    subject = AuthenticatedSubject(
        subject_id="subject-123",
        authentication_method="internal_adapter",
        provider_subject="subject-123",
        active=True,
        credential_version=2,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        deactivated_at=None,
    )
    with Session(engine) as session:
        session.add(subject)
        session.commit()

    def insert_membership(barrier):
        barrier.wait()
        try:
            with Session(engine) as session:
                session.add(
                    CompanyMembership(
                        subject_id="subject-123",
                        company_id="c1",
                        company_generation=7,
                        active=True,
                        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                        valid_until=None,
                        created_at=datetime(2026, 1, 1, tzinfo=UTC),
                        deactivated_at=None,
                    )
                )
                session.commit()
            return "inserted"
        except Exception as exc:
            return exc

    membership_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        membership_futures = [
            executor.submit(insert_membership, membership_barrier)
            for _ in range(2)
        ]
        membership_results = [future.result(timeout=15) for future in membership_futures]
    membership_errors = [result for result in membership_results if isinstance(result, Exception)]
    assert membership_results.count("inserted") == 1
    assert len(membership_errors) == 1
    assert isinstance(membership_errors[0], IntegrityError)

    with Session(engine) as session:
        count = len(session.execute(select(CompanyMembership).where(CompanyMembership.subject_id == "subject-123")).scalars().all())
        assert count == 1

    with Session(engine) as session:
        membership = session.execute(select(CompanyMembership).where(CompanyMembership.subject_id == "subject-123")).scalars().first()
        membership_id = membership.id

        def insert_grant(barrier):
            barrier.wait()
            try:
                with Session(engine) as session2:
                    session2.add(
                        CompanyCapabilityGrant(
                            membership_id=membership_id,
                            capability="allocation_review_propose",
                            active=True,
                            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                            valid_until=None,
                            created_at=datetime(2026, 1, 1, tzinfo=UTC),
                            revoked_at=None,
                        )
                    )
                    session2.commit()
                return "inserted"
            except Exception as exc:
                return exc

        grant_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            grant_futures = [
                executor.submit(insert_grant, grant_barrier) for _ in range(2)
            ]
            grant_results = [future.result(timeout=15) for future in grant_futures]
        grant_errors = [result for result in grant_results if isinstance(result, Exception)]
        assert grant_results.count("inserted") == 1
        assert len(grant_errors) == 1
        assert isinstance(grant_errors[0], IntegrityError)

        grant_count = len(session.execute(select(CompanyCapabilityGrant).where(CompanyCapabilityGrant.membership_id == membership_id)).scalars().all())
        assert grant_count == 1
        engine.dispose()


def test_no_internal_commit_and_setup_rollback(master_session):
    fake = Mock(spec=Session)
    fake.get.return_value = None
    fake.execute.return_value = Mock()
    with pytest.raises(MissingAuthenticatedPrincipalError):
        resolve_company_principal(fake, None, CompanyContext("c1", 7), "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))
    fake.commit.assert_not_called()

    subject = _add_subject(master_session, subject_id="subject-success")
    membership = _add_membership(master_session, subject_id=subject.subject_id, company_id="c1", generation=7)
    _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose")
    commits = []
    on_commit = lambda _: commits.append(True)
    event.listen(master_session, "after_commit", on_commit)
    resolve_company_principal(
        master_session,
        AuthenticatedPrincipal("subject-success", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None),
        CompanyContext("c1", 7),
        "allocation_review_propose",
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    event.remove(master_session, "after_commit", on_commit)
    assert commits == []

    subject = _add_subject(master_session, subject_id="subject-setup")
    membership = CompanyMembership(
        subject_id=subject.subject_id,
        company_id="c1",
        company_generation=7,
        active=True,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        deactivated_at=None,
    )
    master_session.add(membership)
    master_session.rollback()
    assert master_session.execute(
        select(CompanyMembership).where(CompanyMembership.subject_id == "subject-setup")
    ).scalars().all() == []


def test_authorization_failure_writes_nothing(master_session, company_context):
    _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(master_session, subject_id="subject-123", company_id="c1", generation=7)
    with pytest.raises(CapabilityRequiredError):
        resolve_company_principal(master_session, AuthenticatedPrincipal("subject-123", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None), company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))
    assert master_session.execute(select(CompanyMembership).where(CompanyMembership.id == membership.id)).scalars().first() is not None
    assert master_session.execute(select(CompanyCapabilityGrant).where(CompanyCapabilityGrant.membership_id == membership.id)).scalars().all() == []


def _assert_rejected_mutation_preserves_row(
    session: Session,
    *,
    table: str,
    identity_column: str,
    row_id,
    statement: str,
    parameters: dict,
):
    before = session.execute(
        text(f'SELECT * FROM "{table}" WHERE "{identity_column}" = :row_id'),
        {"row_id": row_id},
    ).mappings().one()
    with pytest.raises(IntegrityError):
        session.execute(text(statement), parameters)
    session.rollback()
    after = session.execute(
        text(f'SELECT * FROM "{table}" WHERE "{identity_column}" = :row_id'),
        {"row_id": row_id},
    ).mappings().one()
    assert dict(after) == dict(before)


def test_authority_rows_cannot_be_deleted(master_session):
    subject = _add_subject(master_session, subject_id="subject-123")
    _assert_rejected_mutation_preserves_row(
        master_session,
        table="authenticated_subjects",
        identity_column="subject_id",
        row_id=subject.subject_id,
        statement="DELETE FROM authenticated_subjects WHERE subject_id=:row_id",
        parameters={"row_id": subject.subject_id},
    )

    membership = _add_membership(master_session, subject_id=subject.subject_id, company_id="c1", generation=7)
    _assert_rejected_mutation_preserves_row(
        master_session,
        table="company_memberships",
        identity_column="id",
        row_id=membership.id,
        statement="DELETE FROM company_memberships WHERE id=:row_id",
        parameters={"row_id": membership.id},
    )

    grant = _add_grant(master_session, membership_id=membership.id, capability="allocation_review_propose")
    _assert_rejected_mutation_preserves_row(
        master_session,
        table="company_capability_grants",
        identity_column="id",
        row_id=grant.id,
        statement="DELETE FROM company_capability_grants WHERE id=:row_id",
        parameters={"row_id": grant.id},
    )


def test_authority_identity_and_validity_fields_are_immutable(master_session):
    subject = _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(
        master_session,
        subject_id=subject.subject_id,
        company_id="c1",
        generation=7,
        valid_until=datetime(2026, 2, 1, tzinfo=UTC),
    )
    other_membership = _add_membership(
        master_session,
        subject_id=subject.subject_id,
        company_id="c2",
        generation=8,
    )
    grant = _add_grant(
        master_session,
        membership_id=membership.id,
        capability="allocation_review_propose",
        valid_until=datetime(2026, 2, 1, tzinfo=UTC),
    )

    mutations = (
        ("authenticated_subjects", "subject_id", subject.subject_id, "subject_id", "other-subject"),
        ("authenticated_subjects", "subject_id", subject.subject_id, "authentication_method", "trusted_adapter"),
        ("authenticated_subjects", "subject_id", subject.subject_id, "provider_subject", "other-provider"),
        ("authenticated_subjects", "subject_id", subject.subject_id, "credential_version", 3),
        ("authenticated_subjects", "subject_id", subject.subject_id, "created_at", "2026-01-03 00:00:00.000000"),
        ("company_memberships", "id", membership.id, "id", membership.id + 100),
        ("company_memberships", "id", membership.id, "subject_id", "other-subject"),
        ("company_memberships", "id", membership.id, "company_id", "c2"),
        ("company_memberships", "id", membership.id, "company_generation", 9),
        ("company_memberships", "id", membership.id, "valid_from", "2026-01-03 00:00:00.000000"),
        ("company_memberships", "id", membership.id, "valid_until", "2026-03-01 00:00:00.000000"),
        ("company_memberships", "id", membership.id, "created_at", "2026-01-03 00:00:00.000000"),
        ("company_capability_grants", "id", grant.id, "id", grant.id + 100),
        ("company_capability_grants", "id", grant.id, "membership_id", other_membership.id),
        ("company_capability_grants", "id", grant.id, "capability", "allocation_review_decide"),
        ("company_capability_grants", "id", grant.id, "valid_from", "2026-01-03 00:00:00.000000"),
        ("company_capability_grants", "id", grant.id, "valid_until", "2026-03-01 00:00:00.000000"),
        ("company_capability_grants", "id", grant.id, "created_at", "2026-01-03 00:00:00.000000"),
    )
    for table, identity_column, row_id, column, value in mutations:
        _assert_rejected_mutation_preserves_row(
            master_session,
            table=table,
            identity_column=identity_column,
            row_id=row_id,
            statement=f'UPDATE "{table}" SET "{column}"=:value WHERE "{identity_column}"=:row_id',
            parameters={"value": value, "row_id": row_id},
        )


def test_authority_records_allow_only_one_way_lifecycle_transition(master_session):
    subject = _add_subject(master_session, subject_id="subject-123")
    membership = _add_membership(
        master_session,
        subject_id=subject.subject_id,
        company_id="c1",
        generation=7,
        valid_until=datetime(2026, 2, 1, tzinfo=UTC),
    )
    grant = _add_grant(
        master_session,
        membership_id=membership.id,
        capability="allocation_review_propose",
        valid_until=datetime(2026, 2, 1, tzinfo=UTC),
    )
    lifecycle_at = "2026-01-02 00:00:00.000000"

    for table, identity_column, row_id, lifecycle_column in (
        ("authenticated_subjects", "subject_id", subject.subject_id, "deactivated_at"),
        ("company_memberships", "id", membership.id, "deactivated_at"),
        ("company_capability_grants", "id", grant.id, "revoked_at"),
    ):
        for invalid_timestamp in (
            "not-a-timestamp",
            "2025-12-31 23:59:59.000000",
        ):
            _assert_rejected_mutation_preserves_row(
                master_session,
                table=table,
                identity_column=identity_column,
                row_id=row_id,
                statement=(
                    f'UPDATE "{table}" SET active=0, "{lifecycle_column}"=:at '
                    f'WHERE "{identity_column}"=:row_id'
                ),
                parameters={"at": invalid_timestamp, "row_id": row_id},
            )

    master_session.execute(
        text("UPDATE authenticated_subjects SET active=0, deactivated_at=:at WHERE subject_id=:id"),
        {"at": lifecycle_at, "id": subject.subject_id},
    )
    master_session.commit()
    master_session.execute(
        text("UPDATE company_memberships SET active=0, deactivated_at=:at WHERE id=:id"),
        {"at": lifecycle_at, "id": membership.id},
    )
    master_session.commit()
    master_session.execute(
        text("UPDATE company_capability_grants SET active=0, revoked_at=:at WHERE id=:id"),
        {"at": lifecycle_at, "id": grant.id},
    )
    master_session.commit()

    forbidden_transitions = (
        ("authenticated_subjects", "subject_id", subject.subject_id, "active", 1),
        ("authenticated_subjects", "subject_id", subject.subject_id, "deactivated_at", None),
        ("authenticated_subjects", "subject_id", subject.subject_id, "deactivated_at", "2026-01-03 00:00:00.000000"),
        ("company_memberships", "id", membership.id, "active", 1),
        ("company_memberships", "id", membership.id, "deactivated_at", None),
        ("company_memberships", "id", membership.id, "deactivated_at", "2026-01-03 00:00:00.000000"),
        ("company_memberships", "id", membership.id, "valid_until", "2026-03-01 00:00:00.000000"),
        ("company_capability_grants", "id", grant.id, "active", 1),
        ("company_capability_grants", "id", grant.id, "revoked_at", None),
        ("company_capability_grants", "id", grant.id, "revoked_at", "2026-01-03 00:00:00.000000"),
        ("company_capability_grants", "id", grant.id, "valid_until", "2026-03-01 00:00:00.000000"),
    )
    for table, identity_column, row_id, column, value in forbidden_transitions:
        _assert_rejected_mutation_preserves_row(
            master_session,
            table=table,
            identity_column=identity_column,
            row_id=row_id,
            statement=f'UPDATE "{table}" SET "{column}"=:value WHERE "{identity_column}"=:row_id',
            parameters={"value": value, "row_id": row_id},
        )


def _create_lifecycle_target(session: Session, record_type: str, created_at: datetime):
    subject = _add_subject(
        session,
        subject_id=f"subject-precision-{record_type}",
        created_at=created_at,
    )
    if record_type == "subject":
        return "authenticated_subjects", "subject_id", subject.subject_id, "deactivated_at"
    membership = _add_membership(
        session,
        subject_id=subject.subject_id,
        company_id="c1",
        generation=7,
        created_at=created_at,
    )
    if record_type == "membership":
        return "company_memberships", "id", membership.id, "deactivated_at"
    grant = _add_grant(
        session,
        membership_id=membership.id,
        capability="allocation_review_propose",
        created_at=created_at,
    )
    return "company_capability_grants", "id", grant.id, "revoked_at"


@pytest.mark.parametrize("record_type", ["subject", "membership", "grant"])
@pytest.mark.parametrize(
    ("offset_microseconds", "accepted"),
    [(-1, False), (0, True), (1, True)],
)
def test_lifecycle_timestamp_ordering_preserves_microsecond_precision(
    master_session, record_type, offset_microseconds, accepted
):
    created_at = datetime(2026, 11, 2, 10, 0, 0, 123456, tzinfo=UTC)
    table, identity_column, row_id, lifecycle_column = _create_lifecycle_target(
        master_session, record_type, created_at
    )
    lifecycle_at = created_at + timedelta(microseconds=offset_microseconds)
    stored_value = lifecycle_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    statement = (
        f'UPDATE "{table}" SET active=0, "{lifecycle_column}"=:at '
        f'WHERE "{identity_column}"=:row_id'
    )
    if accepted:
        master_session.execute(text(statement), {"at": stored_value, "row_id": row_id})
        master_session.commit()
        expected_active = 0
        expected_lifecycle = stored_value
    else:
        with pytest.raises(IntegrityError):
            master_session.execute(text(statement), {"at": stored_value, "row_id": row_id})
        master_session.rollback()
        expected_active = 1
        expected_lifecycle = None

    with Session(master_session.get_bind()) as fresh_read:
        actual = fresh_read.execute(
            text(
                f'SELECT active, "{lifecycle_column}" FROM "{table}" '
                f'WHERE "{identity_column}"=:row_id'
            ),
            {"row_id": row_id},
        ).one()
    assert actual[0] == expected_active
    assert actual[1] == expected_lifecycle


@pytest.mark.parametrize("record_type", ["subject", "membership", "grant"])
@pytest.mark.parametrize(
    "malformed_timestamp",
    [
        "not-a-timestamp",
        "2026-11-02 10:00:00.12345",
        "2026-11-02 10:00:00.123456+00:00",
    ],
)
def test_lifecycle_triggers_reject_noncanonical_timestamp_forms(
    master_session, record_type, malformed_timestamp
):
    created_at = datetime(2026, 11, 2, 10, 0, 0, 123456, tzinfo=UTC)
    table, identity_column, row_id, lifecycle_column = _create_lifecycle_target(
        master_session, record_type, created_at
    )
    with pytest.raises(IntegrityError):
        master_session.execute(
            text(
                f'UPDATE "{table}" SET active=0, "{lifecycle_column}"=:at '
                f'WHERE "{identity_column}"=:row_id'
            ),
            {"at": malformed_timestamp, "row_id": row_id},
        )
    master_session.rollback()
    with Session(master_session.get_bind()) as fresh_read:
        actual = fresh_read.execute(
            text(
                f'SELECT active, "{lifecycle_column}" FROM "{table}" '
                f'WHERE "{identity_column}"=:row_id'
            ),
            {"row_id": row_id},
        ).one()
    assert actual == (1, None)


def test_actor_placeholder_and_reviewer_strings_and_dicts_cannot_authorize(master_session, company_context):
    with pytest.raises(InvalidAuthenticatedPrincipalError):
        resolve_company_principal(master_session, {"subject_id": "subject-123"}, company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))
    with pytest.raises(InvalidAuthenticatedPrincipalError):
        resolve_company_principal(master_session, "actor_placeholder", company_context, "allocation_review_propose", datetime(2026, 1, 2, tzinfo=UTC))
    with pytest.raises(InvalidAuthenticatedPrincipalError):
        require_distinct_allocation_reviewer({"subject_id": "s1"}, AuthenticatedPrincipal("subject-2", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None))


def test_master_schema_has_required_authorization_tables_and_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'master-auth.db'}", future=True)
    MasterBase.metadata.create_all(engine)
    with engine.connect() as conn:
        names = {
            row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('authenticated_subjects', 'company_memberships', 'company_capability_grants')")
            )
        }
        assert {"authenticated_subjects", "company_memberships", "company_capability_grants"} <= names
        subject_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(authenticated_subjects)"))}
        membership_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(company_memberships)"))}
        grant_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(company_capability_grants)"))}
        assert {"subject_id", "authentication_method", "active", "created_at"} <= subject_cols
        assert {"subject_id", "company_id", "company_generation", "active", "valid_from", "valid_until"} <= membership_cols
        assert {"membership_id", "capability", "active", "valid_from", "valid_until"} <= grant_cols
    engine.dispose()


def test_master_authorization_indexes_are_repaired_by_signature(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'master-index-repair.db'}", future=True)
    MasterBase.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text('DROP INDEX "ix_authenticated_subjects_active"'))
        conn.execute(text('CREATE INDEX "ix_authenticated_subjects_active" ON authenticated_subjects (subject_id)'))
        conn.execute(text('DROP INDEX "ix_company_memberships_active_lookup"'))
        conn.execute(text('CREATE INDEX "ix_company_memberships_active_lookup" ON company_memberships (active)'))
        conn.execute(text('DROP INDEX "ix_company_capability_grants_membership_capability"'))
        conn.execute(text('CREATE INDEX "ix_company_capability_grants_membership_capability" ON company_capability_grants (capability)'))

    monkeypatch.setattr(master_db, "master_engine", engine)
    master_db._repair_master_authorization_schema()

    with engine.connect() as conn:
        for table, signatures in MASTER_AUTHORIZATION_INDEX_SIGNATURES.items():
            for signature in signatures:
                assert sqlite_index_matches(conn, table, signature)
    engine.dispose()


def test_production_master_initialization_installs_authorization_schema(production_master):
    engine, db_path = production_master
    assert master_db.settings.master_db_path == db_path
    first_changes = master_db.init_master_db(allow_create=True)
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert {
            "authenticated_subjects",
            "company_memberships",
            "company_capability_grants",
        } <= tables
        for table, signatures in MASTER_AUTHORIZATION_INDEX_SIGNATURES.items():
            for signature in signatures:
                assert sqlite_index_matches(conn, table, signature)
        trigger_names = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
        assert set(master_db._master_authorization_trigger_definitions()) <= trigger_names
    assert any(value.startswith("master-auth-trigger:") for value in first_changes)
    assert master_db.init_master_db() == []


def test_production_master_initialization_repairs_triggers_by_definition(production_master):
    engine, _ = production_master
    master_db.init_master_db(allow_create=True)
    malformed_name = "authenticated_subjects_lifecycle_update"
    missing_name = "company_memberships_lifecycle_update"
    with engine.begin() as conn:
        conn.execute(text(f'DROP TRIGGER "{malformed_name}"'))
        conn.execute(
            text(
                f"CREATE TRIGGER {malformed_name} BEFORE UPDATE ON authenticated_subjects "
                "WHEN OLD.active=1 AND NEW.active=0 AND "
                "julianday(NEW.deactivated_at) < julianday(OLD.created_at) "
                "BEGIN SELECT RAISE(ABORT, 'old lifecycle guard'); END"
            )
        )
        conn.execute(text(f'DROP TRIGGER "{missing_name}"'))

    changes = master_db.init_master_db()
    assert set(changes) == {
        f"master-auth-trigger:{malformed_name}",
        f"master-auth-trigger:{missing_name}",
    }
    with engine.connect() as conn:
        repaired_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": malformed_name},
        ).scalar_one().lower()
    assert "julianday" not in repaired_sql
    assert "new.deactivated_at < old.created_at" in repaired_sql
    assert master_db.init_master_db() == []


def test_production_master_initialization_rejects_incompatible_populated_schema(
    production_master,
):
    from sqlalchemy.schema import CreateTable

    from app.models.master import Company

    engine, _ = production_master
    ddl = str(CreateTable(Company.__table__).compile(engine))
    assert "id VARCHAR(32)" in ddl
    ddl = ddl.replace("id VARCHAR(32)", "id INTEGER", 1)
    with engine.begin() as conn:
        conn.exec_driver_sql(ddl)
        conn.execute(
            text(
                "INSERT INTO companies "
                "(id, generation_id, name, country, base_currency, fy_start_month, "
                "gst_registered, default_payment_terms_days) "
                "VALUES (1, 'legacy-generation', 'Synthetic legacy company', "
                "'AU', 'AUD', 7, 1, 28)"
            )
        )

    with pytest.raises(DataRecoveryRequiredError):
        master_db.init_master_db(allow_create=True)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT name FROM companies WHERE id=1")
        ).scalar_one() == "Synthetic legacy company"


@pytest.mark.parametrize(
    ("table", "column", "trigger", "lifecycle_update"),
    [
        ("company_memberships", "valid_from", "company_memberships_lifecycle_update", False),
        ("company_memberships", "valid_until", "company_memberships_lifecycle_update", False),
        ("company_memberships", "deactivated_at", "company_memberships_lifecycle_update", True),
        ("company_capability_grants", "valid_from", "company_capability_grants_lifecycle_update", False),
        ("company_capability_grants", "valid_until", "company_capability_grants_lifecycle_update", False),
        ("company_capability_grants", "revoked_at", "company_capability_grants_lifecycle_update", True),
        ("authenticated_subjects", "deactivated_at", "authenticated_subjects_lifecycle_update", True),
    ],
)
def test_production_master_initialization_rejects_malformed_persisted_authority_timestamps(
    production_master, table, column, trigger, lifecycle_update
):
    engine, _ = production_master
    master_db.init_master_db(allow_create=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO authenticated_subjects "
                "(subject_id, authentication_method, provider_subject, active, "
                "credential_version, created_at) "
                "VALUES ('subject-123', 'internal_adapter', 'subject-123', 1, 0, "
                "'2026-01-01 00:00:00.000000')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO company_memberships "
                "(subject_id, company_id, company_generation, active, valid_from, created_at) "
                "VALUES ('subject-123', 'c1', 7, 1, '2026-01-01 00:00:00.000000', "
                "'2026-01-01 00:00:00.000000')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO company_capability_grants "
                "(membership_id, capability, active, valid_from, created_at) "
                "VALUES (1, 'allocation_review_propose', 1, '2026-01-01 00:00:00.000000', "
                "'2026-01-01 00:00:00.000000')"
            )
        )
        conn.execute(text(f'DROP TRIGGER "{trigger}"'))
        if lifecycle_update:
            lifecycle_column = "revoked_at" if table == "company_capability_grants" else "deactivated_at"
            conn.execute(
                text(
                    f'UPDATE "{table}" SET active=0, "{lifecycle_column}"=:malformed '
                    f'WHERE {"subject_id" if table == "authenticated_subjects" else "id"}=:row_id'
                ),
                {
                    "malformed": "not-a-timestamp",
                    "row_id": "subject-123" if table == "authenticated_subjects" else (1 if table == "company_memberships" else 1),
                },
            )
        else:
            identity = "subject_id" if table == "authenticated_subjects" else "id"
            row_id = "subject-123" if table == "authenticated_subjects" else 1
            conn.execute(
                text(f'UPDATE "{table}" SET "{column}"=:malformed WHERE "{identity}"=:row_id'),
                {"malformed": "not-a-timestamp", "row_id": row_id},
            )

    with pytest.raises(DataRecoveryRequiredError, match="master authorization timestamp"):
        master_db.init_master_db()


def test_master_schema_has_no_company_authorization_tables_in_company_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'company.db'}", future=True)
    from app.db.base import CompanyBase
    CompanyBase.metadata.create_all(engine)
    with engine.connect() as conn:
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert "authenticated_subjects" not in tables
        assert "company_memberships" not in tables
        assert "company_capability_grants" not in tables
    engine.dispose()


def test_composed_policy_requires_capability_and_generation_match(master_session):
    proposer = AuthenticatedPrincipal("subject-1", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    reviewer = AuthenticatedPrincipal("subject-2", "internal_adapter", datetime(2026, 1, 1, tzinfo=UTC), None)
    _add_subject(master_session, subject_id="subject-1")
    _add_subject(master_session, subject_id="subject-2")
    p_membership = _add_membership(master_session, subject_id="subject-1", company_id="c1", generation=7)
    r_membership = _add_membership(master_session, subject_id="subject-2", company_id="c1", generation=7)
    _add_grant(master_session, membership_id=p_membership.id, capability="allocation_review_propose")
    _add_grant(master_session, membership_id=r_membership.id, capability="allocation_review_propose")
    with pytest.raises(CapabilityRequiredError):
        require_allocation_review_policy(master_session, proposer, reviewer, CompanyContext("c1", 7), datetime(2026, 1, 2, tzinfo=UTC))

    _add_grant(master_session, membership_id=r_membership.id, capability="allocation_review_decide")
    require_allocation_review_policy(master_session, proposer, reviewer, CompanyContext("c1", 7), datetime(2026, 1, 2, tzinfo=UTC))
