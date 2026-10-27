"""
persistence.py
====================================================================
Durable audit log for dossier submissions, sanctioned possessions,
and TSR orders.

Default database is local SQLite (sqlite:///./block_planning.db).
A DATABASE_URL environment variable selects Postgres instead.
Each helper opens and closes its own session.

All stored times are simulated. Rows are stamped from the server
clock in sim_clock.py, not from the laptop clock.
====================================================================
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Connection, Engine
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


class SimClockState(Base):
    """Single row (id=1): the simulated clock. Created by create_all only."""

    __tablename__ = "sim_clock_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    absolute_day: Mapped[int] = mapped_column(Integer, nullable=False)
    minute: Mapped[float] = mapped_column(Float, nullable=False)
    multiplier: Mapped[int] = mapped_column(Integer, nullable=False)


class BlockMessage(Base):
    """One board message. Created by create_all only. Every console reads the same rows."""

    __tablename__ = "block_messages"
    __table_args__ = (
        UniqueConstraint("possession_ref", "kind", "sequence", name="uq_block_message_ref_kind_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sim_day: Mapped[int] = mapped_column(Integer, nullable=False)
    sim_time: Mapped[str] = mapped_column(String(40), nullable=False)
    hhmm: Mapped[str] = mapped_column(String(8), nullable=False)
    day_label: Mapped[str] = mapped_column(String(32), nullable=False)
    from_role: Mapped[str] = mapped_column(String(32), nullable=False)
    to_role: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    possession_ref: Mapped[str] = mapped_column(String(128), nullable=False)


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
    _migrate_block_messages()


def _sqlite_table_exists(connection: Connection, table: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _message_identity_ready(connection: Connection, table: str) -> bool:
    """True when the table already keeps one row per possession, kind, and sequence.

    SQLite names that unique key sqlite_autoindex_*, not the name on the model.
    Checking the model name made every startup rebuild the table.
    """
    columns = {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
    if "sequence" not in columns:
        return False
    for index in connection.exec_driver_sql(f"PRAGMA index_list({table})").fetchall():
        if not int(index[2]):
            continue
        index_name = str(index[1]).replace('"', "")
        info = connection.exec_driver_sql(f'PRAGMA index_info("{index_name}")').fetchall()
        names = [str(column[2]) for column in info]
        if names == ["possession_ref", "kind", "sequence"]:
            return True
    return False


def _copy_legacy_messages(connection: Connection) -> None:
    legacy_cols = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(block_messages_legacy)").fetchall()}
    sequence_expr = "sequence" if "sequence" in legacy_cols else "0"
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO block_messages ("
        "sim_day, sim_time, hhmm, day_label, from_role, to_role, kind, text, possession_ref, sequence"
        ") SELECT sim_day, sim_time, hhmm, day_label, from_role, to_role, kind, text, possession_ref, "
        + sequence_expr
        + " FROM block_messages_legacy"
    )


def _migrate_block_messages() -> None:
    """Give an existing message table the (possession, kind, sequence) key.

    create_all does not alter a table that is already there. A table that
    already has the key is left in place. A copy that stopped halfway leaves
    block_messages_legacy beside the live table; those rows are put back.
    """
    if not str(_engine.url).startswith("sqlite"):
        return
    rebuild = False
    with _engine.begin() as conn:
        live = _sqlite_table_exists(conn, "block_messages")
        legacy = _sqlite_table_exists(conn, "block_messages_legacy")
        if legacy and not live:
            conn.exec_driver_sql("ALTER TABLE block_messages_legacy RENAME TO block_messages")
            live = True
            legacy = False
        if not live:
            return
        if _message_identity_ready(conn, "block_messages"):
            if legacy:
                _copy_legacy_messages(conn)
                conn.exec_driver_sql("DROP TABLE block_messages_legacy")
            return
        if legacy:
            conn.exec_driver_sql("ALTER TABLE block_messages_legacy RENAME TO block_messages_prev")
        conn.exec_driver_sql("ALTER TABLE block_messages RENAME TO block_messages_legacy")
        rebuild = True
    if not rebuild:
        return
    BlockMessage.__table__.create(_engine)
    with _engine.begin() as conn:
        _copy_legacy_messages(conn)
        conn.exec_driver_sql("DROP TABLE block_messages_legacy")
        if _sqlite_table_exists(conn, "block_messages_prev"):
            conn.exec_driver_sql("ALTER TABLE block_messages_prev RENAME TO block_messages_legacy")
            _copy_legacy_messages(conn)
            conn.exec_driver_sql("DROP TABLE block_messages_legacy")


def _simulated_now() -> datetime:
    """The simulated present. All stored times are simulated, not the laptop clock."""
    import sim_clock

    return sim_clock.now_datetime()


def _day_stamp(value: datetime) -> dict[str, object]:
    import sim_clock

    return sim_clock.day_fields(value)


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
        submitted_at=_simulated_now(),
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
        sanctioned_at=_simulated_now(),
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
        issued_at=_simulated_now(),
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
        **_day_stamp(row.submitted_at),
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
        **_day_stamp(row.sanctioned_at),
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


def load_sim_clock_state() -> tuple[int, float, int] | None:
    """The saved clock row, or nothing when the clock has never been stored."""
    init_db()
    session = _open_session()
    try:
        row = session.get(SimClockState, 1)
        if row is None:
            return None
        return int(row.absolute_day), float(row.minute), int(row.multiplier)
    finally:
        session.close()


def save_sim_clock_state(absolute_day: int, minute: float, multiplier: int) -> None:
    """Insert or update the single simulated-clock row. Does not alter other tables."""
    init_db()
    session = _open_session()
    try:
        row = session.get(SimClockState, 1)
        if row is None:
            session.add(
                SimClockState(
                    id=1,
                    absolute_day=int(absolute_day),
                    minute=float(minute),
                    multiplier=int(multiplier),
                )
            )
        else:
            row.absolute_day = int(absolute_day)
            row.minute = float(minute)
            row.multiplier = int(multiplier)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _message_clock() -> tuple[int, str, str, str]:
    """Absolute day, ISO simulated time, hh:mm, and a day label with no calendar date."""
    import sim_clock

    minute = sim_clock.minute_of_day()
    hours = int(minute) // 60
    mins = int(minute) % 60
    return (
        sim_clock.absolute_day(),
        sim_clock.now_datetime().isoformat(),
        f"{hours:02d}:{mins:02d}",
        sim_clock.short_label(),
    )


def post_block_message(
    possession_ref: str,
    kind: str,
    from_role: str,
    to_role: str,
    text: str,
    sequence: int = 0,
) -> int:
    """Insert one board message. The same possession, kind, and sequence are stored only once."""
    init_db()
    ref = str(possession_ref)
    message_kind = str(kind)
    message_sequence = int(sequence)
    session = _open_session()
    try:
        existing = session.scalars(
            select(BlockMessage).where(
                BlockMessage.possession_ref == ref,
                BlockMessage.kind == message_kind,
                BlockMessage.sequence == message_sequence,
            )
        ).first()
        if existing is not None:
            return int(existing.id)
        sim_day, sim_time, hhmm, day_label = _message_clock()
        row = BlockMessage(
            sim_day=sim_day,
            sim_time=sim_time,
            hhmm=hhmm,
            day_label=day_label,
            from_role=str(from_role),
            to_role=str(to_role),
            kind=message_kind,
            sequence=message_sequence,
            text=str(text),
            possession_ref=ref,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.scalars(
                select(BlockMessage).where(
                    BlockMessage.possession_ref == ref,
                    BlockMessage.kind == message_kind,
                    BlockMessage.sequence == message_sequence,
                )
            ).first()
            if existing is None:
                raise
            return int(existing.id)
        return int(row.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def has_block_message(possession_ref: str, kind: str, sequence: int | None = None) -> bool:
    """True when this possession already has a message of this kind.

    A sequence narrows the check to that repeat. Without one, any sequence matches.
    """
    init_db()
    statement = select(BlockMessage.id).where(
        BlockMessage.possession_ref == str(possession_ref),
        BlockMessage.kind == str(kind),
    )
    if sequence is not None:
        statement = statement.where(BlockMessage.sequence == int(sequence))
    session = _open_session()
    try:
        existing = session.scalars(statement).first()
        return existing is not None
    finally:
        session.close()


def next_order_serial() -> int:
    """One more than the number of possession rows, so each COA order id is new."""
    init_db()
    session = _open_session()
    try:
        rows = session.scalars(select(SanctionedPossession.id)).all()
        return len(rows) + 1
    finally:
        session.close()


def _message_dict(row: BlockMessage) -> dict[str, object]:
    """Day label and clock time only. The stored ISO time is not returned."""
    return {
        "id": int(row.id),
        "sim_day": int(row.sim_day),
        "hhmm": row.hhmm,
        "day_label": row.day_label,
        "from_role": row.from_role,
        "to_role": row.to_role,
        "kind": row.kind,
        "sequence": int(row.sequence or 0),
        "text": row.text,
        "possession_ref": row.possession_ref,
    }


def list_block_messages(after_id: int = 0, limit: int = 100) -> list[dict[str, object]]:
    """Messages after `after_id`, oldest first. `limit` is capped at 100."""
    init_db()
    bounded = 100 if limit < 1 or limit > 100 else int(limit)
    start = 0 if after_id < 0 else int(after_id)
    statement = (
        select(BlockMessage)
        .where(BlockMessage.id > start)
        .order_by(BlockMessage.id.asc())
        .limit(bounded)
    )
    session = _open_session()
    try:
        rows = list(session.scalars(statement).all())
        return [_message_dict(row) for row in rows]
    finally:
        session.close()


def load_open_possession() -> dict[str, object] | None:
    """The newest possession that is still open, or nothing when the last one was closed.

    The caller puts this on the console. This module does not import the app,
    so `python app.py` and `uvicorn app:app` share one system state.
    """
    session = _open_session()
    try:
        row = session.scalars(
            select(SanctionedPossession).order_by(SanctionedPossession.id.desc()).limit(1)
        ).first()
        if row is None or row.outcome not in ("SANCTIONED", "SHIFTED", "WITHDRAWING"):
            return None
        payload_text = row.possession_payload
        plan_type = row.plan_type
        sanctioned_by = row.sanctioned_by
        emergency = bool(row.emergency)
    finally:
        session.close()

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "plan_type": plan_type,
        "sanctioned_by": sanctioned_by,
        "emergency": emergency,
        "possession": payload,
    }
