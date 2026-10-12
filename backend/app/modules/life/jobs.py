"""Scheduled refresh of the encrypted emergency package (EST-02): a fresh PDF each month, a few kept."""

import contextlib
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.modules.life import estate
from app.platform.config import get_settings
from app.platform.db import today, utcnow
from app.platform.jobs import JobContext, job
from app.platform.models import AppUser
from app.platform.security import decrypt_secret

FILE = re.compile(r"estate-package-\d{4}-\d{2}\.pdf")
DEFAULTS: dict[str, Any] = {"enabled": False, "path_dir": None, "keep": 3}


def package_dir(cfg: dict[str, Any]) -> Path:
    return Path(cfg.get("path_dir") or Path(get_settings().imports_dir) / "estate")


def prune(folder: Path, keep: int) -> list[str]:
    """Delete the oldest monthly files beyond `keep` (names sort by month); other files are never touched."""
    old = sorted((p for p in folder.iterdir() if FILE.fullmatch(p.name)), key=lambda p: p.name, reverse=True)[keep:]
    for p in old:
        p.unlink()
    return [p.name for p in old]


@job("estate.package", "Refresh the encrypted emergency package", cron="0 4 1 * *")
def refresh(ctx: JobContext) -> None:
    user = ctx.db.scalar(select(AppUser).order_by(AppUser.id).limit(1))
    cfg = dict((user.settings or {}).get("estate_package") or {}) if user else {}
    if user is None or not cfg.get("enabled") or not cfg.get("passphrase_enc"):
        ctx.summary = "emergency package not enabled (or no passphrase set)"
        return
    folder = package_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"estate-package-{today():%Y-%m}.pdf"
    data = estate.render_pdf(estate.collect(ctx.db), decrypt_secret(cfg["passphrase_enc"]))
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(data)
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, folder / name)  # never a half-written package under the real name
    removed = prune(folder, max(1, int(cfg.get("keep") or 3)))
    user.settings = {
        **(user.settings or {}),
        "estate_package": {**cfg, "last": {"file": name, "at": utcnow().isoformat(), "dir": str(folder)}},
    }
    ctx.log(f"wrote {folder / name}" + (f", removed {', '.join(removed)}" if removed else ""))
    ctx.summary = f"{name} written" + (f", {len(removed)} old removed" if removed else "")


def last_written(cfg: dict[str, Any]) -> dict[str, Any] | None:
    last = cfg.get("last")
    return dict(last) if isinstance(last, dict) else None
