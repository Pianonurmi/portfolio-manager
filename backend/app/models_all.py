"""Import every model module so Base.metadata is complete (used by Alembic and tests)."""

import importlib

from app.modules import MODULES
from app.platform import models as _platform_models  # noqa: F401
from app.platform.db import Base

for _m in MODULES:
    try:
        importlib.import_module(f"app.modules.{_m}.models")
    except ModuleNotFoundError as exc:
        if exc.name != f"app.modules.{_m}.models":
            raise

metadata = Base.metadata
