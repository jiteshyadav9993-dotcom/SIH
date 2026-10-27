"""
report_validate.py
====================================================================
Local checks for one provider response.

A value is kept only when the excerpt states it. Illustrative grade and
priority maps in data_ingestion.py are not applied. This module does not
compute Formula 1 or Formula 2. A bare up, down, dn, or mid token is a
track line only when the excerpt uses it as a running line.
====================================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from defects import DefectType
from report_documents import DocumentSection
from report_schema import (
    FindingMethod,
    SourcedValue,
    Uncertainty,
    ValueOrigin,
    normalized_value,
    reported_value,
    unknown_value,
)
from topology import CORRIDOR_END_KM, CORRIDOR_START_KM

_UNCERTAIN_RE = re.compile(
    r"\b(?:cannot|can't|can not)\s+(?:be\s+ruled|rule)\s+out\b"
    r"|\bnot\s+ruled\s+out\b|\bno\s+longer\s+ruled\s+out\b",
    re.IGNORECASE,
)
_HEDGE_RE = re.compile(
    r"\b(?:possible|possibly|suspected|may\s+be|might\s+be|appears?\s+to\s+be"
    r"|not\s+confirmed|has\s+not\s+been\s+determined)\b",
    re.IGNORECASE,
)
# The check was not done, or nobody has decided. This is not evidence that the defect is absent.
_NOT_CHECKED_RE = re.compile(
    r"\bno\s+decision\b"
    r"|\b(?:has|have)\s+not\s+been\s+(?:carried\s+out|assessed|examined|verified|done|checked)\b"
    r"|\bnot\s+yet\s+(?:checked|confirmed|verified|assessed|determined|carried\s+out)\b"
    r"|\bnot\s+assessed\s+yet\b"
    r"|\bcondition\s+unknown\b",
    re.IGNORECASE,
)
_DEFECT_WORD_RE = re.compile(r"\b(?:defect|defects|failure|fracture)\b", re.IGNORECASE)
_NIL_RE = re.compile(r"\bnil\s+defects?\b|\bno\s+defects?\b", re.IGNORECASE)
_TSR_LIFTED_RE = re.compile(
    r"\btsr\b.{0,40}\b(?:removed|withdrawn|lifted)\b|\b(?:removed|withdrawn|lifted)\s+(?:the\s+)?tsr\b",
    re.IGNORECASE,
)
_TSR_ACTIVE_RE = re.compile(
    r"\btsr\s+(?:imposed|in force|applied)\b|\btemporary speed restriction\b|\bwith\s+(?:an?\s+)?tsr\b|\bunder\s+tsr\b"
    r"|\bactive\s+tsr\b\s*[:=]?\s*yes\b",
    re.IGNORECASE,
)
_TSR_INACTIVE_RE = re.compile(
    r"\bno\s+tsr\b|\bwithout\s+(?:an?\s+)?tsr\b|\btsr\s+not\s+in\s+force\b"
    r"|\bactive\s+tsr\b\s*[:=]?\s*no\b|\btsr\s+status\s*[:=]?\s*no\b",
    re.IGNORECASE,
)
_SEVERITY_RE = re.compile(r"\bseverity(?:\s+of|\s+is|\s*[:=])?\s*(\d{1,3})\b", re.IGNORECASE)
_GRADE_RE = re.compile(r"\bgrade\s*[:=]?\s*(\d{1,2})\b", re.IGNORECASE)
_OVERDUE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s+days?\s+overdue\b"
    r"|\boverdue(?:\s+by)?\s+(\d+(?:\.\d+)?)\s+days?\b"
    r"|\boverdue\b[^.]{0,80}?(\d+(?:\.\d+)?)\s+days?\b",
    re.IGNORECASE,
)
_DUE_TOKEN = (
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|\d{1,2}\s+[A-Za-z]+\s+\d{4}"
)
# A date already beside "due" or "due date" stays preferred when a later label repeats that day.
_DUE_DIRECT_RE = re.compile(
    rf"\bdue(?:\s+on|\s+date)?\s*[:=]?\s*({_DUE_TOKEN})\b",
    re.IGNORECASE,
)
_DUE_RE = re.compile(
    rf"\bdue(?:\s+on|\s+date)?(?:\s*/\s*overdue)?\s*[:=]?\s*(?:\r?\n\s*)?({_DUE_TOKEN})\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:minutes|minute|mins|min)\b", re.IGNORECASE)
_SPEED_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*km/h\b", re.IGNORECASE)
# A speed such as 30 km/h is not a chainage. A real "30 km" or "KM 30" still is.
_CHAINAGE_RE = re.compile(
    r"\b(?:km|chainage)\s*[:=]?\s*(\d+(?:\.\d+)?)(?!\s*km/h)|\b(\d+(?:\.\d+)?)\s*km\b(?!/h)",
    re.IGNORECASE,
)
_ASSET_RE = re.compile(
    r"\b(?:asset|defect)\s*(?:id|ref|reference)\s*[:=]?\s*([A-Za-z0-9_-]+)\b"
    r"|\b(?:finding|observation)\s+([A-Za-z][A-Za-z0-9]*-\d+)\b",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_AFTER_NEGATION_RE = re.compile(
    r"\b(?:not\s+observed|was\s+not\s+observed|were\s+not\s+observed|is\s+not\s+present|are\s+not\s+present"
    r"|was\s+absent|were\s+absent|not\s+found|was\s+not\s+found|were\s+not\s+found"
    r"|not\s+detected|was\s+not\s+detected|not\s+recorded|was\s+not\s+recorded"
    r"|not\s+seen|was\s+not\s+seen)\b",
    re.IGNORECASE,
)
_NO_EVIDENCE_RE = re.compile(r"\bno\s+evidence\s+of\b", re.IGNORECASE)
_DISTANT_NEGATION_RE = re.compile(
    r"\b(?:do|does|did)\s+not\b[^.]{0,80}?\b(?:establish|confirm|prove|indicate)\b",
    re.IGNORECASE,
)
_WINDOW_NEGATION = frozenset({"not", "without", "nil", "never"})
# Words that may sit between "no" and the defect name. "No decision" does not.
_NO_GAP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "any",
        "outstanding",
        "further",
        "visible",
        "apparent",
        "other",
        "such",
        "confirmed",
        "active",
        "obvious",
        "reported",
    }
)
_DEPARTMENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("TMS", re.compile(r"\btms\b|track management", re.IGNORECASE)),
    ("SMMS", re.compile(r"\bsmms\b|signalling|signaling", re.IGNORECASE)),
    ("TDMS", re.compile(r"\btdms\b|traction distribution", re.IGNORECASE)),
)
def _running_line(tokens: str, blocked_follow: str) -> re.Pattern[str]:
    """Match a bare line code only in a running-line phrase or beside a chainage."""
    blocked = rf"(?!\s+(?:{blocked_follow})\b)"
    return re.compile(
        rf"(?:"
        rf"\b(?:{tokens})\s+lines?\b"
        rf"|\bon\s+(?:the\s+)?(?:{tokens})\b{blocked}"
        rf"|\b(?:km|chainage)\s*[:=]?\s*\d+(?:\.\d+)?\s*[, ]\s*(?:{tokens})\b{blocked}"
        rf")",
        re.IGNORECASE,
    )


_LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DN_FAST", re.compile(r"\bdn[\s_-]*fast\b|\bdown[\s_-]*fast\b", re.IGNORECASE)),
    ("UP_FAST", re.compile(r"\bup[\s_-]*fast\b", re.IGNORECASE)),
    ("DN_SLOW", re.compile(r"\bdn[\s_-]*slow\b|\bdown[\s_-]*slow\b", re.IGNORECASE)),
    ("UP_SLOW", re.compile(r"\bup[\s_-]*slow\b", re.IGNORECASE)),
    ("MID", _running_line("mid", "span|point|way")),
    ("DN", _running_line("dn|down", "to")),
    ("UP", _running_line("up", "to|loop|until")),
)
_BARE_LINE_TOKEN: dict[str, re.Pattern[str]] = {
    "MID": re.compile(r"\bmid\b", re.IGNORECASE),
    "DN": re.compile(r"\b(?:dn|down)\b", re.IGNORECASE),
    "UP": re.compile(r"\bup\b", re.IGNORECASE),
}
_TYPE_PHRASES: tuple[tuple[DefectType, tuple[str, ...]], ...] = (
    (
        DefectType.RAIL_FRACTURE,
        (
            "rail fracture",
            "rail_fracture",
            "rail-fracture",
            "broken rail",
            "rail crack",
            "rail is fractured",
            "rail is broken",
            "rail breakage",
        ),
    ),
    (
        DefectType.WELD_FAILURE,
        (
            "weld failure",
            "weld_failure",
            "weld-failure",
            "failed weld",
            "weld defect",
            "defective weld",
            "weld has failed",
            "weld failed",
            "fractured weld",
            "weld fracture",
        ),
    ),
    (
        DefectType.OHE_SAG,
        (
            "ohe sag",
            "ohe_sag",
            "ohe-sag",
            "conductor sag",
            "contact wire sag",
            "contact-wire sag",
            "catenary sag",
            "sagging catenary",
            "sagging contact wire",
            "overhead line sag",
            "ohe sagging",
        ),
    ),
    (
        DefectType.SIGNAL_INTERLOCK,
        (
            "signal interlock",
            "signal_interlock",
            "signal-interlock",
            "signal interlocking",
            "interlocking failure",
            "interlocking fault",
            "interlocking defect",
            "interlocking has failed",
            "interlocking failed",
            "point machine",
            "point-machine failure",
            "track circuit",
            "track-circuit failure",
        ),
    ),
    (
        DefectType.DEEP_SCREENING,
        ("deep screening", "deep_screening", "deep-screening", "ballast screening", "deep screen"),
    ),
    (
        DefectType.TAMPING_REQUIRED,
        ("tamping required", "tamping_required", "tamping-required", "tamping", "out of tamp", "out-of-tamp"),
    ),
)
_PHRASE_CATALOG: tuple[tuple[str, DefectType], ...] = tuple(
    sorted(
        ((phrase, defect_type) for defect_type, group in _TYPE_PHRASES for phrase in group),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    """Match a catalog phrase. A lone not/never between its words is still the phrase."""
    parts = [re.escape(part) for part in phrase.split()]
    if len(parts) == 1:
        body = parts[0]
    else:
        body = r"[ \t]+(?:(?:not|never)[ \t]+)?".join(parts)
    return re.compile(r"(?<!\w)" + body + r"(?!\w)")


_PHRASE_PATTERNS: tuple[tuple[re.Pattern[str], str, DefectType], ...] = tuple(
    (_phrase_regex(phrase), phrase, defect_type) for phrase, defect_type in _PHRASE_CATALOG
)
_REQUIRED_KEYS = frozenset({"kind", "statement", "excerpt", "reference", "extraction_method", "uncertainty"})
_FINDING_KEYS = _REQUIRED_KEYS | {
    "department",
    "reported_defect_type",
    "normalized_defect_type",
    "reported_severity_text",
    "severity",
    "reported_track_line",
    "track_line",
    "chainage_km",
    "due_date",
    "days_overdue",
    "has_tsr",
    "tsr_speed_kmh",
    "reported_duration_min",
    "reported_asset_ref",
    "ruled_out_reason",
}
_READY_FIELDS = (
    "normalized_defect_type",
    "severity",
    "track_line",
    "chainage_km",
    "days_overdue",
    "has_tsr",
)


class InvalidProviderOutput(Exception):
    """The provider text is not the required JSON object."""


@dataclass(frozen=True, slots=True)
class CheckedFinding:
    """One model finding after the excerpt has been checked."""

    kind: str
    statement: str
    excerpt: str
    reference: str
    page_number: int | None
    department: str | None
    reported_defect_type: str | None
    normalized_defect_type: SourcedValue
    reported_severity_text: str | None
    severity: SourcedValue
    reported_track_line: str | None
    track_line: SourcedValue
    chainage_km: SourcedValue
    due_date: SourcedValue
    days_overdue: SourcedValue
    has_tsr: SourcedValue
    tsr_speed_kmh: float | None
    reported_duration_min: SourcedValue
    reported_asset_ref: str | None
    extraction_method: FindingMethod
    missing_fields: tuple[str, ...]
    uncertainty: Uncertainty
    validation_warnings: tuple[str, ...]
    review_status: str
    ruled_out_reason: str | None


def parse_provider_payload(raw_text: str) -> dict[str, object]:
    """Accept only a JSON object with a string summary and a list of findings."""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidProviderOutput("Provider output is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidProviderOutput("Provider output must be a JSON object.")
    if not isinstance(payload.get("summary"), str):
        raise InvalidProviderOutput("Provider output is missing a string summary.")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise InvalidProviderOutput("Provider output is missing a findings list.")
    for index, finding in enumerate(findings, start=1):
        _require_finding_shape(finding, index)
    return payload


def check_findings(
    payload: dict[str, object],
    sections: tuple[DocumentSection, ...],
) -> tuple[tuple[CheckedFinding, ...], tuple[str, ...]]:
    """Keep findings the chunk supports. Rejected rows become warning messages."""
    accepted: list[CheckedFinding] = []
    warnings: list[str] = []
    findings = payload["findings"]
    if not isinstance(findings, list):
        raise InvalidProviderOutput("Provider output is missing a findings list.")
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            raise InvalidProviderOutput(f"Finding {index} is not an object.")
        checked, warning = _check_one(finding, sections)
        if warning is not None:
            warnings.append(warning)
            continue
        if checked is not None:
            accepted.append(checked)
    return tuple(accepted), tuple(warnings)


def _require_finding_shape(finding: object, index: int) -> None:
    if not isinstance(finding, dict):
        raise InvalidProviderOutput(f"Finding {index} is not an object.")
    unknown = set(finding) - _FINDING_KEYS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise InvalidProviderOutput(f"Finding {index} has unsupported fields: {names}.")
    missing = [key for key in _REQUIRED_KEYS if key not in finding]
    if missing:
        raise InvalidProviderOutput(f"Finding {index} is missing {', '.join(missing)}.")
    if finding["kind"] not in {"defect", "ruled_out"}:
        raise InvalidProviderOutput(f"Finding {index} has an invalid kind.")
    if finding["extraction_method"] not in {"native_text", "ocr", "visual"}:
        raise InvalidProviderOutput(f"Finding {index} has an invalid extraction method.")
    if finding["uncertainty"] not in {"low", "moderate", "high"}:
        raise InvalidProviderOutput(f"Finding {index} has an invalid uncertainty.")
    for key in ("statement", "excerpt", "reference"):
        if not isinstance(finding[key], str) or not str(finding[key]).strip():
            raise InvalidProviderOutput(f"Finding {index} has an empty {key}.")


def _check_one(
    finding: dict[str, object],
    sections: tuple[DocumentSection, ...],
) -> tuple[CheckedFinding | None, str | None]:
    reference = str(finding["reference"])
    excerpt = str(finding["excerpt"])
    section = _section_for(reference, sections)
    image_section = _section_for_image(reference, sections)
    method_name = str(finding["extraction_method"])
    if method_name == "visual":
        if image_section is None:
            return None, f"Rejected a visual finding for {reference}: that image is not in this chunk."
        return _visual_finding(finding, excerpt, reference, image_section), None
    if section is None:
        return None, f"Rejected a finding for {reference}: that reference is not in this chunk."
    if excerpt not in section.native_text and excerpt not in section.ocr_text:
        return None, f"Rejected a finding for {reference}: the excerpt is not in the extracted text."
    method = _text_method(section, excerpt, method_name)
    polarity = classify_polarity(excerpt)
    statement = _statement(section.native_text if method is FindingMethod.NATIVE_TEXT else section.ocr_text, excerpt)
    if polarity in {"negated", "nil", "tsr_lifted"}:
        reason = {"negated": "negated_finding", "nil": "nil_report", "tsr_lifted": "tsr_lifted"}[polarity]
        return _ruled_out(statement, excerpt, reference, section.page_number, method, reason), None
    if str(finding["kind"]) == "ruled_out" and polarity == "positive":
        warning_prefix = "Provider marked the excerpt as ruled out, but the excerpt does not negate a defect."
    else:
        warning_prefix = ""
    built = _defect_from_excerpt(
        finding,
        statement,
        excerpt,
        reference,
        section.page_number,
        method,
        polarity,
        warning_prefix,
    )
    return built, None


@dataclass(frozen=True, slots=True)
class ExcerptCues:
    """Phrase and polarity signals for one excerpt. This is not a defect record."""

    polarity: str
    defect_phrases: tuple[str, ...]
    nil: bool
    tsr_active: bool
    tsr_inactive: bool
    tsr_lifted: bool


def positive_defect_types(excerpt: str) -> tuple[str, ...]:
    """Defect categories named as present. Negated phrases are not included."""
    found: list[str] = []
    for start, end, _phrase, defect_type in _scan_phrases(excerpt.casefold()):
        if _negated_span(excerpt.casefold(), start, end):
            continue
        if defect_type.value not in found:
            found.append(defect_type.value)
    return tuple(found)


def excerpt_cues(excerpt: str) -> ExcerptCues:
    """Reuse the validator's phrase, negation, nil, and TSR checks."""
    lowered = excerpt.casefold()
    phrases = tuple(phrase for _start, _end, phrase, _defect_type in _scan_phrases(lowered))
    return ExcerptCues(
        polarity=classify_polarity(excerpt),
        defect_phrases=phrases,
        nil=_nil_statement(lowered),
        tsr_active=_TSR_ACTIVE_RE.search(excerpt) is not None,
        tsr_inactive=_TSR_INACTIVE_RE.search(excerpt) is not None,
        tsr_lifted=_TSR_LIFTED_RE.search(excerpt) is not None,
    )


def classify_polarity(excerpt: str) -> str:
    """Label the excerpt. This is a local guard, not the professor NLP score."""
    lowered = excerpt.casefold()
    if _UNCERTAIN_RE.search(lowered):
        return "uncertain"
    labels = _phrase_labels(lowered)
    positives = [label for label in labels if label == "positive"]
    negatives = [label for label in labels if label == "negated"]
    if _NOT_CHECKED_RE.search(lowered) and not negatives and (positives or _DEFECT_WORD_RE.search(lowered)):
        return "uncertain"
    if positives and negatives:
        return "uncertain"
    if _TSR_LIFTED_RE.search(lowered) and not positives:
        return "tsr_lifted"
    if _TSR_LIFTED_RE.search(lowered) and positives:
        return "tsr_not_active"
    if not positives and _nil_statement(lowered):
        return "nil"
    if negatives and not positives:
        return "negated"
    if _HEDGE_RE.search(lowered) and (positives or _DEFECT_WORD_RE.search(lowered)):
        return "uncertain"
    return "positive"


def _nil_statement(lowered: str) -> bool:
    """True for a nil-defect report, or a catalogue phrase closed by nil."""
    if _NIL_RE.search(lowered) is not None:
        return True
    for _start, end, _phrase, _defect_type in _scan_phrases(lowered):
        tail = re.findall(r"[a-z']+", lowered[end : end + 24])
        if tail and tail[0] == "nil":
            return True
    return False


def _scan_phrases(lowered: str) -> list[tuple[int, int, str, DefectType]]:
    found: list[tuple[int, int, str, DefectType]] = []
    occupied: list[tuple[int, int]] = []
    for pattern, phrase, defect_type in _PHRASE_PATTERNS:
        for match in pattern.finditer(lowered):
            start, end = match.span()
            if any(start < right and end > left for left, right in occupied):
                continue
            occupied.append((start, end))
            found.append((start, end, phrase, defect_type))
    return found


def _phrase_labels(lowered: str) -> list[str]:
    return [
        "negated" if _negated_span(lowered, start, end) else "positive"
        for start, end, _phrase, _defect_type in _scan_phrases(lowered)
    ]


def _negated_span(lowered: str, start: int, end: int) -> bool:
    if re.search(r"\b(?:not|never)\b", lowered[start:end]):
        return True
    near = lowered[max(0, start - 48) : start]
    words = re.findall(r"[a-z']+", near)[-5:]
    if any(word in _WINDOW_NEGATION for word in words) or _no_denies_phrase(words):
        return True
    if any(word in {"neither", "nor"} for word in words[-3:]):
        return True
    if re.search(r"\bnothing\s+(?:outstanding|pending)\b", near):
        return True
    wider = lowered[max(0, start - 160) : start]
    if _NO_EVIDENCE_RE.search(wider) is not None or _DISTANT_NEGATION_RE.search(wider) is not None:
        return True
    tail_words = re.findall(r"[a-z']+", lowered[end : end + 64])[:5]
    tail = " ".join(tail_words)
    if tail_words and tail_words[0] == "nil":
        return True
    if re.search(r"\bnot\s+warranted\b|\bno\s+defect\s+outstanding\b|\bnothing\s+(?:outstanding|pending)\b", tail):
        return True
    return _AFTER_NEGATION_RE.search(tail) is not None


def _no_denies_phrase(words: list[str]) -> bool:
    """True when `no` directly denies the phrase, not a different object such as a decision."""
    if "no" not in words:
        return False
    index = len(words) - 1 - words[::-1].index("no")
    return all(word in _NO_GAP_WORDS for word in words[index + 1 :])


def _defect_from_excerpt(
    finding: dict[str, object],
    statement: str,
    excerpt: str,
    reference: str,
    page_number: int | None,
    method: FindingMethod,
    polarity: str,
    warning_prefix: str,
) -> CheckedFinding:
    warnings: list[str] = []
    if warning_prefix:
        warnings.append(warning_prefix)
    defect_type, reported_type, type_warning = _type_from_excerpt(excerpt)
    if type_warning:
        warnings.append(type_warning)
    severity, severity_text, severity_warnings = _severity_from_excerpt(excerpt)
    warnings.extend(severity_warnings)
    _warn_if_model_disagrees(finding, "severity", severity.value, warnings, "severity")
    line_value, line_text, _line_origin = _line_from_excerpt(excerpt)
    chainage, chainage_warnings = _chainage_from_excerpt(excerpt, _optional_float(finding.get("chainage_km")))
    warnings.extend(chainage_warnings)
    due = _due_from_excerpt(excerpt)
    overdue = _overdue_from_excerpt(excerpt)
    tsr_conflict = (
        polarity != "tsr_not_active"
        and _TSR_ACTIVE_RE.search(excerpt) is not None
        and _TSR_INACTIVE_RE.search(excerpt) is not None
    )
    if tsr_conflict:
        warnings.append("The excerpt states conflicting TSR conditions, so TSR status stays unknown.")
    has_tsr = unknown_value() if tsr_conflict else _tsr_flag(excerpt, polarity)
    speed = _speed_from_excerpt(excerpt) if has_tsr.value is True else None
    duration = _duration_from_excerpt(excerpt)
    _warn_if_model_disagrees(finding, "days_overdue", overdue.value, warnings, "overdue")
    _warn_if_model_disagrees(finding, "has_tsr", has_tsr.value, warnings, "TSR")
    _warn_if_model_disagrees(finding, "reported_duration_min", duration.value, warnings, "duration")
    asset = _asset_from_excerpt(excerpt)
    department = _department_from_excerpt(excerpt)
    if polarity == "uncertain":
        warnings.append("The excerpt does not clearly confirm or rule out the defect.")
    uncertainty = _uncertainty(str(finding["uncertainty"]), polarity, method, warnings)
    missing = _missing_names(
        department,
        defect_type,
        severity,
        line_value,
        chainage,
        due,
        overdue,
        has_tsr,
        duration,
    )
    review = _review_status(uncertainty, method, warnings, missing)
    return CheckedFinding(
        kind="defect",
        statement=statement,
        excerpt=excerpt,
        reference=reference,
        page_number=page_number,
        department=department,
        reported_defect_type=reported_type,
        normalized_defect_type=defect_type,
        reported_severity_text=severity_text,
        severity=severity,
        reported_track_line=line_text,
        track_line=line_value,
        chainage_km=chainage,
        due_date=due,
        days_overdue=overdue,
        has_tsr=has_tsr,
        tsr_speed_kmh=speed,
        reported_duration_min=duration,
        reported_asset_ref=asset,
        extraction_method=method,
        missing_fields=missing,
        uncertainty=uncertainty,
        validation_warnings=tuple(warnings),
        review_status=review,
        ruled_out_reason=None,
    )


def _visual_finding(
    finding: dict[str, object],
    excerpt: str,
    reference: str,
    section: DocumentSection,
) -> CheckedFinding:
    warning = "A photograph is an observation only. It does not prove that a railway asset is safe or defective."
    uncertainty = Uncertainty.HIGH
    missing = _missing_names(
        None,
        unknown_value(),
        unknown_value(),
        unknown_value(),
        unknown_value(),
        unknown_value(),
        unknown_value(),
        unknown_value(),
        unknown_value(),
    )
    return CheckedFinding(
        kind="defect",
        statement=excerpt,
        excerpt=excerpt,
        reference=reference,
        page_number=section.page_number,
        department=None,
        reported_defect_type=None,
        normalized_defect_type=unknown_value(),
        reported_severity_text=None,
        severity=unknown_value(),
        reported_track_line=None,
        track_line=unknown_value(),
        chainage_km=unknown_value(),
        due_date=unknown_value(),
        days_overdue=unknown_value(),
        has_tsr=unknown_value(),
        tsr_speed_kmh=None,
        reported_duration_min=unknown_value(),
        reported_asset_ref=None,
        extraction_method=FindingMethod.VISUAL,
        missing_fields=missing,
        uncertainty=uncertainty,
        validation_warnings=(warning,),
        review_status="NEEDS_REVIEW",
        ruled_out_reason=None,
    )


def _ruled_out(
    statement: str,
    excerpt: str,
    reference: str,
    page_number: int | None,
    method: FindingMethod,
    reason: str,
) -> CheckedFinding:
    return CheckedFinding(
        kind="ruled_out",
        statement=statement,
        excerpt=excerpt,
        reference=reference,
        page_number=page_number,
        department=None,
        reported_defect_type=None,
        normalized_defect_type=unknown_value(),
        reported_severity_text=None,
        severity=unknown_value(),
        reported_track_line=None,
        track_line=unknown_value(),
        chainage_km=unknown_value(),
        due_date=unknown_value(),
        days_overdue=unknown_value(),
        has_tsr=unknown_value(),
        tsr_speed_kmh=None,
        reported_duration_min=unknown_value(),
        reported_asset_ref=None,
        extraction_method=method,
        missing_fields=(),
        uncertainty=Uncertainty.LOW,
        validation_warnings=(),
        review_status="NEEDS_REVIEW",
        ruled_out_reason=reason,
    )


def _type_from_excerpt(excerpt: str) -> tuple[SourcedValue, str | None, str | None]:
    lowered = excerpt.casefold()
    found: list[tuple[DefectType, str]] = []
    for start, end, _phrase, defect_type in _scan_phrases(lowered):
        if _negated_span(lowered, start, end):
            continue
        found.append((defect_type, excerpt[start:end]))
    distinct = {item[0] for item in found}
    if len(distinct) == 1:
        defect_type, phrase = found[0]
        return normalized_value(defect_type.value), phrase, None
    if len(distinct) > 1:
        return unknown_value(), None, "The excerpt names more than one defect type."
    return unknown_value(), None, None


def _severity_from_excerpt(excerpt: str) -> tuple[SourcedValue, str | None, tuple[str, ...]]:
    warnings: list[str] = []
    grade = _GRADE_RE.search(excerpt)
    if grade is not None:
        warnings.append(
            "A source grade was stated. The illustrative TMS grade map was not applied, so numeric severity stays unknown."
        )
    match = _SEVERITY_RE.search(excerpt)
    if match is None:
        text = grade.group(0) if grade is not None else None
        return unknown_value(), text, tuple(warnings)
    number = int(match.group(1))
    if number < 1 or number > 100:
        warnings.append("The excerpt states a severity outside 1 to 100, so numeric severity stays unknown.")
        return unknown_value(), match.group(0), tuple(warnings)
    return reported_value(number), match.group(0), tuple(warnings)


def _line_from_excerpt(excerpt: str) -> tuple[SourcedValue, str | None, ValueOrigin]:
    for code, pattern in _LINE_RULES:
        match = pattern.search(excerpt)
        if match is None:
            continue
        phrase = match.group(0)
        token_pattern = _BARE_LINE_TOKEN.get(code)
        if token_pattern is not None:
            token = token_pattern.search(phrase)
            if token is None:
                continue
            phrase = token.group(0)
        origin = ValueOrigin.REPORTED if phrase.replace(" ", "_").upper() == code else ValueOrigin.NORMALIZED
        value = SourcedValue(value=code, origin=origin)
        return value, phrase, origin
    return unknown_value(), None, ValueOrigin.UNKNOWN


def _chainage_from_excerpt(excerpt: str, model_value: float | None) -> tuple[SourcedValue, tuple[str, ...]]:
    numbers = [float(group) for match in _CHAINAGE_RE.finditer(excerpt) for group in match.groups() if group is not None]
    unique: list[float] = []
    for number in numbers:
        if not any(abs(number - earlier) < 1e-6 for earlier in unique):
            unique.append(number)
    warnings: list[str] = []
    if not unique:
        if model_value is not None:
            warnings.append("Ignored a chainage that the excerpt does not state.")
        return unknown_value(), tuple(warnings)
    if len(unique) > 1:
        if model_value is not None and any(abs(model_value - number) < 1e-6 for number in unique):
            chosen = model_value
        else:
            warnings.append("The excerpt states more than one chainage, so chainage stays unknown.")
            return unknown_value(), tuple(warnings)
    else:
        chosen = unique[0]
        if model_value is not None and abs(model_value - chosen) > 1e-6:
            warnings.append("Ignored a chainage that the excerpt does not state.")
    if chosen < CORRIDOR_START_KM or chosen > CORRIDOR_END_KM:
        warnings.append("The stated chainage is outside the CSMT to Pune corridor.")
    return reported_value(chosen), tuple(warnings)


def _overdue_from_excerpt(excerpt: str) -> SourcedValue:
    match = _OVERDUE_RE.search(excerpt)
    if match is None:
        return unknown_value()
    raw = match.group(1) or match.group(2) or match.group(3)
    return reported_value(float(raw))


def _duration_from_excerpt(excerpt: str) -> SourcedValue:
    matches = [float(item) for item in _DURATION_RE.findall(excerpt)]
    # findall with one group returns strings; with the pattern above it returns the group.
    if len(matches) != 1:
        return unknown_value()
    return reported_value(matches[0])


def _speed_from_excerpt(excerpt: str) -> float | None:
    found: list[float] = []
    for token in _SPEED_RE.findall(excerpt):
        number = float(token)
        if not any(abs(number - earlier) < 1e-6 for earlier in found):
            found.append(number)
    if len(found) != 1:
        return None
    return found[0]


def _asset_from_excerpt(excerpt: str) -> str | None:
    match = _ASSET_RE.search(excerpt)
    if match is None:
        return None
    return next((group for group in match.groups() if group), None)


def _department_from_excerpt(excerpt: str) -> str | None:
    hits = [name for name, pattern in _DEPARTMENT_RULES if pattern.search(excerpt)]
    if len(hits) == 1:
        return hits[0]
    return None


def _tsr_flag(excerpt: str, polarity: str) -> SourcedValue:
    inactive = _TSR_INACTIVE_RE.search(excerpt) is not None
    active = _TSR_ACTIVE_RE.search(excerpt) is not None
    if polarity != "tsr_not_active" and inactive and active:
        return unknown_value()
    if polarity == "tsr_not_active" or inactive:
        return reported_value(False)
    if _TSR_LIFTED_RE.search(excerpt):
        return unknown_value()
    if active:
        return reported_value(True)
    return unknown_value()


def _uncertainty(model_value: str, polarity: str, method: FindingMethod, warnings: list[str]) -> Uncertainty:
    parsed = Uncertainty(model_value)
    floor = Uncertainty.LOW
    if warnings:
        floor = Uncertainty.MODERATE
    if polarity == "uncertain" or method is FindingMethod.VISUAL:
        floor = Uncertainty.HIGH
    if _rank(parsed) >= _rank(floor):
        return parsed
    return floor


def _rank(value: Uncertainty) -> int:
    return {Uncertainty.LOW: 0, Uncertainty.MODERATE: 1, Uncertainty.HIGH: 2}[value]


def _review_status(uncertainty: Uncertainty, method: FindingMethod, warnings: list[str], missing: tuple[str, ...]) -> str:
    ready_missing = [name for name in _READY_FIELDS if name in missing]
    if method is FindingMethod.VISUAL or uncertainty is not Uncertainty.LOW or warnings or ready_missing:
        return "NEEDS_REVIEW"
    return "READY"


def _missing_names(
    department: str | None,
    defect_type: SourcedValue,
    severity: SourcedValue,
    track_line: SourcedValue,
    chainage: SourcedValue,
    due: SourcedValue,
    overdue: SourcedValue,
    has_tsr: SourcedValue,
    duration: SourcedValue,
) -> tuple[str, ...]:
    values: dict[str, object] = {
        "department": department,
        "normalized_defect_type": defect_type.value,
        "severity": severity.value,
        "track_line": track_line.value,
        "chainage_km": chainage.value,
        "due_date": due.value,
        "days_overdue": overdue.value,
        "has_tsr": has_tsr.value,
        "reported_duration_min": duration.value,
    }
    return tuple(name for name, value in values.items() if value is None)


def _text_method(section: DocumentSection, excerpt: str, requested: str) -> FindingMethod:
    in_native = bool(section.native_text) and excerpt in section.native_text
    in_ocr = bool(section.ocr_text) and excerpt in section.ocr_text
    if in_ocr and not in_native:
        return FindingMethod.OCR
    if requested == "ocr" and in_ocr:
        return FindingMethod.OCR
    return FindingMethod.NATIVE_TEXT


def _statement(text: str, excerpt: str) -> str:
    parts = re.split(r"\n+|(?<=[.!?])\s+", text)
    for part in parts:
        if excerpt in part and part.strip():
            return part.strip()
    return excerpt


def _section_for(reference: str, sections: tuple[DocumentSection, ...]) -> DocumentSection | None:
    for section in sections:
        if section.reference == reference:
            return section
    return None


def _section_for_image(reference: str, sections: tuple[DocumentSection, ...]) -> DocumentSection | None:
    for section in sections:
        if any(image.image_id == reference or image.reference == reference for image in section.images):
            return section
    return None


def _collapse_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _calendar_day(token: str) -> tuple[int, int, int] | None:
    month_name = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", token)
    if month_name:
        month = _MONTHS.get(month_name.group(2).casefold())
        if month is None:
            return None
        return _real_day(int(month_name.group(3)), month, int(month_name.group(1)))
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", token)
    if iso:
        return _real_day(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    numeric = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", token)
    if numeric is None:
        return None
    first = int(numeric.group(1))
    second = int(numeric.group(2))
    year = int(numeric.group(3))
    if year < 100:
        year += 2000
    if first > 12 and second > 12:
        return None
    if 1 <= first <= 12 and 1 <= second <= 12:
        return None
    if first > 12:
        day, month = first, second
    else:
        day, month = second, first
    return _real_day(year, month, day)


def _real_day(year: int, month: int, day: int) -> tuple[int, int, int] | None:
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    return value.year, value.month, value.day


def _due_tokens(pattern: re.Pattern[str], excerpt: str) -> list[str] | None:
    """Return captured dates, or None when any captured token is not a real unique day."""
    raw = [_collapse_space(str(token)) for token in pattern.findall(excerpt)]
    raw = [token for token in raw if token]
    if any(_calendar_day(token) is None for token in raw):
        return None
    unique: list[str] = []
    for token in raw:
        if token not in unique:
            unique.append(token)
    return unique


def _due_from_excerpt(excerpt: str) -> SourcedValue:
    labelled = _due_tokens(_DUE_RE, excerpt)
    direct = _due_tokens(_DUE_DIRECT_RE, excerpt)
    if labelled is None or direct is None:
        return unknown_value()
    if not labelled:
        return unknown_value()
    days = [_calendar_day(token) for token in labelled]
    if len(set(days)) != 1:
        return unknown_value()
    if direct:
        return reported_value(direct[0])
    return reported_value(labelled[0])


def _single_match(pattern: re.Pattern[str], excerpt: str) -> SourcedValue:
    found = pattern.findall(excerpt)
    if len(found) != 1:
        return unknown_value()
    token = found[0]
    if isinstance(token, tuple):
        token = next(part for part in token if part)
    return reported_value(str(token))


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _warn_if_model_disagrees(
    finding: dict[str, object],
    key: str,
    kept: object,
    warnings: list[str],
    label: str,
) -> None:
    supplied = finding.get(key)
    if supplied is None:
        return
    if isinstance(supplied, bool) or isinstance(kept, bool):
        if supplied != kept:
            warnings.append(f"Ignored a {label} value that the excerpt does not state.")
        return
    if kept is None:
        warnings.append(f"Ignored a {label} value that the excerpt does not state.")
        return
    if isinstance(supplied, (int, float)) and isinstance(kept, (int, float)):
        if abs(float(supplied) - float(kept)) > 1e-6:
            warnings.append(f"Ignored a {label} value that the excerpt does not state.")
        return
    if supplied != kept:
        warnings.append(f"Ignored a {label} value that the excerpt does not state.")


__all__ = [
    "CheckedFinding",
    "InvalidProviderOutput",
    "ExcerptCues",
    "check_findings",
    "classify_polarity",
    "excerpt_cues",
    "parse_provider_payload",
    "positive_defect_types",
]
