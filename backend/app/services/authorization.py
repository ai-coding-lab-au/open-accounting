"""Trusted authorization primitives for the master database.

This module intentionally defines only the service contract required by a later
immutable allocation proposal and approval workflow. It does not authenticate a
real person and it does not issue tokens, sessions, cookies, or claims.
Only a trusted adapter may create or supply an AuthenticatedPrincipal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.master import (
    AuthenticatedSubject,
    CompanyCapabilityGrant,
    CompanyMembership,
    validate_credential_version,
)

ALLOWED_AUTHENTICATION_METHODS = frozenset({"internal_adapter", "trusted_adapter"})
ALLOCATION_REVIEW_CAPABILITIES = frozenset(
    {
        "allocation_review_propose",
        "allocation_review_decide",
        "allocation_review_execute",
    }
)


class MissingAuthenticatedPrincipalError(RuntimeError):
    """A trusted principal object was required but missing."""


class InvalidAuthenticatedPrincipalError(ValueError):
    """A trusted principal object was malformed or non-authoritative."""


class UnknownAuthenticatedSubjectError(RuntimeError):
    """The principal subject does not exist in the master database."""


class InactiveAuthenticatedSubjectError(RuntimeError):
    """The subject record is deactivated and no longer eligible."""


class CompanyMembershipRequiredError(RuntimeError):
    """The subject does not have an active membership for the company."""


class InactiveCompanyMembershipError(RuntimeError):
    """The membership exists but is inactive or no longer valid."""


class CompanyGenerationMismatchError(RuntimeError):
    """The company generation in the request does not match the membership."""


class CapabilityRequiredError(RuntimeError):
    """The subject does not hold the required company capability grant."""


class InactiveCapabilityGrantError(RuntimeError):
    """The capability grant is inactive or revoked."""


class CapabilityGrantNotYetValidError(RuntimeError):
    """The capability grant is scheduled for a future validity window."""


class CapabilityGrantExpiredError(RuntimeError):
    """The capability grant validity window has ended."""


class SelfApprovalForbiddenError(RuntimeError):
    """Self-approval is forbidden by separation-of-duties policy."""


class AmbiguousAuthorizationStateError(RuntimeError):
    """The authorization state is ambiguous and must fail closed."""


class InvalidAuthorizationTimestampError(ValueError):
    """An authorization timestamp is malformed or unsupported."""


def _subject_id_has_bad_shape(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    candidate = value.strip()
    if not candidate or len(candidate) > 128:
        return True
    return bool(re.search(r"\s", candidate)) or candidate != value


def _validate_subject_id(value: Any) -> str:
    if value is None or _subject_id_has_bad_shape(value):
        raise InvalidAuthenticatedPrincipalError("subject_id must be a stable nonblank canonical value")
    return value.strip()


def _validate_authentication_method(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidAuthenticatedPrincipalError("authentication_method must be a string")
    method = value.strip()
    if method not in ALLOWED_AUTHENTICATION_METHODS:
        raise InvalidAuthenticatedPrincipalError("authentication_method is not in the trusted domain")
    return method


def _coerce_utc_datetime(value: Any, *, field_name: str) -> datetime:
    if value is None:
        raise InvalidAuthorizationTimestampError(f"{field_name} is required")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidAuthorizationTimestampError(f"{field_name} is blank")
        try:
            candidate = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidAuthorizationTimestampError(f"{field_name} is not a valid ISO-8601 timestamp") from exc
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=UTC)
        return candidate.astimezone(UTC)
    raise InvalidAuthorizationTimestampError(f"{field_name} must be a datetime or ISO-8601 string")


def _require_datetime_utc(value: Any, *, allow_none: bool = False, field_name: str) -> datetime | None:
    if value is None:
        if allow_none:
            return None
        raise InvalidAuthorizationTimestampError(f"{field_name} is required")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    raise InvalidAuthorizationTimestampError(f"{field_name} must be a timezone-aware datetime value")


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Immutable trusted principal contract.

    Only a trusted authentication adapter may create or supply this object.
    This object is not itself an authentication mechanism and it does not grant
    authority beyond the validated facts it carries.
    """

    subject_id: str
    authentication_method: str
    authenticated_at: datetime
    credential_version: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.subject_id, dict):
            raise InvalidAuthenticatedPrincipalError("subject_id may not be a mapping")
        subject_id = _validate_subject_id(self.subject_id)
        object.__setattr__(self, "subject_id", subject_id)

        if isinstance(self.authentication_method, dict):
            raise InvalidAuthenticatedPrincipalError("authentication_method may not be a mapping")
        method = _validate_authentication_method(self.authentication_method)
        object.__setattr__(self, "authentication_method", method)

        if self.authenticated_at is None:
            raise InvalidAuthenticatedPrincipalError("authenticated_at is required")
        normalized = _coerce_utc_datetime(self.authenticated_at, field_name="authenticated_at")
        object.__setattr__(self, "authenticated_at", normalized)

        try:
            credential_version = validate_credential_version(self.credential_version)
        except (TypeError, ValueError) as exc:
            raise InvalidAuthenticatedPrincipalError(str(exc)) from exc
        object.__setattr__(self, "credential_version", credential_version)


@dataclass(frozen=True, slots=True)
class CompanyContext:
    """Immutable company workspace identity for authorization checks."""

    company_id: str
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.company_id, str):
            raise TypeError("company_id must be a string")
        company_id = self.company_id
        if not company_id or len(company_id) > 32:
            raise ValueError("company_id must be a canonical nonblank string")
        if company_id != company_id.strip():
            raise ValueError("company_id must not contain surrounding whitespace")
        if re.search(r"\s", company_id):
            raise ValueError("company_id must not contain whitespace")
        object.__setattr__(self, "company_id", company_id)

        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise TypeError("generation must be a nonnegative integer")
        if self.generation < 0:
            raise ValueError("generation must be nonnegative")


@dataclass(frozen=True, slots=True)
class AuthorizedCompanyPrincipal:
    subject_id: str
    company_id: str
    company_generation: int
    membership_id: int
    capability: str
    authorization_at: datetime


def _require_valid_principal(principal: Any) -> AuthenticatedPrincipal:
    if principal is None:
        raise MissingAuthenticatedPrincipalError("A trusted principal is required")
    if not isinstance(principal, AuthenticatedPrincipal):
        raise InvalidAuthenticatedPrincipalError("principal must be an AuthenticatedPrincipal")
    return principal


def _load_subject(session: Session, subject_id: str) -> AuthenticatedSubject | None:
    return session.get(AuthenticatedSubject, subject_id)


def _is_valid_window(
    started_at: datetime | None,
    ended_at: datetime | None,
    operation_at: datetime,
) -> bool:
    if started_at is None:
        return False
    start = _require_datetime_utc(started_at, field_name="valid_from")
    if operation_at < start:
        return False
    if ended_at is not None:
        end = _require_datetime_utc(ended_at, field_name="valid_until")
        if operation_at >= end:
            return False
    return True


def _validate_lifecycle(
    *,
    active: bool,
    created_at: Any,
    lifecycle_at: Any,
    lifecycle_field: str,
) -> None:
    created = _require_datetime_utc(created_at, field_name="created_at")
    lifecycle = _require_datetime_utc(
        lifecycle_at, allow_none=True, field_name=lifecycle_field
    )
    if active and lifecycle is not None:
        raise AmbiguousAuthorizationStateError(
            f"An active record has a {lifecycle_field} timestamp"
        )
    if not active and lifecycle is None:
        raise AmbiguousAuthorizationStateError(
            f"An inactive record is missing {lifecycle_field}"
        )
    if lifecycle is not None and lifecycle < created:
        raise InvalidAuthorizationTimestampError(
            f"{lifecycle_field} must not precede created_at"
        )


def _membership_for_company(
    session: Session,
    *,
    subject_id: str,
    company_id: str,
) -> list[CompanyMembership]:
    stmt = select(CompanyMembership).where(
        CompanyMembership.subject_id == subject_id,
        CompanyMembership.company_id == company_id,
    )
    return list(session.execute(stmt).scalars().all())


def _grant_for_capability(
    session: Session,
    *,
    membership_id: int,
    capability: str,
) -> list[CompanyCapabilityGrant]:
    stmt = select(CompanyCapabilityGrant).where(
        CompanyCapabilityGrant.membership_id == membership_id,
        CompanyCapabilityGrant.capability == capability,
    )
    return list(session.execute(stmt).scalars().all())


def resolve_company_principal(
    master_db: Session,
    principal: AuthenticatedPrincipal | None,
    company_context: CompanyContext,
    required_capability: str,
    operation_at: datetime | str,
) -> AuthorizedCompanyPrincipal:
    """Resolve a trusted principal to a company-scoped authorization decision."""

    trusted = _require_valid_principal(principal)
    if company_context is None or not isinstance(company_context, CompanyContext):
        raise TypeError("company_context must be a CompanyContext")
    if required_capability not in ALLOCATION_REVIEW_CAPABILITIES:
        raise CapabilityRequiredError("required capability is not in the supported domain")

    normalized_time = _coerce_utc_datetime(operation_at, field_name="operation_at")
    subject = _load_subject(master_db, trusted.subject_id)
    if subject is None:
        raise UnknownAuthenticatedSubjectError("The subject is not known to the master registry")
    _validate_lifecycle(
        active=subject.active,
        created_at=subject.created_at,
        lifecycle_at=subject.deactivated_at,
        lifecycle_field="deactivated_at",
    )
    if not subject.active:
        raise InactiveAuthenticatedSubjectError("The subject is inactive")

    memberships = _membership_for_company(
        master_db,
        subject_id=trusted.subject_id,
        company_id=company_context.company_id,
    )
    if not memberships:
        raise CompanyMembershipRequiredError("No company membership matches the request")

    generation_matches = [m for m in memberships if m.company_generation == company_context.generation]
    if not generation_matches:
        raise CompanyGenerationMismatchError("The company generation does not match the requested workspace")

    active_matches = [m for m in generation_matches if m.active]
    if not active_matches:
        raise InactiveCompanyMembershipError("The membership is inactive")
    if len(active_matches) > 1:
        raise AmbiguousAuthorizationStateError("Multiple active memberships match this company context")

    membership = active_matches[0]
    _validate_lifecycle(
        active=membership.active,
        created_at=membership.created_at,
        lifecycle_at=membership.deactivated_at,
        lifecycle_field="deactivated_at",
    )
    _require_datetime_utc(membership.valid_from, field_name="membership.valid_from")
    _require_datetime_utc(
        membership.valid_until,
        allow_none=True,
        field_name="membership.valid_until",
    )
    if not _is_valid_window(membership.valid_from, membership.valid_until, normalized_time):
        membership_start = _require_datetime_utc(membership.valid_from, field_name="membership.valid_from") if membership.valid_from is not None else None
        if membership_start is not None and membership_start > normalized_time:
            raise CapabilityGrantNotYetValidError("The membership is not yet valid for this operation")
        raise InactiveCompanyMembershipError("The membership validity window is not currently active")

    grants = _grant_for_capability(
        master_db,
        membership_id=membership.id,
        capability=required_capability,
    )
    if not grants:
        raise CapabilityRequiredError("The required capability grant does not exist")

    active_grants = [g for g in grants if g.active]
    if not active_grants:
        raise InactiveCapabilityGrantError("The capability grant is inactive")
    if len(active_grants) > 1:
        raise AmbiguousAuthorizationStateError("Multiple active capability grants match the request")

    grant = active_grants[0]
    _validate_lifecycle(
        active=grant.active,
        created_at=grant.created_at,
        lifecycle_at=grant.revoked_at,
        lifecycle_field="revoked_at",
    )
    _require_datetime_utc(grant.valid_from, field_name="grant.valid_from")
    _require_datetime_utc(
        grant.valid_until,
        allow_none=True,
        field_name="grant.valid_until",
    )
    if not _is_valid_window(grant.valid_from, grant.valid_until, normalized_time):
        grant_start = _require_datetime_utc(grant.valid_from, field_name="grant.valid_from") if grant.valid_from is not None else None
        if grant_start is not None and grant_start > normalized_time:
            raise CapabilityGrantNotYetValidError("The capability grant is not yet valid")
        raise CapabilityGrantExpiredError("The capability grant is no longer valid")

    return AuthorizedCompanyPrincipal(
        subject_id=trusted.subject_id,
        company_id=company_context.company_id,
        company_generation=company_context.generation,
        membership_id=membership.id,
        capability=required_capability,
        authorization_at=normalized_time,
    )


def require_allocation_review_capability(
    master_db: Session,
    principal: AuthenticatedPrincipal | None,
    company_context: CompanyContext,
    capability: str,
    operation_at: datetime | str,
) -> AuthorizedCompanyPrincipal:
    """Resolve a company-scoped allocation review capability."""

    if capability not in ALLOCATION_REVIEW_CAPABILITIES:
        raise CapabilityRequiredError("The required capability is not in the allocation review domain")
    return resolve_company_principal(
        master_db,
        principal,
        company_context,
        capability,
        operation_at,
    )


def require_distinct_allocation_reviewer(
    proposer: AuthenticatedPrincipal | None,
    reviewer: AuthenticatedPrincipal | None,
) -> None:
    """Narrow separation-of-duties contract: distinct stable subject identities.

    This function intentionally checks only the identity distinction required by
    the policy. A higher-level composed policy must also validate the trusted
    principal, active subject status, company membership, capability grants, and
    company generation for both parties before allowing a reviewer decision.
    """

    if proposer is None:
        raise MissingAuthenticatedPrincipalError("A proposer principal is required")
    if reviewer is None:
        raise MissingAuthenticatedPrincipalError("A reviewer principal is required")
    if not isinstance(proposer, AuthenticatedPrincipal):
        raise InvalidAuthenticatedPrincipalError("proposer must be an AuthenticatedPrincipal")
    if not isinstance(reviewer, AuthenticatedPrincipal):
        raise InvalidAuthenticatedPrincipalError("reviewer must be an AuthenticatedPrincipal")
    if proposer.subject_id == reviewer.subject_id:
        raise SelfApprovalForbiddenError("A principal may not approve its own allocation proposal")
    return None


def require_allocation_review_policy(
    master_db: Session,
    proposer: AuthenticatedPrincipal | None,
    reviewer: AuthenticatedPrincipal | None,
    company_context: CompanyContext,
    operation_at: datetime | str,
) -> AuthorizedCompanyPrincipal:
    """Compose the full reviewer policy for allocation review decisions.

    This verifies: distinct stable IDs, proposer capability, reviewer capability,
    both subjects are active, both have active company memberships for the same
    generation, and the required capabilities are active and currently valid.
    """

    require_distinct_allocation_reviewer(proposer, reviewer)
    proposer_authz = resolve_company_principal(
        master_db,
        proposer,
        company_context,
        "allocation_review_propose",
        operation_at,
    )
    reviewer_authz = resolve_company_principal(
        master_db,
        reviewer,
        company_context,
        "allocation_review_decide",
        operation_at,
    )
    if proposer_authz.company_id != reviewer_authz.company_id:
        raise CompanyMembershipRequiredError("The proposer and reviewer are not in the same company")
    if proposer_authz.company_generation != reviewer_authz.company_generation:
        raise CompanyGenerationMismatchError("The proposer and reviewer are not in the same company generation")
    if proposer_authz.subject_id == reviewer_authz.subject_id:
        raise SelfApprovalForbiddenError("A principal may not approve its own allocation proposal")
    return reviewer_authz
