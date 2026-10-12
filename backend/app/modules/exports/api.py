"""Full data export in open formats (NFR-16): every non-derived table as JSON, plus the original import files."""

import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect, select

from app.models_all import metadata
from app.modules.ingest.models import ImportBatch
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.config import get_settings
from app.platform.db import utcnow

router = APIRouter(prefix="/api/export", tags=["export"])

# Never exported: secrets and session state.
EXCLUDE = {"auth_session", "app_user", "alembic_version"}


def _default(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, date | datetime):
        return v.isoformat()
    raise TypeError(type(v))


@router.get("")
def export_all(user: CurrentUser, db: Db) -> StreamingResponse:
    buf = io.BytesIO()
    counts: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for table in metadata.sorted_tables:
            if table.name in EXCLUDE:
                continue
            rows = [dict(r._mapping) for r in db.execute(select(table))]
            counts[table.name] = len(rows)
            z.writestr(f"tables/{table.name}.json", json.dumps(rows, default=_default, ensure_ascii=False, indent=1))
        root = Path(get_settings().imports_dir)
        for b in db.scalars(select(ImportBatch)):
            for f in root.glob(f"{b.file_sha256}*"):
                z.write(f, f"imports/{b.id}-{b.filename}")
        z.writestr(
            "README.txt",
            (
                "Portfolio Manager export\n"
                f"Created {utcnow().isoformat()}\n\n"
                "tables/*.json  one file per database table (decimals as strings, dates ISO 8601)\n"
                "imports/       original files exactly as uploaded\n"
                "Derived data (positions, lots, valuations) is not included: it is recomputed from these tables.\n"
                "Secrets (password hash, TOTP, API tokens' plaintext) are never exported.\n"
            ),
        )
        z.writestr(
            "manifest.json",
            json.dumps({"tables": counts, "schema": [t for t in inspect(db.get_bind()).get_table_names()]}, indent=1),
        )
    audit(db, user.username, "export.all", "system", None, after={"tables": counts})
    buf.seek(0)
    name = f"portfolio-manager-export-{date.today().isoformat()}.zip"  # noqa: DTZ011
    return StreamingResponse(
        buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )
