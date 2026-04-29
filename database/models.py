from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Filter(Base):
    """Группа лотов одного товара. 1 фильтр = 1 товар (раздел на Playerok)."""

    __tablename__ = "filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    cycle_id: Mapped[int | None] = mapped_column(ForeignKey("cycles.id"), nullable=True)

    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    spend_limit_kopecks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spent_kopecks: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    lots: Mapped[list["Lot"]] = relationship(
        back_populates="filter", cascade="all, delete-orphan"
    )
    cycle: Mapped["Cycle | None"] = relationship(back_populates="filters")


class Lot(Base):
    """Активное предложение на Playerok."""

    __tablename__ = "lots"
    __table_args__ = (UniqueConstraint("url", name="uq_lots_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filter_id: Mapped[int] = mapped_column(ForeignKey("filters.id", ondelete="CASCADE"))

    playerok_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(500))
    price_kopecks: Mapped[int] = mapped_column(Integer)
    bump_cost_kopecks: Mapped[int] = mapped_column(Integer)

    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    last_bumped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    filter: Mapped["Filter"] = relationship(back_populates="lots")


class Cycle(Base):
    """Группа фильтров, которые работают вместе по общему расписанию."""

    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    start_time: Mapped[str] = mapped_column(String(5), default="00:00")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    filters: Mapped[list[Filter]] = relationship(back_populates="cycle")


class BumpHistory(Base):
    """Лог каждого поднятия (успешного и нет)."""

    __tablename__ = "bump_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"))

    success: Mapped[bool] = mapped_column(Boolean)
    cost_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Setting(Base):
    """Глобальные настройки бота (key-value)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
