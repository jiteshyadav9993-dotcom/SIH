"""Local text proposer. No live model, fixture script, or network."""

from __future__ import annotations

import hashlib

import pytest

import backlog_state
from report_documents import DocumentBundle, DocumentSection, ExtractionMethod, ReadStatus, TextBlock
from report_nlp import LocalTextProvider, NlpIntegrationPending, PendingProfessorNlpAdapter, propose_parts
from report_pipeline import simplify_report
from report_provider import MockReportProvider
from report_schema import FindingMethod, ReviewStatus, UnderstandingStatus, ValueOrigin
from report_service import apply_stored_report, preview_uploaded_report
from report_validate import _chainage_from_excerpt, check_findings, classify_polarity
from tests.test_report_apply import _ScriptedProvider, _clear_possession_flags, _open_ids, workspace
from tests.test_report_documents import _text_pdf

_COMPLETE = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."


def _install_open(items: list[object]) -> None:
    backlog_state._ensure_loaded()
    with backlog_state._LOCK:
        backlog_state._ACTIVE = list(items)  # type: ignore[arg-type]
        backlog_state._save_snapshot()


def _section(
    reference: str,
    page: int | None,
    text: str,
    *,
    ocr: str = "",
) -> DocumentSection:
    blocks: list[TextBlock] = []
    methods: list[ExtractionMethod] = []
    if text:
        blocks.append(
            TextBlock(
                block_id=f"{reference}/text:1",
                text=text,
                method=ExtractionMethod.NATIVE_TEXT,
                reference=reference,
                page_number=page,
            )
        )
        methods.append(ExtractionMethod.NATIVE_TEXT)
    if ocr:
        blocks.append(
            TextBlock(
                block_id=f"{reference}/ocr:1",
                text=ocr,
                method=ExtractionMethod.OCR,
                reference=reference,
                page_number=page,
            )
        )
        methods.append(ExtractionMethod.OCR)
    if not methods:
        methods.append(ExtractionMethod.NONE)
    return DocumentSection(
        reference=reference,
        page_number=page,
        native_text=text,
        ocr_text=ocr,
        blocks=tuple(blocks),
        images=(),
        methods=tuple(methods),
    )


def _bundle(sections: list[DocumentSection], *, filename: str = "unseen.pdf") -> DocumentBundle:
    return DocumentBundle(
        filename=filename,
        media_kind="pdf",
        sha256=hashlib.sha256(filename.encode("utf-8")).hexdigest(),
        byte_size=128,
        status=ReadStatus.COMPLETE,
        sections=tuple(sections),
        warnings=(),
        methods_used=(ExtractionMethod.NATIVE_TEXT,),
    )


def _read(text: str, *, reference: str = "page:1", page: int | None = 1, ocr: str = ""):
    return simplify_report(_bundle([_section(reference, page, text, ocr=ocr)]), LocalTextProvider())


def test_professor_adapter_stays_pending() -> None:
    adapter = PendingProfessorNlpAdapter()
    assert adapter.available() is False
    with pytest.raises(NlpIntegrationPending):
        adapter.inspect("page:1", "Weld failure at KM 10.0.")


def test_empty_mock_still_returns_no_findings() -> None:
    text = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
    result = simplify_report(_bundle([_section("page:1", 1, text)]), MockReportProvider())
    assert result.provider_name == "mock"
    assert result.candidates == ()
    assert result.ruled_out == ()
    assert result.nlp_status == "pending"


def test_unfamiliar_pdf_does_not_need_the_fixture(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.setenv("REPORT_AI_PROVIDER", "mock")
    monkeypatch.delenv("REPORT_AI_MOCK_FIXTURE", raising=False)
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_text_pdf([_COMPLETE]), "unfamiliar-maintenance.pdf")
    assert preview["provider_name"] == "local-text"
    assert preview["provider"]["scripted"] is False
    assert preview["provider"]["live"] is False
    assert "SCRIPTED MOCK" not in str(preview["summary"])
    candidates = preview["candidates"]
    assert isinstance(candidates, list) and len(candidates) == 1
    evidence = candidates[0]["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["normalized_defect_type"] == "WELD_FAILURE"
    assert evidence["chainage_km"] == 80.0
    assert evidence["excerpt"] in _COMPLETE or _COMPLETE in str(evidence["excerpt"])
    assert candidates[0]["status"] == "READY_TO_APPLY"
    assert _open_ids() == []


def test_three_independent_defects_stay_separate() -> None:
    text = (
        "Weld failure at KM 67.5 on the DN line, severity 84, 22 days overdue, no TSR. "
        "Tamping required at KM 68.2 on the DN line, severity 58, 12 days overdue, no TSR. "
        "Signal interlock at KM 9.4 on the UP fast line, severity 63, 6 days overdue."
    )
    result = _read(text)
    assert result.status is UnderstandingStatus.COMPLETE
    assert [item.normalized_defect_type.value for item in result.candidates] == [
        "WELD_FAILURE",
        "TAMPING_REQUIRED",
        "SIGNAL_INTERLOCK",
    ]
    assert [item.chainage_km.value for item in result.candidates] == [67.5, 68.2, 9.4]
    assert [item.evidence[0].reference for item in result.candidates] == ["page:1", "page:1", "page:1"]
    assert result.candidates[0].review_status is ReviewStatus.READY
    assert result.candidates[2].has_tsr.value is None
    assert result.candidates[2].review_status is ReviewStatus.NEEDS_REVIEW
    assert "67.5" in result.summary and "9.4" in result.summary


def test_positive_sentence_keeps_supported_fields_and_decimal_chainage() -> None:
    result = _read("Weld failure identified at KM 67.5 on the DN line, severity 84, 22 days overdue, TSR imposed.")
    candidate = result.candidates[0]
    assert candidate.normalized_defect_type.value == "WELD_FAILURE"
    assert candidate.chainage_km.value == 67.5
    assert candidate.track_line.value == "DN"
    assert candidate.severity.value == 84
    assert candidate.days_overdue.value == 22.0
    assert candidate.has_tsr.value is True
    assert candidate.excerpt == "Weld failure identified at KM 67.5 on the DN line, severity 84, 22 days overdue, TSR imposed."
    assert candidate.evidence[0].page_number == 1


def test_severity_in_the_next_sentence_is_not_copied() -> None:
    text = "Weld failure identified at KM 67.5 DN. Severity 84/100."
    result = _read(text)
    assert len(result.candidates) == 1
    assert result.candidates[0].severity.value is None
    assert "severity" in result.candidates[0].missing_fields
    assert "84" not in result.candidates[0].excerpt


def test_explicit_negation_and_nil_are_not_defects() -> None:
    samples = (
        "No rail fracture was observed at KM 67.1 DN.",
        "Rail fracture was not found at KM 67.1 DN.",
        "No evidence of weld failure was found.",
        "The report does not establish a signal/interlocking failure.",
        "Nil defect was recorded on the UP line.",
    )
    for text in samples:
        result = _read(text)
        assert result.candidates == (), text
        assert len(result.ruled_out) == 1, text
        assert result.ruled_out[0].excerpt == text
        assert result.ruled_out[0].evidence[0].reference == "page:1"


def test_uncertain_wording_stays_in_review() -> None:
    possible = _read("Possible weld failure was reported at KM 67.5 DN.")
    assert possible.ruled_out == ()
    assert possible.candidates[0].normalized_defect_type.value == "WELD_FAILURE"
    assert possible.candidates[0].uncertainty.value == "high"
    assert possible.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW

    suspected = _read("A weld defect is suspected at KM 67.5 DN.")
    assert suspected.candidates[0].uncertainty.value == "high"
    assert suspected.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW

    generic = _read("There may be a defect near KM 67.5 DN.")
    assert generic.ruled_out == ()
    assert generic.candidates[0].normalized_defect_type.value is None
    assert generic.candidates[0].uncertainty.value == "high"
    assert generic.candidates[0].chainage_km.value == 67.5
    assert generic.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW

    fracture = _read("A fracture cannot be ruled out at KM 67.5 DN.")
    assert fracture.candidates[0].normalized_defect_type.value is None
    assert fracture.candidates[0].uncertainty.value == "high"
    assert classify_polarity(fracture.candidates[0].excerpt) == "uncertain"


def test_unrelated_sentences_produce_no_findings() -> None:
    text = "The inspection team reached the site at 09:30. Maintenance is planned for next week."
    result = _read(text)
    assert result.candidates == ()
    assert result.ruled_out == ()
    assert result.summary == ""


def test_missing_and_ambiguous_fields_stay_unknown() -> None:
    no_severity = _read("Weld failure at KM 67.5 on the DN line, 4 days overdue, no TSR.")
    assert no_severity.candidates[0].severity.value is None
    assert no_severity.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW

    no_chainage = _read("Weld failure on the DN line, severity 70, 3 days overdue, no TSR.")
    assert no_chainage.candidates[0].chainage_km.value is None

    no_line = _read("Weld failure at KM 67.5, severity 70, 3 days overdue, no TSR.")
    assert no_line.candidates[0].track_line.value is None

    two_chainages = _read(
        "Weld failure between KM 67.5 and KM 68.2 on the DN line, severity 84, 4 days overdue, no TSR."
    )
    assert two_chainages.candidates[0].chainage_km.value is None
    assert two_chainages.candidates[0].chainage_km.origin is ValueOrigin.UNKNOWN
    assert any("more than one chainage" in warning for warning in two_chainages.candidates[0].validation_warnings)

    due_only = _read("Weld failure at KM 67.5 on the DN line, severity 70, due 21-09-2026, no TSR.")
    assert due_only.candidates[0].due_date.value == "21-09-2026"
    assert due_only.candidates[0].days_overdue.value is None
    assert "days_overdue" in due_only.candidates[0].missing_fields


def test_tsr_is_not_a_defect_unless_the_same_sentence_states_one() -> None:
    imposed = _read("TSR imposed at KM 40 on the DN line.")
    removed = _read("TSR removed at KM 86.4.")
    assert imposed.candidates == () and imposed.ruled_out == ()
    assert removed.candidates == () and removed.ruled_out == ()

    with_defect = _read("OHE sag at KM 86.4 on the UP line, severity 70, 4 days overdue, TSR imposed.")
    assert with_defect.candidates[0].normalized_defect_type.value == "OHE_SAG"
    assert with_defect.candidates[0].has_tsr.value is True

    lifted = _read("OHE sag at KM 86.4 on the UP line, severity 70, 4 days overdue, TSR removed.")
    assert lifted.candidates[0].has_tsr.value is False
    assert lifted.ruled_out == ()

    conflict = _read(
        "Weld failure at KM 67.5 on the DN line, severity 80, 4 days overdue, TSR imposed and no TSR."
    )
    assert conflict.candidates[0].has_tsr.value is None
    assert any("conflicting TSR" in warning for warning in conflict.candidates[0].validation_warnings)
    assert conflict.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW


def test_signal_interlocking_maps_without_treating_a_bare_speed_as_tsr() -> None:
    result = _read("Signal interlocking was reported at KM 9.4 on the UP fast line, passing at 30 km/h.")
    candidate = result.candidates[0]
    assert candidate.normalized_defect_type.value == "SIGNAL_INTERLOCK"
    assert candidate.track_line.value == "UP_FAST"
    assert candidate.chainage_km.value == 9.4
    assert candidate.has_tsr.value is None
    assert candidate.tsr_speed_kmh is None


def test_two_defects_in_one_sentence_stay_separate() -> None:
    text = "Weld failure at KM 10.0 on the DN line and rail fracture at KM 80.0 on the UP line."
    result = _read(text)
    assert len(result.candidates) == 2
    assert result.candidates[0].normalized_defect_type.value == "WELD_FAILURE"
    assert result.candidates[0].chainage_km.value == 10.0
    assert result.candidates[1].normalized_defect_type.value == "RAIL_FRACTURE"
    assert result.candidates[1].chainage_km.value == 80.0
    assert result.candidates[0].excerpt in text
    assert result.candidates[1].excerpt in text


def test_wrapped_decimal_chainage_stays_one_excerpt() -> None:
    text = "Weld failure identified at KM\n67.5 on the DN line, severity 80, 4 days overdue, no TSR."
    result = _read(text)
    assert len(result.candidates) == 1
    assert result.candidates[0].chainage_km.value == 67.5
    assert result.candidates[0].excerpt in text


def test_ocr_is_used_only_when_it_adds_text() -> None:
    native = "Weld failure at KM 67.5 on the DN line, severity 80, 4 days overdue, no TSR."
    same = _read(native, ocr=native)
    assert len(same.candidates) == 1
    assert same.candidates[0].extraction_method is FindingMethod.NATIVE_TEXT

    extra = "OHE sag at KM 86.4 on the UP line, severity 70, 4 days overdue, no TSR."
    both = _read(native, ocr=extra)
    methods = {item.normalized_defect_type.value: item.extraction_method for item in both.candidates}
    assert methods == {"WELD_FAILURE": FindingMethod.NATIVE_TEXT, "OHE_SAG": FindingMethod.OCR}

    ocr_only = simplify_report(
        _bundle([_section("page:3", 3, "", ocr="Deep screening at KM 156.0 on the DN line.")]),
        LocalTextProvider(),
    )
    assert ocr_only.candidates[0].extraction_method is FindingMethod.OCR
    assert ocr_only.candidates[0].evidence[0].reference == "page:3"
    assert ocr_only.candidates[0].evidence[0].page_number == 3


def test_paragraph_reference_is_kept_for_docx_text() -> None:
    result = simplify_report(
        _bundle([_section("paragraph:4", None, _COMPLETE)]),
        LocalTextProvider(),
    )
    assert result.candidates[0].evidence[0].reference == "paragraph:4"
    assert result.candidates[0].evidence[0].page_number is None


def test_mismatched_excerpt_and_invented_chainage_are_rejected() -> None:
    section = _section("page:1", 1, "Weld failure at KM 67.5 on the DN line.")
    payload = {
        "summary": "",
        "findings": [
            {
                "kind": "defect",
                "statement": "Weld failure at KM 99.",
                "excerpt": "Weld failure at KM 99.",
                "reference": "page:1",
                "department": None,
                "reported_defect_type": None,
                "normalized_defect_type": "WELD_FAILURE",
                "reported_severity_text": None,
                "severity": 90,
                "reported_track_line": None,
                "track_line": "UP",
                "chainage_km": 99,
                "due_date": None,
                "days_overdue": 10,
                "has_tsr": True,
                "tsr_speed_kmh": 30,
                "reported_duration_min": 60,
                "reported_asset_ref": None,
                "extraction_method": "native_text",
                "uncertainty": "low",
                "ruled_out_reason": None,
            }
        ],
    }
    accepted, warnings = check_findings(payload, (section,))
    assert accepted == ()
    assert any("not in the extracted text" in warning for warning in warnings)

    kept = _read("Weld failure at KM 67.5 on the DN line.")
    assert kept.candidates[0].chainage_km.value == 67.5
    assert kept.candidates[0].severity.value is None
    assert kept.candidates[0].has_tsr.value is None


def test_proposer_does_not_emit_visual_findings() -> None:
    rows = propose_parts((("page:1", "Weld failure at KM 10.0 on the DN line.", ""),))
    findings = rows["findings"]
    assert isinstance(findings, list)
    assert findings
    assert all(item["extraction_method"] != "visual" for item in findings)


def test_injected_provider_is_not_replaced_by_local_text(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.delenv("REPORT_AI_MOCK_FIXTURE", raising=False)
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_text_pdf([_COMPLETE]), "inspection.pdf", provider=_ScriptedProvider([]))
    assert preview["candidates"] == []
    assert preview["provider_name"] == "mock"


def test_ready_local_record_applies_once(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.setenv("REPORT_AI_PROVIDER", "mock")
    monkeypatch.delenv("REPORT_AI_MOCK_FIXTURE", raising=False)
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_text_pdf([_COMPLETE]), "apply-local.pdf")
    candidate = preview["candidates"][0]
    assert candidate["status"] == "READY_TO_APPLY"
    applied = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [candidate["candidate_id"]]},
    )
    assert applied["status"] == "APPLIED"
    assert len(applied["applied_defects"]) == 1
    assert str(applied["applied_defects"][0]["defect_id"]).startswith("AI1-")
    assert applied["applied_defects"][0]["required_block_min"] == 60.0
    repeat = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [candidate["candidate_id"]]},
    )
    assert repeat["status"] == "ALREADY_APPLIED"
    assert _open_ids() == [applied["applied_defects"][0]["defect_id"]]


def test_speed_suffix_is_not_a_chainage() -> None:
    stated, warnings = _chainage_from_excerpt("Defect at 30 km on the UP line.", None)
    assert stated.value == 30.0
    assert warnings == ()
    speed_only, speed_warnings = _chainage_from_excerpt("Weld failure at 30 km/h with no location given.", None)
    assert speed_only.value is None
    assert speed_warnings == ()
    ambiguous, ambiguous_warnings = _chainage_from_excerpt("Defect between KM 67.5 and KM 68.5.", None)
    assert ambiguous.value is None
    assert any("more than one chainage" in warning for warning in ambiguous_warnings)

    located = _read(
        "Weld failure at KM 67.5 on the DN line, passing at 30 km/h, severity 80, 4 days overdue, no TSR."
    )
    assert located.candidates[0].chainage_km.value == 67.5
    assert located.candidates[0].normalized_defect_type.value == "WELD_FAILURE"
    assert located.candidates[0].excerpt in (
        "Weld failure at KM 67.5 on the DN line, passing at 30 km/h, severity 80, 4 days overdue, no TSR."
    )
    bare_speed = _read("Weld failure at 30 km/h with no location given.")
    assert bare_speed.candidates[0].chainage_km.value is None


def test_capital_wrap_stays_one_excerpt_and_a_bare_label_is_not_a_candidate() -> None:
    wrapped = "Weld failure\nIdentified at KM 67.5 on the DN line, severity 80, 4 days overdue, no TSR."
    result = _read(wrapped)
    assert len(result.candidates) == 1
    assert result.candidates[0].excerpt in wrapped
    assert "KM 67.5" in result.candidates[0].excerpt
    assert result.candidates[0].chainage_km.value == 67.5
    assert result.candidates[0].severity.value == 80

    labelled = (
        "WELD FAILURE\n"
        "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR.\n"
        "Weld failure"
    )
    findings = _read(labelled)
    assert len(findings.candidates) == 1
    assert findings.candidates[0].excerpt == "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
    assert findings.candidates[0].chainage_km.value == 80.0
    assert findings.candidates[0].evidence[0].reference == "page:1"

    concise = _read("Weld failure on the DN line.")
    assert len(concise.candidates) == 1
    assert concise.candidates[0].track_line.value == "DN"
    assert concise.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW

    broken = _read("Broken rail at KM 40.0 on the UP fast line, severity 90, 8 days overdue, no TSR.")
    assert broken.candidates[0].normalized_defect_type.value == "RAIL_FRACTURE"
    assert broken.candidates[0].chainage_km.value == 40.0
    paraphrased = _read("Fractured rail at KM 40.0 on the UP fast line, severity 90, 8 days overdue, no TSR.")
    assert paraphrased.candidates == ()
    assert paraphrased.ruled_out == ()
