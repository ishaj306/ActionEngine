"""Model proposes, the document disposes.

Every finding a model returns is a proposal. This is where each one is checked
against the source, and the check is not a heuristic -- it is the same span
anchoring the rest of the product is built on, applied to the model's own
words.

    quote -> anchor(quote, document)
             |
             +-- None ................. DROP. The document does not say this.
             +-- score below floor .... KEEP, demoted to UNCERTAIN.
             +-- resolved ............. KEEP, with real offsets into real text.

The first branch is the one that matters. A model that invents a sentence
produces a quote no amount of fuzzy matching will find, so the finding is
discarded before it can become a `Claim` -- which would refuse it anyway, since
a FACT or INFERENCE without evidence raises at construction. Hallucinated
citations are not detected here so much as made impossible to represent.

The second branch is subtler and is why the score travels with the span.
Models quote approximately: a dropped article, re-flowed whitespace, a
corrected typo. That is not fabrication and dropping it would throw away good
findings, but it is not verbatim either, and the difference belongs in the
confidence rather than in a footnote.

A deadline reference is checked differently, because the model was never able
to write a date: it could only cite an id from the list the deterministic layer
supplied. An id that is not in that list is a reference to nothing, and the
action keeps its deadline only when the id resolves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from app.modules.action_engine.planner import ActionVerb
from app.modules.evidence.anchor import AnchorMatch, anchor
from app.modules.evidence.normalize import NormalizedText, normalize
from app.modules.extraction.model import (
    DateOption,
    ModelExtraction,
    ModelGap,
)
from app.modules.extraction.rules import ActionCandidate, GapCandidate

logger = logging.getLogger("action_engine.verify")

__all__ = ["VerificationReport", "verify"]

#: Below this anchoring score a quote is too loose to be called verbatim. The
#: finding survives, because the document plainly contains something close, but
#: it cannot be reported as certain.
VERBATIM_FLOOR = 0.92

#: Below this the quote is not recognisably from the document at all.
KEEP_FLOOR = 0.72

#: What a model-sourced action is worth once its quote has been verified
#: verbatim. Below the rules' 0.94 for an explicit modal, because a model
#: reading an obligation is a judgement and "must" is not.
MODEL_CONFIDENCE = 0.86
MODEL_LOOSE_CONFIDENCE = 0.55


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """What survived, and what did not.

    The counts are not diagnostics. `hallucinated` is the headline number of
    the whole model arm -- claims the model asserted that the document does not
    contain -- and it belongs in the evaluation table, not in a log file.
    """

    actions: tuple[ActionCandidate, ...]
    gaps: tuple[GapCandidate, ...]
    requirements: tuple[str, ...]
    proposed: int = 0
    hallucinated: int = 0
    demoted: int = 0

    @property
    def hallucination_rate(self) -> float:
        return self.hallucinated / self.proposed if self.proposed else 0.0


def verify(
    extraction: ModelExtraction,
    *,
    text: str,
    dates: tuple[DateOption, ...],
    normalized: NormalizedText | None = None,
) -> VerificationReport:
    """Check every model finding against the document it claims to come from."""
    haystack = normalized or normalize(text)
    by_id = {item.id: item for item in dates}

    actions: list[ActionCandidate] = []
    requirements: list[str] = []
    gaps: list[GapCandidate] = []
    proposed = hallucinated = demoted = 0

    for proposed_action in extraction.actions:
        proposed += 1
        found = _locate(proposed_action.quote, text, haystack)
        if found is None:
            hallucinated += 1
            logger.info(
                "dropped an action: quote not in document: %r",
                proposed_action.quote[:80],
            )
            continue

        loose = found.score < VERBATIM_FLOOR
        demoted += int(loose)

        deadline = _deadline_for(proposed_action.deadline_id, by_id)
        actions.append(
            ActionCandidate(
                description=proposed_action.instruction,
                verb=ActionVerb(proposed_action.verb),
                char_start=found.char_start,
                char_end=found.char_end,
                confidence=MODEL_LOOSE_CONFIDENCE if loose else MODEL_CONFIDENCE,
                rationale=_rationale(
                    found,
                    loose,
                    proposed_action.conditional_on,
                    proposed_action.optional,
                    deadline,
                ),
                requires=(),
                conditional_on=proposed_action.conditional_on,
                optional=proposed_action.optional,
                deadline=deadline,
            )
        )

    for proposed_requirement in extraction.requirements:
        proposed += 1
        found = _locate(proposed_requirement.quote, text, haystack)
        if found is None:
            hallucinated += 1
            logger.info(
                "dropped a requirement: quote not in document: %r",
                proposed_requirement.quote[:80],
            )
            continue
        requirements.append(proposed_requirement.text)

    for proposed_gap in extraction.gaps:
        proposed += 1
        found = _locate(proposed_gap.quote, text, haystack)
        if found is None:
            hallucinated += 1
            continue
        gaps.append(_gap(proposed_gap, found))

    return VerificationReport(
        actions=tuple(actions),
        gaps=tuple(gaps),
        requirements=tuple(requirements),
        proposed=proposed,
        hallucinated=hallucinated,
        demoted=demoted,
    )


def _locate(quote: str, text: str, haystack: NormalizedText) -> AnchorMatch | None:
    """Resolve a quote to real offsets, or refuse it.

    `anchor` is asked for a low threshold and the result is judged here, so
    that "found but loose" and "not found at all" stay distinguishable. Merging
    them would lose exactly the information the confidence is supposed to carry.
    """
    found = anchor(quote, text, threshold=KEEP_FLOOR, source_normalized=haystack)
    if found is None or found.score < KEEP_FLOOR:
        return None
    return found


def _deadline_for(
    deadline_id: str | None,
    by_id: dict[str, DateOption],
) -> date | None:
    """Resolve a referenced date id, refusing anything not on the list.

    The model could not write a date; it could only point at one. An id that is
    not in the catalogue points at nothing, so the action loses its deadline
    rather than acquiring an invented one.
    """
    if deadline_id is None:
        return None
    option = by_id.get(deadline_id)
    if option is None:
        logger.info("dropped a deadline reference to an unknown id: %r", deadline_id)
        return None
    try:
        return date.fromisoformat(option.iso)
    except ValueError:
        return None


def _rationale(
    found: AnchorMatch,
    loose: bool,
    conditional_on: str | None,
    optional: bool,
    deadline: date | None,
) -> str:
    if loose:
        parts = [
            "Read by the model. Its quotation matched the document only "
            f"approximately ({found.score:.0%}), so this is reported as "
            "unconfirmed rather than as a finding."
        ]
    else:
        parts = [
            "Read by the model and verified against the document: its "
            "quotation appears in the source verbatim."
        ]
    if deadline:
        parts.append(f"Attached to the stated date of {deadline.isoformat()}.")
    if conditional_on:
        parts.append(f"Applies only {conditional_on}.")
    if optional:
        parts.append("The document offers this rather than requiring it.")
    return " ".join(parts)


def _gap(item: ModelGap, found: AnchorMatch) -> GapCandidate:
    return GapCandidate(
        question=item.question,
        why_it_matters=(
            "The document refers to this without stating it, so the reader "
            "cannot act on it from the document alone."
        ),
        char_start=found.char_start,
        char_end=found.char_end,
        matched_text=found.text,
    )
