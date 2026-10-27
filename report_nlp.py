"""
report_nlp.py
====================================================================
Local text proposer, plus the pending professor-NLP hook.

The professor note was read from BugHunters_nirmolsir_AI.docx outside this
repository. It is a Word document with embedded Python, not an importable
module. PendingProfessorNlpAdapter does not copy that implementation and
stays unavailable.

That note tokenises with spaCy, scores phrases from 1 to 10, and clusters a
supplied chainage within 3 km. This proposer does none of those things. It
splits extracted text into exact sentences and emits schema findings only
when the existing phrase, negation, nil, and TSR checks recognise the
sentence. check_findings still decides which values the excerpt supports.

Limits: the phrase list is the project's six defect types; hedges are
lexical, not a parse; an isolated TSR sentence is not a defect; a sentence
with no defect cue is ignored. A line break stays inside the excerpt when
the line has no sentence terminator, unless the next line opens another
defect phrase. A heading that is only a defect name is not a finding.
An instruction, a work plan, a completed repair, or a machine or cycle
note in the same clause is not emitted as a new defect. A separate
clause in that sentence can still be a finding. Not-yet-checked wording
stays uncertain.
Severity words such as "critical" are not scores. Formula 1 is not
calculated here.

When text contains FINDING or OBSERVATION headings with an identifier such
as D-01, each heading and the text up to the next heading, attachment, or
field register stays one excerpt. An overview, an instruction, or a later
register of those same identifiers is not mined into extra defects.
Sections without those headings keep the sentence fallback.
====================================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from report_provider import ProviderRequest, ProviderResponse
from report_validate import ExcerptCues, _scan_phrases, excerpt_cues, positive_defect_types


class NlpIntegrationPending(Exception):
    """The professor NLP module is not available to import."""


@dataclass(frozen=True, slots=True)
class NlpSentenceNote:
    """Hints only. A note is not a severity and not a defect record."""

    reference: str
    sentence: str
    terminology: tuple[str, ...]
    chainage_text: tuple[str, ...]
    negation: str


class ReportNlpAdapter(Protocol):
    """Optional sentence notes for one section of extracted text."""

    def available(self) -> bool:
        """True only when a real NLP module can be called."""

    def inspect(self, reference: str, text: str) -> tuple[NlpSentenceNote, ...]:
        """Return sentence notes for text that belongs to `reference`."""


class PendingProfessorNlpAdapter:
    """Placeholder. The Word note cannot be imported from this repository."""

    status = "pending"

    def available(self) -> bool:
        return False

    def inspect(self, reference: str, text: str) -> tuple[NlpSentenceNote, ...]:
        raise NlpIntegrationPending(
            "Professor NLP integration is pending. "
            "BugHunters_nirmolsir_AI.docx is not an importable Python module, "
            "and this pipeline does not reimplement it."
        )


_FENCE_RE = re.compile(r"<<<REPORT_DATA\n(.*)\nREPORT_DATA>>>", re.DOTALL)
_SECTION_LINE = re.compile(r"^\[ref=(\S+) page=\S+\]$")
_METHOD_LINE = re.compile(r"^\[method=(native_text|ocr|none)\]$")
_UNTYPED_DEFECT_RE = re.compile(r"\b(?:defect|defects|failure|fracture)\b", re.IGNORECASE)
_CLAUSE_MARKS = ("; ", ", but ", " and ", " but ")
_RULED_OUT = {
    "negated": "negated_finding",
    "nil": "nil_report",
    "tsr_lifted": "tsr_lifted",
}
_HEADING_RE = re.compile(r"(?im)^[ \t]*(?:FINDING|OBSERVATION)\s+([A-Z]{1,6}-\d+)\b[^\n]*")
_BLOCK_STOP_RE = re.compile(r"(?im)^[ \t]*(?:ATTACHMENT\b|.*\bFIELD REGISTER\b)")
_REGISTER_RE = re.compile(r"(?im)^[ \t]*.*\bFIELD REGISTER\b")
_KM_RE = re.compile(r"\b(?:km|chainage)\s*[:=]?\s*\d", re.IGNORECASE)
_INSTRUCTION_RE = re.compile(
    r"\b(?:check|inspect|examine|look)\s+for\b"
    r"|\bwatch\s+for\b"
    r"|\breport\s+any\b"
    r"|\bcheck\s+whether\b"
    r"|\bmust\s+test\b"
    r"|\bwhenever\s+a\s+point\s+machine\s+is\s+replaced\b",
    re.IGNORECASE,
)
_CAPTION_RE = re.compile(r"\b(?:photograph|photo|image)\s+(?:shows|showing)\b|\bcaption\b", re.IGNORECASE)
_HYPOTHETICAL_RE = re.compile(r"\bif\b|\bin case of\b|\bshould a\b|\bwere (?:a|an|the)\b", re.IGNORECASE)
_WORK_PLAN_RE = re.compile(
    r"\b(?:will|shall)\s+be\s+(?:carried out|undertaken|performed)\b"
    r"|\bcarry out\s+(?:tamping|screening)\b"
    r"|\bplanned\s+(?:tamping|screening)\b"
    r"|\byearly\s+programme\b"
    r"|\btamping\s+cycle\b",
    re.IGNORECASE,
)
_COMPLETED_RE = re.compile(
    r"\b(?:weld failure|rail fracture|interlocking fault|tamping|deep screening)\b.{0,48}?"
    r"\b(?:was|were|has been|have been)\s+(?:renewed|rewelded|rectified|repaired|finished|completed|carried out)\b"
    r"|\b(?:was|were)\s+rewelded\b"
    r"|\b(?:has been|was|were)\s+rectified\b"
    r"|\bstands\s+rectified\b"
    r"|\bnone\s+outstanding\b"
    r"|\bwas\s+cut\s+out\b.{0,80}?\btested\s+satisfactory\b"
    r"|\b(?:has|have)\s+been\s+changed\b.{0,80}?\bnothing\s+outstanding\b"
    r"|\bregulated\s+earlier\b.{0,80}?\b(?:rechecked|none\s+outstanding)\b"
    r"|\btamping\s+(?:was\s+)?(?:finished|done|carried out)\b"
    r"|\b(?:deep screening|screening)\s+(?:was\s+)?finished\b",
    re.IGNORECASE,
)
_BACKGROUND_RE = re.compile(
    r"\btamping\s+machine\s+reached\b|\btamping\s+cycle\b|\byearly\s+programme\b",
    re.IGNORECASE,
)
_STILL_PRESENT_RE = re.compile(
    r"\bstill\s+(?:requires|required|needs|outstanding)\b|\bis\s+noted\b|\bremains\b|\bis\s+present\b",
    re.IGNORECASE,
)
_OVERVIEW_RE = re.compile(
    r"\boverview\b|\bthis (?:report|note|document) (?:lists|covers|mentions|contains)\b",
    re.IGNORECASE,
)


def local_text_or_keep(model: object) -> object:
    """Use local text rules when the selected model is still an empty mock."""
    if getattr(model, "fixture_matched", False):
        return model
    if getattr(model, "name", "") != "mock":
        return model
    if getattr(model, "_script", None):
        return model
    notice = getattr(model, "fixture_notice", None)
    text_notice = notice if isinstance(notice, str) and notice.strip() else None
    return LocalTextProvider(fixture_notice=text_notice)


class LocalTextProvider:
    """Sentence proposer. It does not call a network and does not read images."""

    name = "local-text"
    supports_images = False
    fixture_matched = False
    scripted_fixture = None
    scripted_summary = None

    def __init__(self, fixture_notice: str | None = None) -> None:
        self.fixture_notice = fixture_notice
        self.requests: list[ProviderRequest] = []
        self.used_heading_groups = False
        self._seen_finding_ids: set[str] = set()

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        parts = _parse_fenced_sections(request.user_prompt)
        if any(_has_section_heading(text) for _reference, native, ocr in parts for text in (native, ocr)):
            self.used_heading_groups = True
        payload = propose_parts(
            parts,
            seen_ids=self._seen_finding_ids,
            heading_mode=self.used_heading_groups,
        )
        return ProviderResponse(raw_text=json.dumps(payload))


def propose_prompt(user_prompt: str) -> dict[str, object]:
    """Read one fenced report chunk and return schema JSON."""
    return propose_parts(_parse_fenced_sections(user_prompt))


def propose_parts(
    parts: Sequence[tuple[str, str, str]],
    seen_ids: set[str] | None = None,
    heading_mode: bool | None = None,
) -> dict[str, object]:
    """Propose findings from `(reference, native_text, ocr_text)` blocks."""
    detected = any(
        _has_section_heading(native_text) or _has_section_heading(ocr_text)
        for _reference, native_text, ocr_text in parts
    )
    grouped = detected if heading_mode is None else bool(heading_mode) or detected
    ids = seen_ids if seen_ids is not None else set()
    findings: list[dict[str, object]] = []
    for reference, native_text, ocr_text in parts:
        native_rows = _propose_region(native_text, reference, "native_text", ids, grouped)
        findings.extend(native_rows)
        if not ocr_text.strip():
            continue
        if _has_section_heading(native_text) and not _has_section_heading(ocr_text):
            continue
        covered = {_norm(str(row["excerpt"])) for row in native_rows}
        for row in _propose_region(ocr_text, reference, "ocr", ids, grouped):
            folded = _norm(str(row["excerpt"]))
            if folded in covered or any(folded in item for item in covered):
                continue
            findings.append(row)
    return {"summary": "", "findings": findings}


def _parse_fenced_sections(user_prompt: str) -> list[tuple[str, str, str]]:
    match = _FENCE_RE.search(user_prompt)
    if match is None:
        return []
    sections: list[tuple[str, str, str]] = []
    reference: str | None = None
    mode: str | None = None
    native: list[str] = []
    ocr: list[str] = []

    def close() -> None:
        nonlocal reference, native, ocr, mode
        if reference is not None:
            sections.append((reference, "".join(native), "".join(ocr)))
        reference = None
        mode = None
        native = []
        ocr = []

    for line in match.group(1).splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        section_match = _SECTION_LINE.match(stripped)
        if section_match:
            close()
            reference = section_match.group(1)
            continue
        if stripped.startswith("[ref=") or stripped.startswith("[method="):
            method_match = _METHOD_LINE.match(stripped)
            mode = method_match.group(1) if method_match else None
            continue
        if mode == "native_text":
            native.append(line)
        elif mode == "ocr":
            ocr.append(line)
    close()
    return sections


def _has_section_heading(text: str) -> bool:
    return _HEADING_RE.search(text) is not None


def _propose_region(
    text: str,
    reference: str,
    method: str,
    seen_ids: set[str],
    heading_mode: bool,
) -> list[dict[str, object]]:
    if not text.strip():
        return []
    headings = list(_HEADING_RE.finditer(text))
    if headings:
        return _rows_from_headings(text, headings, reference, method, seen_ids)
    if heading_mode and _REGISTER_RE.search(text) is not None:
        return []
    rows: list[dict[str, object]] = []
    for sentence in _sentences(text):
        rows.extend(_rows_for(sentence, reference, method))
    return rows


def _rows_from_headings(
    text: str,
    headings: list[re.Match[str]],
    reference: str,
    method: str,
    seen_ids: set[str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, heading in enumerate(headings):
        finding_id = heading.group(1).upper()
        if finding_id in seen_ids:
            continue
        following = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        stop = _BLOCK_STOP_RE.search(text, heading.end(), following)
        end = stop.start() if stop is not None else following
        excerpt = text[heading.start() : end].strip()
        if not excerpt:
            continue
        cues = excerpt_cues(excerpt)
        if not _should_emit(cues, excerpt):
            continue
        if cues.polarity in {"positive", "tsr_not_active"} and _bare_label(excerpt, cues.defect_phrases):
            continue
        seen_ids.add(finding_id)
        rows.append(_row(excerpt, reference, method, cues.polarity))
    return rows


def _rows_for(sentence: str, reference: str, method: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    parent_blocked = _blocked_positive(sentence)
    for clause in _clauses(sentence):
        if clause in seen or clause not in sentence:
            continue
        if _hypothetical_before_defect(clause):
            continue
        cues = excerpt_cues(clause)
        if cues.polarity not in {"negated", "nil"} and (
            _INSTRUCTION_RE.search(clause) is not None
            or _WORK_PLAN_RE.search(clause) is not None
            or _CAPTION_RE.search(clause) is not None
        ):
            continue
        if cues.polarity in {"positive", "tsr_not_active"} and _positive_not_a_defect(clause):
            continue
        if cues.polarity in {"positive", "tsr_not_active"} and (
            _blocked_positive(clause) or (parent_blocked and _KM_RE.search(clause) is None)
        ):
            continue
        if not _should_emit(cues, clause):
            continue
        if cues.polarity in {"positive", "tsr_not_active"} and _bare_label(clause, cues.defect_phrases):
            continue
        seen.add(clause)
        rows.append(_row(clause, reference, method, cues.polarity))
    return rows


def _positive_not_a_defect(sentence: str) -> bool:
    """True when this clause records finished work or a programme, not a live defect."""
    if _STILL_PRESENT_RE.search(sentence) is not None:
        return False
    if _BACKGROUND_RE.search(sentence) is not None:
        return True
    return _COMPLETED_RE.search(sentence) is not None


def _blocked_positive(sentence: str) -> bool:
    """True when a positive phrase is an overview, instruction, caption, or work plan."""
    cues = excerpt_cues(sentence)
    if cues.polarity not in {"positive", "tsr_not_active"} or not cues.defect_phrases:
        return False
    if _CAPTION_RE.search(sentence) or _INSTRUCTION_RE.search(sentence) or _WORK_PLAN_RE.search(sentence):
        return True
    if _hypothetical_before_defect(sentence):
        return True
    if _OVERVIEW_RE.search(sentence) is None:
        return False
    types = positive_defect_types(sentence)
    chainages = len(_KM_RE.findall(sentence))
    if len(types) >= 2 and chainages < len(types):
        return True
    return len(types) >= 1 and chainages == 0


def _hypothetical_before_defect(sentence: str) -> bool:
    lowered = sentence.casefold()
    spans = _scan_phrases(lowered)
    if not spans:
        return False
    start = min(span[0] for span in spans)
    return _HYPOTHETICAL_RE.search(lowered[:start]) is not None


def _should_emit(cues: ExcerptCues, sentence: str) -> bool:
    if cues.polarity in {"negated", "nil"}:
        return bool(cues.defect_phrases) or cues.nil
    if cues.polarity == "uncertain":
        return bool(cues.defect_phrases) or _UNTYPED_DEFECT_RE.search(sentence) is not None
    if cues.polarity in {"positive", "tsr_not_active"}:
        return bool(cues.defect_phrases)
    return False


def _row(excerpt: str, reference: str, method: str, polarity: str) -> dict[str, object]:
    kind = "ruled_out" if polarity in _RULED_OUT else "defect"
    return {
        "kind": kind,
        "statement": excerpt,
        "excerpt": excerpt,
        "reference": reference,
        "department": None,
        "reported_defect_type": None,
        "normalized_defect_type": None,
        "reported_severity_text": None,
        "severity": None,
        "reported_track_line": None,
        "track_line": None,
        "chainage_km": None,
        "due_date": None,
        "days_overdue": None,
        "has_tsr": None,
        "tsr_speed_kmh": None,
        "reported_duration_min": None,
        "reported_asset_ref": None,
        "extraction_method": method,
        "uncertainty": "high" if polarity == "uncertain" else "low",
        "ruled_out_reason": _RULED_OUT.get(polarity),
    }


def _clauses(sentence: str) -> list[str]:
    for mark in _CLAUSE_MARKS:
        if mark not in sentence:
            continue
        parts: list[str] = []
        cursor = 0
        while cursor <= len(sentence):
            at = sentence.find(mark, cursor)
            if at < 0:
                parts.append(sentence[cursor:])
                break
            parts.append(sentence[cursor:at])
            cursor = at + len(mark)
        cleaned = [part.strip() for part in parts if part.strip()]
        if len(cleaned) < 2 or any(part not in sentence for part in cleaned):
            continue
        counts = [len(set(excerpt_cues(part).defect_phrases)) for part in cleaned]
        if all(count <= 1 for count in counts) and sum(1 for count in counts if count == 1) >= 2:
            return cleaned
    return [sentence]


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for start, end in _sentence_spans(text):
        excerpt = _trimmed(text, start, end)
        if excerpt:
            sentences.append(excerpt)
    return sentences


def _trimmed(text: str, start: int, end: int) -> str | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return text[start:end]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    if not text.strip():
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "." and _decimal_point(text, index):
            index += 1
            continue
        if char in ".!?" and _boundary_after(text, index):
            spans.append((start, index + 1))
            start = _skip_gap(text, index + 1)
            index = start
            continue
        if char == "\n" and _line_break_splits(text, index):
            spans.append((start, index))
            start = _skip_gap(text, index + 1)
            index = start
            continue
        index += 1
    if start < length:
        spans.append((start, length))
    return spans


def _decimal_point(text: str, index: int) -> bool:
    return index > 0 and text[index - 1].isdigit() and index + 1 < len(text) and text[index + 1].isdigit()


def _boundary_after(text: str, index: int) -> bool:
    nxt = index + 1
    if nxt >= len(text):
        return True
    if text[nxt] not in " \t\r\n":
        return False
    cursor = nxt
    while cursor < len(text) and text[cursor] in " \t\r":
        cursor += 1
    if cursor >= len(text) or text[cursor] == "\n":
        return True
    return text[cursor].isupper() or text[cursor] in "\"“‘("


def _line_break_splits(text: str, index: int) -> bool:
    nxt = index + 1
    while nxt < len(text) and text[nxt] in " \t\r":
        nxt += 1
    if nxt >= len(text) or text[nxt] == "\n":
        return True
    if text[nxt].isdigit() or text[nxt].islower():
        return False
    if not _span_has_terminator(text, index):
        return _next_line_opens_finding(text, nxt)
    return text[nxt].isupper() or text[nxt] in "\"“‘(-•*"


def _span_has_terminator(text: str, index: int) -> bool:
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t\r":
        cursor -= 1
    if cursor < 0 or text[cursor] not in ".!?":
        return False
    return not _decimal_point(text, cursor)


def _next_line_opens_finding(text: str, start: int) -> bool:
    end = text.find("\n", start)
    line = text[start:] if end < 0 else text[start:end]
    folded = re.sub(r"[^a-z0-9]+", " ", line.casefold()).strip()
    if not folded:
        return False
    for phrase in excerpt_cues(line).defect_phrases:
        token = re.sub(r"[^a-z0-9]+", " ", phrase.casefold()).strip()
        if token and (folded == token or folded.startswith(token + " ")):
            return True
    return False


def _bare_label(sentence: str, phrases: tuple[str, ...]) -> bool:
    """True when the excerpt is only a defect name and has no other detail."""
    folded = re.sub(r"[^a-z0-9]+", " ", sentence.casefold()).strip()
    if not folded:
        return False
    for phrase in phrases:
        token = re.sub(r"[^a-z0-9]+", " ", phrase.casefold()).strip()
        if token and folded == token:
            return True
    return False


def _skip_gap(text: str, index: int) -> int:
    cursor = index
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


__all__ = [
    "LocalTextProvider",
    "NlpIntegrationPending",
    "NlpSentenceNote",
    "PendingProfessorNlpAdapter",
    "ReportNlpAdapter",
    "local_text_or_keep",
    "propose_parts",
    "propose_prompt",
]
