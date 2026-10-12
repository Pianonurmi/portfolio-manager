"""Audit trail helper (NFR-07)."""

from typing import Any

from sqlalchemy.orm import Session

from app.platform.models import AuditLog


def audit(
    db: Session,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: object | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=None if subject_id is None else str(subject_id),
            before=before,
            after=after,
        )
    )
