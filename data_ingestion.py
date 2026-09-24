"""
data_ingestion.py
====================================================================
Simulated data-integration layer for SIH-26027.

`defects.py::get_default_defects()` is a hand-written synthetic backlog.
This module stands in for the real ingestion pipeline that would pull
defect and possession-availability exports from four separate Central
Railway systems:

    TMS   Track Management System                (CSV,  legacy P-Way register)
    SMMS  Signalling Maintenance & Mgmt System    (CSV,  signalling fault log)
    TDMS  Traction Distribution Management System (JSON, OHE feeder register)
    COA   Control Office Application              (JSON, sanctioned block windows)

Each source ships its own column names, date formats, and severity scale —
real legacy railway systems do not agree on a schema, so this module does
NOT pretend they do. Every source gets its own parser that:

  1. reads the source's native format (csv.DictReader / json.loads — no
     pandas; the volumes here are a few dozen rows per demo file, so the
     added dependency and binary wheel weight of pandas buys nothing),
  2. maps the source's own vocabulary (line codes, fault codes, priority
     bands, physical measurements) onto the internal `defects.DefectType`
     / `defects.TrackDefect` model,
  3. calls `defects._make_defect()` to build the actual `TrackDefect` --
     this reuses the existing severity-range and line-exists-at-km
     validation instead of duplicating it, and
  4. catches every row-level failure so one bad row rejects only that row,
     with a clear reason, rather than aborting the whole file.

COA records are not defects at all (they describe sanctioned block
*windows*, not backlog items), so they normalize into a small local
`CorridorBlockWindow` record instead of `TrackDefect`. They are parsed,
validated, and reported in the ingest summary, but they are NOT wired into
`solver.py` — see INGESTION.md for exactly what that would take.

If nothing has ever been ingested (or every record was rejected),
`get_effective_defects()` falls back to `defects.get_default_defects()` so
the demo keeps working standalone.
====================================================================
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import defects
import topology
from defects import (
    BlockType,
    DefectType,
    SOURCE_SMMS,
    SOURCE_TDMS,
    SOURCE_TMS,
    TrackDefect,
    _make_defect,
)

SOURCE_COA: str = "COA"

# ---------------------------------------------------------------------------
# Per-source vocabulary lookup tables
# ---------------------------------------------------------------------------

# TMS exports a numeric mainframe line code, not the internal line name.
TMS_LINE_CODES: dict[str, str] = {
    "1": topology.LINE_UP_FAST,
    "2": topology.LINE_DN_FAST,
    "3": topology.LINE_UP_SLOW,
    "4": topology.LINE_DN_SLOW,
    "5": topology.LINE_UP,
    "6": topology.LINE_DN,
    "7": topology.LINE_MID,
}

# TMS defect codes map 1:1 onto the track-maintenance subset of DefectType.
TMS_DEFECT_CODES: dict[str, DefectType] = {
    "RF": DefectType.RAIL_FRACTURE,
    "WF": DefectType.WELD_FAILURE,
    "TR": DefectType.TAMPING_REQUIRED,
    "DS": DefectType.DEEP_SCREENING,
}

# SMMS distinguishes several signalling fault subtypes; TrackDefect only
# models one signalling bucket today, so all of these collapse onto
# SIGNAL_INTERLOCK. See INGESTION.md for this limitation.
SMMS_FAULT_TYPES: dict[str, DefectType] = {
    "INTERLOCK_FAULT": DefectType.SIGNAL_INTERLOCK,
    "POINT_MACHINE_FAULT": DefectType.SIGNAL_INTERLOCK,
    "TRACK_CIRCUIT_FAULT": DefectType.SIGNAL_INTERLOCK,
    "AXLE_COUNTER_FAULT": DefectType.SIGNAL_INTERLOCK,
}

# SMMS logs a text priority band rather than a numeric severity.
SMMS_PRIORITY_SEVERITY: dict[str, int] = {
    "CRITICAL": 95,
    "MAJOR": 78,
    "MINOR": 55,
    "ROUTINE": 35,
}

# TDMS reports OHE defect subtype, not a single "OHE_SAG" label; all of
# these are physically conductor-geometry defects and collapse onto
# OHE_SAG, the only traction category TrackDefect currently models.
TDMS_CATEGORY_MAP: dict[str, DefectType] = {
    "CONDUCTOR_SAG": DefectType.OHE_SAG,
    "CONTACT_WIRE_WEAR": DefectType.OHE_SAG,
    "DROPPER_DAMAGE": DefectType.OHE_SAG,
}

# sag_measurement_mm -> severity. This is a placeholder engineering
# assumption for the demo (roughly: ~30 mm sag is a minor observation,
# ~300 mm sag is a critical, TSR-worthy defect), NOT a real IR/RDSO
# tolerance table. Documented explicitly in INGESTION.md.
TDMS_SAG_TO_SEVERITY_DIVISOR: float = 3.0


# ---------------------------------------------------------------------------
# Row-level result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestError:
    """One rejected row, with enough context to show a controller what to fix."""

    source: str
    row_number: int
    raw_row: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "row": self.row_number,
            "raw_row": self.raw_row,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SourceIngestResult:
    """Outcome of parsing one TMS / SMMS / TDMS export."""

    source: str
    records_read: int
    accepted: tuple[TrackDefect, ...]
    errors: tuple[IngestError, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "records_read": self.records_read,
            "accepted": len(self.accepted),
            "rejected": len(self.errors),
            "errors": [e.to_dict() for e in self.errors],
        }


@dataclass(frozen=True, slots=True)
class CorridorBlockWindow:
    """
    A COA-sanctioned corridor possession window.

    Distinct from a WTT entry: this describes when a line MAY be closed for
    maintenance (pre-approved by the Control Office Application), not a
    train movement. Not consumed by solver.py today; see INGESTION.md.
    """

    sanction_id: str
    section_id: str
    line: str
    calendar_date: date
    window_start_hhmm: str
    window_end_hhmm: str
    block_type: BlockType
    sanctioning_authority: str
    remarks: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "sanction_id": self.sanction_id,
            "section_id": self.section_id,
            "line": self.line,
            "date": self.calendar_date.isoformat(),
            "window_start": self.window_start_hhmm,
            "window_end": self.window_end_hhmm,
            "block_type": self.block_type.value,
            "sanctioning_authority": self.sanctioning_authority,
            "remarks": self.remarks,
        }


@dataclass(frozen=True, slots=True)
class CoaIngestResult:
    """Outcome of parsing one COA corridor-block-availability export."""

    records_read: int
    accepted: tuple[CorridorBlockWindow, ...]
    errors: tuple[IngestError, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source": SOURCE_COA,
            "records_read": self.records_read,
            "accepted": len(self.accepted),
            "rejected": len(self.errors),
            "errors": [e.to_dict() for e in self.errors],
        }


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """Combined result of one /api/ingest run, source by source."""

    tms: SourceIngestResult
    smms: SourceIngestResult
    tdms: SourceIngestResult
    coa: CoaIngestResult
    used_fallback: bool

    def all_defects(self) -> tuple[TrackDefect, ...]:
        """Every accepted defect across TMS, SMMS, and TDMS, combined."""
        return tuple(self.tms.accepted) + tuple(self.smms.accepted) + tuple(self.tdms.accepted)

    def to_dict(self) -> dict[str, object]:
        return {
            "used_fallback": self.used_fallback,
            "total_defects_accepted": len(self.all_defects()),
            "total_coa_windows_accepted": len(self.coa.accepted),
            "sources": {
                SOURCE_TMS: self.tms.to_dict(),
                SOURCE_SMMS: self.smms.to_dict(),
                SOURCE_TDMS: self.tdms.to_dict(),
                SOURCE_COA: self.coa.to_dict(),
            },
        }


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _parse_ddmmyyyy(raw: str) -> date:
    text = str(raw).strip()
    try:
        return datetime.strptime(text, "%d-%m-%Y").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date {text!r}; expected DD-MM-YYYY") from exc


def _parse_iso_date(raw: str) -> date:
    text = str(raw).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date {text!r}; expected YYYY-MM-DD") from exc


def _validate_hhmm(raw: str) -> str:
    text = str(raw).strip()
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"Invalid time {text!r}; expected HH:MM (24h)") from exc
    return text


def _require(row: Mapping[str, Any], column: str) -> str:
    if column not in row or row[column] is None or str(row[column]).strip() == "":
        raise ValueError(f"Missing required column {column!r}")
    return str(row[column]).strip()


def _yes_no(raw: str | None) -> bool:
    return str(raw or "N").strip().upper() in ("Y", "YES", "TRUE", "1")


# ---------------------------------------------------------------------------
# TMS — CSV, numeric line codes, DD-MM-YYYY dates, 1-10 legacy grade scale
# ---------------------------------------------------------------------------


def parse_tms_csv(text: str, *, as_of: date | None = None) -> SourceIngestResult:
    """
    Normalize a TMS defect-register CSV export into TrackDefect records.

    Expected columns: ASSET_ID, LINE_ID, CHAINAGE_KM, DEFECT_CODE, GRADE,
    DATE_REPORTED, DATE_DUE, TSR_FLAG, REMARKS.
    """
    reference = as_of or date.today()
    reader = csv.DictReader(io.StringIO(text))
    accepted: list[TrackDefect] = []
    errors: list[IngestError] = []
    records_read = 0

    for index, row in enumerate(reader):
        records_read += 1
        row_number = index + 2  # header is row 1
        try:
            asset_id = _require(row, "ASSET_ID")
            line_code = _require(row, "LINE_ID")
            if line_code not in TMS_LINE_CODES:
                raise ValueError(f"Unknown TMS LINE_ID {line_code!r}")
            track_line = TMS_LINE_CODES[line_code]

            km_text = _require(row, "CHAINAGE_KM")
            try:
                km_location = float(km_text)
            except ValueError as exc:
                raise ValueError(f"Invalid CHAINAGE_KM {km_text!r}") from exc

            defect_code = _require(row, "DEFECT_CODE").upper()
            if defect_code not in TMS_DEFECT_CODES:
                raise ValueError(f"Unknown TMS DEFECT_CODE {defect_code!r}")
            defect_type = TMS_DEFECT_CODES[defect_code]

            grade_text = _require(row, "GRADE")
            try:
                grade = int(grade_text)
            except ValueError as exc:
                raise ValueError(f"Invalid GRADE {grade_text!r}") from exc
            if grade < 1 or grade > 10:
                raise ValueError(f"GRADE must be in [1, 10] (legacy TMS scale), got {grade}")
            severity = min(100, max(1, grade * 10))

            due_date = _parse_ddmmyyyy(_require(row, "DATE_DUE"))
            days_overdue = max(0.0, float((reference - due_date).days))
            has_tsr = _yes_no(row.get("TSR_FLAG"))

            defect = _make_defect(
                defect_id=asset_id,
                km_location=km_location,
                track_line=track_line,
                defect_type=defect_type,
                severity=severity,
                days_overdue=days_overdue,
                has_tsr=has_tsr,
            )
            accepted.append(defect)
        except (KeyError, ValueError) as exc:
            errors.append(IngestError(SOURCE_TMS, row_number, str(dict(row)), str(exc)))

    return SourceIngestResult(SOURCE_TMS, records_read, tuple(accepted), tuple(errors))


# ---------------------------------------------------------------------------
# SMMS — CSV, spaced line names, pre-computed overdue days, text priority
# ---------------------------------------------------------------------------


def parse_smms_csv(text: str, *, as_of: date | None = None) -> SourceIngestResult:
    """
    Normalize an SMMS signalling-fault CSV export into TrackDefect records.

    Expected columns: FAULT_REF, KM_POST, RUNNING_LINE, FAULT_TYPE,
    PRIORITY_BAND, LOGGED_ON, OVERDUE_DAYS, TSR_ACTIVE, REMARKS.

    `as_of` is accepted for signature symmetry with the other parsers but
    is unused here: SMMS already reports OVERDUE_DAYS directly rather than
    a due-date, which is itself a real point of heterogeneity between
    legacy systems worth keeping visible.
    """
    del as_of
    reader = csv.DictReader(io.StringIO(text))
    accepted: list[TrackDefect] = []
    errors: list[IngestError] = []
    records_read = 0

    for index, row in enumerate(reader):
        records_read += 1
        row_number = index + 2
        try:
            fault_ref = _require(row, "FAULT_REF")

            km_text = _require(row, "KM_POST")
            try:
                km_location = float(km_text)
            except ValueError as exc:
                raise ValueError(f"Invalid KM_POST {km_text!r}") from exc

            track_line = _require(row, "RUNNING_LINE").upper().replace(" ", "_")

            fault_type = _require(row, "FAULT_TYPE").upper()
            if fault_type not in SMMS_FAULT_TYPES:
                raise ValueError(f"Unknown SMMS FAULT_TYPE {fault_type!r}")
            defect_type = SMMS_FAULT_TYPES[fault_type]

            priority_band = _require(row, "PRIORITY_BAND").upper()
            if priority_band not in SMMS_PRIORITY_SEVERITY:
                raise ValueError(f"Unknown SMMS PRIORITY_BAND {priority_band!r}")
            severity = SMMS_PRIORITY_SEVERITY[priority_band]

            overdue_text = _require(row, "OVERDUE_DAYS")
            try:
                days_overdue = float(overdue_text)
            except ValueError as exc:
                raise ValueError(f"Invalid OVERDUE_DAYS {overdue_text!r}") from exc
            if days_overdue < 0:
                raise ValueError("OVERDUE_DAYS cannot be negative")

            has_tsr = _yes_no(row.get("TSR_ACTIVE"))

            defect = _make_defect(
                defect_id=fault_ref,
                km_location=km_location,
                track_line=track_line,
                defect_type=defect_type,
                severity=severity,
                days_overdue=days_overdue,
                has_tsr=has_tsr,
            )
            accepted.append(defect)
        except (KeyError, ValueError) as exc:
            errors.append(IngestError(SOURCE_SMMS, row_number, str(dict(row)), str(exc)))

    return SourceIngestResult(SOURCE_SMMS, records_read, tuple(accepted), tuple(errors))


# ---------------------------------------------------------------------------
# TDMS — JSON, ISO dates, raw physical measurement instead of a severity code
# ---------------------------------------------------------------------------


def parse_tdms_json(text: str, *, as_of: date | None = None) -> SourceIngestResult:
    """
    Normalize a TDMS traction-feeder JSON export into TrackDefect records.

    Expected shape: {"feeder_records": [{feeder_id, substation_id,
    chainage_km, traction_line, defect_category, sag_measurement_mm,
    tsr_imposed, tsr_speed_kmh, last_inspection_date, inspection_due_date,
    remarks}, ...]}
    """
    reference = as_of or date.today()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return SourceIngestResult(
            SOURCE_TDMS, 0, (), (IngestError(SOURCE_TDMS, 0, text[:200], f"Invalid JSON: {exc}"),)
        )

    records = payload.get("feeder_records", []) if isinstance(payload, dict) else []
    accepted: list[TrackDefect] = []
    errors: list[IngestError] = []

    for index, rec in enumerate(records):
        row_number = index + 1
        raw_row = json.dumps(rec) if isinstance(rec, dict) else str(rec)
        try:
            if not isinstance(rec, dict):
                raise ValueError("Record is not a JSON object")

            feeder_id = _require(rec, "feeder_id")

            km_raw = rec.get("chainage_km")
            if km_raw is None:
                raise ValueError("Missing required field 'chainage_km'")
            try:
                km_location = float(km_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid chainage_km {km_raw!r}") from exc

            track_line = _require(rec, "traction_line").upper()

            category = _require(rec, "defect_category").upper()
            if category not in TDMS_CATEGORY_MAP:
                raise ValueError(f"Unknown TDMS defect_category {category!r}")
            defect_type = TDMS_CATEGORY_MAP[category]

            sag_raw = rec.get("sag_measurement_mm")
            if sag_raw is None:
                raise ValueError("Missing required field 'sag_measurement_mm'")
            try:
                sag_mm = float(sag_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid sag_measurement_mm {sag_raw!r}") from exc
            if sag_mm <= 0:
                raise ValueError("sag_measurement_mm must be positive")
            severity = min(100, max(1, round(sag_mm / TDMS_SAG_TO_SEVERITY_DIVISOR)))

            has_tsr = bool(rec.get("tsr_imposed", False))

            due_raw = rec.get("inspection_due_date")
            days_overdue = 0.0
            if due_raw:
                due_date = _parse_iso_date(str(due_raw))
                days_overdue = max(0.0, float((reference - due_date).days))

            defect = _make_defect(
                defect_id=feeder_id,
                km_location=km_location,
                track_line=track_line,
                defect_type=defect_type,
                severity=severity,
                days_overdue=days_overdue,
                has_tsr=has_tsr,
            )
            accepted.append(defect)
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(IngestError(SOURCE_TDMS, row_number, raw_row, str(exc)))

    return SourceIngestResult(SOURCE_TDMS, len(records), tuple(accepted), tuple(errors))


# ---------------------------------------------------------------------------
# COA — JSON, sanctioned corridor block/possession windows (not a defect feed)
# ---------------------------------------------------------------------------


def parse_coa_json(text: str) -> CoaIngestResult:
    """
    Normalize a COA corridor-block-availability JSON export.

    Expected shape: {"corridor_block_windows": [{sanction_id, section_id,
    line, date, window_start, window_end, block_type,
    sanctioning_authority, remarks}, ...]}
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return CoaIngestResult(0, (), (IngestError(SOURCE_COA, 0, text[:200], f"Invalid JSON: {exc}"),))

    records = payload.get("corridor_block_windows", []) if isinstance(payload, dict) else []
    accepted: list[CorridorBlockWindow] = []
    errors: list[IngestError] = []

    for index, rec in enumerate(records):
        row_number = index + 1
        raw_row = json.dumps(rec) if isinstance(rec, dict) else str(rec)
        try:
            if not isinstance(rec, dict):
                raise ValueError("Record is not a JSON object")

            sanction_id = _require(rec, "sanction_id")

            section_id = _require(rec, "section_id").upper()
            if section_id not in topology.SECTION_BOUNDS:
                raise ValueError(f"Unknown section_id {section_id!r}")

            line = _require(rec, "line").upper()

            calendar_date = _parse_iso_date(_require(rec, "date"))
            window_start = _validate_hhmm(_require(rec, "window_start"))
            window_end = _validate_hhmm(_require(rec, "window_end"))

            block_type_raw = _require(rec, "block_type").upper()
            if block_type_raw not in BlockType.__members__:
                raise ValueError(f"Unknown block_type {block_type_raw!r}")
            block_type = BlockType[block_type_raw]

            authority = str(rec.get("sanctioning_authority") or "").strip()
            remarks = str(rec.get("remarks") or "").strip()

            window = CorridorBlockWindow(
                sanction_id=sanction_id,
                section_id=section_id,
                line=line,
                calendar_date=calendar_date,
                window_start_hhmm=window_start,
                window_end_hhmm=window_end,
                block_type=block_type,
                sanctioning_authority=authority,
                remarks=remarks,
            )
            accepted.append(window)
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(IngestError(SOURCE_COA, row_number, raw_row, str(exc)))

    return CoaIngestResult(len(records), tuple(accepted), tuple(errors))


# ---------------------------------------------------------------------------
# Combined entry points
# ---------------------------------------------------------------------------


def ingest_all(
    tms_text: str | None = None,
    smms_text: str | None = None,
    tdms_text: str | None = None,
    coa_text: str | None = None,
    *,
    as_of: date | None = None,
) -> IngestSummary:
    """
    Parse whichever source texts are provided and combine into one summary.

    A source with no text supplied contributes an empty (zero-read)
    result rather than an error -- not every demo run has all four feeds.
    """
    reference = as_of or date.today()

    tms_result = (
        parse_tms_csv(tms_text, as_of=reference)
        if tms_text is not None
        else SourceIngestResult(SOURCE_TMS, 0, (), ())
    )
    smms_result = (
        parse_smms_csv(smms_text, as_of=reference)
        if smms_text is not None
        else SourceIngestResult(SOURCE_SMMS, 0, (), ())
    )
    tdms_result = (
        parse_tdms_json(tdms_text, as_of=reference)
        if tdms_text is not None
        else SourceIngestResult(SOURCE_TDMS, 0, (), ())
    )
    coa_result = parse_coa_json(coa_text) if coa_text is not None else CoaIngestResult(0, (), ())

    total_accepted = len(tms_result.accepted) + len(smms_result.accepted) + len(tdms_result.accepted)
    return IngestSummary(
        tms=tms_result,
        smms=smms_result,
        tdms=tdms_result,
        coa=coa_result,
        used_fallback=total_accepted == 0,
    )


_FOLDER_FILENAMES: dict[str, str] = {
    "tms": "tms_defects.csv",
    "smms": "smms_defects.csv",
    "tdms": "tdms_defects.json",
    "coa": "coa_block_windows.json",
}


def ingest_from_folder(folder: str | Path, *, as_of: date | None = None) -> IngestSummary:
    """
    Dev-mode ingestion: read the four well-known filenames from `folder` if
    present, and treat any that are missing as "not supplied this run".
    """
    folder_path = Path(folder)

    def _read(key: str) -> str | None:
        path = folder_path / _FOLDER_FILENAMES[key]
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    return ingest_all(
        tms_text=_read("tms"),
        smms_text=_read("smms"),
        tdms_text=_read("tdms"),
        coa_text=_read("coa"),
        as_of=as_of,
    )


# ---------------------------------------------------------------------------
# Process-lifetime cache consumed by app.py
# ---------------------------------------------------------------------------

_LAST_SUMMARY: IngestSummary | None = None


def set_last_ingest(summary: IngestSummary) -> None:
    """Record the most recent /api/ingest run so subsequent requests can use it."""
    global _LAST_SUMMARY
    _LAST_SUMMARY = summary


def get_last_ingest() -> IngestSummary | None:
    return _LAST_SUMMARY


def get_effective_defects() -> tuple[TrackDefect, ...]:
    """
    The defect set the rest of the app should plan against.

    Uses the most recently ingested TMS/SMMS/TDMS records if at least one
    was accepted; otherwise falls back to defects.get_default_defects() so
    the demo runs standalone with no ingestion step at all.
    """
    if _LAST_SUMMARY is not None:
        combined = _LAST_SUMMARY.all_defects()
        if combined:
            return combined
    return defects.get_default_defects()


def get_effective_coa_windows() -> tuple[CorridorBlockWindow, ...]:
    """Most recently ingested COA sanctioned block windows, if any."""
    if _LAST_SUMMARY is not None:
        return _LAST_SUMMARY.coa.accepted
    return ()


__all__ = [
    "CoaIngestResult",
    "CorridorBlockWindow",
    "IngestError",
    "IngestSummary",
    "SMMS_FAULT_TYPES",
    "SMMS_PRIORITY_SEVERITY",
    "SOURCE_COA",
    "SourceIngestResult",
    "TDMS_CATEGORY_MAP",
    "TDMS_SAG_TO_SEVERITY_DIVISOR",
    "TMS_DEFECT_CODES",
    "TMS_LINE_CODES",
    "get_effective_coa_windows",
    "get_effective_defects",
    "get_last_ingest",
    "ingest_all",
    "ingest_from_folder",
    "parse_coa_json",
    "parse_smms_csv",
    "parse_tdms_json",
    "parse_tms_csv",
    "set_last_ingest",
]
