from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import now_utc


class Station(Base):
    __tablename__ = "stations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # JSON list of [lat, lng]: the whole plant (وصول / انطلاق)
    polygon: Mapped[str] = mapped_column(Text)
    # Optional smaller work zone inside the plant (دخول / خروج)
    site_polygon: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_staff: Mapped[int] = mapped_column(Integer, default=1)
    day_start: Mapped[str] = mapped_column(String(5), default="07:00")
    night_start: Mapped[str] = mapped_column(String(5), default="19:00")
    shift_hours: Mapped[int] = mapped_column(Integer, default=12)
    grace_minutes: Mapped[int] = mapped_column(Integer, default=10)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    # Comma separated rotation, D=day N=night O=off, e.g. "D,D,N,N,O,O,O,O"
    pattern: Mapped[str] = mapped_column(String(200), default="D,D,N,N,O,O,O,O")
    anchor_date: Mapped[date] = mapped_column(Date)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="employee")  # employee | admin
    job_title: Mapped[str | None] = mapped_column(String(80), nullable=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    annual_balance: Mapped[float] = mapped_column(Float, default=14)
    sick_balance: Mapped[float] = mapped_column(Float, default=14)
    departure_hours_balance: Mapped[float] = mapped_column(Float, default=8)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    team = relationship("Team")
    station = relationship("Station")


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    kind: Mapped[str] = mapped_column(String(1))  # D | N
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20), default="auto")  # auto | manual | substitute
    covering_for: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(10))  # arrive | enter | exit | depart
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    inside_station: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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
    amount: Mapped[float] = mapped_column(Float)  # shifts for annual/sick, hours for departure
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    substitute_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    substitute_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
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
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
