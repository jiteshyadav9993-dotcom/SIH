"""
report_store.py
====================================================================
Pending report previews and the evidence rows that stay after apply.

The open-backlog change and the applied status share one transaction.
A failed commit leaves both the preview and the backlog where they were.
====================================================================
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

import sim_clock
from persistence import Base, _SessionLocal, init_db

PREVIEW_RETENTION_DAYS: int = 7
STATUS_PENDING: str = "pending"
STATUS_APPLIED: str = "applied"


class MaintenanceReport(Base):
    """One uploaded report. The preview JSON is the server copy used at apply time."""

    __tablename__ = "maintenance_reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    preview_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReportEvidence(Base):
    """Source metadata for one finding reference.

    defect_id is filled when an approved candidate is applied. A ruled-out
    row stays null and uses preview_status RULED_OUT. Its reason is kept in
    reported_severity_text because this table has no separate reason column.
    """

    __tablename__ = "report_evidence"
    __table_args__ = (
        UniqueConstraint("report_id", "source_finding_id", "reference", name="uq_report_evidence_ref"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_finding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    defect_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    reference: Mapped[str] = mapped_column(String(128), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(32), nullable=False)
    reported_defect_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_defect_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_severity_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_track_line: Mapped[str | None] = mapped_column(String(64), nullable=True)
    track_line: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chainage_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    reported_due_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    days_overdue: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_tsr: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    department: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preview_status: Mapped[str] = mapped_column(String(32), nullable=False)


def ensure_report_tables() -> None:
    """Create report tables. create_all leaves tables that already exist."""
    init_db()


def open_session() -> Session:
    ensure_report_tables()
    return _SessionLocal()


def commit_session(session: Session) -> None:
    session.commit()


def load_report(report_id: str) -> dict[str, object] | None:
    session = open_session()
    try:
        row = session.get(MaintenanceReport, report_id)
        if row is None:
            return None
        return {
            "report_id": row.report_id,
            "filename": row.filename,
            "status": row.status,
            "summary": row.summary,
            "preview_json": row.preview_json,
            "result_json": row.result_json,
        }
    finally:
        session.close()


def load_preview(report_id: str) -> dict[str, object] | None:
    row = load_report(report_id)
    if row is None:
        return None
    payload = json.loads(str(row["preview_json"]))
    if not isinstance(payload, dict):
        return None
    payload["stored_status"] = row["status"]
    raw_result = row["result_json"]
    if isinstance(raw_result, str) and raw_result:
        stored = json.loads(raw_result)
        if isinstance(stored, dict):
            payload["stored_result"] = stored
    return payload


def save_preview(preview: dict[str, object]) -> str:
    """Insert or replace a pending preview. An applied report is left unchanged."""
    report_id = str(preview["report_id"])
    session = open_session()
    try:
        row = session.get(MaintenanceReport, report_id)
        if row is not None and row.status == STATUS_APPLIED:
            return STATUS_APPLIED
        _expire_pending(session, sim_clock.now_datetime())
        row = session.get(MaintenanceReport, report_id)
        now = sim_clock.now_datetime()
        body = json.dumps(preview)
        if row is None:
            row = MaintenanceReport(
                report_id=report_id,
                filename=str(preview["filename"]),
                status=STATUS_PENDING,
                summary=str(preview.get("summary") or ""),
                preview_json=body,
                created_at=now,
                applied_at=None,
                result_json=None,
            )
            session.add(row)
        else:
            row.filename = str(preview["filename"])
            row.status = STATUS_PENDING
            row.summary = str(preview.get("summary") or "")
            row.preview_json = body
            row.applied_at = None
            row.result_json = None
            for evidence in session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).all():
                session.delete(evidence)
        session.flush()
        for evidence in _evidence_rows(preview):
            session.add(evidence)
        commit_session(session)
        return STATUS_PENDING
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def applied_report_exists(report_id: str) -> bool:
    row = load_report(report_id)
    return row is not None and row["status"] == STATUS_APPLIED


def _expire_pending(session: Session, now: datetime) -> None:
    cutoff = now - timedelta(days=PREVIEW_RETENTION_DAYS)
    stale = session.scalars(
        select(MaintenanceReport).where(
            MaintenanceReport.status == STATUS_PENDING,
            MaintenanceReport.created_at < cutoff,
        )
    ).all()
    for row in stale:
        for evidence in session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == row.report_id)).all():
            session.delete(evidence)
        session.delete(row)


def _evidence_rows(preview: dict[str, object]) -> list[ReportEvidence]:
    rows: list[ReportEvidence] = []
    filename = str(preview.get("filename") or "")
    report_id = str(preview["report_id"])
    candidates = preview.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            fields = candidate.get("fields")
            if not isinstance(fields, dict):
                continue
            evidence_rows = fields.get("evidence")
            if not isinstance(evidence_rows, list) or not evidence_rows:
                evidence_rows = [{"reference": "", "page_number": None, "excerpt": fields.get("excerpt") or ""}]
            seen: set[str] = set()
            for item in evidence_rows:
                if not isinstance(item, dict):
                    continue
                reference = str(item.get("reference") or "")
                if reference in seen:
                    continue
                seen.add(reference)
                page = item.get("page_number")
                rows.append(
                    ReportEvidence(
                        report_id=report_id,
                        source_finding_id=str(candidate["candidate_id"]),
                        defect_id=None,
                        filename=filename,
                        reference=reference,
                        page_number=page if isinstance(page, int) else None,
                        excerpt=str(fields.get("excerpt") or ""),
                        statement=str(fields.get("statement") or ""),
                        extraction_method=str(fields.get("extraction_method") or ""),
                        reported_defect_type=_text(fields.get("reported_defect_type")),
                        normalized_defect_type=_sourced_text(fields.get("normalized_defect_type")),
                        reported_severity_text=_text(fields.get("reported_severity_text")),
                        severity=_optional_int(_sourced_value(fields.get("severity"))),
                        reported_track_line=_text(fields.get("reported_track_line")),
                        track_line=_sourced_text(fields.get("track_line")),
                        chainage_km=_optional_float(_sourced_value(fields.get("chainage_km"))),
                        reported_due_date=_sourced_text(fields.get("due_date")),
                        days_overdue=_optional_float(_sourced_value(fields.get("days_overdue"))),
                        has_tsr=_optional_bool(_sourced_value(fields.get("has_tsr"))),
                        department=_text(fields.get("department")),
                        preview_status=str(candidate.get("status") or ""),
                    )
                )
    ruled_out = preview.get("ruled_out")
    if isinstance(ruled_out, list):
        for item in ruled_out:
            if isinstance(item, dict):
                rows.extend(_ruled_out_evidence(report_id, filename, item))
    return rows


def _ruled_out_evidence(report_id: str, filename: str, item: dict[str, object]) -> list[ReportEvidence]:
    """One audit row per source reference. No operational defect id is assigned."""
    finding_id = str(item.get("finding_id") or "").strip()
    if not finding_id:
        return []
    references = item.get("references")
    if not isinstance(references, list) or not references:
        references = [""]
    pages = item.get("page_numbers")
    if not isinstance(pages, list):
        pages = []
    rows: list[ReportEvidence] = []
    seen: set[str] = set()
    for index, reference in enumerate(references):
        ref = str(reference or "")
        if ref in seen:
            continue
        seen.add(ref)
        page = pages[index] if index < len(pages) else None
        rows.append(
            ReportEvidence(
                report_id=report_id,
                source_finding_id=finding_id,
                defect_id=None,
                filename=filename,
                reference=ref,
                page_number=page if isinstance(page, int) else None,
                excerpt=str(item.get("excerpt") or ""),
                statement=str(item.get("statement") or ""),
                extraction_method=str(item.get("extraction_method") or ""),
                reported_defect_type=None,
                normalized_defect_type=None,
                reported_severity_text=_text(item.get("reason")),
                severity=None,
                reported_track_line=None,
                track_line=None,
                chainage_km=None,
                reported_due_date=None,
                days_overdue=None,
                has_tsr=None,
                department=None,
                preview_status="RULED_OUT",
            )
        )
    return rows


def applied_report_summaries() -> list[dict[str, object]]:
    """Saved summaries for applied reports, oldest applied first. Pending previews are omitted."""
    session = open_session()
    try:
        rows = session.scalars(
            select(MaintenanceReport)
            .where(MaintenanceReport.status == STATUS_APPLIED)
            .order_by(MaintenanceReport.applied_at, MaintenanceReport.report_id)
        ).all()
        summaries: list[dict[str, object]] = []
        for row in rows:
            evidence = session.scalars(
                select(ReportEvidence)
                .where(ReportEvidence.report_id == row.report_id)
                .order_by(ReportEvidence.id)
            ).all()
            defect_ids = list(dict.fromkeys(item.defect_id for item in evidence if item.defect_id))
            summaries.append(
                {
                    "report_id": row.report_id,
                    "filename": row.filename,
                    "summary": row.summary,
                    "applied_at": row.applied_at.isoformat() if row.applied_at is not None else None,
                    "defect_ids": defect_ids,
                }
            )
        return summaries
    finally:
        session.close()


def evidence_for_report(report_id: str) -> list[dict[str, object]]:
    session = open_session()
    try:
        rows = session.scalars(
            select(ReportEvidence).where(ReportEvidence.report_id == report_id).order_by(ReportEvidence.id)
        ).all()
        return [_evidence_dict(row) for row in rows]
    finally:
        session.close()


def evidence_for_defect(defect_id: str) -> list[dict[str, object]]:
    """Rows linked to this operational defect id. Other defects are excluded."""
    wanted = str(defect_id).strip()
    if not wanted:
        return []
    session = open_session()
    try:
        rows = session.scalars(
            select(ReportEvidence)
            .where(ReportEvidence.defect_id == wanted)
            .order_by(ReportEvidence.id)
        ).all()
        return [_evidence_dict(row) for row in rows]
    finally:
        session.close()


def _evidence_dict(row: ReportEvidence) -> dict[str, object]:
    return {
        "report_id": row.report_id,
        "defect_id": row.defect_id,
        "source_finding_id": row.source_finding_id,
        "filename": row.filename,
        "reference": row.reference,
        "page_number": row.page_number,
        "excerpt": row.excerpt,
        "statement": row.statement,
        "extraction_method": row.extraction_method,
        "reported_defect_type": row.reported_defect_type,
        "normalized_defect_type": row.normalized_defect_type,
        "reported_severity_text": row.reported_severity_text,
        "severity": row.severity,
        "reported_track_line": row.reported_track_line,
        "track_line": row.track_line,
        "chainage_km": row.chainage_km,
        "reported_due_date": row.reported_due_date,
        "days_overdue": row.days_overdue,
        "has_tsr": row.has_tsr,
        "department": row.department,
        "preview_status": row.preview_status,
    }


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _sourced_value(value: object) -> object:
    if isinstance(value, dict):
        return value.get("value")
    return value


def _sourced_text(value: object) -> str | None:
    raw = _sourced_value(value)
    return raw if isinstance(raw, str) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


__all__ = [
    "PREVIEW_RETENTION_DAYS",
    "STATUS_APPLIED",
    "STATUS_PENDING",
    "MaintenanceReport",
    "ReportEvidence",
    "applied_report_exists",
    "applied_report_summaries",
    "commit_session",
    "ensure_report_tables",
    "evidence_for_defect",
    "evidence_for_report",
    "load_preview",
    "load_report",
    "open_session",
    "save_preview",
]
