"""
backlog_state.py
====================================================================
The live maintenance backlog, kept between runs.

What this file does, in plain words:
  * Holds the list of open defects the whole app plans against.
  * A fresh database starts empty. Report apply is what adds open defects.
  * next_maintenance_cycle() keeps waiting defects and makes each one a
    day more overdue. It does not invent new defects.
  * complete_defects() removes finished defects from the open list and
    writes them to a permanent "completed maintenance" log for reports.
    An empty open list stays empty.
  * Synthetic catalog and cycle-generator rows already stored as open
    work can be archived and later restored. They are not written to the
    completed log.
  * Everything is saved in the same database file as the audit log, so a
    server restart does not lose the backlog or the completed log.

All stored times are simulated. completed_at, updated_at, and the
window base date come from sim_clock, not from the laptop clock.
====================================================================
"""

from __future__ import annotations

import json
import random
import re
import threading
from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

import defects
import sim_clock
import topology
from defects import DefectType, TrackDefect
from persistence import Base, _SessionLocal, _engine

# ---------------------------------------------------------------------------
# Database tables (created in the same SQLite/Postgres file as persistence.py)
# ---------------------------------------------------------------------------


class BacklogSnapshot(Base):
    """One row (id=1): the open defects as JSON, plus the cycle counter."""

    __tablename__ = "backlog_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    defects_json: Mapped[str] = mapped_column(Text, nullable=False)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # All stored times are simulated.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompletedMaintenance(Base):
    """One row per defect that has been fixed."""

    __tablename__ = "completed_maintenance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    defect_id: Mapped[str] = mapped_column(String(64), nullable=False)
    defect_type: Mapped[str] = mapped_column(String(32), nullable=False)
    section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    track_line: Mapped[str] = mapped_column(String(32), nullable=False)
    km_location: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    days_overdue: Mapped[float] = mapped_column(Float, nullable=False)
    required_block_min: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_min: Mapped[int] = mapped_column(Integer, nullable=False)
    end_min: Mapped[int] = mapped_column(Integer, nullable=False)
    window_label: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    # All stored times are simulated.
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)


class WindowState(Base):
    """One row (id=1): the planning base date and the next window index."""

    __tablename__ = "window_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_date: Mapped[str] = mapped_column(Text, nullable=False)
    next_index: Mapped[int] = mapped_column(Integer, nullable=False)


class ArchivedSyntheticDefect(Base):
    """An open synthetic row removed from the backlog. This is not completed work."""

    __tablename__ = "archived_synthetic_defects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    defect_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# In-memory cache (guarded by a lock: FastAPI runs sync routes in threads)
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_ACTIVE: list[TrackDefect] | None = None
_CYCLE: int = 0
_COMPLETED_IDS: set[str] = set()
# Defects held by the one sanctioned block. Still listed on the backlog as SCHEDULED.
_SCHEDULED_IDS: set[str] = set()
# Windows cleared during this process. Not written to the database.
_USED_WINDOWS: set[int] = set()

# ---------------------------------------------------------------------------
# Random generator settings
# ---------------------------------------------------------------------------

TARGET_TOTALS: tuple[int, ...] = (14, 16, 18)
MAX_OPEN_DEFECTS: int = 20  # keeps the demo readable if cycles are pressed without finishing work
LEFTOVER_AGING_DAYS: float = 1.0  # each waiting defect is this many days older on the next cycle
# Same fault type, same line, and this close in km counts as the same problem.
SAME_PROBLEM_KM: float = 3.0
# Only generate_random_defects and _stamp_defect write this id shape.
_RANDOM_DEFECT_ID = re.compile(r"^DEF_S[1-4]_C\d{2}_\d{2}$")

# Heavier jobs (tamping, deep screening) are rarer: their possession is so long
# that the planner often cannot fit them, which makes for a boring demo.
_TYPE_WEIGHTS: tuple[tuple[DefectType, int], ...] = (
    (DefectType.RAIL_FRACTURE, 20),
    (DefectType.WELD_FAILURE, 24),
    (DefectType.OHE_SAG, 17),
    (DefectType.SIGNAL_INTERLOCK, 20),
    (DefectType.TAMPING_REQUIRED, 12),
    (DefectType.DEEP_SCREENING, 3),
)

_SEVERITY_RANGE: dict[DefectType, tuple[int, int]] = {
    DefectType.RAIL_FRACTURE: (70, 98),
    DefectType.WELD_FAILURE: (60, 90),
    DefectType.OHE_SAG: (55, 92),
    DefectType.SIGNAL_INTERLOCK: (50, 90),
    DefectType.TAMPING_REQUIRED: (40, 70),
    DefectType.DEEP_SCREENING: (45, 70),
}

# (section number, km low, km high, lines, weight)
_SECTIONS: tuple[tuple[int, float, float, tuple[str, ...], int], ...] = (
    (1, 1.0, 53.0, (topology.LINE_UP_FAST, topology.LINE_DN_FAST, topology.LINE_UP_SLOW, topology.LINE_DN_SLOW), 30),
    (2, 55.0, 99.0, (topology.LINE_UP, topology.LINE_DN), 25),
    (3, 101.0, 127.0, (topology.LINE_UP, topology.LINE_DN, topology.LINE_MID), 25),
    (4, 129.0, 191.0, (topology.LINE_UP, topology.LINE_DN), 20),
)


def _weighted_choice(rng: random.Random, pairs):
    total = sum(weight for _, weight in pairs)
    pick = rng.uniform(0, total)
    running = 0.0
    for item, weight in pairs:
        running += weight
        if pick <= running:
            return item
    return pairs[-1][0]


def _new_defect(
    rng: random.Random,
    defect_id: str,
    anchor: TrackDefect | None,
) -> TrackDefect:
    """Make one valid random defect. If `anchor` is given, sit close to it (same line)."""
    for _ in range(40):
        defect_type = _weighted_choice(rng, _TYPE_WEIGHTS)
        low, high = _SEVERITY_RANGE[defect_type]
        severity = rng.randint(low, high)
        if anchor is not None:
            line = anchor.track_line
            km = round(anchor.km_location + rng.choice((-1, 1)) * rng.uniform(0.4, 2.2), 1)
        else:
            _, km_low, km_high, lines, _w = _weighted_choice(rng, [(s, s[4]) for s in _SECTIONS])
            line = rng.choice(lines)
            km = round(rng.uniform(km_low, km_high), 1)
        overdue = 0.0 if rng.random() < 0.4 else float(rng.randint(1, 30))
        has_tsr = rng.random() < (0.5 if severity >= 75 else 0.1)
        try:
            return defects._make_defect(defect_id, km, line, defect_type, severity, overdue, has_tsr)
        except ValueError:
            continue  # line not present at that km, try again
    raise RuntimeError("could not generate a valid defect")


def generate_random_defects(count: int, cycle: int, seed: int | None = None) -> list[TrackDefect]:
    """`count` new valid defects. About a third sit near another one so bundling shows up."""
    rng = random.Random(seed)
    made: list[TrackDefect] = []
    for index in range(1, count + 1):
        anchor = rng.choice(made) if made and rng.random() < 0.35 else None
        defect = _new_defect(rng, "TEMP", anchor)  # temporary id, replaced just below
        section = defect.section_id.replace("SECTION_", "S")
        defect = defects._make_defect(
            f"DEF_{section}_C{cycle:02d}_{index:02d}",
            defect.km_location,
            defect.track_line,
            defect.defect_type,
            defect.severity,
            defect.days_overdue,
            defect.has_tsr,
        )
        made.append(defect)
    return made


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def _to_json(items: list[TrackDefect]) -> str:
    return json.dumps(
        [
            {
                "defect_id": d.defect_id,
                "km": d.km_location,
                "line": d.track_line,
                "type": d.defect_type.value,
                "severity": d.severity,
                "days_overdue": d.days_overdue,
                "has_tsr": d.has_tsr,
            }
            for d in items
        ]
    )


def _from_json(text: str) -> list[TrackDefect]:
    rows = json.loads(text)
    return [
        defects._make_defect(
            r["defect_id"], r["km"], r["line"], DefectType(r["type"]),
            int(r["severity"]), float(r["days_overdue"]), bool(r["has_tsr"]),
        )
        for r in rows
    ]


def _save_snapshot() -> None:
    assert _ACTIVE is not None
    session = _SessionLocal()
    try:
        row = session.get(BacklogSnapshot, 1)
        now = sim_clock.now_datetime()
        if row is None:
            session.add(BacklogSnapshot(id=1, defects_json=_to_json(_ACTIVE), cycle_number=_CYCLE, updated_at=now))
        else:
            row.defects_json = _to_json(_ACTIVE)
            row.cycle_number = _CYCLE
            row.updated_at = now
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_loaded() -> None:
    global _ACTIVE, _CYCLE
    with _LOCK:
        if _ACTIVE is not None:
            return
        Base.metadata.create_all(_engine)
        session = _SessionLocal()
        try:
            row = session.get(BacklogSnapshot, 1)
            snapshot = (row.defects_json, row.cycle_number) if row is not None else None
        finally:
            session.close()
        session = _SessionLocal()
        try:
            _COMPLETED_IDS.update(session.scalars(select(CompletedMaintenance.defect_id)).all())
        finally:
            session.close()
        if snapshot is not None:
            _ACTIVE = _from_json(snapshot[0])
            _CYCLE = int(snapshot[1])
        else:
            _ACTIVE = []
            _CYCLE = 0
            _save_snapshot()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def get_active_defects() -> tuple[TrackDefect, ...]:
    """Open defects right now (finished ones are already gone)."""
    _ensure_loaded()
    with _LOCK:
        assert _ACTIVE is not None
        return tuple(_ACTIVE)


def current_cycle() -> int:
    _ensure_loaded()
    return _CYCLE


def _same_problem(candidate: TrackDefect, prior: TrackDefect) -> bool:
    """True when both are the same fault on the same line within SAME_PROBLEM_KM."""
    return (
        candidate.defect_type == prior.defect_type
        and candidate.track_line == prior.track_line
        and abs(candidate.km_location - prior.km_location) <= SAME_PROBLEM_KM
    )


def _stamp_defect(candidate: TrackDefect, cycle: int, index: int) -> TrackDefect:
    section = candidate.section_id.replace("SECTION_", "S")
    return defects._make_defect(
        f"DEF_{section}_C{cycle:02d}_{index:02d}",
        candidate.km_location,
        candidate.track_line,
        candidate.defect_type,
        candidate.severity,
        candidate.days_overdue,
        candidate.has_tsr,
    )


def _defects_unlike(
    rng: random.Random,
    count: int,
    cycle: int,
    previous: list[TrackDefect],
) -> list[TrackDefect]:
    """`count` new defects that do not repeat a problem in `previous`."""
    made: list[TrackDefect] = []
    attempts = 0
    while len(made) < count and attempts < count * 50:
        attempts += 1
        anchor = rng.choice(made) if made and rng.random() < 0.35 else None
        try:
            candidate = _new_defect(rng, "TEMP", anchor)
        except RuntimeError:
            break
        if any(_same_problem(candidate, prior) for prior in previous):
            continue
        made.append(_stamp_defect(candidate, cycle, len(made) + 1))
    while len(made) < count:
        try:
            candidate = _new_defect(rng, "TEMP", None)
        except RuntimeError:
            break
        made.append(_stamp_defect(candidate, cycle, len(made) + 1))
    return made


def _age_leftover(defect: TrackDefect) -> TrackDefect:
    """Same defect, one day more overdue, so urgency is recomputed."""
    return defects._make_defect(
        defect.defect_id,
        defect.km_location,
        defect.track_line,
        defect.defect_type,
        defect.severity,
        float(defect.days_overdue) + LEFTOVER_AGING_DAYS,
        defect.has_tsr,
    )


def _fresh_batch(rng: random.Random, previous: list[TrackDefect]) -> list[TrackDefect]:
    """Build a synthetic open list. Normal cycle and completion do not call this.

    An empty list becomes 14, 16 or 18 generated defects, never more than
    MAX_OPEN_DEFECTS. When defects are already waiting, each one is kept
    and aged by LEFTOVER_AGING_DAYS. A target of 14, 16 or 18 is picked
    at random. New defects fill toward that target, at least two unless
    the open total would pass MAX_OPEN_DEFECTS. A single free slot still
    gets one defect. A full list gains nothing. New defects do not repeat
    a problem in the aged waiting list, and their ids use the incremented
    cycle number.
    """
    global _CYCLE
    _CYCLE += 1
    if not previous:
        fresh_count = min(rng.choice(TARGET_TOTALS), MAX_OPEN_DEFECTS)
        return generate_random_defects(fresh_count, _CYCLE, rng.randint(0, 10**9))
    aged = [_age_leftover(defect) for defect in previous]
    target = rng.choice(TARGET_TOTALS)
    room = max(0, MAX_OPEN_DEFECTS - len(aged))
    incoming = min(max(target - len(aged), 2), room)
    added = _defects_unlike(rng, incoming, _CYCLE, aged)
    return aged + added


def next_maintenance_cycle(seed: int | None = None) -> dict[str, object]:
    """
    Advance the open backlog by one maintenance cycle.

    Waiting defects stay on the list. Each is rebuilt with the same id,
    location, line, type, severity and TSR flag, and with days_overdue
    raised by LEFTOVER_AGING_DAYS, so urgency is recomputed. No new
    defects are generated. An empty backlog stays empty.
    """
    global _ACTIVE, _CYCLE
    _ = seed
    _ensure_loaded()
    with _LOCK:
        assert _ACTIVE is not None
        waiting = list(_ACTIVE)
        if not waiting:
            return {
                "cycle_number": _CYCLE,
                "carried_over": 0,
                "new_defects": 0,
                "open_total": 0,
            }
        _CYCLE += 1
        opened = [_age_leftover(defect) for defect in waiting]
        _ACTIVE = opened
        _save_snapshot()
        return {
            "cycle_number": _CYCLE,
            "carried_over": len(waiting),
            "new_defects": 0,
            "open_total": len(_ACTIVE),
        }


def is_completed(defect_id: str) -> bool:
    _ensure_loaded()
    return defect_id in _COMPLETED_IDS


def window_label_for(start_min: int, emergency: bool) -> str:
    """Name the kind of window a possession started in."""
    if emergency:
        return "EMERGENCY"
    if 0 <= start_min < 240:
        return "NIGHTLY"
    if 690 <= start_min <= 870:
        return "DAY_OFFPEAK"
    return "OTHER"


def complete_defects(
    items: list[TrackDefect],
    bundle_id: str,
    start_min: int,
    end_min: int,
    window_label: str,
    completed_by: str,
) -> int:
    """
    Mark these defects as fixed: write them to the completed log and take them
    off the open list. An empty open list stays empty. Returns how many were logged.
    """
    global _ACTIVE
    _ensure_loaded()
    with _LOCK:
        assert _ACTIVE is not None
        open_ids = {d.defect_id for d in _ACTIVE}
        fresh_items = [d for d in items if d.defect_id in open_ids or d.defect_id not in _COMPLETED_IDS]
        if not fresh_items:
            return 0
        now = sim_clock.now_datetime()
        session = _SessionLocal()
        try:
            for d in fresh_items:
                session.add(
                    CompletedMaintenance(
                        defect_id=d.defect_id,
                        defect_type=d.defect_type.value,
                        section_id=d.section_id,
                        track_line=d.track_line,
                        km_location=d.km_location,
                        severity=d.severity,
                        days_overdue=d.days_overdue,
                        required_block_min=int(d.required_block_min),
                        bundle_id=bundle_id,
                        start_min=int(start_min),
                        end_min=int(end_min),
                        window_label=window_label,
                        completed_by=completed_by,
                        completed_at=now,
                        cycle_number=_CYCLE,
                    )
                )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        done_ids = {d.defect_id for d in fresh_items}
        _COMPLETED_IDS.update(done_ids)
        _SCHEDULED_IDS.difference_update(done_ids)
        _ACTIVE = [d for d in _ACTIVE if d.defect_id not in done_ids]
        _save_snapshot()
        return len(fresh_items)


def get_completed_log(limit: int = 200) -> list[dict[str, object]]:
    """Newest first."""
    _ensure_loaded()
    session = _SessionLocal()
    try:
        rows = session.scalars(
            select(CompletedMaintenance).order_by(CompletedMaintenance.id.desc()).limit(max(1, min(limit, 1000)))
        ).all()
        return [
            {
                "defect_id": r.defect_id,
                "defect_type": r.defect_type,
                "section_id": r.section_id,
                "track_line": r.track_line,
                "km_location": r.km_location,
                "severity": r.severity,
                "days_overdue": r.days_overdue,
                "required_block_min": r.required_block_min,
                "bundle_id": r.bundle_id,
                "start_min": r.start_min,
                "end_min": r.end_min,
                "window_label": r.window_label,
                "completed_by": r.completed_by,
                "completed_at": r.completed_at.isoformat(),
                "cycle_number": r.cycle_number,
                **sim_clock.day_fields(r.completed_at),
            }
            for r in rows
        ]
    finally:
        session.close()


def stage_open_backlog(session: object, items: Iterable[TrackDefect]) -> list[str]:
    """Put `items` on the open-backlog row inside the caller's transaction.

    The caller commits. Completed history, reserved ids, and the in-memory
    list stay as they are until install_open_backlog runs after that commit.
    """
    from sqlalchemy.orm import Session

    if not isinstance(session, Session):
        raise TypeError("session must be a SQLAlchemy session")
    if _ACTIVE is None:
        raise RuntimeError("The open backlog is not loaded.")
    previous = [item.defect_id for item in _ACTIVE]
    payload = _to_json(list(items))
    now = sim_clock.now_datetime()
    row = session.get(BacklogSnapshot, 1)
    if row is None:
        session.add(BacklogSnapshot(id=1, defects_json=payload, cycle_number=_CYCLE, updated_at=now))
    else:
        row.defects_json = payload
        row.cycle_number = _CYCLE
        row.updated_at = now
    return previous


def install_open_backlog(items: Iterable[TrackDefect]) -> None:
    """Publish a backlog that has already been committed."""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = list(items)


def reset_to_demo_backlog() -> dict[str, object]:
    """Leave the open backlog as it is. The synthetic catalog is not loaded."""
    _ensure_loaded()
    with _LOCK:
        assert _ACTIVE is not None
        return {
            "status": "RETIRED",
            "open_total": len(_ACTIVE),
            "message": "The synthetic demo set is not loaded. The report-derived backlog was not changed.",
        }


def _defect_payload(defect: TrackDefect) -> dict[str, object]:
    return {
        "defect_id": defect.defect_id,
        "km": defect.km_location,
        "line": defect.track_line,
        "type": defect.defect_type.value,
        "severity": defect.severity,
        "days_overdue": defect.days_overdue,
        "has_tsr": defect.has_tsr,
    }


def _matches_aged_default(defect: TrackDefect, original: TrackDefect) -> bool:
    """True when `defect` is the catalog row, possibly aged by whole days."""
    if defect.km_location != original.km_location or defect.track_line != original.track_line:
        return False
    if defect.defect_type != original.defect_type or defect.severity != original.severity:
        return False
    if defect.has_tsr != original.has_tsr:
        return False
    delta = float(defect.days_overdue) - float(original.days_overdue)
    if delta < -1e-6:
        return False
    return abs(delta - round(delta)) < 1e-6


def synthetic_origin(defect: TrackDefect, linked_ids: set[str] | None = None) -> str | None:
    """Name the generator that produced `defect`, or None when it is not synthetic.

    A report id, or any id already tied to report evidence, is kept even when
    the text looks like a catalog id. A catalog id counts only when the stored
    line, chainage, type, severity and TSR still match that generator. Overdue
    days may be the catalog value plus whole leftover-aging steps. The cycle
    generator is the only writer of DEF_S<n>_C<cycle>_<index> ids.
    """
    linked = linked_ids or set()
    if defect.defect_id.startswith("AI1-") or defect.defect_id in linked:
        return None
    catalog = {item.defect_id: item for item in defects.get_default_defects()}
    original = catalog.get(defect.defect_id)
    if original is not None and _matches_aged_default(defect, original):
        return "default_catalog"
    if _RANDOM_DEFECT_ID.fullmatch(defect.defect_id):
        return "random_cycle_generator"
    return None


def _evidence_linked_ids() -> set[str]:
    import report_store

    report_store.ensure_report_tables()
    session = _SessionLocal()
    try:
        rows = session.scalars(select(report_store.ReportEvidence.defect_id)).all()
        return {str(item) for item in rows if item}
    finally:
        session.close()


def _protected_defect_ids() -> set[str]:
    """Ids reserved in this process or named on the open possession."""
    protected = set(_SCHEDULED_IDS)
    import persistence

    loaded = persistence.load_open_possession()
    if not isinstance(loaded, dict):
        return protected
    payload = loaded.get("possession")
    if not isinstance(payload, dict):
        return protected
    raw_ids = payload.get("defect_ids")
    if isinstance(raw_ids, list):
        protected.update(str(item) for item in raw_ids if isinstance(item, str) and item)
    return protected


def migrate_legacy_synthetic_open_records() -> dict[str, object]:
    """Archive open synthetic rows. Reserved or possession-backed rows block the whole move."""
    global _ACTIVE
    _ensure_loaded()
    with _LOCK:
        assert _ACTIVE is not None
        linked = _evidence_linked_ids()
        chosen = [(defect, origin) for defect in _ACTIVE if (origin := synthetic_origin(defect, linked)) is not None]
        current_ids = [defect.defect_id for defect in _ACTIVE]
        if not chosen:
            return {
                "status": "UNCHANGED",
                "archived_defect_ids": [],
                "kept_defect_ids": current_ids,
                "blocked_defect_ids": [],
                "conflict": None,
                "message": "No synthetic open records were found. The backlog was not changed.",
            }
        protected = _protected_defect_ids()
        blocked = [defect.defect_id for defect, _origin in chosen if defect.defect_id in protected]
        if blocked:
            listed = ", ".join(blocked)
            return {
                "status": "CONFLICT",
                "archived_defect_ids": [],
                "kept_defect_ids": current_ids,
                "blocked_defect_ids": blocked,
                "conflict": (
                    "Legacy synthetic records were not archived because active work still names them: "
                    f"{listed}. The open backlog was not changed."
                ),
                "message": (
                    "Legacy synthetic records were not archived because active work still names them: "
                    f"{listed}. The open backlog was not changed."
                ),
            }
        archived_ids = [defect.defect_id for defect, _origin in chosen]
        kept = [defect for defect in _ACTIVE if defect.defect_id not in set(archived_ids)]
        now = sim_clock.now_datetime()
        session = _SessionLocal()
        try:
            for defect, origin in chosen:
                payload = json.dumps(_defect_payload(defect))
                existing = session.scalars(
                    select(ArchivedSyntheticDefect).where(
                        ArchivedSyntheticDefect.defect_id == defect.defect_id,
                        ArchivedSyntheticDefect.restored_at.is_(None),
                    )
                ).first()
                if existing is None:
                    session.add(
                        ArchivedSyntheticDefect(
                            defect_id=defect.defect_id,
                            payload_json=payload,
                            reason=origin,
                            archived_at=now,
                            restored_at=None,
                        )
                    )
                else:
                    existing.payload_json = payload
                    existing.reason = origin
                    existing.archived_at = now
            row = session.get(BacklogSnapshot, 1)
            stored = _to_json(kept)
            if row is None:
                session.add(BacklogSnapshot(id=1, defects_json=stored, cycle_number=_CYCLE, updated_at=now))
            else:
                row.defects_json = stored
                row.cycle_number = _CYCLE
                row.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        _ACTIVE = kept
        return {
            "status": "ARCHIVED",
            "archived_defect_ids": archived_ids,
            "kept_defect_ids": [defect.defect_id for defect in kept],
            "blocked_defect_ids": [],
            "conflict": None,
            "message": (
                "Synthetic open records were archived separately from completed maintenance. "
                "restore_archived_synthetic_defects() can put them back."
            ),
        }


def restore_archived_synthetic_defects(defect_ids: Iterable[str] | None = None) -> dict[str, object]:
    """Put archived synthetic rows back on the open backlog. Completed history is not changed."""
    global _ACTIVE
    _ensure_loaded()
    with _LOCK:
        assert _ACTIVE is not None
        wanted = None if defect_ids is None else {str(item) for item in defect_ids}
        session = _SessionLocal()
        try:
            rows = session.scalars(
                select(ArchivedSyntheticDefect).where(ArchivedSyntheticDefect.restored_at.is_(None))
            ).all()
            chosen = [row for row in rows if wanted is None or row.defect_id in wanted]
            if not chosen:
                return {
                    "status": "UNCHANGED",
                    "restored_defect_ids": [],
                    "message": "No archived synthetic records were waiting to be restored.",
                }
            present = {defect.defect_id for defect in _ACTIVE}
            restored: list[TrackDefect] = []
            now = sim_clock.now_datetime()
            for row in chosen:
                if row.defect_id in present:
                    row.restored_at = now
                    continue
                restored.extend(_from_json(f"[{row.payload_json}]"))
                row.restored_at = now
                present.add(row.defect_id)
            _ACTIVE = list(_ACTIVE) + restored
            snapshot = session.get(BacklogSnapshot, 1)
            stored = _to_json(_ACTIVE)
            if snapshot is None:
                session.add(BacklogSnapshot(id=1, defects_json=stored, cycle_number=_CYCLE, updated_at=now))
            else:
                snapshot.defects_json = stored
                snapshot.cycle_number = _CYCLE
                snapshot.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return {
            "status": "RESTORED",
            "restored_defect_ids": [defect.defect_id for defect in restored],
            "message": "Archived synthetic records were put back on the open backlog. Completed history was not changed.",
        }


def mark_window_used(index: int) -> None:
    """Remember a cleared window until reset. The database is not changed."""
    _ensure_loaded()
    with _LOCK:
        _USED_WINDOWS.add(int(index))


def get_used_windows() -> frozenset[int]:
    """Window indexes cleared in this process."""
    _ensure_loaded()
    with _LOCK:
        return frozenset(_USED_WINDOWS)


def scheduled_defect_ids() -> frozenset[str]:
    """Defects reserved by the sanctioned block. They stay on the backlog."""
    _ensure_loaded()
    with _LOCK:
        return frozenset(_SCHEDULED_IDS)


def reserve_defects(defect_ids: Iterable[str]) -> None:
    """Mark these defects SCHEDULED so planners skip them until release or completion."""
    _ensure_loaded()
    with _LOCK:
        for defect_id in defect_ids:
            if defect_id:
                _SCHEDULED_IDS.add(str(defect_id))


def release_defects(defect_ids: Iterable[str]) -> None:
    """Return withdrawn or rejected defects to the open planning set."""
    _ensure_loaded()
    with _LOCK:
        for defect_id in defect_ids:
            _SCHEDULED_IDS.discard(str(defect_id))


def get_window_start() -> tuple[date, int]:
    """The stored base date, and the first window the simulated clock can still book.

    A window that has ended is skipped. A window on today's simulated date
    is skipped unless it starts at least block_windows.PLANNING_LEAD_MIN
    minutes from now. Cleared windows remembered in this process are skipped too.
    """
    import block_windows

    _ensure_loaded()
    today = sim_clock.sim_date()
    session = _SessionLocal()
    try:
        row = session.get(WindowState, 1)
        if row is None:
            base_text = today.isoformat()
            stored = 0
            session.add(WindowState(id=1, base_date=base_text, next_index=stored))
            session.commit()
        else:
            base_text = row.base_date
            stored = int(row.next_index)
        base = date.fromisoformat(base_text)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    whole_days = (today - base).days
    # Every day has two windows (night, then daytime off-peak).
    # A window that ended earlier on the simulated clock is skipped too.
    passed = 0 if whole_days <= 0 else 2 * whole_days
    index = max(stored, passed)
    with _LOCK:
        used = set(_USED_WINDOWS)
    guard = 0
    while guard < 64:
        plan_date, window = block_windows.window_for_index(base, index)
        if index not in used and not block_windows.window_is_past(plan_date, window):
            break
        index += 1
        guard += 1
    return base, index
