"""Action planning and backward scheduling.

Two classes of behaviour are load-bearing here. First, the plan must survive a
malformed dependency graph, because the graph comes from a language model.
Second, backward deadline propagation must be correct, since it is what lets
the product say "start this today or you will miss the deadline".
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.claims import Claim, ClaimClass, Confidence
from app.domain.span import EvidenceSpan
from app.modules.action_engine.planner import (
    Action,
    ActionVerb,
    Priority,
    build_plan,
)

TODAY = date(2026, 9, 1)

SPAN = EvidenceSpan(page=1, char_start=0, char_end=10, text="stated in")


def stated(text: str = "stated in the notice") -> Claim[str]:
    return Claim[str](
        value=text,
        classification=ClaimClass.FACT,
        confidence=Confidence(score=0.95, rationale="Directly stated."),
        evidence=SPAN,
    )


def unconfirmed(text: str = "implied") -> Claim[str]:
    return Claim[str](
        value=text,
        classification=ClaimClass.UNCERTAIN,
        confidence=Confidence(score=0.4, rationale="Implied, not stated."),
    )


def make(
    action_id: str,
    *,
    verb: ActionVerb = ActionVerb.PREPARE,
    deadline: date | None = None,
    effort: int = 1,
    depends_on: set[str] | None = None,
    claim: Claim[str] | None = None,
) -> Action:
    return Action(
        id=action_id,
        description=action_id.replace("_", " "),
        verb=verb,
        claim=claim or stated(),
        deadline=deadline,
        effort_days=effort,
        depends_on=frozenset(depends_on or set()),
    )


def ids(plan) -> list[str]:
    return [item.action.id for item in plan.scheduled]


def lookup(plan, action_id: str):
    return next(item for item in plan.scheduled if item.action.id == action_id)


class TestOrdering:
    def test_prerequisites_come_before_dependents(self):
        plan = build_plan(
            [
                make("submit", depends_on={"fill"}),
                make("fill", depends_on={"collect"}),
                make("collect"),
            ],
            today=TODAY,
        )

        assert ids(plan) == ["collect", "fill", "submit"]

    def test_depth_reflects_the_longest_prerequisite_chain(self):
        plan = build_plan(
            [
                make("submit", depends_on={"fill"}),
                make("fill", depends_on={"collect"}),
                make("collect"),
            ],
            today=TODAY,
        )

        assert [lookup(plan, name).depth for name in ("collect", "fill", "submit")] == [0, 1, 2]

    def test_independent_actions_are_ordered_by_deadline(self):
        plan = build_plan(
            [
                make("later", deadline=date(2026, 10, 1)),
                make("sooner", deadline=date(2026, 9, 5)),
                make("undated"),
            ],
            today=TODAY,
        )

        assert ids(plan) == ["sooner", "later", "undated"]

    def test_ordering_is_deterministic_across_input_permutations(self):
        actions = [
            make("a", deadline=date(2026, 9, 10)),
            make("b", deadline=date(2026, 9, 10)),
            make("c", deadline=date(2026, 9, 10)),
        ]
        forward = build_plan(actions, today=TODAY)
        reversed_input = build_plan(list(reversed(actions)), today=TODAY)

        assert ids(forward) == ids(reversed_input)


class TestBackwardScheduling:
    def test_prerequisite_inherits_its_dependents_deadline(self):
        plan = build_plan(
            [
                make("submit", deadline=date(2026, 9, 18), effort=1, depends_on={"certificate"}),
                make("certificate", effort=10),
            ],
            today=TODAY,
        )

        certificate = lookup(plan, "certificate")
        assert certificate.effective_deadline == date(2026, 9, 17)
        assert certificate.latest_start == date(2026, 9, 7)

    def test_slack_goes_negative_when_the_plan_cannot_be_met(self):
        plan = build_plan(
            [
                make("submit", deadline=date(2026, 9, 5), effort=1, depends_on={"certificate"}),
                make("certificate", effort=10),
            ],
            today=TODAY,
        )

        assert lookup(plan, "certificate").slack_days < 0
        assert lookup(plan, "certificate").priority is Priority.OVERDUE
        assert not plan.is_feasible

    def test_a_comfortable_plan_is_feasible(self):
        plan = build_plan(
            [
                make("submit", deadline=date(2026, 12, 1), effort=1, depends_on={"certificate"}),
                make("certificate", effort=10),
            ],
            today=TODAY,
        )

        assert plan.is_feasible

    def test_the_tightest_downstream_deadline_wins(self):
        plan = build_plan(
            [
                make("urgent", deadline=date(2026, 9, 10), effort=1, depends_on={"shared"}),
                make("relaxed", deadline=date(2026, 11, 1), effort=1, depends_on={"shared"}),
                make("shared", effort=2),
            ],
            today=TODAY,
        )

        assert lookup(plan, "shared").effective_deadline == date(2026, 9, 9)

    def test_actions_without_any_governing_deadline_have_no_slack(self):
        plan = build_plan([make("optional")], today=TODAY)

        assert lookup(plan, "optional").slack_days is None
        assert lookup(plan, "optional").priority is Priority.LOW


class TestPriority:
    @pytest.mark.parametrize(
        ("deadline", "expected"),
        [
            (date(2026, 8, 30), Priority.OVERDUE),
            (date(2026, 9, 2), Priority.CRITICAL),
            (date(2026, 9, 6), Priority.HIGH),
            (date(2026, 9, 15), Priority.MEDIUM),
            (date(2026, 12, 1), Priority.LOW),
        ],
    )
    def test_priority_tracks_remaining_slack(self, deadline, expected):
        plan = build_plan([make("task", deadline=deadline)], today=TODAY)

        assert lookup(plan, "task").priority is expected

    def test_unconfirmed_actions_are_held_back_regardless_of_deadline(self):
        plan = build_plan(
            [make("maybe", deadline=date(2026, 9, 2), claim=unconfirmed())],
            today=TODAY,
        )

        item = lookup(plan, "maybe")
        assert item.priority is Priority.LOW
        assert "verify" in item.rationale.lower()

    def test_rationale_distinguishes_inherited_from_stated_deadlines(self):
        plan = build_plan(
            [
                make("submit", deadline=date(2026, 9, 18), depends_on={"collect"}),
                make("collect"),
            ],
            today=TODAY,
        )

        assert "stated deadline" in lookup(plan, "submit").rationale.lower()
        assert "dependent action" in lookup(plan, "collect").rationale.lower()


class TestMalformedGraphs:
    def test_a_dependency_cycle_is_broken_and_reported(self):
        plan = build_plan(
            [
                make("a", depends_on={"b"}, deadline=date(2026, 9, 5)),
                make("b", depends_on={"a"}, deadline=date(2026, 9, 20)),
            ],
            today=TODAY,
        )

        assert plan.broken_cycles
        assert len(plan.scheduled) == 2

    def test_a_three_node_cycle_still_produces_a_complete_plan(self):
        plan = build_plan(
            [
                make("a", depends_on={"c"}),
                make("b", depends_on={"a"}),
                make("c", depends_on={"b"}),
            ],
            today=TODAY,
        )

        assert sorted(ids(plan)) == ["a", "b", "c"]
        assert len(plan.broken_cycles) == 1

    def test_reference_to_an_unknown_action_is_dropped_and_reported(self):
        plan = build_plan([make("submit", depends_on={"ghost"})], today=TODAY)

        assert plan.dangling_dependencies == (("submit", "ghost"),)
        assert lookup(plan, "submit").blocked_by == ()

    def test_self_dependency_is_ignored(self):
        plan = build_plan([make("loop", depends_on={"loop"})], today=TODAY)

        assert lookup(plan, "loop").blocked_by == ()


class TestReadiness:
    def test_blocked_by_lists_incomplete_prerequisites_only(self):
        plan = build_plan(
            [
                make("submit", depends_on={"fill", "collect"}),
                make("fill"),
                make("collect"),
            ],
            today=TODAY,
            completed=frozenset({"collect"}),
        )

        assert lookup(plan, "submit").blocked_by == ("fill",)

    def test_next_actions_excludes_blocked_work(self):
        plan = build_plan(
            [
                make("submit", deadline=date(2026, 9, 3), depends_on={"collect"}),
                make("collect", deadline=date(2026, 9, 10)),
            ],
            today=TODAY,
        )

        assert [item.action.id for item in plan.next_actions] == ["collect"]

    def test_next_actions_are_ordered_by_urgency(self):
        plan = build_plan(
            [
                make("relaxed", deadline=date(2026, 11, 1)),
                make("urgent", deadline=date(2026, 9, 2)),
            ],
            today=TODAY,
        )

        assert [item.action.id for item in plan.next_actions] == ["urgent", "relaxed"]

    def test_completing_a_prerequisite_unblocks_its_dependent(self):
        actions = [make("submit", depends_on={"collect"}), make("collect")]

        before = build_plan(actions, today=TODAY)
        after = build_plan(actions, today=TODAY, completed=frozenset({"collect"}))

        assert lookup(before, "submit").blocked_by == ("collect",)
        assert lookup(after, "submit").blocked_by == ()


def test_empty_input_produces_an_empty_plan():
    plan = build_plan([], today=TODAY)

    assert plan.scheduled == ()
    assert plan.is_feasible
    assert plan.next_actions == ()
