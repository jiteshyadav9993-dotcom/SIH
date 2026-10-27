"""Gold-schema and evaluator checks. Synthetic counts are not NLP accuracy."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import report_eval
from report_eval import (
    CORPUS_REAL,
    CORPUS_SYNTHETIC,
    DEFECT_TYPES,
    CorpusValidationError,
    DocumentRun,
    Prediction,
    SectionText,
    _claim_allowed,
    evaluate_corpus,
    load_corpus,
    parse_corpus_rows,
    render_summary,
    synthetic_pdf_bytes,
)
ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "eval" / "synthetic" / "corpus.jsonl"
_SPEC = importlib.util.spec_from_file_location("evaluate_reports", ROOT / "tools" / "evaluate_reports.py")
assert _SPEC is not None and _SPEC.loader is not None
_TOOL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_TOOL)
main = _TOOL.main
WELD = "Weld failure at KM 67.5 on the DN line, severity 80, 4 days overdue, no TSR."


def _finding(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "finding_id": "SYN-1-F1",
        "excerpt": WELD,
        "reference": "page:1",
        "page_number": 1,
        "paragraph_reference": None,
        "table_reference": None,
        "defect_type": "WELD_FAILURE",
        "polarity": "positive",
        "chainage_km": 67.5,
        "track_line": "DN",
        "severity": 80,
        "due_date": None,
        "days_overdue": 4,
        "tsr_state": "not_active",
        "rationale": None,
        "confirmation": "confirmed",
    }
    row.update(overrides)
    return row


def _document(
    findings: list[dict[str, object]] | None = None,
    pages: list[dict[str, object]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    chosen = findings if findings is not None else [_finding()]
    if pages is None:
        pages = [{"reference": "page:1", "page_number": 1, "text": str(chosen[0]["excerpt"])}]
    row: dict[str, object] = {
        "schema_version": "ai1-gold-1",
        "dataset_version": "synthetic-evaluator-smoke-1",
        "corpus_kind": CORPUS_SYNTHETIC,
        "document_id": "SYN-1",
        "source_document_id": "SYN-1",
        "source_split": "test",
        "anonymised": True,
        "filename": "syn-1.pdf",
        "source_path": None,
        "pages": pages,
        "findings": chosen,
    }
    row.update(overrides)
    return row


def _prediction(**overrides: object) -> Prediction:
    excerpt = str(overrides.pop("excerpt", WELD))
    values: dict[str, object] = {
        "kind": "defect",
        "preview_status": "READY_TO_APPLY",
        "excerpt": excerpt,
        "locations": (("page:1", 1),),
        "defect_type": "WELD_FAILURE",
        "chainage_km": 67.5,
        "track_line": "DN",
        "severity": 80,
        "due_date": None,
        "days_overdue": 4.0,
        "tsr_state": "not_active",
        "uncertainty": "low",
        "ruled_out_reason": None,
    }
    values.update(overrides)
    return Prediction(**values)  # type: ignore[arg-type]


def _runner(predictions: list[Prediction], text: str = WELD, *, provider: str = "stub") -> object:
    def run(document: object, file_bytes: bytes) -> DocumentRun:
        del document, file_bytes
        return DocumentRun(
            provider_name=provider,
            fixture_id=None,
            predictions=tuple(predictions),
            sections=(SectionText("page:1", 1, text, ""),),
            readable=True,
        )

    return run


def _score(rows: list[dict[str, object]], predictions: list[Prediction], text: str = WELD) -> dict[str, object]:
    corpus = parse_corpus_rows(rows, source_name="memory")
    return evaluate_corpus(corpus, base_dir=ROOT, runner=_runner(predictions, text))  # type: ignore[arg-type]


def test_schema_accepts_a_confirmed_positive_and_rejects_a_new_category() -> None:
    corpus = parse_corpus_rows([_document()], source_name="memory")
    assert corpus.documents[0].findings[0].defect_type == "WELD_FAILURE"
    assert corpus.documents[0].findings[0].chainage_km == 67.5
    broken = _document([_finding(defect_type="FRACTURED_RAIL")])
    with pytest.raises(CorpusValidationError, match="defect_type"):
        parse_corpus_rows([broken], source_name="memory")


def test_schema_accepts_negated_nil_uncertain_and_missing_fields() -> None:
    negated = _document(
        [_finding(
            finding_id="SYN-NEG-F1",
            excerpt="No rail fracture was observed at KM 40.0 on the UP line.",
            defect_type="RAIL_FRACTURE",
            polarity="negated",
            chainage_km=40.0,
            track_line="UP",
            severity=None,
            days_overdue=None,
            tsr_state=None,
        )],
        document_id="SYN-NEG",
        source_document_id="SYN-NEG",
        filename="syn-neg.pdf",
    )
    nil = _document(
        [_finding(
            finding_id="SYN-NIL-F1",
            excerpt="Nil defects were recorded on the MID line during this inspection.",
            defect_type=None,
            polarity="nil",
            chainage_km=None,
            track_line="MID",
            severity=None,
            days_overdue=None,
            tsr_state=None,
        )],
        document_id="SYN-NIL",
        source_document_id="SYN-NIL",
        filename="syn-nil.pdf",
    )
    uncertain = _document(
        [_finding(
            finding_id="SYN-UNC-F1",
            excerpt="Possible weld failure at KM 70.0 on the DN line.",
            polarity="uncertain",
            chainage_km=70.0,
            severity=None,
            days_overdue=None,
            tsr_state=None,
        )],
        document_id="SYN-UNC",
        source_document_id="SYN-UNC",
        filename="syn-unc.pdf",
    )
    partial = _document(
        [_finding(
            finding_id="SYN-PART-F1",
            excerpt="Weld failure on the DN line.",
            chainage_km=None,
            severity=None,
            days_overdue=None,
            tsr_state=None,
        )],
        document_id="SYN-PART",
        source_document_id="SYN-PART",
        filename="syn-part.pdf",
    )
    corpus = parse_corpus_rows([negated, nil, uncertain, partial], source_name="memory")
    polarities = [finding.polarity for document in corpus.documents for finding in document.findings]
    assert polarities == ["negated", "nil", "uncertain", "positive"]
    assert corpus.documents[3].findings[0].chainage_km is None


def test_ambiguous_finding_needs_a_rationale_and_background_uses_no_defect() -> None:
    missing = _document([_finding(confirmation="ambiguous", rationale=None, defect_type=None, polarity="uncertain")])
    with pytest.raises(CorpusValidationError, match="rationale"):
        parse_corpus_rows([missing], source_name="memory")
    background = _document(
        [_finding(
            excerpt="The inspection party completed the walk and recorded the attendance.",
            defect_type="NO_DEFECT",
            polarity="not_applicable",
            chainage_km=None,
            track_line=None,
            severity=None,
            days_overdue=None,
            tsr_state=None,
        )]
    )
    parsed = parse_corpus_rows([background], source_name="memory")
    assert parsed.documents[0].findings[0].defect_type == "NO_DEFECT"
    positive_background = _document([_finding(defect_type="NO_DEFECT")])
    with pytest.raises(CorpusValidationError, match="NO_DEFECT"):
        parse_corpus_rows([positive_background], source_name="memory")


def test_multiple_findings_stay_separate_and_repeated_spans_are_rejected() -> None:
    page = (
        "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR. "
        "OHE sag at KM 150.0 on the UP line, severity 60, 2 days overdue, no TSR."
    )
    first = _finding(
        finding_id="SYN-MULTI-F1",
        excerpt="Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR.",
        chainage_km=80.0,
        severity=84,
        days_overdue=22,
    )
    second = _finding(
        finding_id="SYN-MULTI-F2",
        excerpt="OHE sag at KM 150.0 on the UP line, severity 60, 2 days overdue, no TSR.",
        defect_type="OHE_SAG",
        chainage_km=150.0,
        track_line="UP",
        severity=60,
        days_overdue=2,
    )
    corpus = parse_corpus_rows(
        [_document([first, second], pages=[{"reference": "page:1", "page_number": 1, "text": page}])],
        source_name="memory",
    )
    assert [item.defect_type for item in corpus.documents[0].findings] == ["WELD_FAILURE", "OHE_SAG"]
    clone = dict(first)
    clone["finding_id"] = "SYN-MULTI-F9"
    repeated = _document([first, clone])
    with pytest.raises(CorpusValidationError, match="already annotated"):
        parse_corpus_rows([repeated], source_name="memory")


def test_excerpt_must_be_in_the_synthetic_page_and_demo_files_are_not_authorised() -> None:
    invented = _document(
        [_finding(excerpt="This sentence was not on the page.")],
        pages=[{"reference": "page:1", "page_number": 1, "text": WELD}],
    )
    with pytest.raises(CorpusValidationError, match="exact substring"):
        parse_corpus_rows([invented], source_name="memory")
    demo = _document(
        pages=[],
        corpus_kind=CORPUS_REAL,
        dataset_version="authorised-review-1",
        filename="ai1_railway_maintenance_demo_report_4_pages.pdf",
        source_path="ai1_railway_maintenance_demo_report_4_pages.pdf",
    )
    with pytest.raises(CorpusValidationError, match="fixture"):
        parse_corpus_rows([demo], source_name="memory")


def test_same_source_document_cannot_cross_splits_and_excerpts_cannot_leak() -> None:
    shared = "Deep screening at KM 12.0 on the DN line, severity 50, 1 day overdue, no TSR."
    train = _document(
        [_finding(finding_id="TRAIN-F1", excerpt=shared, defect_type="DEEP_SCREENING", chainage_km=12.0, severity=50, days_overdue=1)],
        document_id="SRC-1-A",
        source_document_id="SRC-1",
        source_split="train",
        filename="train.pdf",
    )
    test_row = _document(
        [_finding(finding_id="TEST-F1")],
        document_id="SRC-1-B",
        source_document_id="SRC-1",
        filename="test.pdf",
    )
    with pytest.raises(CorpusValidationError, match="more than one split"):
        parse_corpus_rows([train, test_row], source_name="memory")
    other = _document(
        [_finding(finding_id="TEST-F1", excerpt=shared, defect_type="DEEP_SCREENING", chainage_km=12.0, severity=50, days_overdue=1)],
        document_id="OTHER",
        source_document_id="OTHER",
        filename="other.pdf",
    )
    with pytest.raises(CorpusValidationError, match="test excerpt"):
        parse_corpus_rows([train, other], source_name="memory")


def test_empty_predictions_leave_rates_undefined_instead_of_perfect() -> None:
    report = _score([_document()], [])
    identification = report["identification"]
    assert isinstance(identification, dict)
    assert identification["true_positive"] == 0
    assert identification["false_positive"] == 0
    assert identification["missed"] == 1
    assert identification["precision"] is None
    assert identification["recall"] == 0
    assert identification["f1"] is None
    chainage = report["fields"]["chainage_km"]  # type: ignore[index]
    assert chainage["missing"] == 1
    assert chainage["incorrect"] == 0
    assert chainage["accuracy"] == 0
    summary = render_summary(report)
    assert "precision: undefined" in summary
    assert "1.000" not in summary
    assert "not a measurement of real-world NLP accuracy" in summary


def test_no_positive_gold_and_no_predictions_are_undefined() -> None:
    row = _document(
        [_finding(
            excerpt="The inspection party completed the walk and recorded the attendance.",
            defect_type="NO_DEFECT",
            polarity="not_applicable",
            chainage_km=None,
            track_line=None,
            severity=None,
            days_overdue=None,
            tsr_state=None,
        )]
    )
    text = str(row["findings"][0]["excerpt"])  # type: ignore[index]
    report = _score([row], [], text)
    identification = report["identification"]
    assert isinstance(identification, dict)
    assert identification["precision"] is None
    assert identification["recall"] is None
    assert identification["f1"] is None
    deep = identification["by_defect_type"]["DEEP_SCREENING"]
    assert deep["precision"] is None
    assert deep["recall"] is None
    assert "identification.precision" in report["undefined_metrics"]
    summary = render_summary(report)
    assert "precision: undefined" in summary
    assert "not treated as perfect performance" in summary


def test_duplicate_predictions_are_not_extra_true_positives() -> None:
    report = _score([_document()], [_prediction(), _prediction()])
    identification = report["identification"]
    duplicates = report["duplicates"]
    assert isinstance(identification, dict)
    assert isinstance(duplicates, dict)
    assert identification["true_positive"] == 1
    assert identification["false_positive"] == 0
    assert duplicates["gold_findings_with_multiple_predictions"] == 1
    assert duplicates["extra_predictions"] == 1


def test_incorrect_chainage_and_line_are_separate_from_missing_values() -> None:
    report = _score([_document()], [_prediction(chainage_km=30.0, track_line="UP")])
    fields = report["fields"]
    assert isinstance(fields, dict)
    assert fields["chainage_km"]["incorrect"] == 1
    assert fields["chainage_km"]["missing"] == 0
    assert fields["chainage_km"]["accuracy"] == 0
    assert fields["chainage_km"]["coverage"] == 1
    assert fields["track_line"]["incorrect"] == 1
    assert fields["severity"]["correct"] == 1
    unlabelled = _document(
        [_finding(excerpt="Weld failure on the DN line.", chainage_km=None, severity=None, days_overdue=None, tsr_state=None)]
    )
    unlabelled_text = "Weld failure on the DN line."
    quiet = _score(
        [unlabelled],
        [_prediction(excerpt=unlabelled_text, chainage_km=30.0, severity=None, days_overdue=None, tsr_state=None)],
        unlabelled_text,
    )
    assert quiet["fields"]["chainage_km"]["labelled"] == 0  # type: ignore[index]
    assert quiet["fields"]["chainage_km"]["incorrect"] == 0  # type: ignore[index]
    assert quiet["fields"]["chainage_km"]["accuracy"] is None  # type: ignore[index]


def test_ready_prediction_on_a_negated_sentence_is_an_operational_false_positive() -> None:
    excerpt = "No rail fracture was observed at KM 40.0 on the UP line."
    row = _document(
        [_finding(
            excerpt=excerpt,
            defect_type="RAIL_FRACTURE",
            polarity="negated",
            chainage_km=40.0,
            track_line="UP",
            severity=None,
            days_overdue=None,
            tsr_state=None,
        )]
    )
    report = _score([row], [_prediction(excerpt=excerpt, defect_type="RAIL_FRACTURE", chainage_km=40.0, track_line="UP")], excerpt)
    assert report["operational_false_positives"]["ready_to_apply"] == 1  # type: ignore[index]
    assert report["polarity"]["false_positive_conversions"]["negated"] == 1  # type: ignore[index]
    assert report["identification"]["false_positive"] == 1  # type: ignore[index]
    assert report["identification"]["true_positive"] == 0  # type: ignore[index]


def test_traceability_checks_exact_excerpt_and_source_reference() -> None:
    good = _score([_document()], [_prediction()])
    assert good["traceability"]["failures"] == 0  # type: ignore[index]
    assert good["traceability"]["exact_substring"] == 1  # type: ignore[index]
    assert good["traceability"]["reference_and_page_match"] == 1  # type: ignore[index]
    bad_excerpt = _score([_document()], [_prediction(excerpt="not the source sentence")])
    assert bad_excerpt["traceability"]["failures"] == 1  # type: ignore[index]
    bad_reference = _score([_document()], [_prediction(locations=(("page:2", 2),))])
    assert bad_reference["traceability"]["failures"] == 1  # type: ignore[index]


def test_gold_excerpt_missing_from_the_read_text_is_not_counted_as_a_miss() -> None:
    corpus = parse_corpus_rows([_document()], source_name="memory")

    def run(document: object, file_bytes: bytes) -> DocumentRun:
        del document, file_bytes
        return DocumentRun("stub", None, (), (SectionText("page:1", 1, "A different page.", ""),), True)

    report = evaluate_corpus(corpus, base_dir=ROOT, runner=run)
    assert report["annotation_errors"] == 1
    assert report["identification"]["missed"] == 0  # type: ignore[index]
    assert report["identification"]["recall"] is None  # type: ignore[index]


def test_multiple_gold_defects_match_separately() -> None:
    page = (
        "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR. "
        "OHE sag at KM 150.0 on the UP line, severity 60, 2 days overdue, no TSR."
    )
    weld = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
    ohe = "OHE sag at KM 150.0 on the UP line, severity 60, 2 days overdue, no TSR."
    row = _document(
        [
            _finding(finding_id="M-F1", excerpt=weld, chainage_km=80.0, severity=84, days_overdue=22),
            _finding(
                finding_id="M-F2",
                excerpt=ohe,
                defect_type="OHE_SAG",
                chainage_km=150.0,
                track_line="UP",
                severity=60,
                days_overdue=2,
            ),
        ],
        pages=[{"reference": "page:1", "page_number": 1, "text": page}],
    )
    report = _score(
        [row],
        [
            _prediction(excerpt=weld, chainage_km=80.0, severity=84, days_overdue=22.0),
            _prediction(excerpt=ohe, defect_type="OHE_SAG", chainage_km=150.0, track_line="UP", severity=60, days_overdue=2.0),
        ],
        page,
    )
    assert report["identification"]["true_positive"] == 2  # type: ignore[index]
    assert report["identification"]["false_positive"] == 0  # type: ignore[index]
    assert report["identification"]["missed"] == 0  # type: ignore[index]


def test_claim_gate_requires_a_large_held_out_authorised_test_split() -> None:
    ready = _authorised_rows(30)
    assert _claim_allowed(parse_corpus_rows(ready, source_name="claim"))[0] is True
    assert _claim_allowed(parse_corpus_rows(ready[:29], source_name="claim"))[0] is False
    leaked = [dict(item) for item in ready]
    leaked[0]["source_split"] = "train"
    assert _claim_allowed(parse_corpus_rows(leaked, source_name="claim"))[0] is False
    synthetic = parse_corpus_rows([_document()], source_name="synthetic")
    allowed, note = _claim_allowed(synthetic)
    assert allowed is False
    assert "not been measured" in note


def test_substituted_runner_cannot_create_an_accuracy_claim(tmp_path: Path) -> None:
    sentence = "Weld failure at KM 10.0 on the DN line."
    (tmp_path / "note.pdf").write_bytes(synthetic_pdf_bytes([sentence]))
    row = _document(
        [_finding(excerpt=sentence, chainage_km=10.0, severity=None, days_overdue=None, tsr_state=None)],
        pages=[],
        corpus_kind=CORPUS_REAL,
        dataset_version="authorised-review-1",
        filename="note.pdf",
        source_path="note.pdf",
    )
    corpus = parse_corpus_rows([row], source_name="note.jsonl")
    report = evaluate_corpus(corpus, base_dir=tmp_path, runner=_runner([], sentence))  # type: ignore[arg-type]
    assert report["real_world_accuracy_measured"] is False
    assert report["claim_note"] == "A substituted runner cannot support a real-world accuracy claim."


def test_default_runner_reads_the_pdf_with_the_demo_fixture_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_AI_MOCK_FIXTURE", "demo_mr_26027_041")
    monkeypatch.setenv("REPORT_AI_PROVIDER", "mock")
    seen: dict[str, object] = {}
    original_read = report_eval.read_report_document
    original_fixture = report_eval.maybe_scripted_fixture

    def spy_read(file_bytes: bytes, filename: str, **kwargs: object) -> object:
        seen["header"] = file_bytes[:5]
        seen["filename"] = filename
        return original_read(file_bytes, filename, **kwargs)  # type: ignore[arg-type]

    def spy_fixture(model: object, bundle: object, environ: object = None) -> object:
        seen["environ"] = dict(environ) if isinstance(environ, dict) else None
        return original_fixture(model, bundle, environ)  # type: ignore[arg-type]

    monkeypatch.setattr(report_eval, "read_report_document", spy_read)
    monkeypatch.setattr(report_eval, "maybe_scripted_fixture", spy_fixture)
    corpus = parse_corpus_rows([_document()], source_name="memory")
    report = evaluate_corpus(corpus, base_dir=ROOT)
    assert seen["header"] == b"%PDF-"
    assert seen["environ"] == {"REPORT_AI_PROVIDER": "mock"}
    assert report["configuration"]["local_pipeline_confirmed"] is True  # type: ignore[index]
    assert report["configuration"]["report_ai_mock_fixture"] is None  # type: ignore[index]
    assert report["configuration"]["fixture_ids"] == []  # type: ignore[index]
    assert report["real_world_accuracy_measured"] is False
    summary = render_summary(report)
    assert WELD not in summary


def test_shipped_synthetic_corpus_is_a_tool_check_only(tmp_path: Path) -> None:
    corpus = load_corpus(SHIPPED)
    assert corpus.corpus_kind == CORPUS_SYNTHETIC
    assert {document.source_split for document in corpus.documents} == {"train", "validation", "test"}
    held_out = corpus.select_split("test")
    assert "SYN-TRAIN-01" not in [document.document_id for document in held_out.documents]
    report = evaluate_corpus(held_out, base_dir=SHIPPED.parent)
    summary = render_summary(report)
    assert report["real_world_accuracy_measured"] is False
    assert report["annotation_errors"] == 0
    assert report["traceability"]["failures"] == 0  # type: ignore[index]
    assert report["configuration"]["local_pipeline_confirmed"] is True  # type: ignore[index]
    assert "Nil defects were recorded" not in summary
    assert "not a measurement of real-world NLP accuracy" in summary
    assert "Baseline evaluation is waiting" in summary
    json_path = tmp_path / "report.json"
    summary_path = tmp_path / "summary.txt"
    assert main(["--corpus", str(SHIPPED), "--split", "test", "--json-out", str(json_path), "--summary-out", str(summary_path)]) == 0
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["real_world_accuracy_measured"] is False
    assert saved["split_filter"] == "test"
    assert "SYN-TRAIN-01" in saved["corpus_document_ids"]
    assert "SYN-TRAIN-01" not in saved["document_ids"]
    assert "Nil defects were recorded" not in json_path.read_text(encoding="utf-8")
    assert "Nil defects were recorded" not in summary_path.read_text(encoding="utf-8")


def test_cli_rejects_a_corpus_that_fails_validation(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    assert main(["--corpus", str(path)]) == 2


def test_excerpts_stay_out_of_the_summary_unless_local_review_is_requested() -> None:
    corpus = parse_corpus_rows([_document()], source_name="memory")

    def run(document: object, file_bytes: bytes) -> DocumentRun:
        del document, file_bytes
        return DocumentRun("stub", None, (), (SectionText("page:1", 1, "other", ""),), True)

    hidden = evaluate_corpus(corpus, base_dir=ROOT, runner=run, include_excerpts=False)
    shown = evaluate_corpus(corpus, base_dir=ROOT, runner=run, include_excerpts=True)
    assert WELD not in json.dumps(hidden)
    local_review = shown["local_review"]
    assert isinstance(local_review, dict)
    assert WELD in json.dumps(local_review)
    assert "Local review only" in str(local_review["warning"])
    assert WELD not in render_summary(shown)


def test_evaluator_does_not_train_a_model() -> None:
    source = (ROOT / "report_eval.py").read_text(encoding="utf-8")
    assert "sklearn" not in source
    assert "joblib" not in source
    assert ".fit(" not in source


def test_authorised_corpus_directory_is_gitignored() -> None:
    import subprocess

    ignored = subprocess.run(
        ["git", "check-ignore", "eval/real/corpus/secret.pdf"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    tracked = subprocess.run(
        ["git", "check-ignore", "eval/synthetic/corpus.jsonl"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0
    assert tracked.returncode != 0


def test_summary_uses_the_measurement_heading_only_when_the_flag_is_set() -> None:
    measured = render_summary(
        {
            "real_world_accuracy_measured": True,
            "accuracy_claim_allowed": True,
            "claim_note": "Held-out test documents meet the minimum size and coverage checks.",
            "identification": {
                "true_positive": 0,
                "false_positive": 0,
                "missed": 0,
                "precision": None,
                "recall": None,
                "f1": None,
                "by_defect_type": {},
            },
        }
    )
    assert "Held-out test split measurement of the existing local NLP." in measured
    assert "not a measurement of real-world NLP accuracy" not in measured
    assert "precision: undefined" in measured


def _authorised_rows(count: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        defect = DEFECT_TYPES[index % len(DEFECT_TYPES)]
        excerpt = f"Packet {index} records {defect} at KM {index + 1}.0 on the DN line."
        findings = [
            _finding(
                finding_id=f"C-{index}-F1",
                excerpt=excerpt,
                defect_type=defect,
                chainage_km=float(index + 1),
                severity=None,
                days_overdue=None,
                tsr_state=None,
            )
        ]
        if index == 0:
            findings.extend(
                [
                    _finding(
                        finding_id="C-0-NEG",
                        excerpt="No rail fracture was observed at KM 2.0 on the UP line.",
                        defect_type="RAIL_FRACTURE",
                        polarity="negated",
                        chainage_km=2.0,
                        track_line="UP",
                        severity=None,
                        days_overdue=None,
                        tsr_state=None,
                    ),
                    _finding(
                        finding_id="C-0-NIL",
                        excerpt="Nil defects were recorded during this authorised packet.",
                        defect_type=None,
                        polarity="nil",
                        chainage_km=None,
                        track_line=None,
                        severity=None,
                        days_overdue=None,
                        tsr_state=None,
                    ),
                    _finding(
                        finding_id="C-0-UNC",
                        excerpt="Possible weld failure at KM 3.0 on the DN line.",
                        polarity="uncertain",
                        chainage_km=3.0,
                        severity=None,
                        days_overdue=None,
                        tsr_state=None,
                    ),
                ]
            )
        rows.append(
            _document(
                findings,
                pages=[],
                corpus_kind=CORPUS_REAL,
                dataset_version="authorised-review-1",
                document_id=f"AUTH-{index}",
                source_document_id=f"AUTH-{index}",
                filename=f"auth-{index}.pdf",
                source_path=f"auth-{index}.pdf",
            )
        )
    return rows
