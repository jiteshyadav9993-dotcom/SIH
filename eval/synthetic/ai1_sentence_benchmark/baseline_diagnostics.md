# AI-1 local-text baseline on the 216 synthetic sentences

This is an exploratory diagnostic of the current local proposer and validator. It is not a gold-standard evaluation. The provisional answer key has not been checked by a person. Nothing in this note is an accuracy, precision, recall, F1, or reliability result, and the generated labels are not treated as truth.

Each sentence was passed, by itself, to the existing in-memory path:

1. `simplify_report` with a fresh `LocalTextProvider` (`name` `local-text`, `fixture_matched` false, `scripted_fixture` null).
2. `validate_candidates` with an empty existing-defect list and no reference date.

The scripted demo fixture was not selected. The professor-NLP adapter stayed `pending`. No upload, preview service, storage, or backlog apply call was used. The answer key was not given to the proposer or the validator. It was read only after the predictions existed, and only to list disagreements for a human reviewer.

The source PDFs were not opened again. Each input sentence was the whole native text of one section, using that row's `source_reference` and page number.

## What was processed

Counts below were taken from `baseline_predictions.jsonl`.

| Observation | Count |
| --- | --- |
| Inputs read | 216 |
| Output records | 216 |
| Skipped | 0 |
| Failed (`diagnostic_error` set, or understanding status `failed`) | 0 |
| Provider | `local-text` on every record |
| NLP status | `pending` on every record |
| Understanding status | `complete` on every record |
| Pipeline warnings | 0 |

Input-level outcome, from whether the pipeline emitted anything:

| Outcome | Inputs |
| --- | --- |
| No proposal, no candidate, and no ruled-out record | 130 |
| Defect candidates only | 70 (67 with one candidate, 3 with two) |
| Ruled-out record only | 16 |

Finding-level outcome, from the records the current code actually returned:

| Outcome | Findings |
| --- | --- |
| Proposals | 89 |
| Proposal kind `defect` | 73 |
| Proposal kind `ruled_out` | 16 |
| Validated candidates | 73 |
| Candidate `review_status` `NEEDS_REVIEW` | 73 |
| Candidate `review_status` `READY` | 0 |
| Ruled out | 16 (`negated_finding` 11, `nil_report` 5) |
| Preview status `NEEDS_REVIEW` | 73 |
| Preview status `RULED_OUT` | 16 |
| Preview status `READY_TO_APPLY`, `REJECTED`, or `POSSIBLE_DUPLICATE` | 0 |

Every proposal excerpt is a substring of its input sentence, and every accepted excerpt kept the input's `source_reference`. No proposal was rejected by `check_findings`.

Polarity below is `classify_polarity` on the emitted excerpt, which is the same check the preview uses. It is present only where a finding was emitted.

| Emitted polarity | Findings |
| --- | --- |
| `positive` candidate | 61 |
| `uncertain` candidate | 12 |
| `negated` ruled out | 11 |
| `nil` ruled out | 5 |

Category on the 73 candidates, after validation:

| Category value | Candidates |
| --- | --- |
| `TAMPING_REQUIRED` | 22 |
| `DEEP_SCREENING` | 15 |
| `OHE_SAG` | 10 |
| `SIGNAL_INTERLOCK` | 8 |
| `RAIL_FRACTURE` | 8 |
| `WELD_FAILURE` | 6 |
| none | 4 |

The proposer left severity, track line, chainage, due date, days overdue, and TSR null. The validator then filled a defect type on 69 candidates. It filled a track line on 4. It filled no severity, chainage, due date, days overdue, TSR, duration, or speed on any candidate. Twelve candidates carry the warning "The excerpt does not clearly confirm or rule out the defect." Three carry "The excerpt names more than one defect type."

All 73 candidates stay in preview as `NEEDS_REVIEW` because the scheduling fields are absent. Sixty-five are missing track line, chainage, severity, days overdue, and TSR. The four with a track line are still missing the other four. The four with no type are also missing defect type. None became `READY_TO_APPLY`.

## Observable patterns

### Catalogue word order leaves plain defect sentences with no finding

The local phrase list matches fixed expressions such as `rail fracture`, `weld failure`, `ohe sag`, `interlocking fault`, and `deep screening`. Many sentences use another order, a shorter form, or a nearby description, and the proposer returned nothing.

Unverified provisional labels mark 40 of the 130 silent inputs as `positive`. That is a review queue, not a miss rate. Examples:

- Rail wording other than `rail fracture`: `AI1SYN-01-002` ("transverse crack extending to full fracture" at `KM 123/4`), `AI1SYN-02-001`, `AI1SYN-02-002` ("Fractured rail is still outstanding"), `AI1SYN-03-001` ("Rail found fractured").
- Weld wording other than `weld failure`: `AI1SYN-01-005` ("AT weld failed"), `AI1SYN-01-027` ("failure of the thermit weld"), `AI1SYN-03-008` ("Failed flash butt weld"), `AI1SYN-03-009` ("Thermit weld ... has failed").
- OHE wording other than `ohe sag` or `contact wire sag`: `AI1SYN-02-016` ("ohe sagging"), `AI1SYN-06-004` ("catenary was seen sagging"), `AI1SYN-06-006` ("the sag in the overhead"), `AI1SYN-06-007` (contact wire "hangs visibly lower").
- Signal wording other than `signal interlock`, `interlocking fault`, or `point machine`: `AI1SYN-02-022` ("interlocking failed to prove point detection"), `AI1SYN-05-004` ("locking fault"), `AI1SYN-05-005` ("Lever locking did not release").
- Abbreviated fragments with no catalogue phrase: `AI1SYN-01-001` ("rail frcture"), `AI1SYN-01-016` and `AI1SYN-02-030` ("tampng reqd"), `AI1SYN-01-030` ("deep scrning"), `AI1SYN-02-023` ("sgnl interlock"), `AI1SYN-05-002` ("intrlkg fail").

`AI1SYN-01-002` is the only input that writes a `KM` value. It produced no finding, so the chainage was not read and was not attached to another finding.

### Positive candidates from instructions, programmes, and completed work

A positive candidate was emitted for 30 inputs whose provisional polarity is not `positive` (13 provisional `negated`, 9 `not_applicable`, 7 `uncertain`, 1 `nil`). The generated polarity is unverified. The local result is still a positive defect candidate:

- Instructions that are not the `check/inspect/examine/look for` pattern: `AI1SYN-02-052` ("watch for rail fracture"), `AI1SYN-02-054` ("report any OHE sag"), `AI1SYN-02-055` ("check whether tamping is needed"), `AI1SYN-05-025` ("must test the interlocking whenever a point machine is replaced"). The phrase `point machine` alone is enough for a signal candidate.
- A hypothetical that the current `if` rule did suppress: `AI1SYN-02-053` ("If weld failure is seen") produced no finding.
- Programme and cycle mentions: `AI1SYN-04-022` ("Tamping machine reached the depot"), `AI1SYN-04-024` ("Tamping was carried out"), `AI1SYN-04-032` ("Tamping cycle ... eighteen months"), `AI1SYN-04-028` (yearly programme; see the split below).
- Completed, rectified, or "nothing outstanding" wording kept as a positive defect: `AI1SYN-02-003`, `AI1SYN-02-012`, `AI1SYN-02-026`, `AI1SYN-03-025`, `AI1SYN-03-026`, `AI1SYN-04-008`, `AI1SYN-04-009`, `AI1SYN-04-018`.
- "Not checked" or "not assessed" kept as a positive defect: `AI1SYN-02-006`, `AI1SYN-02-021`, `AI1SYN-02-035`, `AI1SYN-02-042`, `AI1SYN-04-011`.

`AI1SYN-01-009` ("OHE sag nil") is positive. `nil` after the phrase is not the nil-report pattern, which looks for `nil defects` or `no defects`.

### Negation, nil, and uncertainty

The 16 ruled-out rows are the cases where a catalogue phrase sits near `no`/`not`/`nil`, or the sentence matches `no defects` / `nil` plus `defects`. Several of those agree with the provisional polarity, including `AI1SYN-01-003`, `AI1SYN-01-006`, `AI1SYN-01-012`, `AI1SYN-01-015`, `AI1SYN-01-018`, and `AI1SYN-01-021`. Agreement here is still not a confirmed label.

Other negation shapes produced no ruled-out record:

- Bare `Nil.`: `AI1SYN-01-020`. The nil pattern requires `nil defects` or `no defects`.
- "No fracture found" / "no fracture exists" without the words `rail fracture`: `AI1SYN-02-004`, `AI1SYN-03-017`, `AI1SYN-03-018`.
- `Neither ... nor`: `AI1SYN-03-031` became one positive candidate with no type. The warning says the excerpt names more than one defect type. `neither` and `nor` are not negation words.
- `AI1SYN-04-020` ("No decision yet on whether deep screening is needed") was ruled out as `negated_finding`. The word `no` in "No decision" falls in the five-word window before `deep screening`. The provisional polarity is `uncertain`, and it has not been reviewed.

Uncertain hedges fire when the excerpt says `possible`, `suspected`, `may be`, or `not confirmed` and a defect phrase or the word `defect`/`failure`/`fracture` is present. That left 12 uncertain candidates, all `NEEDS_REVIEW`. `AI1SYN-03-019` ("Fracture suspected last week was not found on re-sounding") is one of them, with no category, because `fracture` alone is not `rail fracture`.

### Several defect terms in one sentence

The proposer splits on `; `, `, but `, ` and `, and ` but ` only when each side has at most one defect phrase and at least two sides have one.

- `AI1SYN-03-027` became two positive candidates: `WELD_FAILURE` for "Earlier weld failure was rewelded" and `RAIL_FRACTURE` for the separate fracture "15 m away." The historical weld repair and the new fracture are both positive. The distance was not stored as chainage.
- `AI1SYN-04-007` became two positive `TAMPING_REQUIRED` candidates. "up loop" was stored as track line `UP` on the clause that says tamping was finished. The clause that still requires tamping ("the main line") has no track line.
- `AI1SYN-04-009` became two positive tamping candidates, including "nothing outstanding for tamping."
- `AI1SYN-04-028` was split on ` and `. The bare word `Tamping` was dropped. The emitted excerpt starts at "deep screening both appear in the yearly programme..." and is a positive `DEEP_SCREENING` candidate. "no field observation is recorded" did not change that.
- `AI1SYN-03-029`, `AI1SYN-03-030`, and `AI1SYN-03-031` stayed one excerpt, so the validator refused a single category and warned that more than one defect type is named. `AI1SYN-03-030` is an instruction to staff, and `AI1SYN-03-031` is a neither/nor observation. Both are still positive candidates.

### Track line was associated with the wrong words

Four candidates received a track line. Each match is a catalogue line word inside other English:

| Example | Sentence wording | Stored line |
| --- | --- | --- |
| `AI1SYN-01-028` | "mid span" | `MID` |
| `AI1SYN-06-002` | "mid span" | `MID` |
| `AI1SYN-04-012` | "right up to the sleeper bottoms" | `UP` |
| `AI1SYN-04-007` | "up loop" on the finished-tamping clause | `UP` |

No severity, chainage, due date, overdue days, or TSR was filled on any row. `AI1SYN-06-006` mentions a "speed caution" and did not become a TSR, because that input produced no finding.

## Limits of sentence-only testing

These inputs are one generated sentence each, not a maintenance report. A real report has headings, repeated registers, several findings on one page, and values that may sit in a neighbouring column. This run cannot show those behaviours.

A sentence that does not state line, chainage, severity, overdue days, and TSR cannot pass `validate_candidates` into `READY_TO_APPLY`. That is why every candidate here is `NEEDS_REVIEW`. The absence of a planner-ready record is not evidence about the formulas or the DMO dashboard.

The provisional categories and polarities were produced by the same generator as the sentences. A difference between the local result and that label means a person should look at the row. It does not show which side is right.

Paraphrase pairs that share a template family or a within-PDF paraphrase group can fall on opposite sides of this run: one formal sentence may match a catalogue phrase while its abbreviated twin does not. `AI1SYN-02-029` ("tamping is required") produced a candidate; `AI1SYN-02-030` ("tampng reqd") produced none.

## Small improvements, ranked

1. **Phrase coverage for the word orders this run left silent.** Add the forms that already appear as ordinary sentences, such as "fractured rail", "rail found fractured", "weld failed" / "weld has failed", "failed weld", "interlocking failed", and "sagging" tied to OHE or the contact wire. Usefulness is high because those shapes account for most of the 40 silent provisional-positive inputs. Risk is medium if the new phrases also match instructions and old repairs. Abbreviations such as `frcture` and `tampng` should wait until a person accepts them on the worksheet.
2. **Do not emit a positive candidate for an instruction, a programme mention, or a completed / not-checked statement.** Cover the cues this run still treated as live defects: "watch for", "report any", "must test", "whenever a point machine is replaced", "nothing outstanding", "not assessed", "not yet checked", "rectified", "was finished", "was carried out", and "yearly programme". Usefulness is high for the 30 positive candidates sitting on provisional non-positive rows. Risk is lower than a wider phrase list if each cue is specific and covered by a test. The existing `if` rule already held `AI1SYN-02-053`.
3. **Tighten track-line matching.** Require a line context so `mid span`, `right up to`, and a finished "up loop" clause are not stored as `MID` or `UP`. Usefulness is smaller (4 candidates) and the risk is low. These are the only structured fields the validator added beyond defect type, and all four look wrongly associated.
4. **Broaden nil and negation only at the phrases already reviewed.** Bare `Nil.`, "no fracture found", and "neither ... nor" are the gaps. Do not widen the five-word `no` window: `AI1SYN-04-020` shows "No decision" already becoming a ruled-out deep-screening finding.

## Next improvement

The single next change worth making, and not made here, is a local phrase and polarity patch in the existing text rules: recognise the word orders in item 1, and in the same change refuse a positive candidate for the instruction, programme, and completion cues in item 2. Do that behind the current report tests, using the example IDs in this note as cases to inspect. Leave abbreviations, the planner, the formulas, and the DMO path alone until a person has reviewed the worksheet.
