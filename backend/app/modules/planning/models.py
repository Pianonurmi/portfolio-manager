from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin


class Scenario(IdMixin, TimestampMixin, Base):
    """Saved planning inputs; results are recomputed from params + seed (NFR-15)."""

    __tablename__ = "scenario"
    name: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_base: Mapped[bool] = mapped_column(default=False)
