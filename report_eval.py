"""
report_eval.py
====================================================================
Gold-standard checks and an offline score for the existing local report NLP.

The runner reads each source file with the document reader, then calls the
ordinary local proposer and validator with the scripted demo fixture unset.
It does not train a model, and it does not change extraction rules.
A synthetic corpus exercises this tool. It is not a real-world accuracy claim.
====================================================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from defects import DefectType
from report_documents import DocumentReadError, read_report_document
from report_nlp import local_text_or_keep
from report_pipeline import simplify_report
from report_preview import PreviewStatus, validate_candidates
from report_provider import build_report_provider
from report_schema import UnderstandingStatus, ValueOrigin
from report_test_fixtures import maybe_scripted_fixture

SCHEMA_VERSION = "ai1-gold-1"
EVAL_REPORT_VERSION = "ai1-eval-1"
CORPUS_SYNTHETIC = "synthetic_evaluator_smoke"
CORPUS_REAL = "authorised_real"
CORPUS_KINDS = frozenset({CORPUS_SYNTHETIC, CORPUS_REAL})
SPLITS = frozenset({"train", "validation", "test"})
POLARITIES = frozenset({"positive", "negated", "nil", "uncertain", "not_applicable"})
CONFIRMATIONS = frozenset({"confirmed", "ambiguous"})
TSR_STATES = frozenset({"active", "not_active", "lifted"})
LINE_CODES = frozenset({"DN", "UP", "DN_FAST", "UP_FAST", "DN_SLOW", "UP_SLOW", "MID"})
DEFECT_TYPES: tuple[str, ...] = tuple(item.value for item in DefectType)
DEFECT_TYPE_SET = frozenset(DEFECT_TYPES)
DEFECT_LABELS = DEFECT_TYPE_SET | {"NO_DEFECT"}
OPERATIONAL_STATUSES = frozenset(
    {PreviewStatus.READY_TO_APPLY.value, PreviewStatus.POSSIBLE_DUPLICATE.value}
)
FIELD_NAMES = (
    "defect_type",
    "chainage_km",
    "track_line",
    "severity",
    "due_date",
    "days_overdue",
    "tsr_state",
)
NUMERIC_FIELDS = frozenset({"chainage_km", "severity", "days_overdue"})
EXCERPT_LEAKAGE_MIN_CHARS = 1
MINIMUM_SOURCE_DOCUMENTS_FOR_CLAIM = 30
MINIMUM_TEST_DOCUMENTS_FOR_CLAIM = 10
RECOMMENDED_SOURCE_DOCUMENTS = 100
_NOT_AUTHORISED_NAMES = frozenset(
    {
        "ai1_railway_maintenance_demo_report_4_pages.pdf",
        "tms_defects.csv",
        "tdms_defects.json",
        "smms_defects.csv",
        "coa_block_windows.json",
    }
)
_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "dataset_version",
        "corpus_kind",
        "document_id",
        "source_document_id",
        "source_split",
        "anonymised",
        "filename",
        "source_path",
        "pages",
        "findings",
    }
)
_PAGE_KEYS = frozenset({"reference", "page_number", "text"})
_FINDING_KEYS = frozenset(
    {
        "finding_id",
        "excerpt",
        "reference",
        "page_number",
        "paragraph_reference",
        "table_reference",
        "defect_type",
        "polarity",
        "chainage_km",
        "track_line",
        "severity",
        "due_date",
        "days_overdue",
        "tsr_state",
        "rationale",
        "confirmation",
    }
)
_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


class CorpusValidationError(Exception):
    """The gold file does not match the annotation schema or the split rules."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True, slots=True)
class GoldPage:
    """One page stored with a synthetic document. Real files are not copied here."""

    reference: str
    page_number: int
    text: str


@dataclass(frozen=True, slots=True)
class GoldFinding:
    """One human annotation. Null means the source does not support a value."""

    finding_id: str
    excerpt: str
    reference: str
    page_number: int | None
    paragraph_reference: str | None
    table_reference: str | None
    defect_type: str | None
    polarity: str
    chainage_km: float | None
    track_line: str | None
    severity: int | None
    due_date: str | None
    days_overdue: float | None
    tsr_state: str | None
    rationale: str | None
    confirmation: str


@dataclass(frozen=True, slots=True)
class GoldDocument:
    """One anonymised source document and its findings."""

    schema_version: str
    dataset_version: str
    corpus_kind: str
    document_id: str
    source_document_id: str
    source_split: str
    anonymised: bool
    filename: str
    source_path: str | None
    pages: tuple[GoldPage, ...]
    findings: tuple[GoldFinding, ...]


@dataclass(frozen=True, slots=True)
class Corpus:
    """A validated gold file, or the split selected from that file."""

    documents: tuple[GoldDocument, ...]
    dataset_version: str
    schema_version: str
    corpus_kind: str
    source_name: str

    def select_split(self, split: str) -> Corpus:
        """Keep every document, or only one source split. Splits are not mixed later."""
        if split == "all":
            return self
        if split not in SPLITS:
            raise CorpusValidationError((f"Unknown split {split!r}.",))
        chosen = tuple(document for document in self.documents if document.source_split == split)
        return Corpus(
            documents=chosen,
            dataset_version=self.dataset_version,
            schema_version=self.schema_version,
            corpus_kind=self.corpus_kind,
            source_name=self.source_name,
        )


@dataclass(frozen=True, slots=True)
class SectionText:
    """Text the document reader actually returned for one reference."""

    reference: str
    page_number: int | None
    native_text: str
    ocr_text: str


@dataclass(frozen=True, slots=True)
class Prediction:
    """One finding the existing pipeline returned. Status is the preview status."""

    kind: str
    preview_status: str
    excerpt: str
    locations: tuple[tuple[str, int | None], ...]
    defect_type: str | None
    chainage_km: float | None
    track_line: str | None
    severity: int | None
    due_date: str | None
    days_overdue: float | None
    tsr_state: str | None
    uncertainty: str | None
    ruled_out_reason: str | None


@dataclass(frozen=True, slots=True)
class DocumentRun:
    """Reader output plus pipeline predictions for one gold document."""

    provider_name: str
    fixture_id: str | None
    predictions: tuple[Prediction, ...]
    sections: tuple[SectionText, ...]
    readable: bool


Runner = Callable[[GoldDocument, bytes], DocumentRun]


def evaluation_environ() -> dict[str, str]:
    """Provider settings for a score run. The demo fixture is intentionally absent."""
    return {"REPORT_AI_PROVIDER": "mock"}


class _UnavailableOcr:
    """Keep evaluation on native text. A scanned page stays unread rather than invented."""

    def available(self) -> bool:
        return False

    def read_image(self, image: object) -> str:
        raise DocumentReadError("The evaluation runner does not read images with OCR.")


def synthetic_pdf_bytes(pages: Sequence[str]) -> bytes:
    """Build a small text PDF so the synthetic fixture can pass through the real reader."""
    if not pages:
        raise ValueError("A synthetic PDF needs at least one page.")
    for page in pages:
        if not isinstance(page, str) or page == "":
            raise ValueError("A synthetic page must be non-empty text.")
        if any(ord(character) < 32 or ord(character) > 126 for character in page):
            raise ValueError("Synthetic page text must be printable ASCII so the reader can round-trip it.")
    font_id = 3 + (2 * len(pages))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Count {len(pages)} /Kids ["
            + " ".join(f"{3 + (2 * index)} 0 R" for index in range(len(pages)))
            + "] >>"
        ).encode("ascii"),
    ]
    for index, text in enumerate(pages):
        page_id = 3 + (2 * index)
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode("ascii")
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    chunks = [header]
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_at = sum(len(chunk) for chunk in chunks)
    xref = [f"xref\n0 {len(objects) + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for offset in offsets:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return b"".join(chunks + xref + [trailer])


def load_corpus(path: Path) -> Corpus:
    """Read one JSONL gold file and reject schema, leakage, and split errors together."""
    source = Path(path)
    if not source.is_file():
        raise CorpusValidationError((f"{source} does not exist.",))
    try:
        raw_lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CorpusValidationError((f"Could not read {source.name}.",)) from exc
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    seen_payload = False
    for line_number, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        seen_payload = True
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"Line {line_number} is not a JSON object.")
            continue
        if not isinstance(payload, dict):
            errors.append(f"Line {line_number} is not a JSON object.")
            continue
        rows.append(payload)
    if not seen_payload:
        errors.append("The corpus file has no documents.")
    if errors:
        raise CorpusValidationError(errors)
    return parse_corpus_rows(rows, source_name=source.name)


def parse_corpus_rows(rows: Sequence[Mapping[str, object]], *, source_name: str = "memory") -> Corpus:
    """Validate gold rows. `source_name` is a label, not a path to a report."""
    errors: list[str] = []
    documents: list[GoldDocument] = []
    for index, row in enumerate(rows, start=1):
        document = _parse_document(row, index, errors)
        if document is not None:
            documents.append(document)
    if documents:
        _validate_collection(documents, errors)
    elif not errors:
        errors.append("The corpus file has no documents.")
    if errors:
        raise CorpusValidationError(errors)
    first = documents[0]
    return Corpus(
        documents=tuple(documents),
        dataset_version=first.dataset_version,
        schema_version=first.schema_version,
        corpus_kind=first.corpus_kind,
        source_name=source_name,
    )


def evaluate_corpus(
    corpus: Corpus,
    *,
    base_dir: Path,
    include_excerpts: bool = False,
    runner: Runner | None = None,
) -> dict[str, object]:
    """Score `corpus` with the local pipeline unless a test supplies another runner."""
    used_default_runner = runner is None
    active_runner = runner if runner is not None else run_local_pipeline
    per_document: list[_DocumentScore] = []
    provider_names: list[str] = []
    fixture_ids: list[str] = []
    for document in corpus.documents:
        try:
            file_bytes = _document_bytes(document, base_dir)
        except (OSError, ValueError, CorpusValidationError):
            per_document.append(_unreadable(document))
            continue
        try:
            run = active_runner(document, file_bytes)
        except (DocumentReadError, OSError, ValueError):
            per_document.append(_unreadable(document))
            continue
        provider_names.append(run.provider_name)
        if run.fixture_id:
            fixture_ids.append(run.fixture_id)
        per_document.append(_score_document(document, run))
    return _aggregate(
        corpus,
        per_document,
        provider_names=provider_names,
        fixture_ids=fixture_ids,
        include_excerpts=include_excerpts,
        used_default_runner=used_default_runner,
    )


def run_local_pipeline(document: GoldDocument, file_bytes: bytes) -> DocumentRun:
    """Read `file_bytes` and run local NLP. The process environment cannot select the demo fixture."""
    environ = evaluation_environ()
    bundle = read_report_document(file_bytes, document.filename, ocr=_UnavailableOcr())
    model = build_report_provider(environ)
    model = maybe_scripted_fixture(model, bundle, environ)
    model = local_text_or_keep(model)
    understanding = simplify_report(bundle, model)
    preview = validate_candidates(understanding, existing_defects=(), as_of=None)
    predictions = _predictions_from_preview(understanding, preview)
    sections = tuple(
        SectionText(
            reference=section.reference,
            page_number=section.page_number,
            native_text=section.native_text,
            ocr_text=section.ocr_text,
        )
        for section in bundle.sections
    )
    fixture_id = getattr(model, "scripted_fixture", None)
    fixture_text = fixture_id if isinstance(fixture_id, str) and fixture_id.strip() else None
    readable = understanding.status is not UnderstandingStatus.FAILED
    return DocumentRun(
        provider_name=str(understanding.provider_name),
        fixture_id=fixture_text,
        predictions=tuple(predictions) if readable else (),
        sections=sections,
        readable=readable,
    )


def render_summary(report: Mapping[str, object]) -> str:
    """Human-readable totals. Source excerpts are never printed here."""
    lines = [
        "AI-1 local NLP evaluation",
        f"Report version: {report.get('report_version')}",
        f"Dataset version: {report.get('dataset_version')}",
        f"Corpus kind: {report.get('corpus_kind')}",
        f"Corpus file: {report.get('source_name')}",
        f"Split filter: {report.get('split_filter')}",
        f"Scored documents: {report.get('documents_scored')}",
        f"Real-world accuracy measured: {'yes' if report.get('real_world_accuracy_measured') else 'no'}",
        f"Accuracy claim allowed: {'yes' if report.get('accuracy_claim_allowed') else 'no'}",
        f"Claim note: {report.get('claim_note')}",
        "",
    ]
    if report.get("real_world_accuracy_measured") is True:
        lines.append("Held-out test split measurement of the existing local NLP.")
    else:
        lines.append(
            "These worksheet counts are not a measurement of real-world NLP accuracy. "
            "Baseline evaluation is waiting for a human-annotated authorised corpus "
            f"of at least {MINIMUM_SOURCE_DOCUMENTS_FOR_CLAIM} source documents "
            f"({RECOMMENDED_SOURCE_DOCUMENTS} recommended), with at least "
            f"{MINIMUM_TEST_DOCUMENTS_FOR_CLAIM} documents held out in the test split."
        )
    lines.extend(["", "Identification"])
    identification = _mapping(report.get("identification"))
    lines.extend(_rate_lines(identification, ("true_positive", "false_positive", "missed", "precision", "recall", "f1")))
    lines.append("By defect type:")
    by_type = _mapping(identification.get("by_defect_type"))
    for name in DEFECT_TYPES:
        bucket = _mapping(by_type.get(name))
        lines.append(
            f"  {name}: tp={bucket.get('true_positive')} fp={bucket.get('false_positive')} "
            f"missed={bucket.get('missed')} precision={_show(bucket.get('precision'))} "
            f"recall={_show(bucket.get('recall'))} f1={_show(bucket.get('f1'))}"
        )
    lines.extend(["", "Gold counts"])
    gold_counts = _mapping(report.get("gold_counts"))
    for name in ("findings", "confirmed", "ambiguous", "positive", "negated", "nil", "uncertain", "not_applicable", "no_defect"):
        lines.append(f"  {name}: {gold_counts.get(name)}")
    lines.extend(["", "Polarity"])
    polarity = _mapping(report.get("polarity"))
    lines.append(f"  correct: {json.dumps(polarity.get('correct'), sort_keys=True)}")
    lines.append(f"  missed: {json.dumps(polarity.get('missed'), sort_keys=True)}")
    lines.append(f"  confused: {json.dumps(polarity.get('confused'), sort_keys=True)}")
    lines.append(
        "  false-positive conversions: "
        + json.dumps(polarity.get("false_positive_conversions"), sort_keys=True)
    )
    operational = _mapping(report.get("operational_false_positives"))
    lines.extend(
        [
            "",
            "Operational false positives",
            f"  ready_to_apply: {operational.get('ready_to_apply')}",
            f"  possible_duplicate: {operational.get('possible_duplicate')}",
            "",
            "Field extraction",
        ]
    )
    fields = _mapping(report.get("fields"))
    for name in FIELD_NAMES:
        bucket = _mapping(fields.get(name))
        lines.append(
            f"  {name}: labelled={bucket.get('labelled')} correct={bucket.get('correct')} "
            f"incorrect={bucket.get('incorrect')} missing={bucket.get('missing')} "
            f"accuracy={_show(bucket.get('accuracy'))} coverage={_show(bucket.get('coverage'))}"
        )
    trace = _mapping(report.get("traceability"))
    duplicates = _mapping(report.get("duplicates"))
    lines.extend(
        [
            "",
            "Traceability",
            f"  predictions: {trace.get('predictions')}",
            f"  exact_substring: {trace.get('exact_substring')}",
            f"  reference_and_page_match: {trace.get('reference_and_page_match')}",
            f"  failures: {trace.get('failures')}",
            "",
            "Duplicates",
            f"  gold_findings_with_multiple_predictions: {duplicates.get('gold_findings_with_multiple_predictions')}",
            f"  extra_predictions: {duplicates.get('extra_predictions')}",
            "",
            "Undefined metrics",
        ]
    )
    undefined = report.get("undefined_metrics")
    if isinstance(undefined, list) and undefined:
        lines.extend(f"  {item}" for item in undefined)
    else:
        lines.append("  none")
    lines.extend(["", "Limitations"])
    limitations = report.get("limitations")
    if isinstance(limitations, list) and limitations:
        lines.extend(f"  {item}" for item in limitations)
    else:
        lines.append("  none")
    if report.get("include_excerpts") is True:
        lines.extend(["", "Excerpts were written only to the JSON local_review section."])
    return "\n".join(lines) + "\n"


def _parse_document(row: Mapping[str, object], index: int, errors: list[str]) -> GoldDocument | None:
    label = f"Document {index}"
    if set(row) != _DOCUMENT_KEYS:
        errors.append(f"{label} must contain exactly the documented keys.")
        return None
    schema_version = _text(row.get("schema_version"), f"{label} schema_version", errors)
    dataset_version = _text(row.get("dataset_version"), f"{label} dataset_version", errors)
    corpus_kind = _choice(row.get("corpus_kind"), CORPUS_KINDS, f"{label} corpus_kind", errors)
    document_id = _identifier(row.get("document_id"), f"{label} document_id", errors)
    source_document_id = _identifier(row.get("source_document_id"), f"{label} source_document_id", errors)
    source_split = _choice(row.get("source_split"), SPLITS, f"{label} source_split", errors)
    anonymised = row.get("anonymised")
    if anonymised is not True:
        errors.append(f"{label} anonymised must be true.")
    filename = _filename(row.get("filename"), f"{label} filename", errors)
    source_path = _source_path(row.get("source_path"), corpus_kind, filename, label, errors)
    pages = _pages(row.get("pages"), corpus_kind, label, errors)
    findings = _findings(row.get("findings"), pages, corpus_kind, label, errors)
    if (
        schema_version is None
        or dataset_version is None
        or corpus_kind is None
        or document_id is None
        or source_document_id is None
        or source_split is None
        or filename is None
        or pages is None
        or findings is None
        or anonymised is not True
    ):
        return None
    if schema_version != SCHEMA_VERSION:
        errors.append(f"{label} schema_version must be {SCHEMA_VERSION}.")
        return None
    if corpus_kind == CORPUS_REAL and dataset_version.startswith("synthetic"):
        errors.append(f"{label} uses a synthetic dataset version for an authorised corpus.")
        return None
    if filename in _NOT_AUTHORISED_NAMES and corpus_kind == CORPUS_REAL:
        errors.append(f"{label} filename {filename} is a fixture, not an authorised report.")
        return None
    return GoldDocument(
        schema_version=schema_version,
        dataset_version=dataset_version,
        corpus_kind=corpus_kind,
        document_id=document_id,
        source_document_id=source_document_id,
        source_split=source_split,
        anonymised=True,
        filename=filename,
        source_path=source_path,
        pages=pages,
        findings=findings,
    )


def _pages(
    value: object,
    corpus_kind: str | None,
    label: str,
    errors: list[str],
) -> tuple[GoldPage, ...] | None:
    if not isinstance(value, list):
        errors.append(f"{label} pages must be a list.")
        return None
    if corpus_kind == CORPUS_REAL:
        if value:
            errors.append(f"{label} must not copy report text into pages. Keep the file outside git.")
        return ()
    if corpus_kind == CORPUS_SYNTHETIC and not value:
        errors.append(f"{label} synthetic documents must include page text.")
        return None
    pages: list[GoldPage] = []
    references: set[str] = set()
    for page_index, item in enumerate(value, start=1):
        page_label = f"{label} page {page_index}"
        if not isinstance(item, dict) or set(item) != _PAGE_KEYS:
            errors.append(f"{page_label} must contain reference, page_number, and text.")
            continue
        reference = _text(item.get("reference"), f"{page_label} reference", errors)
        page_number = item.get("page_number")
        text = item.get("text")
        if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
            errors.append(f"{page_label} page_number must be a positive integer.")
            page_number = None
        if not isinstance(text, str) or text == "":
            errors.append(f"{page_label} text must be the exact synthetic page text.")
            text = None
        elif any(ord(character) < 32 or ord(character) > 126 for character in text):
            errors.append(f"{page_label} text must be printable ASCII.")
            text = None
        if reference is not None and page_number is not None and reference != f"page:{page_number}":
            errors.append(f"{page_label} reference must be page:{page_number}.")
        if reference is not None and reference in references:
            errors.append(f"{page_label} repeats reference {reference}.")
        if reference is not None:
            references.add(reference)
        if reference is None or page_number is None or text is None:
            continue
        pages.append(GoldPage(reference=reference, page_number=page_number, text=text))
    if corpus_kind == CORPUS_SYNTHETIC and len(pages) != len(value):
        return None
    return tuple(pages)


def _findings(
    value: object,
    pages: tuple[GoldPage, ...] | None,
    corpus_kind: str | None,
    label: str,
    errors: list[str],
) -> tuple[GoldFinding, ...] | None:
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must contain at least one finding.")
        return None
    findings: list[GoldFinding] = []
    seen_ids: set[str] = set()
    seen_spans: set[tuple[str, str, str | None, str]] = set()
    page_text = {page.reference: page.text for page in pages or ()}
    for finding_index, item in enumerate(value, start=1):
        finding_label = f"{label} finding {finding_index}"
        parsed = _parse_finding(item, finding_label, page_text, corpus_kind, errors)
        if parsed is None:
            continue
        if parsed.finding_id in seen_ids:
            errors.append(f"{finding_label} repeats finding_id {parsed.finding_id}.")
            continue
        span = (parsed.reference, parsed.excerpt, parsed.defect_type, parsed.polarity)
        if span in seen_spans:
            errors.append(f"{finding_label} repeats a finding that is already annotated.")
            continue
        seen_ids.add(parsed.finding_id)
        seen_spans.add(span)
        findings.append(parsed)
    if len(findings) != len(value):
        return None
    return tuple(findings)


def _parse_finding(
    item: object,
    label: str,
    page_text: Mapping[str, str],
    corpus_kind: str | None,
    errors: list[str],
) -> GoldFinding | None:
    if not isinstance(item, dict) or set(item) != _FINDING_KEYS:
        errors.append(f"{label} must contain exactly the documented finding keys.")
        return None
    finding_id = _identifier(item.get("finding_id"), f"{label} finding_id", errors)
    excerpt = item.get("excerpt")
    reference = _text(item.get("reference"), f"{label} reference", errors)
    if not isinstance(excerpt, str) or excerpt.strip() == "":
        errors.append(f"{label} excerpt must be the exact source span.")
        excerpt = None
    page_number = _optional_int(item.get("page_number"), f"{label} page_number", errors)
    paragraph_reference = _optional_text(item.get("paragraph_reference"), f"{label} paragraph_reference", errors)
    table_reference = _optional_text(item.get("table_reference"), f"{label} table_reference", errors)
    defect_type = _optional_choice(item.get("defect_type"), DEFECT_LABELS, f"{label} defect_type", errors)
    polarity = _choice(item.get("polarity"), POLARITIES, f"{label} polarity", errors)
    chainage_km = _optional_float(item.get("chainage_km"), f"{label} chainage_km", errors)
    track_line = _optional_choice(item.get("track_line"), LINE_CODES, f"{label} track_line", errors)
    severity = _optional_int(item.get("severity"), f"{label} severity", errors)
    due_date = _optional_text(item.get("due_date"), f"{label} due_date", errors)
    days_overdue = _optional_float(item.get("days_overdue"), f"{label} days_overdue", errors)
    tsr_state = _optional_choice(item.get("tsr_state"), TSR_STATES, f"{label} tsr_state", errors)
    rationale = _optional_text(item.get("rationale"), f"{label} rationale", errors)
    confirmation = _choice(item.get("confirmation"), CONFIRMATIONS, f"{label} confirmation", errors)
    if excerpt is not None and reference is not None and corpus_kind == CORPUS_SYNTHETIC:
        if reference not in page_text or excerpt not in page_text[reference]:
            errors.append(f"{label} excerpt is not an exact substring of its synthetic page.")
    _check_location(reference, page_number, paragraph_reference, table_reference, label, errors)
    _check_polarity_shape(defect_type, polarity, confirmation, rationale, label, errors)
    if (
        finding_id is None
        or not isinstance(excerpt, str)
        or reference is None
        or polarity is None
        or confirmation is None
    ):
        return None
    if confirmation == "ambiguous" and (rationale is None or rationale == ""):
        return None
    return GoldFinding(
        finding_id=finding_id,
        excerpt=excerpt,
        reference=reference,
        page_number=page_number,
        paragraph_reference=paragraph_reference,
        table_reference=table_reference,
        defect_type=defect_type,
        polarity=polarity,
        chainage_km=chainage_km,
        track_line=track_line,
        severity=severity,
        due_date=due_date,
        days_overdue=days_overdue,
        tsr_state=tsr_state,
        rationale=rationale,
        confirmation=confirmation,
    )


def _check_location(
    reference: str | None,
    page_number: int | None,
    paragraph_reference: str | None,
    table_reference: str | None,
    label: str,
    errors: list[str],
) -> None:
    if paragraph_reference is not None and table_reference is not None:
        errors.append(f"{label} cannot be both a paragraph and a table.")
    if paragraph_reference is not None and paragraph_reference != reference:
        errors.append(f"{label} paragraph_reference must match reference.")
    if table_reference is not None and table_reference != reference:
        errors.append(f"{label} table_reference must match reference.")
    if page_number is not None and paragraph_reference is None and table_reference is None:
        if reference != f"page:{page_number}":
            errors.append(f"{label} reference must be page:{page_number}.")
    if page_number is None and paragraph_reference is None and table_reference is None:
        errors.append(f"{label} needs a page, paragraph, or table reference.")


def _check_polarity_shape(
    defect_type: str | None,
    polarity: str | None,
    confirmation: str | None,
    rationale: str | None,
    label: str,
    errors: list[str],
) -> None:
    if polarity == "positive" and defect_type == "NO_DEFECT":
        errors.append(f"{label} positive finding cannot use NO_DEFECT.")
    if polarity == "not_applicable" and defect_type not in {None, "NO_DEFECT"}:
        errors.append(f"{label} not_applicable finding must use NO_DEFECT or null.")
    if polarity in {"negated", "nil", "uncertain"} and defect_type == "NO_DEFECT":
        errors.append(f"{label} {polarity} finding cannot use NO_DEFECT.")
    if confirmation == "ambiguous" and (rationale is None or rationale == ""):
        errors.append(f"{label} ambiguous finding needs a rationale.")


def _validate_collection(documents: Sequence[GoldDocument], errors: list[str]) -> None:
    versions = {document.dataset_version for document in documents}
    kinds = {document.corpus_kind for document in documents}
    schemas = {document.schema_version for document in documents}
    if len(versions) != 1:
        errors.append("Every document must share one dataset_version.")
    if len(kinds) != 1:
        errors.append("Every document must share one corpus_kind.")
    if len(schemas) != 1:
        errors.append("Every document must share one schema_version.")
    document_ids: set[str] = set()
    finding_ids: set[str] = set()
    split_by_source: dict[str, str] = {}
    for document in documents:
        if document.document_id in document_ids:
            errors.append(f"document_id {document.document_id} is repeated.")
        document_ids.add(document.document_id)
        previous_split = split_by_source.get(document.source_document_id)
        if previous_split is not None and previous_split != document.source_split:
            errors.append(
                f"source_document_id {document.source_document_id} appears in more than one split."
            )
        split_by_source[document.source_document_id] = document.source_split
        for finding in document.findings:
            if finding.finding_id in finding_ids:
                errors.append(f"finding_id {finding.finding_id} is repeated.")
            finding_ids.add(finding.finding_id)
    pages_by_split: dict[str, list[str]] = {split: [] for split in SPLITS}
    excerpts_by_split: dict[str, list[str]] = {split: [] for split in SPLITS}
    for document in documents:
        for page in document.pages:
            pages_by_split[document.source_split].append(page.text)
        for finding in document.findings:
            excerpts_by_split[document.source_split].append(finding.excerpt)
    for left, right in (("test", "train"), ("test", "validation"), ("validation", "train")):
        for excerpt in excerpts_by_split[left]:
            if len(excerpt) < EXCERPT_LEAKAGE_MIN_CHARS:
                continue
            if any(excerpt in page for page in pages_by_split[right]):
                errors.append(f"A {left} excerpt also appears in a {right} page.")
        for page in pages_by_split[left]:
            if page in pages_by_split[right]:
                errors.append(f"The same page text appears in both {left} and {right}.")


def _document_bytes(document: GoldDocument, base_dir: Path) -> bytes:
    if document.corpus_kind == CORPUS_SYNTHETIC:
        return synthetic_pdf_bytes(tuple(page.text for page in document.pages))
    if document.source_path is None:
        raise CorpusValidationError(("An authorised document is missing source_path.",))
    root = base_dir.resolve()
    candidate = (root / document.source_path).resolve()
    if not candidate.is_relative_to(root):
        raise CorpusValidationError((f"{document.document_id} source_path leaves the corpus directory.",))
    if not candidate.is_file():
        raise CorpusValidationError((f"{document.document_id} source file is missing.",))
    return candidate.read_bytes()


def _predictions_from_preview(understanding: object, preview: object) -> list[Prediction]:
    candidates = {item.candidate_id: item for item in understanding.candidates}  # type: ignore[attr-defined]
    ruled = {item.finding_id: item for item in understanding.ruled_out}  # type: ignore[attr-defined]
    predictions: list[Prediction] = []
    for record in preview.records:  # type: ignore[attr-defined]
        evidence = record.evidence
        locations = tuple(zip(evidence.references, evidence.page_numbers))
        status = record.status.value
        candidate = candidates.get(record.source_finding_id)
        ruled_item = ruled.get(record.source_finding_id)
        if status == PreviewStatus.RULED_OUT.value:
            reason = ruled_item.reason if ruled_item is not None else _reason_code(record.reasons)
            predictions.append(
                Prediction(
                    kind="ruled_out",
                    preview_status=status,
                    excerpt=evidence.excerpt,
                    locations=locations,
                    defect_type=None,
                    chainage_km=None,
                    track_line=None,
                    severity=None,
                    due_date=None,
                    days_overdue=None,
                    tsr_state="lifted" if reason == "tsr_lifted" else None,
                    uncertainty=None,
                    ruled_out_reason=reason,
                )
            )
            continue
        due_date = None
        uncertainty = None
        if candidate is not None:
            if candidate.due_date.origin is not ValueOrigin.UNKNOWN and candidate.due_date.value is not None:
                due_date = str(candidate.due_date.value)
            uncertainty = candidate.uncertainty.value
        predictions.append(
            Prediction(
                kind="defect",
                preview_status=status,
                excerpt=evidence.excerpt,
                locations=locations,
                defect_type=_known_text(evidence.normalized_defect_type, evidence.defect_type_origin),
                chainage_km=_known_float(evidence.chainage_km, evidence.chainage_origin),
                track_line=_known_text(evidence.track_line, evidence.track_line_origin),
                severity=_known_int(evidence.severity, evidence.severity_origin),
                due_date=due_date,
                days_overdue=_known_float(evidence.days_overdue, evidence.days_overdue_origin),
                tsr_state=_tsr_state(evidence.has_tsr, evidence.has_tsr_origin),
                uncertainty=uncertainty,
                ruled_out_reason=None,
            )
        )
    return predictions


def _known_text(value: object, origin: object) -> str | None:
    if origin is ValueOrigin.UNKNOWN or not isinstance(value, str) or not value.strip():
        return None
    return value


def _known_float(value: object, origin: object) -> float | None:
    if origin is ValueOrigin.UNKNOWN or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _known_int(value: object, origin: object) -> int | None:
    if origin is ValueOrigin.UNKNOWN or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _tsr_state(value: object, origin: object) -> str | None:
    if origin is ValueOrigin.UNKNOWN or not isinstance(value, bool):
        return None
    return "active" if value else "not_active"


def _reason_code(reasons: Sequence[str]) -> str | None:
    if not reasons:
        return None
    return reasons[0].split(" Source:", 1)[0].strip() or None


@dataclass
class _FieldTally:
    labelled: int = 0
    correct: int = 0
    incorrect: int = 0
    missing: int = 0

    def add(self, outcome: str | None) -> None:
        if outcome is None:
            return
        self.labelled += 1
        if outcome == "correct":
            self.correct += 1
        elif outcome == "incorrect":
            self.incorrect += 1
        else:
            self.missing += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "labelled": self.labelled,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "missing": self.missing,
            "accuracy": _ratio(self.correct, self.labelled),
            "coverage": _ratio(self.correct + self.incorrect, self.labelled),
        }


@dataclass
class _TypeTally:
    true_positive: int = 0
    false_positive: int = 0
    missed: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "missed": self.missed,
            "precision": _ratio(self.true_positive, self.true_positive + self.false_positive),
            "recall": _ratio(self.true_positive, self.true_positive + self.missed),
            "f1": _f1(
                _ratio(self.true_positive, self.true_positive + self.false_positive),
                _ratio(self.true_positive, self.true_positive + self.missed),
            ),
        }


@dataclass
class _DocumentScore:
    document_id: str
    readable: bool
    gold_counts: dict[str, int]
    true_positive: int = 0
    false_positive: int = 0
    missed: int = 0
    untyped_false_positives: int = 0
    operational_ready: int = 0
    operational_possible_duplicate: int = 0
    duplicate_findings: int = 0
    extra_predictions: int = 0
    trace_predictions: int = 0
    trace_exact: int = 0
    trace_reference: int = 0
    trace_failures: int = 0
    annotation_errors: int = 0
    polarity_correct: dict[str, int] | None = None
    polarity_missed: dict[str, int] | None = None
    polarity_confused: dict[str, int] | None = None
    conversions: dict[str, int] | None = None
    types: dict[str, _TypeTally] | None = None
    fields: dict[str, _FieldTally] | None = None
    local_review: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.polarity_correct is None:
            self.polarity_correct = {name: 0 for name in POLARITIES}
        if self.polarity_missed is None:
            self.polarity_missed = {name: 0 for name in POLARITIES}
        if self.polarity_confused is None:
            self.polarity_confused = {name: 0 for name in POLARITIES}
        if self.conversions is None:
            self.conversions = {name: 0 for name in ("negated", "nil", "uncertain", "not_applicable")}
        if self.types is None:
            self.types = {name: _TypeTally() for name in DEFECT_TYPES}
        if self.fields is None:
            self.fields = {name: _FieldTally() for name in FIELD_NAMES}
        if self.local_review is None:
            self.local_review = []


def _unreadable(document: GoldDocument) -> _DocumentScore:
    return _DocumentScore(
        document_id=document.document_id,
        readable=False,
        gold_counts=_empty_gold_counts(),
        annotation_errors=len(document.findings),
    )


def _score_document(document: GoldDocument, run: DocumentRun) -> _DocumentScore:
    score = _DocumentScore(
        document_id=document.document_id,
        readable=run.readable,
        gold_counts=_gold_counts(document),
    )
    if not run.readable:
        score.annotation_errors = len(document.findings)
        return score
    scoreable: list[GoldFinding] = []
    for finding in document.findings:
        if finding.confirmation != "confirmed":
            continue
        if _gold_located(finding, run.sections):
            scoreable.append(finding)
            continue
        score.annotation_errors += 1
        score.local_review.append(
            {"document_id": document.document_id, "finding_id": finding.finding_id, "excerpt": finding.excerpt}
        )
    positives = [finding for finding in scoreable if finding.polarity == "positive"]
    ambiguous = [finding for finding in document.findings if finding.confirmation == "ambiguous"]
    defect_predictions = [item for item in run.predictions if item.kind == "defect"]
    assigned, used = _assign(positives, defect_predictions, require_type=True)
    extra_used = set(used)
    matched_gold: dict[int, list[int]] = {}
    for gold_index, prediction_index in assigned:
        matched_gold.setdefault(gold_index, []).append(prediction_index)
    for gold_index, _prediction_index in assigned:
        for prediction_index, prediction in enumerate(defect_predictions):
            if prediction_index in extra_used:
                continue
            if _align_score(prediction, positives[gold_index], require_type=True) is None:
                continue
            matched_gold.setdefault(gold_index, []).append(prediction_index)
            extra_used.add(prediction_index)
    for gold_index, prediction_indexes in matched_gold.items():
        if len(prediction_indexes) > 1:
            score.duplicate_findings += 1
            score.extra_predictions += len(prediction_indexes) - 1
        primary = defect_predictions[prediction_indexes[0]]
        _mark_true_positive(score, positives[gold_index], primary)
    matched_positive = {index for indexes in matched_gold.values() for index in indexes}
    missed_indexes = set(range(len(positives))) - set(matched_gold)
    for gold_index in missed_indexes:
        _mark_miss(score, positives[gold_index])
    for prediction_index, prediction in enumerate(defect_predictions):
        if prediction_index in matched_positive:
            continue
        _mark_operational(score, prediction)
        if any(_align_score(prediction, finding, require_type=False) is not None for finding in ambiguous):
            continue
        _mark_false_positive(score, prediction)
    for finding in scoreable:
        prediction = _best_prediction(finding, run.predictions)
        _mark_polarity(score, finding, prediction)
    for prediction in run.predictions:
        _mark_trace(score, prediction, run.sections)
    return score


def _mark_true_positive(score: _DocumentScore, finding: GoldFinding, prediction: Prediction) -> None:
    score.true_positive += 1
    if finding.defect_type in DEFECT_TYPE_SET:
        assert score.types is not None
        score.types[str(finding.defect_type)].true_positive += 1
    _score_fields(score, finding, prediction)


def _mark_miss(score: _DocumentScore, finding: GoldFinding) -> None:
    score.missed += 1
    if finding.defect_type in DEFECT_TYPE_SET:
        assert score.types is not None
        score.types[str(finding.defect_type)].missed += 1
    _score_fields(score, finding, None)


def _mark_false_positive(score: _DocumentScore, prediction: Prediction) -> None:
    score.false_positive += 1
    if prediction.defect_type in DEFECT_TYPE_SET:
        assert score.types is not None
        score.types[str(prediction.defect_type)].false_positive += 1
    else:
        score.untyped_false_positives += 1


def _mark_operational(score: _DocumentScore, prediction: Prediction) -> None:
    if prediction.preview_status == PreviewStatus.READY_TO_APPLY.value:
        score.operational_ready += 1
    elif prediction.preview_status == PreviewStatus.POSSIBLE_DUPLICATE.value:
        score.operational_possible_duplicate += 1


def _mark_polarity(score: _DocumentScore, finding: GoldFinding, prediction: Prediction | None) -> None:
    assert score.polarity_correct is not None
    assert score.polarity_missed is not None
    assert score.polarity_confused is not None
    assert score.conversions is not None
    outcome = _polarity_outcome(finding, prediction)
    if outcome == "correct":
        score.polarity_correct[finding.polarity] += 1
    elif outcome == "missed":
        score.polarity_missed[finding.polarity] += 1
    elif outcome == "converted" and finding.polarity in score.conversions:
        score.conversions[finding.polarity] += 1
        score.polarity_confused[finding.polarity] += 1
    else:
        score.polarity_confused[finding.polarity] += 1


def _polarity_outcome(finding: GoldFinding, prediction: Prediction | None) -> str:
    operational = prediction is not None and prediction.preview_status in OPERATIONAL_STATUSES
    if finding.polarity == "positive":
        if prediction is None:
            return "missed"
        return "correct" if prediction.kind == "defect" else "confused"
    if finding.polarity == "negated":
        if operational:
            return "converted"
        if prediction is None:
            return "missed"
        if prediction.kind == "ruled_out" and prediction.ruled_out_reason == "negated_finding":
            return "correct"
        return "confused"
    if finding.polarity == "nil":
        if operational:
            return "converted"
        if prediction is None:
            return "missed"
        if prediction.kind == "ruled_out" and prediction.ruled_out_reason == "nil_report":
            return "correct"
        return "confused"
    if finding.polarity == "uncertain":
        if operational:
            return "converted"
        if prediction is None:
            return "missed"
        if (
            prediction.kind == "defect"
            and prediction.uncertainty == "high"
            and prediction.preview_status == PreviewStatus.NEEDS_REVIEW.value
        ):
            return "correct"
        return "confused"
    if operational and prediction is not None and prediction.kind == "defect":
        return "converted"
    if prediction is None or prediction.kind == "ruled_out":
        return "correct"
    return "confused"


def _score_fields(score: _DocumentScore, finding: GoldFinding, prediction: Prediction | None) -> None:
    assert score.fields is not None
    for name in FIELD_NAMES:
        gold_value = getattr(finding, name)
        if gold_value is None:
            continue
        predicted = None if prediction is None else getattr(prediction, name)
        if predicted is None:
            outcome = "missing"
        elif name in NUMERIC_FIELDS:
            outcome = "correct" if _same_number(gold_value, predicted) else "incorrect"
        else:
            outcome = "correct" if predicted == gold_value else "incorrect"
        score.fields[name].add(outcome)


def _mark_trace(score: _DocumentScore, prediction: Prediction, sections: Sequence[SectionText]) -> None:
    score.trace_predictions += 1
    if not prediction.excerpt or not prediction.locations:
        score.trace_failures += 1
        return
    holders = [section for section in sections if _section_contains(section, prediction.excerpt)]
    exact = bool(holders)
    referenced = False
    for reference, page_number in prediction.locations:
        for section in holders:
            if section.reference == reference and section.page_number == page_number:
                referenced = True
                break
    if exact:
        score.trace_exact += 1
    if exact and referenced and all(
        any(
            section.reference == reference and section.page_number == page_number and _section_contains(section, prediction.excerpt)
            for section in sections
        )
        for reference, page_number in prediction.locations
    ):
        score.trace_reference += 1
    else:
        score.trace_failures += 1


def _aggregate(
    corpus: Corpus,
    scores: Sequence[_DocumentScore],
    *,
    provider_names: Sequence[str],
    fixture_ids: Sequence[str],
    include_excerpts: bool,
    used_default_runner: bool,
) -> dict[str, object]:
    identification_tp = sum(item.true_positive for item in scores)
    identification_fp = sum(item.false_positive for item in scores)
    identification_missed = sum(item.missed for item in scores)
    precision = _ratio(identification_tp, identification_tp + identification_fp)
    recall = _ratio(identification_tp, identification_tp + identification_missed)
    types = {name: _TypeTally() for name in DEFECT_TYPES}
    fields = {name: _FieldTally() for name in FIELD_NAMES}
    gold_counts = _empty_gold_counts()
    polarity_correct = {name: 0 for name in POLARITIES}
    polarity_missed = {name: 0 for name in POLARITIES}
    polarity_confused = {name: 0 for name in POLARITIES}
    conversions = {name: 0 for name in ("negated", "nil", "uncertain", "not_applicable")}
    for item in scores:
        for name, value in item.gold_counts.items():
            gold_counts[name] += value
        assert item.types is not None and item.fields is not None
        assert item.polarity_correct is not None and item.polarity_missed is not None
        assert item.polarity_confused is not None and item.conversions is not None
        for name in DEFECT_TYPES:
            types[name].true_positive += item.types[name].true_positive
            types[name].false_positive += item.types[name].false_positive
            types[name].missed += item.types[name].missed
        for name in FIELD_NAMES:
            fields[name].labelled += item.fields[name].labelled
            fields[name].correct += item.fields[name].correct
            fields[name].incorrect += item.fields[name].incorrect
            fields[name].missing += item.fields[name].missing
        for name in POLARITIES:
            polarity_correct[name] += item.polarity_correct[name]
            polarity_missed[name] += item.polarity_missed[name]
            polarity_confused[name] += item.polarity_confused[name]
        for name in conversions:
            conversions[name] += item.conversions[name]
    allowed, claim_note = _claim_allowed(corpus)
    local_names = sorted(set(provider_names))
    local_confirmed = bool(corpus.documents) and bool(provider_names) and local_names == ["local-text"] and not fixture_ids
    if not used_default_runner:
        allowed = False
        if corpus.corpus_kind == CORPUS_REAL:
            claim_note = "A substituted runner cannot support a real-world accuracy claim."
    elif corpus.documents and not local_confirmed:
        allowed = False
        if corpus.corpus_kind == CORPUS_REAL:
            claim_note = "The local pipeline was not confirmed for every scored document."
    limitations = _limitations(corpus, scores, allowed, claim_note)
    if used_default_runner and corpus.documents and not local_confirmed:
        limitations.append("The local pipeline was not confirmed for every scored document.")
    identification = {
        "true_positive": identification_tp,
        "false_positive": identification_fp,
        "missed": identification_missed,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "untyped_false_positives": sum(item.untyped_false_positives for item in scores),
        "by_defect_type": {name: types[name].as_dict() for name in DEFECT_TYPES},
    }
    field_report = {name: fields[name].as_dict() for name in FIELD_NAMES}
    report: dict[str, object] = {
        "report_version": EVAL_REPORT_VERSION,
        "dataset_version": corpus.dataset_version,
        "schema_version": corpus.schema_version,
        "corpus_kind": corpus.corpus_kind,
        "source_name": corpus.source_name,
        "split_filter": "selected",
        "document_ids": [document.document_id for document in corpus.documents],
        "source_document_ids": sorted({document.source_document_id for document in corpus.documents}),
        "splits": {
            split: [document.document_id for document in corpus.documents if document.source_split == split]
            for split in ("train", "validation", "test")
        },
        "documents_scored": len(corpus.documents),
        "documents_unreadable": [item.document_id for item in scores if not item.readable],
        "configuration": {
            "predictor": (
                "read_report_document + maybe_scripted_fixture + local_text_or_keep "
                "+ simplify_report + validate_candidates"
            ),
            "report_ai_provider": "mock",
            "report_ai_mock_fixture": None,
            "demo_fixture_environment": "unset",
            "existing_backlog_defects": 0,
            "as_of_date": None,
            "provider_names": local_names,
            "fixture_ids": sorted(set(fixture_ids)),
            "local_pipeline_confirmed": local_confirmed,
        },
        "real_world_accuracy_measured": allowed,
        "accuracy_claim_allowed": allowed,
        "claim_note": claim_note,
        "gold_counts": gold_counts,
        "identification": identification,
        "polarity": {
            "gold": {name: gold_counts[name] for name in POLARITIES},
            "correct": polarity_correct,
            "missed": polarity_missed,
            "confused": polarity_confused,
            "false_positive_conversions": conversions,
        },
        "operational_false_positives": {
            "ready_to_apply": sum(item.operational_ready for item in scores),
            "possible_duplicate": sum(item.operational_possible_duplicate for item in scores),
        },
        "fields": field_report,
        "traceability": {
            "predictions": sum(item.trace_predictions for item in scores),
            "exact_substring": sum(item.trace_exact for item in scores),
            "reference_and_page_match": sum(item.trace_reference for item in scores),
            "failures": sum(item.trace_failures for item in scores),
            "failure_document_ids": [item.document_id for item in scores if item.trace_failures],
        },
        "duplicates": {
            "gold_findings_with_multiple_predictions": sum(item.duplicate_findings for item in scores),
            "extra_predictions": sum(item.extra_predictions for item in scores),
        },
        "annotation_errors": sum(item.annotation_errors for item in scores),
        "include_excerpts": include_excerpts,
        "limitations": limitations,
    }
    report["undefined_metrics"] = _undefined_metrics(report)
    if include_excerpts:
        report["local_review"] = {
            "warning": "Local review only. Do not commit or publish this section.",
            "rows": [row for item in scores for row in (item.local_review or [])],
        }
    return report


def _claim_allowed(corpus: Corpus) -> tuple[bool, str]:
    if corpus.corpus_kind != CORPUS_REAL:
        return (
            False,
            "This corpus is a synthetic evaluator fixture. Real-world accuracy has not been measured.",
        )
    if not corpus.documents:
        return False, "No documents were scored."
    splits = {document.source_split for document in corpus.documents}
    if splits != {"test"}:
        return False, "A reliability claim scores the held-out test split only."
    source_ids = {document.source_document_id for document in corpus.documents}
    if len(source_ids) < MINIMUM_SOURCE_DOCUMENTS_FOR_CLAIM:
        return (
            False,
            f"Only {len(source_ids)} source documents were scored. "
            f"A claim needs at least {MINIMUM_SOURCE_DOCUMENTS_FOR_CLAIM}, "
            f"and {RECOMMENDED_SOURCE_DOCUMENTS} are recommended.",
        )
    if len(corpus.documents) < MINIMUM_TEST_DOCUMENTS_FOR_CLAIM:
        return (
            False,
            f"The test split has {len(corpus.documents)} documents. "
            f"A claim needs at least {MINIMUM_TEST_DOCUMENTS_FOR_CLAIM}.",
        )
    confirmed = [finding for document in corpus.documents for finding in document.findings if finding.confirmation == "confirmed"]
    present_types = {finding.defect_type for finding in confirmed if finding.polarity == "positive"}
    missing_types = [name for name in DEFECT_TYPES if name not in present_types]
    missing_polarity = [
        name for name in ("negated", "nil", "uncertain") if not any(finding.polarity == name for finding in confirmed)
    ]
    if missing_types or missing_polarity:
        return (
            False,
            "The test split does not yet cover "
            + ", ".join([*missing_types, *missing_polarity])
            + ".",
        )
    return True, "Held-out test documents meet the minimum size and coverage checks."


def _limitations(
    corpus: Corpus,
    scores: Sequence[_DocumentScore],
    allowed: bool,
    claim_note: str,
) -> list[str]:
    notes = [
        claim_note,
        "No model was trained by this evaluation.",
        "The demo report, sample_data files, and unit-test sentences are not this corpus.",
        f"Cross-split leakage checks exact page copies and excerpts of at least {EXCERPT_LEAKAGE_MIN_CHARS} characters.",
    ]
    if corpus.corpus_kind == CORPUS_SYNTHETIC:
        notes.append("Synthetic worksheet counts test the evaluator. They are not railway-report accuracy.")
    if any(not item.readable for item in scores):
        notes.append("An unreadable document was excluded from the rates.")
    if any(item.annotation_errors for item in scores):
        notes.append("A gold excerpt that was not in the extracted source was excluded from the rates.")
    if not allowed:
        notes.append("Undefined rates stay null. They are not treated as perfect performance.")
    return notes


def _undefined_metrics(report: Mapping[str, object]) -> list[str]:
    names: list[str] = []
    identification = _mapping(report.get("identification"))
    for name in ("precision", "recall", "f1"):
        if identification.get(name) is None:
            names.append(f"identification.{name}")
    by_type = _mapping(identification.get("by_defect_type"))
    for defect_type in DEFECT_TYPES:
        bucket = _mapping(by_type.get(defect_type))
        for name in ("precision", "recall", "f1"):
            if bucket.get(name) is None:
                names.append(f"identification.by_defect_type.{defect_type}.{name}")
    fields = _mapping(report.get("fields"))
    for field_name in FIELD_NAMES:
        bucket = _mapping(fields.get(field_name))
        for name in ("accuracy", "coverage"):
            if bucket.get(name) is None:
                names.append(f"fields.{field_name}.{name}")
    return names


def _assign(
    golds: Sequence[GoldFinding],
    predictions: Sequence[Prediction],
    *,
    require_type: bool,
) -> tuple[list[tuple[int, int]], set[int]]:
    pairs: list[tuple[int, int, int]] = []
    for gold_index, finding in enumerate(golds):
        for prediction_index, prediction in enumerate(predictions):
            score = _align_score(prediction, finding, require_type=require_type)
            if score is not None:
                pairs.append((score, gold_index, prediction_index))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_gold: set[int] = set()
    used_prediction: set[int] = set()
    assigned: list[tuple[int, int]] = []
    for _score, gold_index, prediction_index in pairs:
        if gold_index in used_gold or prediction_index in used_prediction:
            continue
        used_gold.add(gold_index)
        used_prediction.add(prediction_index)
        assigned.append((gold_index, prediction_index))
    return assigned, used_prediction


def _best_prediction(finding: GoldFinding, predictions: Sequence[Prediction]) -> Prediction | None:
    ranked: list[tuple[int, int, int, Prediction]] = []
    for index, prediction in enumerate(predictions):
        score = _align_score(prediction, finding, require_type=False)
        if score is None:
            continue
        operational_rank = 0 if prediction.preview_status in OPERATIONAL_STATUSES else 1
        ranked.append((score, operational_rank, index, prediction))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return ranked[0][3]


def _align_score(prediction: Prediction, finding: GoldFinding, *, require_type: bool) -> int | None:
    if not prediction.locations:
        return None
    if prediction.locations[0][0] != finding.reference:
        return None
    if (
        require_type
        and finding.defect_type in DEFECT_TYPE_SET
        and prediction.defect_type is not None
        and prediction.defect_type != finding.defect_type
    ):
        return None
    left = _norm(prediction.excerpt)
    right = _norm(finding.excerpt)
    if not left or not right:
        return None
    if left == right:
        return 3
    if left in right or right in left:
        return 2
    return None


def _gold_located(finding: GoldFinding, sections: Sequence[SectionText]) -> bool:
    return any(
        section.reference == finding.reference
        and section.page_number == finding.page_number
        and _section_contains(section, finding.excerpt)
        for section in sections
    )


def _section_contains(section: SectionText, excerpt: str) -> bool:
    return bool(excerpt) and (excerpt in section.native_text or excerpt in section.ocr_text)


def _gold_counts(document: GoldDocument) -> dict[str, int]:
    counts = _empty_gold_counts()
    counts["findings"] = len(document.findings)
    for finding in document.findings:
        if finding.confirmation == "confirmed":
            counts["confirmed"] += 1
        else:
            counts["ambiguous"] += 1
        counts[finding.polarity] += 1
        if finding.defect_type == "NO_DEFECT":
            counts["no_defect"] += 1
    return counts


def _empty_gold_counts() -> dict[str, int]:
    counts = {name: 0 for name in ("findings", "confirmed", "ambiguous", "no_defect")}
    for name in POLARITIES:
        counts[name] = 0
    return counts


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _same_number(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return abs(float(left) - float(right)) <= 1e-6


def _norm(text: str) -> str:
    return " ".join(text.split())


def _show(value: object) -> str:
    if value is None:
        return "undefined"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _rate_lines(bucket: Mapping[str, object], names: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for name in names:
        value = bucket.get(name)
        shown = _show(value) if name in {"precision", "recall", "f1"} else value
        lines.append(f"  {name}: {shown}")
    return lines


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _text(value: object, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string.")
        return None
    return value.strip()


def _optional_text(value: object, label: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{label} must be a string or null.")
        return None
    stripped = value.strip()
    return stripped or None


def _identifier(value: object, label: str, errors: list[str]) -> str | None:
    text = _text(value, label, errors)
    if text is None:
        return None
    if len(text) > 64 or any(character not in _ID_CHARS for character in text):
        errors.append(f"{label} must be a stable anonymised id.")
        return None
    return text


def _filename(value: object, label: str, errors: list[str]) -> str | None:
    text = _text(value, label, errors)
    if text is None:
        return None
    if text != Path(text).name or text in {".", ".."}:
        errors.append(f"{label} must be a file name, not a path.")
        return None
    return text


def _source_path(
    value: object,
    corpus_kind: str | None,
    filename: str | None,
    label: str,
    errors: list[str],
) -> str | None:
    if corpus_kind == CORPUS_SYNTHETIC:
        if value is not None:
            errors.append(f"{label} synthetic source_path must be null.")
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} source_path must point at the local report file.")
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{label} source_path must stay inside the corpus directory.")
        return None
    if filename is not None and path.name in _NOT_AUTHORISED_NAMES:
        errors.append(f"{label} source_path names a fixture, not an authorised report.")
        return None
    return value


def _choice(value: object, allowed: frozenset[str], label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or value not in allowed:
        errors.append(f"{label} must be one of {', '.join(sorted(allowed))}.")
        return None
    return value


def _optional_choice(value: object, allowed: frozenset[str], label: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    return _choice(value, allowed, label, errors)


def _optional_int(value: object, label: str, errors: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{label} must be an integer or null.")
        return None
    return value


def _optional_float(value: object, label: str, errors: list[str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} must be a number or null.")
        return None
    return float(value)


__all__ = [
    "CORPUS_REAL",
    "CORPUS_SYNTHETIC",
    "Corpus",
    "CorpusValidationError",
    "DEFECT_TYPES",
    "DocumentRun",
    "EVAL_REPORT_VERSION",
    "GoldDocument",
    "GoldFinding",
    "GoldPage",
    "MINIMUM_SOURCE_DOCUMENTS_FOR_CLAIM",
    "MINIMUM_TEST_DOCUMENTS_FOR_CLAIM",
    "Prediction",
    "RECOMMENDED_SOURCE_DOCUMENTS",
    "SCHEMA_VERSION",
    "SectionText",
    "evaluate_corpus",
    "evaluation_environ",
    "load_corpus",
    "parse_corpus_rows",
    "render_summary",
    "run_local_pipeline",
    "synthetic_pdf_bytes",
]
