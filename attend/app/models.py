from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import now_utc


class Plant(Base):
    """A plant profile. Every plant-specific value lives here, never in code."""
    __tablename__ = "plants"
    __table_args__ = (UniqueConstraint("tenant_id", "plant_ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    plant_ref: Mapped[str] = mapped_column(String(64))  # external plantId
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64))
    locale: Mapped[str] = mapped_column(String(16), default="ar")
    polygon: Mapped[str] = mapped_column(Text)  # JSON [[lat,lng],...] the plant (arrive/depart)
    site_polygon: Mapped[str | None] = mapped_column(Text, nullable=True)  # work zone (enter/exit)
    # JSON [{"code":"D","start":"07:00","hours":12,"label":"..."}]
    shifts: Mapped[str] = mapped_column(Text)
    grace_minutes: Mapped[int] = mapped_column(Integer, default=10)
    min_staff: Mapped[int] = mapped_column(Integer, default=1)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    plant_id: Mapped[int] = mapped_column(ForeignKey("plants.id"))
    pattern: Mapped[str] = mapped_column(String(400))  # shift codes + O, comma separated
    anchor_date: Mapped[date] = mapped_column(Date)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "person_ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    person_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # SolNerve person id
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="employee")  # employee | admin
    job_title: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plant_id: Mapped[int | None] = mapped_column(ForeignKey("plants.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Local mode keeps balances here; in SolNerve mode they are mirrored from SolNerve.
    annual_balance: Mapped[float] = mapped_column(Float, default=14)
    sick_balance: Mapped[float] = mapped_column(Float, default=14)
    departure_hours_balance: Mapped[float] = mapped_column(Float, default=8)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    team = relationship("Team")
    plant = relationship("Plant")


class PersonAlias(Base):
    """Known name forms for one person (e.g. a first name, a title + name, a family name).
    Matching is exact on the normalised form; unknown forms become questions, never guesses."""
    __tablename__ = "person_aliases"
    __table_args__ = (UniqueConstraint("tenant_id", "norm"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    form: Mapped[str] = mapped_column(String(120))
    norm: Mapped[str] = mapped_column(String(120))


class NameQuestion(Base):
    __tablename__ = "name_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    form: Mapped[str] = mapped_column(String(120))
    norm: Mapped[str] = mapped_column(String(120))
    context: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plants.id"))
    code: Mapped[str] = mapped_column(String(8))  # a shift code from the plant profile
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20), default="auto")  # auto | manual | substitute
    covering_for: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plant_id: Mapped[int | None] = mapped_column(ForeignKey("plants.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(10))  # arrive | enter | exit | depart
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    inside_plant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    inside_site: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    offline: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # annual | sick | departure
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    amount: Mapped[float] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    substitute_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    substitute_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(20), default="local")  # local | solnerve
    external_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)  # shifts already moved
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    user = relationship("User", foreign_keys=[user_id])
    substitute = relationship("User", foreign_keys=[substitute_id])


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(30))  # sos | left_geofence
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])


class LocationPing(Base):
    __tablename__ = "location_pings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)


class Notification(Base):
    """In-app inbox row and outbox row for the outbound hook (Shams -> WhatsApp)."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="info")
    text: Mapped[str] = mapped_column(Text)
    params: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    # outbox: pending | delivered | failed | disabled
    out_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(80), nullable=True)  # code only, no data


class JobRun(Base):
    """Last period a periodic job ran for. A missed period runs once on the next tick."""
    __tablename__ = "job_runs"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    period: Mapped[str] = mapped_column(String(40))
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
