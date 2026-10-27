"""
report_pipeline.py
====================================================================
One report-understanding pass over a DocumentBundle.

The provider proposes findings. Local checks keep a value only when the
extracted text states it. Ruled-out sentences stay out of the candidate list.
This module does not write the backlog and does not call the scheduler.
====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from report_documents import DocumentBundle, DocumentSection, ExtractionMethod, ImageReference, ReadStatus, TextBlock
from report_nlp import PendingProfessorNlpAdapter, ReportNlpAdapter
from report_prompts import SYSTEM_PROMPT, build_user_prompt
from report_provider import (
    FIXTURE_NOT_MATCHED_MESSAGE,
    ImagePayload,
    ProviderCallError,
    ProviderRequest,
    ProviderTimeoutError,
    ReportUnderstandingProvider,
    SCRIPTED_MOCK_LABEL,
    build_report_provider,
)
from report_schema import (
    PROVIDER_RESPONSE_SCHEMA,
    DefectCandidate,
    Evidence,
    FindingMethod,
    ReportUnderstanding,
    ReportWarning,
    ReviewStatus,
    RuledOutFinding,
    Uncertainty,
    UnderstandingStatus,
)
from report_validate import CheckedFinding, InvalidProviderOutput, check_findings, excerpt_cues, parse_provider_payload


@dataclass(frozen=True, slots=True)
class PipelineLimits:
    """Bounds for one understanding pass."""

    max_chunk_chars: int = 6000
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_chunk_chars < 40:
            raise ValueError("max_chunk_chars must be at least 40")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


def simplify_report(
    bundle: DocumentBundle,
    provider: ReportUnderstandingProvider | None = None,
    *,
    limits: PipelineLimits | None = None,
    nlp: ReportNlpAdapter | None = None,
) -> ReportUnderstanding:
    """Summarise `bundle` and return separate candidates and ruled-out findings."""
    if not isinstance(bundle, DocumentBundle):
        raise TypeError("bundle must be a DocumentBundle")
    active_limits = limits if limits is not None else PipelineLimits()
    if not isinstance(active_limits, PipelineLimits):
        raise TypeError("limits must be a PipelineLimits")
    reader = nlp if nlp is not None else PendingProfessorNlpAdapter()
    model = provider if provider is not None else build_report_provider()
    nlp_status = "used" if reader.available() else "pending"
    hints = _nlp_hints(reader, bundle) if reader.available() else {}
    warnings = [
        ReportWarning(item.code, item.message, item.reference)
        for item in bundle.warnings
    ]
    if bundle.status is not ReadStatus.COMPLETE:
        warnings.append(
            ReportWarning(
                "DOCUMENT_PARTIAL",
                "The document reader did not extract every page. Unread pages were not interpreted.",
                None,
            )
        )
    report_id = "report-" + bundle.sha256[:12]
    chunks = build_report_chunks(bundle, active_limits.max_chunk_chars)
    if any(chunk.bounded for chunk in chunks):
        warnings.append(
            ReportWarning(
                "CHUNK_BOUNDED",
                "A long section was split into bounded chunks. Each piece keeps its source reference.",
                None,
            )
        )
    accepted: list[CheckedFinding] = []
    rejected: list[str] = []
    attempted = 0
    try:
        for chunk in chunks:
            attempted += 1
            response = model.complete(
                _request(bundle.filename, chunk, active_limits.timeout_seconds, model.supports_images, hints)
            )
            payload = parse_provider_payload(response.raw_text)
            found, row_warnings = check_findings(payload, chunk.sections)
            accepted.extend(found)
            rejected.extend(row_warnings)
    except (ProviderTimeoutError, ProviderCallError, InvalidProviderOutput) as exc:
        return _failed(bundle, report_id, model, warnings, attempted, str(exc), nlp_status)
    except Exception as exc:
        return _failed(bundle, report_id, model, warnings, attempted, str(exc), nlp_status)
    for message in rejected:
        warnings.append(ReportWarning("FINDING_REJECTED", message, None))
    candidates, ruled_out = _publish(bundle.filename, _merge(accepted))
    status = UnderstandingStatus.COMPLETE
    if bundle.status is not ReadStatus.COMPLETE or rejected or any(chunk.bounded for chunk in chunks):
        status = UnderstandingStatus.PARTIAL
    statements = [item.statement for item in candidates] + [item.statement for item in ruled_out]
    if getattr(model, "name", "") == "local-text":
        summary = _local_text_summary(candidates, ruled_out)
    else:
        summary = _summary(statements)
    provider_name = str(getattr(model, "name", "provider"))
    scripted_summary = getattr(model, "scripted_summary", None)
    notice = getattr(model, "fixture_notice", None)
    if (
        bool(getattr(model, "fixture_matched", False))
        and isinstance(scripted_summary, str)
        and scripted_summary.strip()
        and (candidates or ruled_out)
    ):
        summary = scripted_summary.strip()
        fixture_id = str(getattr(model, "scripted_fixture", "") or "").strip()
        if fixture_id:
            provider_name = "mock:" + fixture_id
        warnings.append(
            ReportWarning(
                "SCRIPTED_MOCK",
                SCRIPTED_MOCK_LABEL
                + ". The summary is a canned interpretation. Excerpts and source images come from the document reader.",
                None,
            )
        )
    elif isinstance(notice, str) and notice.strip():
        if not candidates and not ruled_out:
            summary = notice.strip()
        warnings.append(ReportWarning("FIXTURE_NOT_MATCHED", notice.strip() or FIXTURE_NOT_MATCHED_MESSAGE, None))
    return ReportUnderstanding(
        report_id=report_id,
        filename=bundle.filename,
        summary=summary,
        candidates=candidates,
        ruled_out=ruled_out,
        warnings=tuple(warnings),
        status=status,
        provider_name=provider_name,
        chunks_processed=len(chunks),
        error=None,
        nlp_status=nlp_status,
    )


@dataclass(frozen=True, slots=True)
class ReportChunk:
    """One provider call. Sections inside it keep their original references."""

    chunk_id: str
    text: str
    sections: tuple[DocumentSection, ...]
    images: tuple[ImageReference, ...]
    bounded: bool


def build_report_chunks(bundle: DocumentBundle, max_chunk_chars: int) -> tuple[ReportChunk, ...]:
    """Pack sections into chunks. A section longer than the bound is split by line."""
    pieces: list[tuple[DocumentSection, bool]] = []
    for section in bundle.sections:
        pieces.extend(_section_pieces(section, max_chunk_chars))
    groups: list[list[tuple[DocumentSection, bool]]] = []
    current: list[tuple[DocumentSection, bool]] = []
    current_len = 0
    for piece in pieces:
        rendered = _render_section(piece[0])
        if current and current_len + len(rendered) + 1 > max_chunk_chars:
            groups.append(current)
            current = []
            current_len = 0
        current.append(piece)
        current_len += len(rendered) + 1
    if current:
        groups.append(current)
    chunks: list[ReportChunk] = []
    for index, group in enumerate(groups, start=1):
        sections = tuple(item[0] for item in group)
        images = tuple(image for section in sections for image in section.images)
        chunks.append(
            ReportChunk(
                chunk_id=f"chunk-{index}",
                text="\n".join(_render_section(section) for section in sections),
                sections=sections,
                images=images,
                bounded=any(item[1] for item in group),
            )
        )
    return tuple(chunks)


def _section_pieces(section: DocumentSection, max_chunk_chars: int) -> list[tuple[DocumentSection, bool]]:
    rendered = _render_section(section)
    if len(rendered) <= max_chunk_chars:
        return [(section, False)]
    native_parts = _split_text(section.native_text, max_chunk_chars) or [""]
    ocr_parts = _split_text(section.ocr_text, max_chunk_chars) if section.ocr_text else []
    pieces: list[tuple[DocumentSection, bool]] = []
    for index, native in enumerate(native_parts):
        ocr = ocr_parts[index] if index < len(ocr_parts) else ""
        images = section.images if index == 0 else ()
        pieces.append((_copy_section(section, native, ocr, images), True))
    for extra in ocr_parts[len(native_parts) :]:
        pieces.append((_copy_section(section, "", extra, ()), True))
    return pieces


def _split_text(text: str, max_chars: int) -> list[str]:
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if current and size + len(line) > max_chars:
            parts.append("".join(current))
            current = []
            size = 0
        if len(line) > max_chars:
            if current:
                parts.append("".join(current))
                current = []
                size = 0
            for start in range(0, len(line), max_chars):
                parts.append(line[start : start + max_chars])
            continue
        current.append(line)
        size += len(line)
    if current:
        parts.append("".join(current))
    return parts


def _copy_section(
    section: DocumentSection,
    native_text: str,
    ocr_text: str,
    images: tuple[ImageReference, ...],
) -> DocumentSection:
    blocks: list[TextBlock] = []
    if native_text:
        blocks.append(
            TextBlock(
                block_id=f"{section.reference}/text:1",
                text=native_text,
                method=ExtractionMethod.NATIVE_TEXT,
                reference=section.reference,
                page_number=section.page_number,
            )
        )
    if ocr_text:
        blocks.append(
            TextBlock(
                block_id=f"{section.reference}/ocr:1",
                text=ocr_text,
                method=ExtractionMethod.OCR,
                reference=section.reference,
                page_number=section.page_number,
            )
        )
    methods: list[ExtractionMethod] = []
    if native_text:
        methods.append(ExtractionMethod.NATIVE_TEXT)
    if ocr_text:
        methods.append(ExtractionMethod.OCR)
    if images:
        methods.append(ExtractionMethod.IMAGE)
    if not methods:
        methods.append(ExtractionMethod.NONE)
    return DocumentSection(
        reference=section.reference,
        page_number=section.page_number,
        native_text=native_text,
        ocr_text=ocr_text,
        blocks=tuple(blocks),
        images=images,
        methods=tuple(methods),
    )


def _render_section(section: DocumentSection) -> str:
    page = "none" if section.page_number is None else str(section.page_number)
    lines = [f"[ref={section.reference} page={page}]"]
    if section.native_text:
        lines.append("[method=native_text]")
        lines.append(section.native_text)
    if section.ocr_text:
        lines.append("[method=ocr]")
        lines.append(section.ocr_text)
    for image in section.images:
        lines.append(f"[ref={image.image_id} method=image media={image.media_type}]")
    if len(lines) == 1:
        lines.append("[method=none]")
    return "\n".join(lines)


def _request(
    filename: str,
    chunk: ReportChunk,
    timeout_seconds: float,
    supports_images: bool,
    hints: dict[str, tuple[str, ...]],
) -> ProviderRequest:
    image_ids = tuple(image.image_id for image in chunk.images)
    attached = tuple(
        ImagePayload(image.image_id, image.media_type, image.data)
        for image in chunk.images
    ) if supports_images else ()
    chunk_hints: list[str] = []
    for section in chunk.sections:
        chunk_hints.extend(hints.get(section.reference, ()))
    return ProviderRequest(
        chunk_id=chunk.chunk_id,
        filename=filename,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(
            filename=filename,
            chunk_id=chunk.chunk_id,
            chunk_text=chunk.text,
            image_ids=image_ids,
            include_image_bytes=supports_images and bool(image_ids),
            nlp_hints=tuple(chunk_hints),
        ),
        response_schema=dict(PROVIDER_RESPONSE_SCHEMA),
        images=attached,
        timeout_seconds=timeout_seconds,
    )


def _nlp_hints(nlp: ReportNlpAdapter, bundle: DocumentBundle) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for section in bundle.sections:
        notes = []
        for text in (section.native_text, section.ocr_text):
            if text.strip():
                notes.extend(nlp.inspect(section.reference, text))
        grouped[section.reference] = [
            f"{note.reference} negation={note.negation} terms={', '.join(note.terminology) or 'none'}"
            for note in notes
        ]
    return {key: tuple(value) for key, value in grouped.items()}


def _merge(items: list[CheckedFinding]) -> list[tuple[CheckedFinding, tuple[tuple[str, int | None, FindingMethod], ...]]]:
    grouped: list[tuple[CheckedFinding, list[tuple[str, int | None, FindingMethod]]]] = []
    positions: dict[tuple[object, ...], int] = {}
    for item in items:
        key = (
            item.kind,
            item.ruled_out_reason,
            " ".join(item.excerpt.casefold().split()),
            item.normalized_defect_type.value,
            item.chainage_km.value,
            item.track_line.value,
        )
        location = (item.reference, item.page_number, item.extraction_method)
        if key not in positions:
            positions[key] = len(grouped)
            grouped.append((item, [location]))
            continue
        index = positions[key]
        current, locations = grouped[index]
        if location not in locations:
            locations.append(location)
        warnings = tuple(dict.fromkeys([*current.validation_warnings, *item.validation_warnings]))
        uncertainty = current.uncertainty if _uncertainty_rank(current.uncertainty) >= _uncertainty_rank(item.uncertainty) else item.uncertainty
        review = _merged_review(current, uncertainty, warnings)
        grouped[index] = (replace(current, uncertainty=uncertainty, validation_warnings=warnings, review_status=review), locations)
    return [(item, tuple(locations)) for item, locations in grouped]


def _merged_review(item: CheckedFinding, uncertainty: Uncertainty, warnings: tuple[str, ...]) -> str:
    blocked = {
        "normalized_defect_type",
        "severity",
        "track_line",
        "chainage_km",
        "days_overdue",
        "has_tsr",
    }
    if (
        item.kind != "defect"
        or item.extraction_method is FindingMethod.VISUAL
        or uncertainty is not Uncertainty.LOW
        or warnings
        or any(name in item.missing_fields for name in blocked)
    ):
        return ReviewStatus.NEEDS_REVIEW.value
    return ReviewStatus.READY.value


def _uncertainty_rank(value: Uncertainty) -> int:
    return {Uncertainty.LOW: 0, Uncertainty.MODERATE: 1, Uncertainty.HIGH: 2}[value]


def _publish(
    filename: str,
    grouped: list[tuple[CheckedFinding, tuple[tuple[str, int | None, FindingMethod], ...]]],
) -> tuple[tuple[DefectCandidate, ...], tuple[RuledOutFinding, ...]]:
    candidates: list[DefectCandidate] = []
    ruled_out: list[RuledOutFinding] = []
    evidence_number = 1
    for item, locations in grouped:
        evidence: list[Evidence] = []
        for reference, page_number, method in locations:
            evidence.append(
                Evidence(
                    evidence_id=f"E{evidence_number}",
                    filename=filename,
                    reference=reference,
                    page_number=page_number,
                    excerpt=item.excerpt,
                    extraction_method=method,
                )
            )
            evidence_number += 1
        if item.kind == "ruled_out":
            ruled_out.append(
                RuledOutFinding(
                    finding_id=f"R{len(ruled_out) + 1}",
                    statement=item.statement,
                    excerpt=item.excerpt,
                    evidence=tuple(evidence),
                    reason=item.ruled_out_reason or "negated_finding",
                    extraction_method=item.extraction_method,
                )
            )
            continue
        candidates.append(
            DefectCandidate(
                candidate_id=f"F{len(candidates) + 1}",
                statement=item.statement,
                excerpt=item.excerpt,
                evidence=tuple(evidence),
                department=item.department,
                reported_defect_type=item.reported_defect_type,
                normalized_defect_type=item.normalized_defect_type,
                reported_severity_text=item.reported_severity_text,
                severity=item.severity,
                reported_track_line=item.reported_track_line,
                track_line=item.track_line,
                chainage_km=item.chainage_km,
                due_date=item.due_date,
                days_overdue=item.days_overdue,
                has_tsr=item.has_tsr,
                tsr_speed_kmh=item.tsr_speed_kmh,
                reported_duration_min=item.reported_duration_min,
                reported_asset_ref=item.reported_asset_ref,
                extraction_method=item.extraction_method,
                missing_fields=item.missing_fields,
                uncertainty=item.uncertainty,
                validation_warnings=item.validation_warnings,
                review_status=ReviewStatus(item.review_status),
            )
        )
    return tuple(candidates), tuple(ruled_out)


_SUMMARY_LIMIT = 1200


def _summary(parts: list[str]) -> str:
    text = " ".join(part.replace("\n", " ").strip() for part in parts if part.strip())
    return _fit_complete_sentences(text)


_FIELD_SEPARATOR = " \u00b7 "
_DEFECT_LABELS: dict[str, str] = {
    "RAIL_FRACTURE": "Rail fracture",
    "WELD_FAILURE": "Weld failure",
    "OHE_SAG": "OHE sag",
    "SIGNAL_INTERLOCK": "Signal interlock",
    "TAMPING_REQUIRED": "Tamping required",
    "DEEP_SCREENING": "Deep screening",
}
_RULED_OUT_LINES: dict[str, tuple[str, str]] = {
    "nil_report": ("Nil defect report", "Ruled out; not a maintenance job"),
    "tsr_lifted": ("Speed restriction removed", "Ruled out; not a new defect"),
}


def _local_text_summary(
    candidates: tuple[DefectCandidate, ...],
    ruled_out: tuple[RuledOutFinding, ...],
) -> str:
    """Deterministic local summary: one compact field line per validated finding.

    Each line is built only from that finding's own checked fields and review
    state. Unknown values stay unknown. Mock and scripted providers do not use
    this, and the source excerpt and its reference are not changed by it.
    """
    lines = [_candidate_line(item) for item in candidates]
    lines.extend(_ruled_out_line(item) for item in ruled_out)
    return _fit_complete_sentences(" ".join(line for line in lines if line))


def _fit_complete_sentences(text: str) -> str:
    if len(text) <= _SUMMARY_LIMIT:
        return text
    window = text[:_SUMMARY_LIMIT]
    boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if boundary >= 0:
        return window[: boundary + 1].rstrip()
    end = _end_of_first_sentence(text)
    if end is not None:
        return text[:end].rstrip()
    return text


def _end_of_first_sentence(text: str) -> int | None:
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "." and index > 0 and text[index - 1].isdigit() and index + 1 < length and text[index + 1].isdigit():
            index += 1
            continue
        if char in ".!?" and (index + 1 == length or text[index + 1].isspace()):
            return index + 1
        index += 1
    return None


def _candidate_line(item: DefectCandidate) -> str:
    """One `label · location · fields · unknowns · review` line for a candidate."""
    uncertain = item.uncertainty is Uncertainty.HIGH
    unknown: list[str] = []
    type_label = _type_label(item.normalized_defect_type.value)
    if type_label is None:
        unknown.append("defect type")
        subject = "Uncertain observation" if uncertain else "Reported finding"
    elif uncertain:
        subject = f"Possible {_lower_first(type_label)}"
    else:
        subject = type_label
    parts: list[str] = [subject]
    location, location_unknown = _location_part(item)
    if location:
        parts.append(location)
    unknown.extend(location_unknown)
    severity = _shown_number(item.severity.value)
    if severity is not None:
        parts.append(f"Severity {severity}")
    else:
        unknown.append("severity")
    overdue = _shown_number(item.days_overdue.value)
    if overdue is not None:
        parts.append(f"{overdue} {'day' if overdue == '1' else 'days'} overdue")
    else:
        due = item.due_date.value
        if isinstance(due, str) and due.strip():
            parts.append(f"Due {due.strip()}")
        unknown.append("days overdue")
    restriction = _tsr_part(item)
    if restriction is not None:
        parts.append(restriction)
    else:
        unknown.append("TSR")
    if unknown:
        parts.append(_unknown_part(unknown))
    if uncertain:
        parts.append("Not a confirmed defect")
    if item.review_status is not ReviewStatus.READY:
        parts.append("Needs review")
    return _FIELD_SEPARATOR.join(parts) + "."


def _ruled_out_line(item: RuledOutFinding) -> str:
    """A ruled-out finding is named as negative and kept out of the job list."""
    fixed = _RULED_OUT_LINES.get(item.reason)
    if fixed is not None:
        return _FIELD_SEPARATOR.join(fixed) + "."
    phrases = excerpt_cues(item.excerpt).defect_phrases
    subject = "Negative observation"
    if phrases:
        subject = f"Negative {phrases[0].replace('_', ' ')} observation"
    return _FIELD_SEPARATOR.join((subject, "Ruled out; not a maintenance job")) + "."


def _type_label(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    known = _DEFECT_LABELS.get(value.strip().upper())
    if known is not None:
        return known
    words = value.strip().replace("_", " ").split()
    return " ".join([words[0].capitalize(), *(word.casefold() for word in words[1:])])


def _lower_first(label: str) -> str:
    first = label.split(" ", 1)[0]
    if len(first) > 1 and first.isupper():
        return label
    return label[0].lower() + label[1:]


def _location_part(item: DefectCandidate) -> tuple[str, list[str]]:
    km = _shown_number(item.chainage_km.value)
    line = item.track_line.value
    line_text = line.strip() if isinstance(line, str) and line.strip() else None
    unknown: list[str] = []
    if km is None:
        unknown.append("chainage")
    if line_text is None:
        unknown.append("line")
    prefix = "Near KM" if " near " in f" {item.excerpt.casefold()} " else "KM"
    if km is not None and line_text is not None:
        return f"{prefix} {km} {line_text}", unknown
    if km is not None:
        return f"{prefix} {km}", unknown
    if line_text is not None:
        return f"Line {line_text}", unknown
    return "", unknown


def _tsr_part(item: DefectCandidate) -> str | None:
    flag = item.has_tsr.value
    if flag is True:
        speed = _shown_number(item.tsr_speed_kmh)
        return f"Active TSR {speed} km/h" if speed is not None else "Active TSR"
    if flag is False:
        return "No TSR"
    return None


def _unknown_part(names: list[str]) -> str:
    if len(names) == 1:
        text = names[0]
    else:
        text = ", ".join(names[:-1]) + " and " + names[-1]
    return text[0].upper() + text[1:] + " unknown"


def _shown_number(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _failed(
    bundle: DocumentBundle,
    report_id: str,
    provider: ReportUnderstandingProvider,
    warnings: list[ReportWarning],
    chunks_processed: int,
    error: str,
    nlp_status: str,
) -> ReportUnderstanding:
    warnings.append(ReportWarning("PROVIDER_FAILED", error, None))
    return ReportUnderstanding(
        report_id=report_id,
        filename=bundle.filename,
        summary="",
        candidates=(),
        ruled_out=(),
        warnings=tuple(warnings),
        status=UnderstandingStatus.FAILED,
        provider_name=getattr(provider, "name", "provider"),
        chunks_processed=chunks_processed,
        error=error,
        nlp_status=nlp_status,
    )


__all__ = [
    "PipelineLimits",
    "ReportChunk",
    "build_report_chunks",
    "simplify_report",
]
