# AI-1 synthetic sentence benchmark

This directory is a developer-only benchmark. Every example was copied from a synthetic sentence table. None of these rows is a genuine railway inspection, and none of the labels has been checked by a person.

Do not register these files as `authorised_real`. Do not copy them into `eval/real/corpus/gold.jsonl`. Do not treat a score on this benchmark as a measurement of real-world railway accuracy.

The existing smoke fixture at `eval/synthetic/corpus.jsonl` is a separate file. It is not this benchmark.

## Source files

The six PDFs remain in `eval/real/corpus/Railway AI-1 training reports/`. They were read in place and were not edited. `SOURCE_MANIFEST.csv` records the SHA-256 of each file at extraction time.

| File | SHA-256 | Rows |
| --- | --- | --- |
| `01_mixed_tms_tdms_smms_sentences.pdf` | `f3d9a395228bf861dfc32378496e9237592efd4b448f88566e7dd2f7f36965e9` | 30 |
| `02_challenging_evaluation_set.pdf` | `53a0882d753f165b26d936dc367893dc385c1788f0c42f219fb82de52608a79d` | 56 |
| `03_tdms_rail_fracture_vs_weld_failure.pdf` | `9f1611ed3b49bb072be3f42c9219abaa80f145aa73aac5628a11d093fb441be3` | 36 |
| `04_tms_tamping_and_deep_screening_log.pdf` | `d54966d083492528129c8dd0ee74985e576b812f2a74d1f5da1a6afd2d6fb82c` | 32 |
| `05_tms_smms_signalling_and_interlocking.pdf` | `b14e803f3ba92f82f47736416eb5479b37911c27ffc26256c22b042ef811f0cd` | 32 |
| `06_smms_ohe_sag_with_hard_negatives-1.pdf` | `485789aac099d50216926c295595bbd14225ed9a94e09cc126f8e0cf7069c902` | 30 |

Each PDF says, on its first page, that the records are fictional and were invented for research and local model development. The banner provenance string is `synthetic_llm_generated_not_from_real_reports` on files 01–03 and `synthetic_llm_generated_not_a_genuine_railway_record` on files 04–06. That string is stored on each provisional answer-key row as `source_banner_provenance`.

The PDFs are landscape tables, produced by ReportLab. The columns are `#`, `source_style`, `text`, `defect_type`, `polarity`, `template_family`, `paraphrase_group`, and `split`. The category, polarity, template, paraphrase group, and split sit in the same row as the sentence. They are answer-key columns, not part of the sentence.

## How the sentences were reconstructed

`report_documents.py` reads a PDF with `pypdf` plain `extract_text()`. On these tables that flattens each row into one stream, so a wrapped sentence is split from its continuation and can sit next to a wrapped label such as `TAMPING_REQUIRE` / `D`. This benchmark does not use that flat stream as the sentence.

Reconstruction used the local `pypdf` 6.20.1 text visitor, which returns each text run with its page position, and the page content stream, which shows when runs are on the same line (`Tj` with no line advance) or on the next line of the same cell (`T*`). No package was installed. Pillow was already present and was used only to measure line width while checking wraps. The Windows PDF renderer was used to draw every page, and every data row was compared with the reconstructed sentence.

For each numbered row:

- The sentence is the `text` column only.
- A wrap inside that column is joined with one space when the rendered lines break between words. The space is the wrap point ReportLab removed. No other word was inserted, completed, or respelt.
- Same-line runs that the PDF splits around `&` are already one string in the text visitor, so `S&T` stays `S&T`.
- A wrap in `defect_type` or `template_family` stays in that column. Narrow labels such as `TAMPING_REQUIRE` + `D` are joined without a space, because the fragments contain no whitespace and the rendered cell is one token. Those joins are recorded in `warnings` and are not copied into `inputs.jsonl`.
- Negation and uncertainty words stay in the sentence (`no`, `not`, `nil`, `possible`, `suspected`, `may`).
- Abbreviated and misspelled fragments stay as printed (`frcture`, `pckng`, `scrning`, `wont`).
- A sentence that names more than one defect keeps both terms.

No source row was dropped, and no replacement sentence was written. All 216 numbered rows were reconstructed. 76 sentences were joined from wrapped text-column lines. 21 defect labels and 71 template-family ids were rejoined from a mid-token wrap. Unresolved rows: 0.

## Input file and provisional answer key

`inputs.jsonl` is the only file a reader should see. Each line has:

- `example_id`, of the form `AI1SYN-01-001` (source file number and the PDF row number)
- `sentence`, the reconstructed text-column sentence
- `source_pdf`, `source_page`, and `source_reference` (`page:N`)

It does not contain category, polarity, template family, paraphrase group, split, source style, or the banner provenance.

`provisional_answer_key.jsonl` keeps the generator columns for the same ids. `label_provenance` is `generated_provisional_not_human_verified`. `human_verified` is false and `review_state` is `unreviewed` on every row. The generator token `null` is stored as the string `null`. It is not a human decision that the category is absent, and it is not the annotation-guide value `NO_DEFECT`. The generator polarity `background` is kept as `background`; other files use `not_applicable` for similar rows. Those tokens were not translated into the guide's vocabulary.

`source_split` is the PDF's own token: `train`, `val`, or `test`. `val` was not renamed to `validation`. The splits are provisional metadata. They are not a licence to score a model, and they were not redrawn.

## Paraphrase links

Inside one PDF, rows that share `paraphrase_group` already share one generator split. There are 23 such pairs and 170 groups of one row. Do not move one row of a pair into another split.

`paraphrase_group` values such as `pg_01` start again in every PDF. The same string in two files is not one group. Use `(source_pdf, paraphrase_group)` together.

`template_family` is also not a substitute for that pair. 17 family ids occur twice, and both rows already share a split. In file 01, 12 rows sit in a paraphrase group that contains two different template-family ids (the formal line and its fragment were given different family ids). Check both fields before treating two sentences as unrelated.

Exact duplicate sentences: 0. None were deleted.

## What this set is for

Use it to test a local reader, simplifier, or structured-field extractor while developing:

- whether one table row becomes one sentence
- whether a wrapped line is reassembled without taking the next column
- whether negation and uncertainty stay attached to the sentence
- whether missing chainage, severity, line, date, and TSR state stay missing instead of being invented
- whether a sentence that names two defects is not reduced to one term

Compare system output with `provisional_answer_key.jsonl` only as a debugging aid. The comparison is not an accuracy result. This stage does not train a model and does not report a score.

A result on these sentences cannot support a claim about railway reports. The text was generated, the labels were generated, the layout is a labelled sentence table rather than an inspection report, and no person has accepted the labels. Real-report rules remain those in `eval/ANNOTATION_GUIDE.md` and `eval/real/README.md`.

## Human review

`human_review_worksheet.csv` has one row per example. The proposed sentence and the provisional category and polarity are filled in. These columns are blank and must stay blank until a person reviews that row:

- `human_decision`
- `corrected_category`
- `corrected_polarity`
- `reviewer_rationale`
- `review_status`

Every one of the 216 rows needs that review before a label can be treated as gold. Rows to look at first, already flagged in the answer-key `warnings` and still unreviewed:

- 3 rows whose template-family id contains `ambiguous`
- 4 rows whose template-family id contains `both_terms`
- 5 sentences that visibly contain both a fracture term and a weld term
- 1 sentence that visibly contains both a tamping term and a screening term

When a person reviews a row:

- `accept` means the generated category and polarity are the right reading of this sentence. Say so in `reviewer_rationale` if the generator token is `null` or `background`, and write the corrected category and polarity in the annotation-guide vocabulary so the acceptance is not just a copy of the generator token.
- `correct` means the generated label is wrong. Write `corrected_category` and `corrected_polarity`. Use only a category the sentence supports: `RAIL_FRACTURE`, `WELD_FAILURE`, `OHE_SAG`, `SIGNAL_INTERLOCK`, `TAMPING_REQUIRED`, `DEEP_SCREENING`, or `NO_DEFECT`. Use `positive`, `negated`, `nil`, `uncertain`, or `not_applicable`. Leave a field blank when the sentence does not state it. Do not invent chainage, severity, or any other value.
- `ambiguous` means a person cannot choose one reading. Leave the corrected fields blank unless one reading should be recorded beside the doubt, and explain both readings in `reviewer_rationale`.
- `reject` means the row should not be used even as a developer example. Explain why.

Set `review_status` to `reviewed` only after that decision. If the row still needs a second reader, set `review_status` to `needs_discussion` and leave `human_decision` as `ambiguous`.

Do not correct spelling in the sentence while reviewing. The spelling is the source text. If a sentence does not match the PDF, set `human_decision` to `reject` and describe the mismatch in `reviewer_rationale`.

The guide for a later human gold file is `eval/ANNOTATION_GUIDE.md`. Filling this worksheet does not create that gold file.
