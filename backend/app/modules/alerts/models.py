from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin


class AlertLog(IdMixin, TimestampMixin, Base):
    """Sent notifications; `key` de-duplicates (same condition is not re-sent within the quiet period)."""

    __tablename__ = "alert_log"
    key: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    channels: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime]
    error: Mapped[str | None] = mapped_column(Text)
