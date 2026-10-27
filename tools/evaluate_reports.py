"""Score a gold JSONL corpus with the existing local report NLP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from report_eval import CorpusValidationError, evaluate_corpus, load_corpus, render_summary


def main(argv: list[str] | None = None) -> int:
    """Write a JSON report and a summary. Excerpts stay out unless requested."""
    parser = argparse.ArgumentParser(
        description="Compare the existing local report NLP with a gold JSONL corpus."
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--split", choices=("all", "train", "validation", "test"), default="all")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--summary-out", type=Path)
    parser.add_argument("--include-excerpts", action="store_true")
    args = parser.parse_args(argv)
    try:
        corpus = load_corpus(args.corpus)
    except CorpusValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    selected = corpus.select_split(args.split)
    report = evaluate_corpus(
        selected,
        base_dir=args.corpus.resolve().parent,
        include_excerpts=args.include_excerpts,
    )
    report["split_filter"] = args.split
    report["corpus_document_ids"] = [document.document_id for document in corpus.documents]
    summary = render_summary(report)
    print(summary, end="")
    if args.summary_out is not None:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(summary, encoding="utf-8")
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
