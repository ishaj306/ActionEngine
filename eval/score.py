"""Score the engine against the labelled corpus.

    python eval/score.py              # the table
    python eval/score.py --detail     # plus every miss and every false positive

Matching rules are written out below rather than buried, because a benchmark is
only as honest as its notion of "correct", and a rule chosen to flatter the
system is the easiest way to produce a good number that means nothing.

Nothing here is fixed by tuning the matcher. Where the engine is wrong, the
number is meant to say so.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from labels import GoldDocument, Span, load_corpus  # noqa: E402

from app.modules.extraction.model import (  # noqa: E402
    AnthropicExtractor,
    DateOption,
    ModelExtraction,
)
from app.modules.extraction.temporal import extract_temporal  # noqa: E402
from app.modules.ingestion.document import from_text  # noqa: E402
from app.pipeline import analyse  # noqa: E402

#: Fixed so the benchmark is reproducible. Chosen a few days after the circular
#: date in `relative-window`, so that anchoring a relative period to the reading
#: date instead of the document date produces a visibly wrong answer rather than
#: an accidentally right one.
REFERENCE = date(2026, 9, 5)

#: Predicted and gold spans count as the same find when they share this much of
#: the shorter one. Loose on purpose: the engine returns whole sentences and the
#: labels quote fragments, and scoring boundary agreement would measure
#: punctuation rather than extraction.
_SPAN_RATIO = 0.3

#: Requirement wording varies ("previous marksheet" / "copy of the previous
#: marksheet"), so requirements match on content words rather than on strings.
_TOKEN_RATIO = 0.6

_FILLER = frozenset(
    "a an the of to and or with for from in on at self attested copy copies "
    "their your two three completed scanned".split()
)


@dataclass
class Tally:
    """Counts for one stage, across the whole corpus."""

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    misses: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        found = self.true_positive + self.false_positive
        return self.true_positive / found if found else 0.0

    @property
    def recall(self) -> float:
        real = self.true_positive + self.false_negative
        return self.true_positive / real if real else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def gold_count(self) -> int:
        return self.true_positive + self.false_negative


@dataclass
class Prediction:
    """One claim, and whether it turned out to be right.

    Kept per claim rather than per stage because calibration is a claim-level
    question: of everything the engine asserted at 90%, how much was true?
    """

    stage: str
    confidence: float
    correct: bool
    label: str


def tokens(text: str) -> frozenset[str]:
    return frozenset(
        word for word in "".join(
            character if character.isalnum() else " " for character in text.lower()
        ).split() if word not in _FILLER
    )


def token_match(left: str, right: str) -> bool:
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= _TOKEN_RATIO


@dataclass
class Report:
    stages: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    predictions: list[Prediction] = field(default_factory=list)
    type_correct: int = 0
    type_total: int = 0
    #: Per-tag recall, so a category of failure is visible as a category.
    by_tag: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))


class CachedExtractor:
    """Records every model response to disk, and replays it thereafter.

    The ablation is meant to be re-run whenever the merge logic changes, and
    paying for the same fifty documents each time is both wasteful and a good
    way to end up not re-running it. Responses are keyed by document text, so a
    changed corpus re-queries only what changed.

    It also makes the numbers reproducible by anyone with the cache, without a
    key and without spend.
    """

    def __init__(self, directory: Path, inner=None) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.inner = inner
        self.hits = 0
        self.misses = 0

    def extract(self, text: str, dates: tuple[DateOption, ...]) -> ModelExtraction:
        key = _cache_key(text)
        path = self.directory / f"{key}.json"
        if path.exists():
            self.hits += 1
            return ModelExtraction.model_validate_json(path.read_text(encoding="utf-8"))
        if self.inner is None:
            raise RuntimeError(
                f"No cached response for {key} and no live extractor configured. "
                "Set ANTHROPIC_API_KEY to query the model, or run with --arm rules."
            )
        self.misses += 1
        result = self.inner.extract(text, dates)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result


def build_arm(arm: str, cache_dir: Path, documents: list[GoldDocument]):
    """The extractor for one ablation arm, or None for the rule baseline.

    Refuses upfront when the model arm cannot actually run. The pipeline
    deliberately degrades to the rules when a model call fails -- correct in
    production, where a notice needed today beats a perfect reading of it --
    but in a benchmark that silence is a lie: the run completes and prints
    rule-arm numbers under a "hybrid" heading. Better to stop here.
    """
    if arm == "rules":
        return None

    live = AnthropicExtractor() if os.getenv("ANTHROPIC_API_KEY") else None
    extractor = CachedExtractor(cache_dir, inner=live)
    if live is not None:
        return extractor

    missing = [
        gold.name
        for gold in documents
        if not (cache_dir / f"{_cache_key(gold.text)}.json").exists()
    ]
    if missing:
        raise SystemExit(
            f"--arm hybrid needs the model for {len(missing)} of {len(documents)} "
            f"documents and ANTHROPIC_API_KEY is not set.\n"
            f"  Missing: {', '.join(missing[:5])}"
            f"{' ...' if len(missing) > 5 else ''}\n"
            "Set the key to query them (this spends money), or run --arm rules."
        )
    return extractor


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def score_document(gold: GoldDocument, report: Report, extractor=None) -> None:
    analysis = analyse(from_text(gold.text), today=REFERENCE, extractor=extractor)

    report.type_total += 1
    if analysis.document_type.value.lower().replace(" ", "_") == _type_label(gold.document_type):
        report.type_correct += 1

    _score_dates(gold, report)
    _score_deadlines(gold, analysis, report)
    _score_actions(gold, analysis, report)
    _score_requirements(gold, analysis, report)
    _score_conditions(gold, analysis, report)
    _score_gaps(gold, analysis, report)
    _score_modality(gold, analysis, report)


#: Gold vocabulary to the label the engine actually emits. Not identity:
#: `other` surfaces as "Document", and comparing the two strings directly
#: scored a correct answer as wrong.
_TYPE_LABEL = {
    "notice": "notice",
    "form": "form",
    "job_description": "job_description",
    "policy": "policy",
    "other": "document",
}


def _type_label(kind: str) -> str:
    return _TYPE_LABEL[kind]


def _score_dates(gold: GoldDocument, report: Report) -> None:
    """Every date in the document, cutoff or not.

    Separate from deadlines because they fail separately: a date can be found
    and then misjudged as ordinary text, and a date can be missed outright. The
    comma-series bug is invisible unless the two are counted apart.
    """
    tally = report.stages["dates (all)"]
    found = [
        item for item in extract_temporal(gold.text, reference=REFERENCE) if item.resolved
    ]
    unmatched = list(found)

    for want in gold.deadlines:
        hit = next(
            (
                item for item in unmatched
                if item.resolved == want.value
                and Span(item.char_start, item.char_end).overlaps(want.span, ratio=_SPAN_RATIO)
            ),
            None,
        )
        if hit:
            unmatched.remove(hit)
            tally.true_positive += 1
            for tag in gold.tags:
                report.by_tag[tag].true_positive += 1
        else:
            tally.false_negative += 1
            quoted = gold.text[want.span.start : want.span.end]
            tally.misses.append(f"{gold.name}: {want.value} ({quoted!r})")
            for tag in gold.tags:
                report.by_tag[tag].false_negative += 1

    for extra in unmatched:
        tally.false_positive += 1
        tally.spurious.append(f"{gold.name}: {extra.resolved} ({extra.text!r})")


def _score_deadlines(gold: GoldDocument, analysis, report: Report) -> None:
    """Only the dates that are actually cutoffs."""
    tally = report.stages["deadlines (cutoffs)"]
    wanted = [item for item in gold.deadlines if item.is_deadline]
    predicted = list(analysis.deadlines)

    for want in wanted:
        hit = next(
            (
                claim for claim in predicted
                if claim.value == want.value
                and claim.evidence
                and Span(claim.evidence.char_start, claim.evidence.char_end).overlaps(
                    want.span, ratio=_SPAN_RATIO
                )
            ),
            None,
        )
        if hit:
            predicted.remove(hit)
            tally.true_positive += 1
            report.predictions.append(
                Prediction("deadline", hit.confidence.score, True, f"{gold.name}:{want.value}")
            )
        else:
            tally.false_negative += 1
            tally.misses.append(f"{gold.name}: {want.value}")

    for extra in predicted:
        tally.false_positive += 1
        tally.spurious.append(f"{gold.name}: {extra.value}")
        report.predictions.append(
            Prediction("deadline", extra.confidence.score, False, f"{gold.name}:{extra.value}")
        )


def _score_actions(gold: GoldDocument, analysis, report: Report) -> None:
    found = report.stages["actions (found)"]
    verbs = report.stages["actions (verb correct)"]
    predicted = [item.action for item in analysis.plan.scheduled]
    unmatched = list(predicted)

    for want in gold.actions:
        hit = next(
            (
                action for action in unmatched
                if action.claim.evidence
                and Span(
                    action.claim.evidence.char_start, action.claim.evidence.char_end
                ).overlaps(want.span, ratio=_SPAN_RATIO)
            ),
            None,
        )
        if hit:
            unmatched.remove(hit)
            found.true_positive += 1
            correct_verb = hit.verb.value == want.verb
            if correct_verb:
                verbs.true_positive += 1
            else:
                verbs.false_positive += 1
                verbs.misses.append(
                    f"{gold.name}: {want.gist!r} wanted {want.verb}, got {hit.verb.value}"
                )
            report.predictions.append(
                Prediction("action", hit.claim.confidence.score, correct_verb, f"{gold.name}:{want.gist}")
            )
            for tag in gold.tags:
                report.by_tag[tag].true_positive += 1
        else:
            found.false_negative += 1
            verbs.false_negative += 1
            found.misses.append(f"{gold.name}: {want.gist!r} ({want.verb})")
            for tag in gold.tags:
                report.by_tag[tag].false_negative += 1

    for extra in unmatched:
        found.false_positive += 1
        found.spurious.append(f"{gold.name}: {extra.description!r}")
        report.predictions.append(
            Prediction("action", extra.claim.confidence.score, False, f"{gold.name}:{extra.description}")
        )


def _score_requirements(gold: GoldDocument, analysis, report: Report) -> None:
    tally = report.stages["requirements"]
    kinds = report.stages["requirement kind"]
    unmatched = list(analysis.requirements)

    for want in gold.requirements:
        hit = next((item for item in unmatched if token_match(item.text, want.text)), None)
        if hit:
            unmatched.remove(hit)
            tally.true_positive += 1
            if hit.kind.value == want.kind:
                kinds.true_positive += 1
            else:
                kinds.false_positive += 1
                kinds.misses.append(
                    f"{gold.name}: {want.text!r} wanted {want.kind}, got {hit.kind.value}"
                )
        else:
            tally.false_negative += 1
            kinds.false_negative += 1
            tally.misses.append(f"{gold.name}: {want.text!r}")

    for extra in unmatched:
        tally.false_positive += 1
        tally.spurious.append(f"{gold.name}: {extra.text!r}")


def _score_conditions(gold: GoldDocument, analysis, report: Report) -> None:
    tally = report.stages["eligibility conditions"]
    unmatched = list(analysis.conditions)

    for want in gold.conditions:
        hit = next(
            (item for item in unmatched if item.criterion.attribute.value == want.attribute),
            None,
        )
        if hit:
            unmatched.remove(hit)
            if hit.criterion.comparator.value == want.comparator:
                tally.true_positive += 1
            else:
                tally.false_positive += 1
                tally.misses.append(
                    f"{gold.name}: {want.attribute} wanted {want.comparator}, "
                    f"got {hit.criterion.comparator.value}"
                )
        else:
            tally.false_negative += 1
            tally.misses.append(f"{gold.name}: {want.attribute} {want.comparator} {want.value}")

    for extra in unmatched:
        tally.false_positive += 1
        tally.spurious.append(f"{gold.name}: {extra.criterion.requirement!r}")


def _score_gaps(gold: GoldDocument, analysis, report: Report) -> None:
    tally = report.stages["information gaps"]
    unmatched = list(analysis.gaps)

    for want in gold.gaps:
        hit = next(
            (
                gap for gap in unmatched
                if gap.prompted_by
                and Span(gap.prompted_by.char_start, gap.prompted_by.char_end).overlaps(
                    want, ratio=_SPAN_RATIO
                )
            ),
            None,
        )
        if hit:
            unmatched.remove(hit)
            tally.true_positive += 1
        else:
            tally.false_negative += 1
            tally.misses.append(f"{gold.name}: {gold.text[want.start : want.end]!r}")

    for extra in unmatched:
        tally.false_positive += 1
        tally.spurious.append(f"{gold.name}: {extra.question!r}")


def _score_modality(gold: GoldDocument, analysis, report: Report) -> None:
    """Conditional and optional obligations.

    An action the reader can decline, or one that applies only to some people,
    is the most consequential thing to get wrong here: presented as mandatory it
    sends someone to fetch a document they never needed.

    Scored only over actions the engine actually found, so this row measures
    whether modality is read correctly rather than re-punishing a recall miss
    the actions row has already counted.
    """
    tally = report.stages["conditional / optional"]
    predicted = [item.action for item in analysis.plan.scheduled]

    for want in gold.actions:
        if not (want.conditional or want.optional):
            continue
        hit = next(
            (
                action for action in predicted
                if action.claim.evidence
                and Span(
                    action.claim.evidence.char_start, action.claim.evidence.char_end
                ).overlaps(want.span, ratio=_SPAN_RATIO)
            ),
            None,
        )
        kind = "conditional" if want.conditional else "optional"
        if hit is None:
            tally.false_negative += 1
            tally.misses.append(f"{gold.name}: {want.gist!r} ({kind}) -- action not found")
            continue
        got_conditional = hit.conditional_on is not None
        if (want.conditional and got_conditional) or (want.optional and hit.optional):
            tally.true_positive += 1
        else:
            tally.false_negative += 1
            tally.misses.append(
                f"{gold.name}: {want.gist!r} is {kind}, reported as mandatory"
            )

    # An action wrongly marked restricted is its own harm: it tells a reader a
    # step is not theirs when it is.
    for action in predicted:
        if action.conditional_on is None and not action.optional:
            continue
        match = next(
            (
                want for want in gold.actions
                if (want.conditional or want.optional)
                and action.claim.evidence
                and Span(
                    action.claim.evidence.char_start, action.claim.evidence.char_end
                ).overlaps(want.span, ratio=_SPAN_RATIO)
            ),
            None,
        )
        if match is None:
            tally.false_positive += 1
            tally.spurious.append(
                f"{gold.name}: {action.description!r} marked "
                f"{action.conditional_on or 'optional'}, but is neither"
            )


def calibration(predictions: list[Prediction]) -> list[tuple[str, int, float, float]]:
    """Claimed confidence against observed accuracy.

    The single most important table here. Everything the engine emits carries a
    number; this is the only thing that says whether the number means anything.
    """
    buckets: dict[str, list[Prediction]] = defaultdict(list)
    for item in predictions:
        low = min(int(item.confidence * 10) / 10, 0.9)
        buckets[f"{low:.1f}-{low + 0.1:.1f}"] = buckets[f"{low:.1f}-{low + 0.1:.1f}"] + [item]

    rows = []
    for name in sorted(buckets):
        group = buckets[name]
        claimed = sum(item.confidence for item in group) / len(group)
        actual = sum(1 for item in group if item.correct) / len(group)
        rows.append((name, len(group), claimed, actual))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail", action="store_true", help="list every miss")
    parser.add_argument("--corpus", type=Path, default=Path(__file__).parent / "corpus")
    parser.add_argument(
        "--arm",
        choices=["rules", "hybrid"],
        default="rules",
        help="rules: deterministic only, free. hybrid: adds the model arm, "
        "which queries the API for any document not already cached.",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(__file__).parent / "cache",
        help="Where model responses are recorded and replayed from.",
    )
    args = parser.parse_args()

    documents = load_corpus(args.corpus)
    extractor = build_arm(args.arm, args.cache, documents)
    report = Report()
    for gold in documents:
        score_document(gold, report, extractor)

    print(f"\nCorpus: {len(documents)} documents, reference date {REFERENCE}")
    print(f"Arm:    {args.arm}")
    if isinstance(extractor, CachedExtractor):
        print(f"Model:  {extractor.hits} cached, {extractor.misses} queried")
    print()

    print(f"{'stage':<28} {'gold':>5} {'P':>7} {'R':>7} {'F1':>7}")
    print("-" * 58)
    for name, tally in report.stages.items():
        print(
            f"{name:<28} {tally.gold_count:>5} "
            f"{tally.precision:>7.2f} {tally.recall:>7.2f} {tally.f1:>7.2f}"
        )
    accuracy = report.type_correct / report.type_total if report.type_total else 0.0
    print(f"{'document type (accuracy)':<28} {report.type_total:>5} {accuracy:>23.2f}")

    print(f"\n{'confidence claimed':<20} {'n':>5} {'mean claimed':>14} {'actual':>9}")
    print("-" * 52)
    for name, count, claimed, actual in calibration(report.predictions):
        flag = "  <-- overconfident" if claimed - actual > 0.15 else ""
        print(f"{name:<20} {count:>5} {claimed:>14.2f} {actual:>9.2f}{flag}")

    if args.detail:
        for name, tally in report.stages.items():
            if not tally.misses and not tally.spurious:
                continue
            print(f"\n### {name}")
            for miss in tally.misses:
                print(f"  MISSED    {miss}")
            for extra in tally.spurious:
                print(f"  SPURIOUS  {extra}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
