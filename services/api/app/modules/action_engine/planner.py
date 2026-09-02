"""Turn a set of extracted actions into an ordered, scheduled plan.

Ordering alone is not planning. "Collect income certificate" before "Submit
application" is obvious; what the reader actually needs is *when to start*,
which depends on how long the blocking work takes and how much slack the
deadline leaves. A certificate that takes ten working days to obtain, needed
for a deadline eight days away, is already late -- and no competitor surfaces
that.

So deadlines propagate backwards through the dependency graph: an action
inherits the tightest deadline of anything that depends on it, minus the time
that dependent work needs. The result is a latest-safe-start per action, and
slack that can go negative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from app.domain.claims import Claim, ClaimClass


class ActionVerb(str, Enum):
    """Coarse buckets, chosen for how differently the reader must behave.

    A finer taxonomy (submit vs upload vs send) reads as precision but changes
    nothing about what anyone does, and gives the extractor more ways to be
    inconsistent.
    """

    OBTAIN = "obtain"
    PREPARE = "prepare"
    SUBMIT = "submit"
    ATTEND = "attend"
    CONFIRM = "confirm"


class Priority(str, Enum):
    OVERDUE = "overdue"
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Fallback effort when the extractor cannot estimate one, in calendar days.
DEFAULT_EFFORT_DAYS = 1

#: Slack at or below this many days is critical.
_CRITICAL_SLACK = 2
_HIGH_SLACK = 7
_MEDIUM_SLACK = 21


@dataclass(frozen=True, slots=True)
class Action:
    """An extracted action, before scheduling."""

    id: str
    description: str
    verb: ActionVerb
    claim: Claim[str]
    #: Deadline stated for this action specifically, if any.
    deadline: date | None = None
    #: Calendar days this action is expected to take once started.
    effort_days: int = DEFAULT_EFFORT_DAYS
    #: Ids of actions that must complete before this one can start.
    depends_on: frozenset[str] = frozenset()
    #: Requirements this action produces or consumes, for the checklist view.
    requires: tuple[str, ...] = ()
    #: The restriction that governs this action, in the document's own words,
    #: when it applies only to some readers. None means it applies to everyone.
    conditional_on: str | None = None
    #: True when the document offers this rather than requiring it.
    optional: bool = False


@dataclass(frozen=True, slots=True)
class ScheduledAction:
    """An action with its computed position in the plan."""

    action: Action
    order: int
    #: Deepest chain of prerequisites behind this action.
    depth: int
    #: The deadline that actually governs this action, after inheriting the
    #: constraints of everything downstream of it.
    effective_deadline: date | None
    #: Last date work can begin and still meet `effective_deadline`.
    latest_start: date | None
    #: Days of buffer. Negative means the plan cannot be met as stated.
    slack_days: int | None
    priority: Priority
    #: Ids of incomplete prerequisites blocking this action right now.
    blocked_by: tuple[str, ...]
    #: Human-readable justification for the priority, for the UI tooltip.
    rationale: str


@dataclass(frozen=True, slots=True)
class Plan:
    scheduled: tuple[ScheduledAction, ...]
    #: Dependency edges dropped to make the graph acyclic, as (from, to) pairs.
    #: Non-empty means the extractor produced contradictory ordering, which the
    #: UI must surface rather than hide.
    broken_cycles: tuple[tuple[str, str], ...] = ()
    #: Ids referenced as prerequisites that no action defines.
    dangling_dependencies: tuple[tuple[str, str], ...] = ()
    #: Actions the reader has marked done.
    completed: frozenset[str] = frozenset()

    @property
    def is_feasible(self) -> bool:
        """Whether the work still outstanding can be finished in time.

        Completed actions are excluded deliberately. A step finished last week
        is not a schedule problem, and leaving it in meant a plan could never
        stop reporting itself infeasible however much of it got done.
        """
        return all(
            item.slack_days is None or item.slack_days >= 0
            for item in self.outstanding
        )

    @property
    def outstanding(self) -> tuple[ScheduledAction, ...]:
        return tuple(
            item for item in self.scheduled if item.action.id not in self.completed
        )

    @property
    def next_actions(self) -> tuple[ScheduledAction, ...]:
        """Actions that can be started immediately, tightest deadline first."""
        ready = [item for item in self.outstanding if not item.blocked_by]
        ready.sort(key=lambda item: (_slack_key(item), item.order))
        return tuple(ready)


def build_plan(
    actions: list[Action],
    *,
    today: date,
    completed: frozenset[str] = frozenset(),
) -> Plan:
    """Order and schedule `actions`, tolerating a malformed dependency graph.

    Cycles and references to unknown actions are repaired rather than raised:
    the graph comes from a language model, and a plan with one dropped edge is
    far more useful than an exception.
    """
    by_id = {action.id: action for action in actions}
    graph, dangling = _prune_dangling(by_id)
    graph, broken = _break_cycles(graph, by_id)

    order = _topological_order(graph, by_id)
    depths = _depths(graph, order)
    deadlines = _propagate_deadlines(graph, by_id, order)

    scheduled: list[ScheduledAction] = []
    for position, action_id in enumerate(order):
        action = by_id[action_id]
        effective = deadlines[action_id]
        latest_start = (
            effective - timedelta(days=action.effort_days) if effective else None
        )
        slack = (latest_start - today).days if latest_start else None
        blocked_by = tuple(
            sorted(dep for dep in graph[action_id] if dep not in completed)
        )
        priority, rationale = _prioritize(
            action=action,
            slack_days=slack,
            effective_deadline=effective,
            blocks_others=any(action_id in graph[other] for other in graph),
        )
        scheduled.append(
            ScheduledAction(
                action=action,
                order=position,
                depth=depths[action_id],
                effective_deadline=effective,
                latest_start=latest_start,
                slack_days=slack,
                priority=priority,
                blocked_by=blocked_by,
                rationale=rationale,
            )
        )

    return Plan(
        scheduled=tuple(scheduled),
        broken_cycles=broken,
        dangling_dependencies=dangling,
        completed=frozenset(completed & by_id.keys()),
    )


def _prune_dangling(
    by_id: dict[str, Action],
) -> tuple[dict[str, set[str]], tuple[tuple[str, str], ...]]:
    graph: dict[str, set[str]] = {}
    dangling: list[tuple[str, str]] = []
    for action_id, action in by_id.items():
        kept: set[str] = set()
        for dependency in sorted(action.depends_on):
            if dependency in by_id and dependency != action_id:
                kept.add(dependency)
            else:
                dangling.append((action_id, dependency))
        graph[action_id] = kept
    return graph, tuple(dangling)


def _break_cycles(
    graph: dict[str, set[str]],
    by_id: dict[str, Action],
) -> tuple[dict[str, set[str]], tuple[tuple[str, str], ...]]:
    """Remove the fewest edges needed to make the graph acyclic.

    The edge dropped from each cycle is the one entering the action with the
    latest deadline, since that is the ordering claim least likely to be the
    load-bearing one.
    """
    broken: list[tuple[str, str]] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in done:
            return
        visiting.add(node)
        path.append(node)
        for dependency in sorted(graph[node]):
            if dependency in visiting:
                cycle = path[path.index(dependency) :]
                victim = max(cycle, key=lambda item: _deadline_key(by_id[item]))
                predecessor = cycle[(cycle.index(victim) - 1) % len(cycle)]
                if victim in graph[predecessor]:
                    graph[predecessor].discard(victim)
                    broken.append((predecessor, victim))
                continue
            visit(dependency, path)
        path.pop()
        visiting.discard(node)
        done.add(node)

    for node in sorted(graph):
        visit(node, [])
    return graph, tuple(broken)


def _topological_order(
    graph: dict[str, set[str]],
    by_id: dict[str, Action],
) -> list[str]:
    """Prerequisites first; ties broken by deadline, then effort, then id.

    Deterministic tie-breaking matters: the same document must always produce
    the same plan, or the golden-file tests are worthless.
    """
    remaining = {node: set(deps) for node, deps in graph.items()}
    order: list[str] = []

    while remaining:
        ready = [node for node, deps in remaining.items() if not deps]
        if not ready:  # unreachable once cycles are broken
            ready = [min(remaining, key=lambda node: (_deadline_key(by_id[node]), node))]
        ready.sort(key=lambda node: (_deadline_key(by_id[node]), -by_id[node].effort_days, node))
        chosen = ready[0]
        order.append(chosen)
        del remaining[chosen]
        for deps in remaining.values():
            deps.discard(chosen)
    return order


def _depths(graph: dict[str, set[str]], order: list[str]) -> dict[str, int]:
    depth: dict[str, int] = {}
    for node in order:
        depth[node] = 1 + max((depth[dep] for dep in graph[node]), default=-1)
    return depth


def _propagate_deadlines(
    graph: dict[str, set[str]],
    by_id: dict[str, Action],
    order: list[str],
) -> dict[str, date | None]:
    """Push deadlines backwards from dependents onto their prerequisites.

    Walking in reverse topological order guarantees every dependent has already
    been resolved when its prerequisite is visited.
    """
    dependents: dict[str, list[str]] = {node: [] for node in graph}
    for node, deps in graph.items():
        for dependency in deps:
            dependents[dependency].append(node)

    effective: dict[str, date | None] = {}
    for node in reversed(order):
        candidates: list[date] = []
        own = by_id[node].deadline
        if own:
            candidates.append(own)
        for dependent in dependents[node]:
            inherited = effective.get(dependent)
            if inherited:
                # The prerequisite must finish before the dependent may start.
                candidates.append(inherited - timedelta(days=by_id[dependent].effort_days))
        effective[node] = min(candidates) if candidates else None
    return effective


def _prioritize(
    *,
    action: Action,
    slack_days: int | None,
    effective_deadline: date | None,
    blocks_others: bool,
) -> tuple[Priority, str]:
    if action.claim.classification is ClaimClass.UNCERTAIN:
        return (
            Priority.LOW,
            "Held back because the action itself is unconfirmed — verify it before scheduling.",
        )

    if slack_days is None:
        return (
            Priority.LOW,
            "No deadline governs this action, directly or through anything that depends on it.",
        )

    assert effective_deadline is not None
    inherited = effective_deadline != action.deadline
    source = (
        f"Governed by a dependent action's deadline of {effective_deadline.isoformat()}."
        if inherited
        else f"Stated deadline of {effective_deadline.isoformat()}."
    )

    if slack_days < 0:
        return (
            Priority.OVERDUE,
            f"{source} Needed to start {_days(abs(slack_days))} ago to finish in time.",
        )
    if slack_days <= _CRITICAL_SLACK:
        within = "today" if slack_days == 0 else f"within {_days(slack_days)}"
        return Priority.CRITICAL, f"{source} Must start {within}."
    if slack_days <= _HIGH_SLACK:
        blocking = " It also blocks later work." if blocks_others else ""
        return Priority.HIGH, f"{source} {_days(slack_days)} of buffer.{blocking}"
    if slack_days <= _MEDIUM_SLACK:
        return Priority.MEDIUM, f"{source} {_days(slack_days)} of buffer."
    return Priority.LOW, f"{source} {_days(slack_days)} of buffer."


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def _deadline_key(action: Action) -> tuple[int, str]:
    """Sort key placing dated actions before undated ones."""
    if action.deadline is None:
        return (1, "")
    return (0, action.deadline.isoformat())


def _slack_key(item: ScheduledAction) -> tuple[int, int]:
    if item.slack_days is None:
        return (1, 0)
    return (0, item.slack_days)


__all__ = [
    "Action",
    "ActionVerb",
    "Plan",
    "Priority",
    "ScheduledAction",
    "build_plan",
]
