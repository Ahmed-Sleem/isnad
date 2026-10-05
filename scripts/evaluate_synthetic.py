"""Run a deterministic synthetic baseline; this is not a production benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from isnad_core.engine import VerificationEngine
from isnad_core.models import MatchStatus, VerificationInput
from isnad_core.normalization import normalize_with_spans
from isnad_core.quran import QuranCorpus
from isnad_core.streaming import StreamEventType, StreamingCitationGate

SEED = 20_261_005
SAMPLE_SIZE = 200
DETECTION_POSITIVES = 100
VERIFICATION_POSITIVES = 100


class _NoopEngine:
    """Keep the marker-detection portion independent of source matching."""

    def verify(self, citation: VerificationInput):
        raise AssertionError(f"Detection-only case unexpectedly reached verification: {citation!r}")


def _f1(true_positive: int, false_positive: int, false_negative: int) -> dict[str, Any]:
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
    }


def run_evaluation() -> dict[str, Any]:
    """Generate and score fixed-count examples from the pinned Arabic source."""

    corpus = QuranCorpus.load_default()
    normalized_counts = Counter(
        normalize_with_spans(verse.text, language="ar").value for verse in corpus.verses
    )
    unique_verses = [
        verse
        for verse in corpus.verses
        if normalized_counts[normalize_with_spans(verse.text, language="ar").value] == 1
    ]
    if len(unique_verses) < SAMPLE_SIZE:
        raise RuntimeError("The pinned corpus has too few unique ayahs for the synthetic set.")
    sample = sorted(
        unique_verses,
        key=lambda verse: hashlib.sha256(f"{SEED}:{verse.reference}".encode()).digest(),
    )[:SAMPLE_SIZE]
    reference_indices = {verse.reference: index for index, verse in enumerate(corpus.verses)}

    engine = VerificationEngine()
    try:
        detection = _evaluate_marker_detection(sample[:DETECTION_POSITIVES], engine)
        verification = _evaluate_verification_precision(sample[:VERIFICATION_POSITIVES], engine)
        correction = _evaluate_ayah_correction(sample, engine, corpus, reference_indices)
    finally:
        engine.close()

    return {
        "evaluation_id": "isnad-synthetic-baseline-v1",
        "seed": SEED,
        "source": {
            "source_id": corpus.source.source_id,
            "version": corpus.source.version,
            "content_sha256": corpus.source.content_sha256,
        },
        "sample_references": [verse.reference for verse in sample],
        "case_counts": {
            "marker_detection": 2 * DETECTION_POSITIVES,
            "verification_precision": 2 * VERIFICATION_POSITIVES,
            "ayah_correction": SAMPLE_SIZE,
        },
        "metrics": {
            "explicit_marker_detection": {
                **detection,
                "target": 0.85,
                "target_met_on_this_synthetic_set": detection["f1"] >= 0.85,
                "interpretation": (
                    "Reserved streaming-marker recognition only; this does not measure "
                    "free-form natural-language citation extraction."
                ),
            },
            "textual_verification_precision": {
                **verification,
                "target": 0.90,
                "target_met_on_this_synthetic_set": verification["precision"] >= 0.90,
            },
            "wrong_reference_ayah_correction_accuracy": {
                **correction,
                "target": 0.95,
                "target_met_on_this_synthetic_set": correction["accuracy"] >= 0.95,
            },
        },
        "limitations": [
            "Examples are generated from the same pinned Qur'an source used by the verifier.",
            (
                "Quotes are clean source ayahs or obviously unrelated synthetic strings; "
                "there is no human annotation."
            ),
            (
                "The location task tests Arabic ayah lookup when a full source ayah is cited "
                "at a wrong reference."
            ),
            "No free-form citation extractor is implemented or evaluated by this report.",
            (
                "Synthetic scores do not establish production performance or the original "
                "product targets."
            ),
        ],
    }


def _evaluate_marker_detection(verses, engine: VerificationEngine) -> dict[str, Any]:
    true_positive = false_positive = false_negative = 0
    for index, verse in enumerate(verses):
        text = (
            f"prose {index} [[ISNAD-CITATION source=quran language=ar "
            f"reference={verse.reference}]]{verse.text}[[/ISNAD-CITATION]]"
        )
        gate = StreamingCitationGate(engine)
        events = list(gate.feed(text)) + list(gate.finish())
        predicted = any(event.type is StreamEventType.CITATION_CHECKING for event in events)
        true_positive += int(predicted)
        false_negative += int(not predicted)

    for index in range(DETECTION_POSITIVES):
        gate = StreamingCitationGate(_NoopEngine())
        prose = f"Ordinary prose mentioning a book title, section, or number {index}."
        events = list(gate.feed(prose)) + list(gate.finish())
        predicted = any(event.type is StreamEventType.CITATION_CHECKING for event in events)
        false_positive += int(predicted)

    return _f1(true_positive, false_positive, false_negative)


def _evaluate_verification_precision(verses, engine: VerificationEngine) -> dict[str, Any]:
    true_positive = false_positive = false_negative = 0
    for verse in verses:
        result = engine.verify(VerificationInput("quran", "ar", verse.text, verse.reference))
        predicted_match = result.status in {
            MatchStatus.EXACT_MATCH,
            MatchStatus.NORMALIZED_MATCH,
            MatchStatus.PARTIAL_MATCH,
        }
        true_positive += int(predicted_match)
        false_negative += int(not predicted_match)

    for index, verse in enumerate(verses):
        unrelated = f"Synthetic unrelated phrase number {index} from no cited ayah."
        result = engine.verify(VerificationInput("quran", "ar", unrelated, verse.reference))
        predicted_match = result.status in {
            MatchStatus.EXACT_MATCH,
            MatchStatus.NORMALIZED_MATCH,
            MatchStatus.PARTIAL_MATCH,
        }
        false_positive += int(predicted_match)

    denominator = true_positive + false_positive
    return {
        "precision": round(true_positive / denominator, 4) if denominator else 0.0,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
    }


def _evaluate_ayah_correction(sample, engine, corpus, reference_indices) -> dict[str, Any]:
    correct = 0
    status_counts: Counter[str] = Counter()
    for verse in sample:
        next_index = (reference_indices[verse.reference] + 1) % len(corpus.verses)
        wrong_reference = corpus.verses[next_index].reference
        result = engine.verify(VerificationInput("quran", "ar", verse.text, wrong_reference))
        status_counts[result.status.value] += 1
        correct += int(
            result.status is MatchStatus.QUOTE_FOUND_WRONG_REFERENCE
            and result.matched_references == (verse.reference,)
        )
    return {
        "accuracy": round(correct / len(sample), 4),
        "correct": correct,
        "incorrect": len(sample) - correct,
        "status_counts": dict(sorted(status_counts.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for a JSON report; stdout is always written.",
    )
    args = parser.parse_args()
    report = json.dumps(run_evaluation(), ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
