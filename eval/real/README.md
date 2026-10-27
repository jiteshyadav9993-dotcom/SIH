# Authorised report corpus

Put anonymised source files in `corpus/` and their JSONL annotation beside them, for example `corpus/gold.jsonl`. Both the files and the annotation stay on this machine. Git ignores `corpus/` and `output/`.

Do not copy `sample_data/`, the scripted demo PDF, or unit-test sentences into this folder and mark them as authorised reports.

The field rules are in `eval/ANNOTATION_GUIDE.md`. Score a held-out test split with:

```
python tools/evaluate_reports.py --corpus eval/real/corpus/gold.jsonl --split test --json-out eval/real/output/report.json --summary-out eval/real/output/summary.txt
```

A result is not a real-world accuracy claim until the guide's minimum size and coverage are met. Add `--include-excerpts` only for a local review file inside `output/`.
