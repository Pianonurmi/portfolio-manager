"""Rule tables as data (NFR-14, ADR-09). Files: rules/<domain>-<year>.yaml"""

import hashlib
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.models import RuleSet


def load_rules(db: Session, directory: str) -> list[str]:
    loaded: list[str] = []
    root = Path(directory)
    if not root.is_dir():
        return loaded
    for path in sorted(root.glob("*.yaml")):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        doc = yaml.safe_load(raw) or {}
        domain, year, version = str(doc["domain"]), int(doc["year"]), str(doc["version"])
        existing = db.scalar(
            select(RuleSet).where(RuleSet.domain == domain, RuleSet.year == year, RuleSet.version == version)
        )
        if existing is None:
            db.add(RuleSet(domain=domain, year=year, version=version, payload=doc, source_hash=digest))
            loaded.append(f"{domain}-{year}@{version}")
        elif existing.source_hash != digest:
            existing.payload, existing.source_hash = doc, digest
            loaded.append(f"{domain}-{year}@{version} (updated)")
    return loaded


def rules_for(db: Session, domain: str, year: int) -> dict[str, Any]:
    """Latest version for the year; falls back to the most recent earlier year."""
    rs = db.scalar(
        select(RuleSet)
        .where(RuleSet.domain == domain, RuleSet.year <= year)
        .order_by(RuleSet.year.desc(), RuleSet.created_at.desc())
        .limit(1)
    )
    if rs is None:  # before the first table: use the earliest one (its version is reported with every result)
        rs = db.scalar(
            select(RuleSet).where(RuleSet.domain == domain).order_by(RuleSet.year, RuleSet.created_at.desc()).limit(1)
        )
    if rs is None:
        raise LookupError(f"no {domain} rules loaded")
    payload = dict(rs.payload)
    payload["_version"] = f"{rs.domain.upper()}-{rs.year}.{rs.version}"
    return payload
