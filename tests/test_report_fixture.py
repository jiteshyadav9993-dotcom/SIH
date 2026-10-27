"""Scripted mock for DEMO-MR-26027-041. No live model, OCR, or API key."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import backlog_state
import data_ingestion
import defects
import ml_priority
import solver
import timetable
from defects import DEFECT_TYPE_SPEC, DefectType, _make_defect
from report_documents import read_report_document
from report_provider import SCRIPTED_MOCK_LABEL, ProviderNotConfiguredError
from report_service import ReportRequestError, apply_stored_report, preview_uploaded_report
from report_store import load_preview
from report_test_fixtures import DEMO_FIXTURE_ID
from tests.test_report_apply import _clear_possession_flags, _completed_ids, _open_ids, _snapshot_json, workspace
from tests.test_report_documents import _text_pdf

_PDF = Path(__file__).resolve().parent / "fixtures" / "ai1_railway_maintenance_demo_report_4_pages.pdf"
_NAME = "ai1_railway_maintenance_demo_report_4_pages.pdf"
_PAGE = Path(__file__).resolve().parents[1] / "index.html"
_INTERPRETATION = (
    "The simulated multi-department report contains three reported maintenance findings: "
    "a rail-joint defect reported as WELD_FAILURE at KM 67.5 DN, a TAMPING_REQUIRED finding at KM 68.2 DN, "
    "and an intermittent SIGNAL_INTERLOCK indication at KM 9.4 UP_FAST."
)
_FRACTURE = (
    "No rail fracture was observed at KM 67.1 on DN during the visual inspection. "
    "No fracture was recorded at this location in this observation."
)
_VIBRATION = (
    "Vibration was reported near KM 69.0 on DN. The cause has not been determined. "
    "The report does not identify a confirmed defect type, numeric severity, due date, days overdue, or TSR status."
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _install_open(items: list[object]) -> None:
    backlog_state._ensure_loaded()
    with backlog_state._LOCK:
        backlog_state._ACTIVE = list(items)  # type: ignore[arg-type]
        backlog_state._save_snapshot()


def _enable(monkeypatch: pytest.MonkeyPatch, fixture: str | None = DEMO_FIXTURE_ID) -> None:
    monkeypatch.setenv("REPORT_AI_PROVIDER", "mock")
    monkeypatch.delenv("REPORT_AI_API_KEY", raising=False)
    if fixture is None:
        monkeypatch.delenv("REPORT_AI_MOCK_FIXTURE", raising=False)
    else:
        monkeypatch.setenv("REPORT_AI_MOCK_FIXTURE", fixture)


def _pdf() -> bytes:
    return _PDF.read_bytes()


def _by_asset(preview: dict[str, object], asset: str) -> dict[str, object]:
    rows = preview["candidates"]
    assert isinstance(rows, list)
    for item in rows:
        assert isinstance(item, dict)
        fields = item.get("fields")
        if isinstance(fields, dict) and fields.get("reported_asset_ref") == asset:
            return item
    raise AssertionError(f"{asset} was not in the preview")


def test_demo_signature_selects_the_fixture_and_filename_does_not(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    before = _snapshot_json()
    data = _pdf()
    bundle = read_report_document(data, "renamed-demo.pdf")
    preview = preview_uploaded_report(data, "renamed-demo.pdf")
    assert preview["provider_name"] == "mock:" + DEMO_FIXTURE_ID
    provider = preview["provider"]
    assert isinstance(provider, dict)
    assert provider["provider"] == "mock"
    assert provider["live"] is False
    assert provider["scripted"] is True
    assert provider["fixture"] == DEMO_FIXTURE_ID
    assert provider["label"] == SCRIPTED_MOCK_LABEL
    assert "deterministic mock" in str(provider["limitation"])
    summary = str(preview["summary"])
    assert summary.startswith(SCRIPTED_MOCK_LABEL)
    assert _INTERPRETATION in summary
    assert "SIMULATED TEST DATA - NOT AN OFFICIAL RAILWAY RECORD" in summary
    assert "document reader" in summary
    assert "scripted mock interpretation" in summary
    assert _open_ids() == []
    assert _snapshot_json() == before
    page_text = {section.page_number: section.native_text for section in bundle.sections}
    image_bytes = {
        image.reference: bytes(image.data)
        for section in bundle.sections
        for image in section.images
        if image.data
    }
    shown = preview["source_images"]
    assert isinstance(shown, list)
    assert [item["reference"] for item in shown] == list(image_bytes)
    assert {item["page_number"] for item in shown} == {1, 2, 3}
    for image in shown:
        assert str(image["data_url"]).startswith("data:image/")
        assert base64.b64decode(str(image["data_url"]).split(",", 1)[1]) == image_bytes[str(image["reference"])]
    stored = load_preview(str(preview["report_id"]))
    assert stored is not None
    assert stored["provider"]["scripted"] is True
    assert stored["provider"]["provider"] == "mock"
    assert stored["provider"]["label"] == SCRIPTED_MOCK_LABEL
    assert str(stored["provider_name"]).startswith("mock")
    assert SCRIPTED_MOCK_LABEL in str(stored["summary"])
    assert "data_url" not in json.dumps(stored)
    assert "source_images" not in stored
    candidates = preview["candidates"]
    ruled = preview["ruled_out"]
    assert isinstance(candidates, list) and isinstance(ruled, list)
    assert [item["fields"]["reported_asset_ref"] for item in candidates] == ["D-01", "D-02", "D-03", "A-01"]
    assert len(ruled) == 1
    for item in candidates:
        evidence = item["evidence"]
        excerpt = str(evidence["excerpt"])
        page = evidence["page_numbers"][0]
        assert excerpt in page_text[page]
        assert item["candidate_id"] != item["fields"]["reported_asset_ref"]
        assert str(item["candidate_id"]).startswith("F")
    d01 = _by_asset(preview, "D-01")
    d02 = _by_asset(preview, "D-02")
    d03 = _by_asset(preview, "D-03")
    a01 = _by_asset(preview, "A-01")
    assert d01["status"] == "READY_TO_APPLY"
    assert d02["status"] == "READY_TO_APPLY"
    assert d01["duplicate_of"] == []
    assert d02["duplicate_of"] == []
    assert "do not assume it depicts KM 67.5 DN" in str(d01["evidence"]["excerpt"])
    assert "Reference date: 16 October 2026" in _flat(str(d01["evidence"]["excerpt"]))
    assert "not an engineering diagnosis" in _flat(str(d02["evidence"]["excerpt"]))
    assert "does not state whether an active TSR is in force" in _flat(str(d03["evidence"]["excerpt"]))
    assert "signal/interlocking failure" in summary
    _assert_ready(d01, "WELD_FAILURE", "TMS", "DN", 67.5, 84, 22.0, True, 60.0, 30.0, "24 September 2026")
    _assert_ready(d02, "TAMPING_REQUIRED", "TMS", "DN", 68.2, 58, 12.0, False, 120.0, None, "4 October 2026")
    assert d03["status"] == "NEEDS_REVIEW"
    assert d03["missing_fields"] == ["has_tsr"]
    assert d03["scheduling_preview"] is None
    assert d03["evidence"]["normalized_defect_type"] == "SIGNAL_INTERLOCK"
    assert d03["evidence"]["department"] == "SMMS"
    assert d03["evidence"]["track_line"] == "UP_FAST"
    assert d03["evidence"]["chainage_km"] == 9.4
    assert d03["evidence"]["severity"] == 63
    assert d03["evidence"]["days_overdue"] == 6.0
    assert d03["evidence"]["has_tsr"] is None
    assert d03["fields"]["due_date"]["value"] == "10 October 2026"
    assert d03["evidence"]["references"] == ["page:3"]
    assert a01["status"] == "NEEDS_REVIEW"
    assert a01["scheduling_preview"] is None
    assert a01["fields"]["normalized_defect_type"]["value"] is None
    assert a01["fields"]["severity"]["value"] is None
    assert a01["fields"]["due_date"]["value"] is None
    assert a01["fields"]["days_overdue"]["value"] is None
    assert a01["fields"]["has_tsr"]["value"] is None
    assert a01["evidence"]["chainage_km"] == 69.0
    assert a01["evidence"]["track_line"] == "DN"
    assert _VIBRATION in _flat(str(a01["evidence"]["excerpt"]))
    assert a01["evidence"]["references"] == ["page:4"]
    ruled_row = ruled[0]
    assert ruled_row["finding_id"] == "R1"
    assert ruled_row["finding_id"] != "N-01"
    assert ruled_row["status"] == "RULED_OUT"
    assert _FRACTURE in _flat(str(ruled_row["excerpt"]))
    assert "N-01" in str(ruled_row["excerpt"])
    assert ruled_row["references"] == ["page:4"]
    assert all(item["evidence"].get("normalized_defect_type") != "RAIL_FRACTURE" for item in candidates)
    linked = {item["fields"]["reported_asset_ref"]: item["evidence"]["references"][0] for item in candidates}
    for asset, reference in linked.items():
        assert any(str(image["reference"]).startswith(reference + "/image:") for image in shown) or asset == "A-01"
    assert any(str(image["reference"]).startswith("page:1/image:") for image in shown)
    assert any(str(image["reference"]).startswith("page:2/image:") for image in shown)
    assert any(str(image["reference"]).startswith("page:3/image:") for image in shown)


def test_same_filename_with_different_bytes_does_not_select_the_fixture(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    before = _open_ids()
    preview = preview_uploaded_report(_text_pdf(["A different inspection with no demo findings."]), _NAME)
    assert preview["candidates"] == []
    assert preview["ruled_out"] == []
    assert preview["summary"] == "No scripted fixture matched this document. The ordinary mock returned no findings."
    assert any(item["code"] == "FIXTURE_NOT_MATCHED" for item in preview["warnings"])
    provider = preview["provider"]
    assert isinstance(provider, dict)
    assert provider["scripted"] is False
    assert provider["provider"] == "mock"
    assert provider["live"] is False
    assert _open_ids() == before


def test_demo_pdf_uses_local_text_without_the_fixture_setting(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch, fixture=None)
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_pdf(), _NAME)
    assert preview["provider_name"] == "local-text"
    assert _INTERPRETATION not in str(preview["summary"])
    assert SCRIPTED_MOCK_LABEL not in str(preview["summary"])
    assert "No scripted fixture matched" not in str(preview["summary"])
    provider = preview["provider"]
    assert isinstance(provider, dict)
    assert provider["provider"] == "mock"
    assert provider["live"] is False
    assert provider["scripted"] is False
    assert provider["local_text"] is True
    ruled = preview["ruled_out"]
    assert isinstance(ruled, list)
    assert any("No rail fracture was observed" in str(item.get("excerpt")) for item in ruled)
    candidates = preview["candidates"]
    assert isinstance(candidates, list)
    assert any(
        isinstance(item, dict)
        and isinstance(item.get("evidence"), dict)
        and item["evidence"].get("normalized_defect_type") == "WELD_FAILURE"
        for item in candidates
    )
    assert _open_ids() == []


def test_unknown_fixture_name_does_not_use_the_script(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch, fixture="some_other_report")
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_pdf(), _NAME)
    assert preview["provider_name"] == "local-text"
    assert preview["provider"]["scripted"] is False
    assert preview["provider"]["fixture"] is None
    assert _INTERPRETATION not in str(preview["summary"])
    assert SCRIPTED_MOCK_LABEL not in str(preview["summary"])
    assert any(item["code"] == "FIXTURE_NOT_MATCHED" for item in preview["warnings"])
    ruled = preview["ruled_out"]
    assert isinstance(ruled, list)
    assert any("No rail fracture was observed" in str(item.get("excerpt")) for item in ruled)
    assert _open_ids() == []


def test_explicit_provider_is_not_replaced_by_the_fixture(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    from tests.test_report_apply import _ScriptedProvider

    preview = preview_uploaded_report(_pdf(), _NAME, provider=_ScriptedProvider([]))
    assert preview["candidates"] == []
    assert _INTERPRETATION not in str(preview["summary"])
    assert preview["provider"]["scripted"] is False


def test_nearby_same_type_is_flagged_and_a_different_type_stays_separate(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    neighbor = _make_defect("NEAR-WELD", 66.0, "DN", DefectType.WELD_FAILURE, 70, days_overdue=1.0, has_tsr=False)
    _install_open([neighbor])
    preview = preview_uploaded_report(_pdf(), _NAME)
    d01 = _by_asset(preview, "D-01")
    d02 = _by_asset(preview, "D-02")
    assert d01["status"] == "POSSIBLE_DUPLICATE"
    assert d01["duplicate_of"] == ["NEAR-WELD"]
    assert d01["scheduling_preview"] is not None
    assert d02["status"] == "READY_TO_APPLY"
    assert d02["duplicate_of"] == []
    assert _by_asset(preview, "D-03")["status"] == "NEEDS_REVIEW"
    assert _open_ids() == ["NEAR-WELD"]


def test_ready_records_apply_and_incomplete_records_do_not(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    data_ingestion.clear_last_ingest()
    preview = preview_uploaded_report(_pdf(), _NAME)
    report_id = str(preview["report_id"])
    d01 = _by_asset(preview, "D-01")
    d02 = _by_asset(preview, "D-02")
    d03 = _by_asset(preview, "D-03")
    a01 = _by_asset(preview, "A-01")
    before = _snapshot_json()
    with pytest.raises(ReportRequestError) as missing_tsr:
        apply_stored_report(report_id, {"operation": "append_report_defects", "candidate_ids": [d03["candidate_id"]]})
    assert missing_tsr.value.status_code == 422
    with pytest.raises(ReportRequestError) as unresolved:
        apply_stored_report(report_id, {"operation": "append_report_defects", "candidate_ids": [a01["candidate_id"]]})
    assert unresolved.value.status_code == 422
    with pytest.raises(ReportRequestError) as ruled:
        apply_stored_report(report_id, {"operation": "append_report_defects", "candidate_ids": ["R1"]})
    assert ruled.value.status_code == 400
    assert _open_ids() == []
    assert _snapshot_json() == before
    applied = apply_stored_report(
        report_id,
        {"operation": "append_report_defects", "candidate_ids": [d01["candidate_id"], d02["candidate_id"]]},
    )
    assert applied["status"] == "APPLIED"
    assert applied["operation"] == "append_report_defects"
    assert applied["replaced_defect_ids"] == []
    created = applied["applied_defects"]
    assert isinstance(created, list) and len(created) == 2
    by_type = {item["defect_type"]: item for item in created}
    assert set(by_type) == {"WELD_FAILURE", "TAMPING_REQUIRED"}
    assert by_type["WELD_FAILURE"]["required_block_min"] == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
    assert by_type["TAMPING_REQUIRED"]["required_block_min"] == DEFECT_TYPE_SPEC[DefectType.TAMPING_REQUIRED][0]
    assert by_type["WELD_FAILURE"]["has_tsr"] is True
    assert by_type["TAMPING_REQUIRED"]["has_tsr"] is False
    assert all(str(item["defect_id"]).startswith("AI1-") and item["defect_id"] not in {"D-01", "D-02"} for item in created)
    assert _open_ids() == [item["defect_id"] for item in created]
    with pytest.raises(ReportRequestError) as repeat:
        apply_stored_report(
            report_id,
            {"operation": "append_report_defects", "candidate_ids": [d03["candidate_id"]]},
        )
    assert repeat.value.status_code == 422
    assert _open_ids() == [item["defect_id"] for item in created]
    with TestClient(app_module.app) as client:
        master = client.get("/api/master-data")
    assert master.status_code == 200
    body = master.json()
    tasks = {item["task_id"] for item in body["defects"]}
    assert tasks == {item["defect_id"] for item in created}
    assert all(item["description"] != "Rail Fracture" for item in body["defects"])
    reports = [item for item in body["applied_reports"] if item["report_id"] == report_id]
    assert len(reports) == 1
    assert SCRIPTED_MOCK_LABEL in reports[0]["summary"]
    assert _INTERPRETATION in reports[0]["summary"]
    assert reports[0]["filename"] == _NAME
    assert set(reports[0]["defect_ids"]) == tasks


def test_reviewer_tsr_makes_only_the_signal_record_applicable(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_pdf(), _NAME)
    d03 = _by_asset(preview, "D-03")
    report_id = str(preview["report_id"])
    with pytest.raises(ReportRequestError):
        apply_stored_report(report_id, {"operation": "append_report_defects", "candidate_ids": [d03["candidate_id"]]})
    assert _open_ids() == []
    applied = apply_stored_report(
        report_id,
        {
            "operation": "append_report_defects",
            "candidate_ids": [d03["candidate_id"]],
            "edits": {d03["candidate_id"]: {"has_tsr": True}},
        },
    )
    assert applied["status"] == "APPLIED"
    row = applied["applied_defects"][0]
    assert row["defect_type"] == "SIGNAL_INTERLOCK"
    assert row["km_location"] == 9.4
    assert row["track_line"] == "UP_FAST"
    assert row["severity"] == 63
    assert row["days_overdue"] == 6.0
    assert row["has_tsr"] is True
    assert row["required_block_min"] == DEFECT_TYPE_SPEC[DefectType.SIGNAL_INTERLOCK][0]
    assert d03["fields"]["reported_duration_min"]["value"] is None
    assert _open_ids() == [row["defect_id"]]


def test_possession_blocks_replacement_and_append_keeps_reserved_work(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    hold = _make_defect("FIXTURE-HOLD", 80.0, "UP", DefectType.OHE_SAG, 70, days_overdue=1.0, has_tsr=False)
    _install_open([hold])
    backlog_state.reserve_defects([hold.defect_id])
    preview = preview_uploaded_report(_pdf(), _NAME)
    ready = [item["candidate_id"] for item in preview["candidates"] if item["status"] == "READY_TO_APPLY"]
    assert len(ready) == 2
    with pytest.raises(ReportRequestError) as conflict:
        apply_stored_report(
            str(preview["report_id"]),
            {"operation": "replace_open_backlog", "candidate_ids": ready},
            possession_active=True,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.code == "POSSESSION_CONFLICT"
    assert hold.defect_id in conflict.value.message
    assert _open_ids() == [hold.defect_id]
    session_status = load_preview(str(preview["report_id"]))
    assert session_status is not None
    assert session_status["stored_status"] == "pending"
    applied = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": ready},
        possession_active=True,
    )
    assert applied["status"] == "APPLIED"
    assert hold.defect_id in _open_ids()
    assert hold.defect_id in applied["kept_defect_ids"]
    assert hold.defect_id in backlog_state.scheduled_defect_ids()


def test_failed_apply_rolls_back_the_fixture_report(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_pdf(), _NAME)
    before = _snapshot_json()
    ready = [item["candidate_id"] for item in preview["candidates"] if item["status"] == "READY_TO_APPLY"]

    def explode(_session: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("report_service.commit_session", explode)
    with pytest.raises(ReportRequestError) as failure:
        apply_stored_report(
            str(preview["report_id"]),
            {"operation": "append_report_defects", "candidate_ids": ready},
        )
    assert failure.value.code == "APPLY_FAILED"
    assert "disk full" not in failure.value.message
    assert _open_ids() == []
    assert _snapshot_json() == before
    stored = load_preview(str(preview["report_id"]))
    assert stored is not None
    assert stored["stored_status"] == "pending"


def test_fixture_tests_do_not_call_a_live_provider(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    calls: list[object] = []

    def explode(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("live HTTP was called")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    preview = preview_uploaded_report(_pdf(), _NAME)
    assert preview["provider"]["live"] is False
    assert preview["provider"]["provider"] == "mock"
    assert calls == []
    monkeypatch.setenv("REPORT_AI_PROVIDER", "http")
    monkeypatch.setenv("REPORT_AI_API_KEY", "secret-key")
    with pytest.raises(ProviderNotConfiguredError) as refused:
        preview_uploaded_report(_text_pdf(["Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."]), "other.pdf")
    assert "secret-key" not in str(refused.value)
    assert calls == []


def test_applied_records_feed_the_dashboard_formulas_and_solver(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    _enable(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    data_ingestion.clear_last_ingest()
    catalog = {item.defect_id for item in defects.get_default_defects()}
    completed_before = _completed_ids()
    snapshot_before = _snapshot_json()
    assert _open_ids() == []
    assert catalog.isdisjoint(_open_ids())

    with TestClient(app_module.app) as client:
        empty = client.get("/api/optimize")
    assert empty.status_code == 200
    empty_body = empty.json()
    assert empty_body["status"] == "NO_DEFECTS"
    assert empty_body["candidate_options"] == []
    assert empty_body["target_bundle"] is None
    assert "DEF_" not in empty.text
    assert _open_ids() == []

    with pytest.raises(ReportRequestError):
        preview_uploaded_report(b"this is not a report", "notes.txt")
    assert _open_ids() == []
    assert _completed_ids() == completed_before

    unrelated = preview_uploaded_report(
        _text_pdf(["Drainage note. No maintenance finding is recorded in this file."]),
        _NAME,
    )
    assert unrelated["provider"]["scripted"] is False
    assert unrelated["candidates"] == []
    assert "D-01" not in json.dumps(unrelated["ruled_out"])
    assert _open_ids() == []
    assert _completed_ids() == completed_before

    preview = preview_uploaded_report(_pdf(), _NAME)
    assert preview["provider"]["scripted"] is True
    assert preview["provider"]["live"] is False
    assert preview["provider"]["label"] == SCRIPTED_MOCK_LABEL
    assert str(preview["summary"]).startswith(SCRIPTED_MOCK_LABEL)
    d01 = _by_asset(preview, "D-01")
    d02 = _by_asset(preview, "D-02")
    d03 = _by_asset(preview, "D-03")
    a01 = _by_asset(preview, "A-01")
    assert d01["status"] == "READY_TO_APPLY"
    assert d02["status"] == "READY_TO_APPLY"
    assert d03["status"] == "NEEDS_REVIEW"
    assert d03["missing_fields"] == ["has_tsr"]
    assert d03["scheduling_preview"] is None
    assert a01["status"] == "NEEDS_REVIEW"
    assert a01["scheduling_preview"] is None
    assert a01["fields"]["normalized_defect_type"]["value"] is None
    assert a01["fields"]["severity"]["value"] is None
    ruled = preview["ruled_out"]
    assert isinstance(ruled, list) and len(ruled) == 1
    assert ruled[0]["finding_id"] == "R1"
    assert ruled[0]["status"] == "RULED_OUT"
    assert "N-01" in str(ruled[0]["excerpt"])
    images = preview["source_images"]
    assert isinstance(images, list)
    assert [item["reference"] for item in images] == ["page:1/image:1", "page:2/image:1", "page:3/image:1"]
    assert d01["evidence"]["references"] == ["page:1"]
    assert d02["evidence"]["references"] == ["page:2"]
    assert d03["evidence"]["references"] == ["page:3"]
    assert _open_ids() == []
    assert _completed_ids() == completed_before
    assert _snapshot_json() == snapshot_before

    report_id = str(preview["report_id"])
    for candidate_id, code in (
        (d03["candidate_id"], 422),
        (a01["candidate_id"], 422),
        ("R1", 400),
    ):
        with pytest.raises(ReportRequestError) as refused:
            apply_stored_report(report_id, {"operation": "append_report_defects", "candidate_ids": [candidate_id]})
        assert refused.value.status_code == code
    assert _open_ids() == []
    assert _completed_ids() == completed_before

    applied = apply_stored_report(
        report_id,
        {"operation": "append_report_defects", "candidate_ids": [d01["candidate_id"], d02["candidate_id"]]},
    )
    assert applied["status"] == "APPLIED"
    active = list(data_ingestion.get_effective_defects())
    by_type = {item.defect_type: item for item in active}
    assert set(by_type) == {DefectType.WELD_FAILURE, DefectType.TAMPING_REQUIRED}
    weld = by_type[DefectType.WELD_FAILURE]
    tamping = by_type[DefectType.TAMPING_REQUIRED]
    assert (weld.km_location, weld.track_line, weld.severity, weld.days_overdue, weld.has_tsr) == (67.5, "DN", 84, 22.0, True)
    assert (tamping.km_location, tamping.track_line, tamping.severity, tamping.days_overdue, tamping.has_tsr) == (68.2, "DN", 58, 12.0, False)
    assert weld.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
    assert tamping.required_block_min == DEFECT_TYPE_SPEC[DefectType.TAMPING_REQUIRED][0]
    assert catalog.isdisjoint(item.defect_id for item in active)
    assert _completed_ids() == completed_before

    with TestClient(app_module.app) as client:
        master = client.get("/api/master-data")
    assert master.status_code == 200
    body = master.json()
    rows = {item["task_id"]: item for item in body["defects"]}
    assert set(rows) == {item.defect_id for item in active}
    for defect in active:
        shown = rows[defect.defect_id]
        density = defects.estimate_traffic_density(defect)
        score = defects.compute_priority_urgency_score(
            defect,
            days_overdue=defect.days_overdue,
            has_tsr=defect.has_tsr,
            traffic_density=density,
        )
        assert shown["priority_score"] == round(score)
        assert shown["predicted_days_to_critical"] == round(ml_priority.predict_days_to_critical(defect), 1)
        assert shown["chainage_km"] == defect.km_location
        assert shown["track_line"] == defect.track_line
        assert shown["required_duration_min"] == int(defect.required_block_min)
        assert shown["backlog_status"] == "OPEN"
    expected_bundles = defects.cluster_defects_into_bundles(active)
    shown_groups = [tuple(task["task_id"] for task in bundle["tasks"]) for bundle in body["bundles"]]
    assert shown_groups == [tuple(item.defect_id for item in bundle.defects) for bundle in expected_bundles]
    assert {task_id for group in shown_groups for task_id in group} == {weld.defect_id, tamping.defect_id}

    received: list[tuple[str, ...]] = []
    real_solve = solver.solve_corridor_schedule

    def _record_solver_input(bundles: object, trains: object, **kwargs: object) -> solver.SolverResult:
        assert isinstance(bundles, list)
        received.append(tuple(defect.defect_id for bundle in bundles for defect in bundle.defects))
        for bundle in bundles:
            members = tuple(bundle.defects)
            urgency = max(
                defects.compute_priority_urgency_score(
                    item,
                    days_overdue=item.days_overdue,
                    has_tsr=item.has_tsr,
                    traffic_density=defects.estimate_traffic_density(item),
                    clip=False,
                )
                for item in members
            )
            assert bundle.urgency_score == urgency
        limited = dict(kwargs)
        limited["time_limit_sec"] = 1.0
        return real_solve(bundles, trains, **limited)

    monkeypatch.setattr(solver, "solve_corridor_schedule", _record_solver_input)
    planning = app_module._planning_defects()
    assert [item.defect_id for item in planning] == [item.defect_id for item in active]
    bundled = list(defects.cluster_defects_into_bundles(planning))
    solved = app_module._solve_bundles_for(
        bundled,
        timetable.get_all_trajectories(),
        "ASSET_FIRST",
        300,
        50,
        0,
        1.0,
    )
    assert isinstance(solved, solver.SolverResult)
    assert received == [tuple(item.defect_id for bundle in bundled for item in bundle.defects)]
    assert set(received[0]) == {weld.defect_id, tamping.defect_id}
    assert catalog.isdisjoint(received[0])

    repeat = apply_stored_report(
        report_id,
        {"operation": "append_report_defects", "candidate_ids": [d01["candidate_id"], d02["candidate_id"]]},
    )
    assert repeat["status"] == "ALREADY_APPLIED"
    assert [item.defect_id for item in data_ingestion.get_effective_defects()] == [item.defect_id for item in active]
    assert _completed_ids() == completed_before


def test_portal_labels_the_scripted_mock_without_calling_it_a_live_result() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    assert "SCRIPTED MOCK RESPONSE - NOT A LIVE AI RESULT" in page
    assert "report-scripted-label" in page
    loader = page.split("async function reportLoadProviderStatus()", 1)[1].split("function bindReportReview()", 1)[0]
    assert "status.label" in loader
    assert "report-scripted-label" in loader
    assert "not a live model" in page
    assert "photograph" not in page.casefold()
    assert 'id="secReport"' in page
    assert 'id="tabReport"' in page
    assert "photograph" not in page.casefold()
    assert 'id="secReport"' in page
    assert 'id="tabReport"' in page


def _assert_ready(
    item: dict[str, object],
    defect_type: str,
    department: str,
    line: str,
    chainage: float,
    severity: int,
    overdue: float,
    has_tsr: bool,
    duration: float,
    speed: float | None,
    due: str,
) -> None:
    evidence = item["evidence"]
    fields = item["fields"]
    scheduling = item["scheduling_preview"]
    assert isinstance(evidence, dict) and isinstance(fields, dict) and isinstance(scheduling, dict)
    assert evidence["normalized_defect_type"] == defect_type
    assert evidence["department"] == department
    assert evidence["track_line"] == line
    assert evidence["chainage_km"] == chainage
    assert evidence["severity"] == severity
    assert evidence["days_overdue"] == overdue
    assert evidence["days_overdue_origin"] == "reported"
    assert evidence["has_tsr"] is has_tsr
    assert fields["due_date"]["value"] == due
    assert fields["reported_duration_min"]["value"] == duration
    assert fields["tsr_speed_kmh"] == speed
    assert scheduling["defect_type"] == defect_type
    assert scheduling["required_block_min"] == DEFECT_TYPE_SPEC[DefectType(defect_type)][0]
    assert str(scheduling["defect_id"]).startswith("AI1-")
    assert not item["fields"]["validation_warnings"]
