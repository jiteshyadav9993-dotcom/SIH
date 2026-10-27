# Stage 10 behavior compared with the Stage 9 baseline

This note compares two runs of the local text pipeline on the same 216 synthetic sentences. It is a behavioral regression check. It is not an accuracy, precision, recall, or F1 result, and the generated answer key was not used.

Both runs used the same in-memory path: one sentence as a native-text section, `simplify_report` with a fresh `LocalTextProvider`, then `validate_candidates` with an empty defect list and no reference date. The scripted fixture stayed off (`scripted_fixture` null, provider `local-text`, NLP status `pending`). No upload, storage, apply, or backlog call was used. The answer key was not opened by the runner and does not appear in the new prediction records.

The Stage 9 file `baseline_predictions.jsonl` was loaded only for this comparison. It was not passed to the proposer or the validator. Its hash was the same after the new prediction file was written.

## Counts from the two output files

Input-level outcome. One input is one row, even when it produced more than one finding.

| Outcome | Stage 9 | After Stage 10 |
| --- | ---: | ---: |
| No finding | 130 | 139 |
| Candidates only | 70 | 58 |
| Ruled out only | 16 | 19 |
| Both a candidate and a ruled-out finding | 0 | 0 |
| Processed | 216 | 216 |
| Failed | 0 | 0 |

Finding-level outcome.

| Outcome | Stage 9 | After Stage 10 |
| --- | ---: | ---: |
| Proposals | 89 | 77 |
| Candidates | 73 | 58 |
| Candidate review `NEEDS_REVIEW` | 73 | 58 |
| Candidate review `READY` | 0 | 0 |
| Ruled out | 16 | 19 |
| Ruled out `negated_finding` | 11 | 13 |
| Ruled out `nil_report` | 5 | 6 |
| Preview `NEEDS_REVIEW` | 73 | 58 |
| Preview `RULED_OUT` | 16 | 19 |
| Preview `READY_TO_APPLY`, `REJECTED`, or `POSSIBLE_DUPLICATE` | 0 | 0 |
| Positive candidates | 61 | 40 |
| Uncertain candidates | 12 | 18 |

Pipeline warnings stayed empty. No severity, chainage, due date, overdue days, TSR, or track line was filled on any current candidate. Stage 9 had filled a track line on four candidates. Those four values are gone.

Candidate categories after Stage 10: `TAMPING_REQUIRED` 14, `DEEP_SCREENING` 13, `OHE_SAG` 9, `SIGNAL_INTERLOCK` 7, `WELD_FAILURE` 6, `RAIL_FRACTURE` 6, no type 3.

## What changed

32 of the 216 inputs have a different outcome, excerpt, polarity, category, or track line. The other 184 are unchanged. Every change is listed below. None of them invented a scheduling field, and none became `READY_TO_APPLY`.

### Newly recognized wording

These four inputs had no finding in Stage 9 and are now one positive candidate in `NEEDS_REVIEW`. The excerpt is the whole sentence. No line, chainage, severity, overdue days, or TSR was added.

| Example | Sentence | Category now |
| --- | --- | --- |
| `AI1SYN-01-005` | AT weld failed at joint, gap visible, SSE told | `WELD_FAILURE` |
| `AI1SYN-02-008` | AT weld failed at joint, separation visible, gang called | `WELD_FAILURE` |
| `AI1SYN-02-022` | At the cabin, interlocking failed to prove point detection… | `SIGNAL_INTERLOCK` |
| `AI1SYN-02-016` | ohe sagging badly mid span, slip raised | `OHE_SAG` |

`AI1SYN-01-011` ("signal interlocking failed") was already a `SIGNAL_INTERLOCK` candidate before Stage 10, through the older phrase `signal interlocking`. It is unchanged.

`AI1SYN-02-016` also contains "mid span". The new candidate has no track line.

### Instructions, plans, and finished work that no longer become jobs

These inputs were positive candidates and are now no finding, or a ruled-out observation, or only the separate live clause.

| Example | Stage 9 | After Stage 10 |
| --- | --- | --- |
| `AI1SYN-02-052` | positive rail fracture, "watch for" | no finding |
| `AI1SYN-02-054` | positive OHE sag, "report any" | no finding |
| `AI1SYN-02-055` | positive tamping, "check whether" | no finding |
| `AI1SYN-05-025` | positive signal, "must test" / "point machine" | no finding |
| `AI1SYN-02-053` | no finding ("If weld failure is seen") | unchanged, still no finding |
| `AI1SYN-04-022` | positive tamping, machine reached the depot | no finding |
| `AI1SYN-04-024` | positive tamping, "was carried out" | no finding |
| `AI1SYN-04-028` | positive deep screening, yearly programme | no finding |
| `AI1SYN-04-032` | positive tamping, tamping cycle | no finding |
| `AI1SYN-02-003` | positive rail fracture, "was renewed" | no finding |
| `AI1SYN-02-012` | positive weld failure, "has been rectified" | no finding |
| `AI1SYN-02-026` | positive signal, "was rectified" | no finding |
| `AI1SYN-02-040` | positive deep screening, "was completed" | no finding |
| `AI1SYN-04-018` | positive deep screening, "finished" | no finding |
| `AI1SYN-04-008` | positive tamping, "not warranted" | ruled out, `negated_finding` |
| `AI1SYN-04-009` | two positive tamping candidates | the "tamping done" clause is gone; "nothing outstanding for tamping" is ruled out |
| `AI1SYN-03-027` | positive weld failure and positive rail fracture | only "today a separate rail fracture is noted 15 m away." |
| `AI1SYN-04-007` | finished "up loop" tamping as `UP`, plus a second tamping candidate | only "the main line beside it still requires tamping.", with no track line |

### Nil, neither, and not-yet-checked

| Example | Stage 9 | After Stage 10 |
| --- | --- | --- |
| `AI1SYN-01-009` | positive OHE sag for "OHE sag nil" | ruled out, `nil_report` |
| `AI1SYN-03-031` | positive candidate with no category for "Neither rail fracture nor weld failure" | ruled out, `negated_finding` |
| `AI1SYN-01-020` | no finding for bare "Nil." | unchanged, still no finding |
| `AI1SYN-02-006` | positive rail fracture | same excerpt, uncertain, high uncertainty, `NEEDS_REVIEW` |
| `AI1SYN-02-021` | positive OHE sag, "not yet checked" | uncertain `OHE_SAG`, `NEEDS_REVIEW` |
| `AI1SYN-02-042` | positive deep screening, "has not been examined" | uncertain `DEEP_SCREENING`, `NEEDS_REVIEW` |
| `AI1SYN-03-023` | positive rail fracture, "not yet verified" | uncertain `RAIL_FRACTURE`, `NEEDS_REVIEW` |
| `AI1SYN-04-011` | positive tamping, "has not been assessed" | uncertain `TAMPING_REQUIRED`, `NEEDS_REVIEW` |
| `AI1SYN-04-020` | ruled out as `negated_finding` | uncertain `DEEP_SCREENING`, high uncertainty, `NEEDS_REVIEW` |

`AI1SYN-04-020` matches the Stage 10 report. "No decision yet" is no longer treated as proof that deep screening was ruled out. It is also not a confirmed job: preview status is `NEEDS_REVIEW`, and no scheduling fields were filled.

`AI1SYN-03-014` ("no defect found") was already `nil_report` and did not change.

### Track line

Stage 9 stored a line on four candidates. All four were ordinary English, not a running line. After Stage 10 none of the 216 candidates has a track line.

| Example | Stage 9 line | After Stage 10 |
| --- | --- | --- |
| `AI1SYN-01-028` | `MID` from "mid span" | `OHE_SAG` candidate, line empty |
| `AI1SYN-06-002` | `MID` from "mid span" | `OHE_SAG` candidate, line empty |
| `AI1SYN-04-012` | `UP` from "right up to" | `DEEP_SCREENING` candidate, line empty |
| `AI1SYN-04-007` | `UP` from "up loop" on the finished clause | that clause is not emitted; the remaining tamping clause has no line |

This packet does not show that valid `DN`, `UP_FAST`, or `MID` extraction still works. No sentence contains `UP_FAST`, `DN fast`, or `MID line`. The sentences that say "dn line" (`AI1SYN-01-001`, `AI1SYN-02-007`) or "up line" (`AI1SYN-03-009`) produced no finding, so a line was never read. `AI1SYN-03-009` is "Thermit weld on the up line has failed". The words between "weld" and "has failed" keep it outside both the old phrase and the new `weld failed` phrase. Preservation of those valid line forms was checked in `tests/test_ai1_local_rule_safety.py` during Stage 10, not by these 216 sentences.

## Evidence

For all 216 new records, the sentence, source PDF, page, and `source_reference` match the input row. Every proposal, candidate, ruled-out, and preview excerpt is a substring of that sentence. References on proposals, candidates, and ruled-out rows stay `source_reference`. No answer-key field name is present in the prediction file.

## Where the Stage 10 report does not cover the current output

These inputs still produce a positive, low-uncertainty candidate. They were already doing so in Stage 9, so they are not new regressions. They are also not the cases the Stage 10 patterns actually name.

`AI1SYN-02-035`, "Need for tamping not assessed yet.", is still a positive `TAMPING_REQUIRED` candidate with low uncertainty. The Stage 10 not-checked rule looks for "not yet assessed" and "has not been assessed". This sentence puts "yet" after "assessed", so the rule does not fire. The implementation report said not-yet-checked wording stays under review. This wording does not.

Completed or repaired wording that uses a different verb is still a positive candidate:

- `AI1SYN-02-019`: "OHE sag regulated earlier and rechecked within limits, none outstanding." Positive `OHE_SAG`. The filter looks for "nothing outstanding", not "none outstanding", and it does not treat "regulated earlier" as finished work.
- `AI1SYN-03-025`: "Defective weld was cut out, a new thermit weld made and tested satisfactory." Positive `WELD_FAILURE`. "was cut out" and "tested satisfactory" are not in the finished-work list.
- `AI1SYN-03-026`: "Weld failure of last month stands rectified; no fresh failure noted." Positive `WELD_FAILURE`. "stands rectified" is not "was rectified" or "has been rectified".
- `AI1SYN-05-007`: the interlocking fault "has been changed" and the sentence ends "nothing outstanding." Positive `SIGNAL_INTERLOCK`. "has been changed" is not one of the finished-work verbs. "nothing outstanding" sits too far after the defect phrase to negate it, and the second clause has no defect phrase of its own, so it is not split off.

`AI1SYN-03-030`, "Staff told to differentiate rail fracture from weld failure…", is still one positive candidate with no category. Stage 10 blocks "watch for", "report any", "check whether", and "must test". "told to" is not one of those.

`AI1SYN-03-029`, "Whether the break is a rail fracture or a weld failure is not yet determined.", is still one positive untyped candidate. "not yet determined" is not the existing "has not been determined" hedge, so it does not become uncertain.

`AI1SYN-04-003` states a twist and then "tamping to be programmed in the next available block." It remains a positive `TAMPING_REQUIRED` candidate. "to be programmed" is not the yearly-programme or "will be carried out" pattern.

No input that Stage 9 left as a live defect was dropped unless the sentence matches one of the filters above. The 32 changes are the new phrase hits, the filter hits, the nil and neither changes, the not-checked uncertainty changes, and the four line corrections.

## Remaining weaknesses

- Close paraphrases of finished work and not-yet-checked work still become positive candidates: `AI1SYN-02-019`, `AI1SYN-02-035`, `AI1SYN-03-025`, `AI1SYN-03-026`, `AI1SYN-05-007`.
- Instruction and ambiguity that the new patterns do not name: `AI1SYN-03-030`, `AI1SYN-03-029`, `AI1SYN-04-003`.
- `AI1SYN-01-002` still produces no finding, so "KM 123/4" is still unread.
- `AI1SYN-03-009` still produces no finding, so "on the up line" is still unread.
- Abbreviated fragments such as `AI1SYN-01-001` ("rail frcture") still produce no finding.
- Bare `Nil.` (`AI1SYN-01-020`) still produces no finding and no category.
- Every candidate on this packet remains `NEEDS_REVIEW` because these isolated sentences do not state the scheduling fields. That is expected for this set. It does not show planner or DMO behavior.

## Next improvement

The single next change worth making, and not made here, is a small extension of the existing completion and not-checked checks so the five sentences above stop becoming positive jobs: "stands rectified", "none outstanding", "was cut out" followed by a satisfactory test, "has been changed" with a later "nothing outstanding", and "not assessed yet". Keep that inside the current local rules. Do not add a new defect category, and do not treat the provisional labels as truth.

## How this run was executed

```
cmd /c "set DATABASE_URL=sqlite:///C:/Users/JITESH/AppData/Local/Temp/ai1_stage11_unused.db&& python - < C:\Users\JITESH\AppData\Local\Temp\ai1_stage11_compare.py"
```

The temporary sqlite URL was set so the process could not open the live `block_planning.db` if an import connected. The run returned 216 records, 0 failures, 32 changed inputs, 0 grounding failures, and 0 answer-key field names. Protected benchmark files, including `baseline_predictions.jsonl`, had the same SHA-256 after the prediction file was written. No production file, test, model, or database was edited.
