"""Average draft position, per source and scoring format."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class Adp(Base):
    __tablename__ = "adp"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_id: Mapped[int] = mapped_column(ForeignKey("player_ids.canonical_id"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # 'sleeper' | 'espn' | 'ffc'
    format: Mapped[str] = mapped_column(String(16))  # 'ppr' | 'half_ppr' | 'std'
    teams: Mapped[int | None] = mapped_column(Integer)  # NULL when source isn't size-specific
    value: Mapped[float] = mapped_column(Float)
    stdev: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("canonical_id", "source", "format", "teams", name="uq_adp_key"),
    )
