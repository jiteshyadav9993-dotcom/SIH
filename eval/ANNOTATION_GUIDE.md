# AI-1 gold annotation guide

Schema version `ai1-gold-1`. One JSON object per line. The checker is `report_eval.py`. It does not train a model.

This format is for later human annotation of authorised reports. `eval/synthetic/corpus.jsonl` is a labelled fake packet used only to test the scorer. `sample_data/`, `tests/fixtures/ai1_railway_maintenance_demo_report_4_pages.pdf`, and sentences written for unit tests are not evaluation data.

## How to add a real report

1. Remove names, phone numbers, staff ids, and any other personal data before the file is copied.
2. Put the file in `eval/real/corpus/`. That directory is gitignored.
3. Add one JSONL row with `corpus_kind` `authorised_real`, `source_path` relative to the JSONL file, and `pages` as an empty list. Do not paste the whole report into the JSONL.
4. Give the original report one `source_document_id`. Every page, scan, and repeated register from that report uses the same id and the same `source_split`: `train`, `validation`, or `test`.
5. Score only the test split. A reliability claim needs at least 30 distinct source documents in that held-out split, and 100 is the recommended review size. The test split must include all six defect categories plus negated, nil, and uncertain findings. Below that, the tool still runs and labels the counts as a worksheet.

`dataset_version` is an id you choose, such as `cr-2026-04`. Do not start it with `synthetic`. `anonymised` must be true. `document_id` and `finding_id` are stable ids you assign, not operational `AI1-` defect ids.

## Finding fields

| Field | Rule |
| --- | --- |
| `excerpt` | Exact source span, copied from the file. It must survive the document reader unchanged. |
| `reference` | Reader reference: `page:N` for a PDF, `paragraph:N` or `table:N` for a DOCX. |
| `page_number` | PDF page, or null for a DOCX block. |
| `paragraph_reference`, `table_reference` | The same reference when the span is a DOCX paragraph or table; otherwise null. |
| `defect_type` | One of `RAIL_FRACTURE`, `WELD_FAILURE`, `OHE_SAG`, `SIGNAL_INTERLOCK`, `TAMPING_REQUIRED`, `DEEP_SCREENING`, or `NO_DEFECT`. Use null when the source does not establish a category. |
| `polarity` | `positive`, `negated`, `nil`, `uncertain`, or `not_applicable`. |
| `chainage_km`, `track_line`, `severity`, `due_date`, `days_overdue`, `tsr_state` | Copy only a value the source states. Otherwise null. |
| `confirmation` | `confirmed` or `ambiguous`. |
| `rationale` | Required when `confirmation` is `ambiguous`. Short, and only when a decision needs one. |

`track_line` is `DN`, `UP`, `DN_FAST`, `UP_FAST`, `DN_SLOW`, `UP_SLOW`, or `MID`. `tsr_state` is `active`, `not_active`, or `lifted`. `due_date` is the source's own date expression, not a normalised date the source did not write. `severity` is an integer the source states. Words such as "critical" are not a severity.

`broken rail` is annotated as `RAIL_FRACTURE` because that is the existing category. Do not add a new category for a paraphrase the source happens to use. The excerpt keeps the source wording.

## Decisions

Multiple defects in one sentence become separate findings. Each excerpt is its own exact substring. Do not merge them because they share a type.

A repeated register entry that adds no new location or defect is one finding. Mention the repeat in `rationale`. A second location or a second defect type is a second finding.

Negation (`No rail fracture was observed`) is `negated` and names the denied category. A nil report (`Nil defects were recorded`) is `nil`; leave `defect_type` null when no category is named. A hedge (`Possible weld failure`) is `uncertain`. Background with no defect is `NO_DEFECT` and `not_applicable`.

Missing chainage, line, severity, date, overdue days, or TSR state stays null. Do not infer them from a nearby row.

Conflicting values stay null. Set `confirmation` to `ambiguous` and write both source values in `rationale`. Do not pick one.

An ambiguous finding is kept in the file so the scorer can see it, and it is not treated as a confirmed hit or a confirmed miss.

## What the scorer reports

The tool reads the file with the existing document reader and runs the local NLP with the demo fixture unset. Rates with an empty denominator are `undefined`, not a perfect score. Synthetic runs always say that real-world accuracy was not measured.
