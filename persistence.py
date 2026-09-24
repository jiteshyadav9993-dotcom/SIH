"""
persistence.py
====================================================================
Durable audit log for dossier submissions, sanctioned possessions,
and TSR orders.

Default database is local SQLite (sqlite:///./block_planning.db).
A DATABASE_URL environment variable selects Postgres instead.
Each helper opens and closes its own session.
====================================================================
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

_DEFAULT_URL = "sqlite:///./block_planning.db"
_HISTORY_DEFAULT_LIMIT = 50
_HISTORY_MAX_LIMIT = 200


class Base(DeclarativeBase):
    pass


class DossierSubmission(Base):
    __tablename__ = "dossier_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_type: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SanctionedPossession(Base):
    __tablename__ = "sanctioned_possessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_id: Mapped[str] = mapped_column(String(64), nullable=False)
    track_line: Mapped[str] = mapped_column(String(32), nullable=False)
    section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    start_min: Mapped[int] = mapped_column(Integer, nullable=False)
    end_min: Mapped[int] = mapped_column(Integer, nullable=False)
    machine_type: Mapped[str] = mapped_column(String(64), nullable=False)
    power_sector_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_type: Mapped[str] = mapped_column(String(64), nullable=False)
    solver_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sanctioned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sanctioned_by: Mapped[str] = mapped_column(String(128), nullable=False)
    emergency: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    possession_payload: Mapped[str] = mapped_column(Text, nullable=False)


class TsrOrder(Base):
    __tablename__ = "tsr_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line: Mapped[str] = mapped_column(String(32), nullable=False)
    start_km: Mapped[float] = mapped_column(Float, nullable=False)
    end_km: Mapped[float] = mapped_column(Float, nullable=False)
    max_kmh: Mapped[float] = mapped_column(Float, nullable=False)
    extra_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(128), nullable=False)


def resolve_database_url() -> str:
    """SQLite unless DATABASE_URL is set. Bare postgres URLs use the psycopg2 driver."""
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        return _DEFAULT_URL
    if raw.startswith("postgres://"):
        return "postgresql+psycopg2://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg2://" + raw[len("postgresql://") :]
    return raw


def _build_engine(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


_engine = _build_engine(resolve_database_url())
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_page(limit: int, offset: int) -> tuple[int, int]:
    bounded_limit = _HISTORY_DEFAULT_LIMIT if limit < 1 else limit
    if bounded_limit > _HISTORY_MAX_LIMIT:
        bounded_limit = _HISTORY_MAX_LIMIT
    bounded_offset = 0 if offset < 0 else offset
    return bounded_limit, bounded_offset


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _day_end(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _open_session() -> Session:
    return _SessionLocal()


def record_dossier_submission(
    plan_type: str,
    submitted_by: str,
    status: str,
    reject_reason: str | None = None,
) -> int:
    row = DossierSubmission(
        plan_type=plan_type,
        submitted_at=_utc_now(),
        submitted_by=submitted_by,
        status=status,
        reject_reason=reject_reason,
    )
    session = _open_session()
    try:
        session.add(row)
        session.commit()
        return int(row.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_possession_event(
    bundle_id: str,
    track_line: str,
    section_id: str,
    start_min: int,
    end_min: int,
    machine_type: str,
    power_sector_id: str,
    plan_type: str,
    solver_status: str,
    sanctioned_by: str,
    emergency: bool,
    outcome: str,
    *,
    reject_reason: str | None = None,
    possession_payload: str,
) -> int:
    row = SanctionedPossession(
        bundle_id=bundle_id,
        track_line=track_line,
        section_id=section_id,
        start_min=start_min,
        end_min=end_min,
        machine_type=machine_type,
        power_sector_id=power_sector_id,
        plan_type=plan_type,
        solver_status=solver_status,
        sanctioned_at=_utc_now(),
        sanctioned_by=sanctioned_by,
        emergency=emergency,
        outcome=outcome,
        reject_reason=reject_reason,
        possession_payload=possession_payload,
    )
    session = _open_session()
    try:
        session.add(row)
        session.commit()
        return int(row.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_tsr_order(
    line: str,
    start_km: float,
    end_km: float,
    max_kmh: float,
    extra_minutes: float,
    issued_by: str,
) -> int:
    row = TsrOrder(
        line=line,
        start_km=start_km,
        end_km=end_km,
        max_kmh=max_kmh,
        extra_minutes=extra_minutes,
        issued_at=_utc_now(),
        issued_by=issued_by,
    )
    session = _open_session()
    try:
        session.add(row)
        session.commit()
        return int(row.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _dossier_dict(row: DossierSubmission) -> dict[str, object]:
    return {
        "id": int(row.id),
        "plan_type": row.plan_type,
        "submitted_at": _iso(row.submitted_at),
        "submitted_by": row.submitted_by,
        "status": row.status,
        "reject_reason": row.reject_reason,
    }


def _payload_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _possession_dict(row: SanctionedPossession) -> dict[str, object]:
    return {
        "id": int(row.id),
        "bundle_id": row.bundle_id,
        "track_line": row.track_line,
        "section_id": row.section_id,
        "start_min": int(row.start_min),
        "end_min": int(row.end_min),
        "machine_type": row.machine_type,
        "power_sector_id": row.power_sector_id,
        "plan_type": row.plan_type,
        "solver_status": row.solver_status,
        "sanctioned_at": _iso(row.sanctioned_at),
        "sanctioned_by": row.sanctioned_by,
        "emergency": bool(row.emergency),
        "outcome": row.outcome,
        "reject_reason": row.reject_reason,
        "possession_payload": _payload_value(row.possession_payload),
    }


def get_possession_history(
    line: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = _HISTORY_DEFAULT_LIMIT,
    offset: int = 0,
) -> list[dict[str, object]]:
    bounded_limit, bounded_offset = _clamp_page(limit, offset)
    statement = select(SanctionedPossession).order_by(SanctionedPossession.id.desc())
    if line:
        statement = statement.where(SanctionedPossession.track_line == line)
    if start_date is not None:
        statement = statement.where(SanctionedPossession.sanctioned_at >= _day_start(start_date))
    if end_date is not None:
        statement = statement.where(SanctionedPossession.sanctioned_at <= _day_end(end_date))
    statement = statement.offset(bounded_offset).limit(bounded_limit)
    session = _open_session()
    try:
        rows = list(session.scalars(statement).all())
        return [_possession_dict(row) for row in rows]
    finally:
        session.close()


def get_dossier_history(
    line: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = _HISTORY_DEFAULT_LIMIT,
    offset: int = 0,
) -> list[dict[str, object]]:
    """Dossier events have no line column, so `line` is accepted and not applied."""
    del line
    bounded_limit, bounded_offset = _clamp_page(limit, offset)
    statement = select(DossierSubmission).order_by(DossierSubmission.id.desc())
    if start_date is not None:
        statement = statement.where(DossierSubmission.submitted_at >= _day_start(start_date))
    if end_date is not None:
        statement = statement.where(DossierSubmission.submitted_at <= _day_end(end_date))
    statement = statement.offset(bounded_offset).limit(bounded_limit)
    session = _open_session()
    try:
        rows = list(session.scalars(statement).all())
        return [_dossier_dict(row) for row in rows]
    finally:
        session.close()


def restore_open_possession_on_startup() -> bool:
    """Copy the newest still-open possession into SYSTEM_STATE. Returns whether that happened."""
    from app import SYSTEM_STATE

    session = _open_session()
    try:
        row = session.scalars(
            select(SanctionedPossession).order_by(SanctionedPossession.id.desc()).limit(1)
        ).first()
        if row is None or row.outcome not in ("SANCTIONED", "SHIFTED"):
            return False
        payload_text = row.possession_payload
        plan_type = row.plan_type
        sanctioned_by = row.sanctioned_by
        emergency = bool(row.emergency)
    finally:
        session.close()

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False

    SYSTEM_STATE["active_plan"] = plan_type
    SYSTEM_STATE["sanctioned"] = True
    SYSTEM_STATE["sanctioned_by"] = sanctioned_by
    SYSTEM_STATE["freight_held"] = True
    SYSTEM_STATE["emergency_active"] = emergency
    SYSTEM_STATE["dossier_submitted"] = True
    SYSTEM_STATE["reject_reason"] = None
    SYSTEM_STATE["possession"] = payload
    restored_id = payload.get("coa_dossier_id")
    SYSTEM_STATE["coa_dossier_id"] = restored_id if isinstance(restored_id, str) and restored_id else None
    return True
