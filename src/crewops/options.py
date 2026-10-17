"""Cover options for an empty seat.

Candidates are crew of the right rank employed by the airline that operates the trip's
aircraft. Legal ones are ranked by cost; cancelling is always the last option. If fewer than
three of the seat's own rank are legal, qualified substitutes are added (command-qualified
first officers for a captain, right-seat captains for a first officer), and a cabin seat can
be left empty if one cabin crew per 50 seats remains."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

from falkordb import Graph

from crewops.graph import run
from crewops.legality import OPEN_STATUSES, Assessment, Candidate, Context, PairingView, assess_candidate
from crewops.rules import BASE, CABIN_RANKS, CERT, DUTY, FDP, FLIGHT, PILOT_RANKS, QUAL, REST


class AlreadyStarted(ValueError):
    """The pairing reported before now: too late to re-crew."""


class TripCancelled(AlreadyStarted):
    """An approved plan cancelled the pairing's flights."""


class Strategy(StrEnum):
    RESERVE_CALLOUT = "reserve_callout"
    DAY_OFF_CALLOUT = "day_off_callout"
    POSITIONING = "positioning"
    COMMAND_UPGRADE = "command_upgrade"
    RIGHT_SEAT = "right_seat"
    SHORT_CREW = "short_crew"
    CANCEL = "cancel"

    @property
    def label(self) -> str:
        return {
            Strategy.RESERVE_CALLOUT: "Reserve callout",
            Strategy.DAY_OFF_CALLOUT: "Day-off callout",
            Strategy.POSITIONING: "Position from another base",
            Strategy.COMMAND_UPGRADE: "Command-qualified first officer",
            Strategy.RIGHT_SEAT: "Captain in the right seat",
            Strategy.SHORT_CREW: "Operate one cabin crew short",
            Strategy.CANCEL: "Cancel",
        }[self]


SUBSTITUTE_BELOW = 3
SEATS_PER_CABIN_CREW = 50
REACH_ROUNDING_MINUTES = 5

FUNNEL = (
    ("qualified", (QUAL,), "not rated on the aircraft, not active, or not available"),
    ("certified", (CERT,), "a certification is invalid on a duty date"),
    ("in position", (BASE,), "cannot reach the departure, or the on-call window misses the report"),
    ("rested", (REST,), "rest conflict or already rostered"),
    ("within limits", (FDP, DUTY, FLIGHT), "would breach a duty or flight-hour limit"),
)
"""The order a controller thinks in. A candidate drops out at the first stage they fail."""


# --- Cypher ---------------------------------------------------------------------------------

CANDIDATE_POOL_QUERY = """
MATCH (p:Pairing {id: $pairing})-[:FLOWN_BY]->(:Aircraft)-[:OPERATED_BY]->(al:Airline)
MATCH (c:Crew)-[:EMPLOYED_BY]->(al)
WHERE c.rank = $rank AND NOT (c)-[:ASSIGNED]->(p)
  AND (NOT $command OR c.command_qualified)
  AND (NOT $right_seat OR c.right_seat_qualified)
RETURN c.id AS id
ORDER BY id
"""

RESERVE_POOL_QUERY = """
MATCH (c:Crew {rank: $rank})-[oc:ON_CALL]->(:Station {code: $base})
WHERE oc.date = $date
RETURN count(DISTINCT c) AS n
"""

PAIRING_FLIGHTS_QUERY = """
MATCH (p:Pairing {id: $pairing})-[:HAS_DAY]->(:DutyDay)-[:LEG]->(f:Flight)
RETURN max(f.seats) AS seats, count(f) AS flights,
       sum(CASE WHEN f.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
"""

CABIN_PRESENT_QUERY = """
MATCH (c:Crew)-[a:ASSIGNED]->(:Pairing {id: $pairing})
WHERE a.role IN $cabin
OPTIONAL MATCH (dis:Disruption)-[g:GROUNDS]->(c)
WHERE dis.status IN $open AND g.date IN $dates
RETURN c.id AS crew, count(dis) AS grounded
"""

COSTS_QUERY = "MATCH (c:CostTable) RETURN properties(c) AS costs"


@dataclass(frozen=True, slots=True)
class Option:
    rank: int
    strategy: Strategy
    crew_id: str | None
    name: str
    crew_rank: str | None
    base: str | None
    cost_inr: int
    cost_breakdown: dict[str, int]
    delay_hours: float
    positioning_flight: str | None = None
    reach_minutes: int | None = None
    seniority: int | None = None
    risk_score: float | None = None
    resilience: float | None = None
    """0-100: how much of today's reserve cover for this rank and base survives the choice."""
    experience_years: int | None = None

    @property
    def action(self) -> str:
        if self.crew_id is None:
            return self.name
        who = f"{self.crew_rank} {self.name} ({self.crew_id})"
        if self.strategy is Strategy.COMMAND_UPGRADE:
            return f"Assign {who} in command (command-qualified, {self.experience_years} years)"
        if self.strategy is Strategy.RIGHT_SEAT:
            return f"Assign {who} in the right seat"
        if self.strategy is Strategy.POSITIONING:
            return f"Assign {who}, positioning from {self.base} (first departure ~{self.delay_hours:g}h late)"
        return f"Assign {who} ({self.strategy.label.lower()})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "action": self.action,
            "strategy": self.strategy.value,
            "crew_id": self.crew_id,
            "name": self.name,
            "crew_rank": self.crew_rank,
            "base": self.base,
            "cost_inr": self.cost_inr,
            "cost_breakdown": self.cost_breakdown,
            "delay_hours": self.delay_hours,
            "positioning_flight": self.positioning_flight,
            "risk_score": self.risk_score,
            "resilience": self.resilience,
        }


@dataclass(frozen=True, slots=True)
class Exclusion:
    crew_id: str
    name: str
    stage: str
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "crew_id": self.crew_id,
            "name": self.name,
            "stage": self.stage,
            "rules": list(self.rule_ids),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class FunnelStage:
    stage: str
    remaining: int
    dropped: int
    reason: str


@dataclass(frozen=True, slots=True)
class OptionsResult:
    pairing_id: str
    role: str
    options: tuple[Option, ...]
    excluded: tuple[Exclusion, ...]
    funnel: tuple[FunnelStage, ...]
    assessments: dict[str, Assessment] = field(default_factory=dict)
    policies: dict[str, Option] = field(default_factory=dict)

    @property
    def crew_options(self) -> tuple[Option, ...]:
        return tuple(o for o in self.options if o.crew_id is not None)

    @property
    def recommended(self) -> Option | None:
        return self.crew_options[0] if self.crew_options else None

    @property
    def cancellation(self) -> Option:
        return self.options[-1]

    @property
    def cancellation_multiple(self) -> int:
        if not self.recommended or not self.recommended.cost_inr:
            return 0
        return round(self.cancellation.cost_inr / self.recommended.cost_inr)

    def as_dict(self, limit: int | None = None) -> dict[str, Any]:
        options = list(self.options)
        if limit is not None and len(options) > limit:
            options = options[: limit - 1] + [options[-1]]
        return {
            "pairing_id": self.pairing_id,
            "role": self.role,
            "recommended": self.recommended.as_dict() if self.recommended else None,
            "options": [o.as_dict() for o in options],
            "excluded": [e.as_dict() for e in self.excluded],
            "funnel": [asdict(s) for s in self.funnel],
            "cancellation_multiple": self.cancellation_multiple,
            "policies": {k: o.crew_id for k, o in self.policies.items()},
        }


@dataclass(frozen=True, slots=True)
class Costs:
    values: dict[str, int]

    def callout(self, rank: str, on_reserve: bool) -> int:
        pilot = rank in PILOT_RANKS
        if on_reserve:
            return self.values["reserve_callout_pilot" if pilot else "reserve_callout_cabin"]
        return self.values["dayoff_callout_pilot" if pilot else "dayoff_callout_cabin"]

    def seniority_allowance(self, rank: str, years: int) -> int:
        key = "seniority_allowance_pilot" if rank in PILOT_RANKS else "seniority_allowance_cabin"
        return self.values.get(key, 0) * years

    def travel(self, reach_minutes: int) -> int:
        return self.values.get("travel_per_10_minutes", 0) * -(-reach_minutes // 10)

    def __getattr__(self, key: str) -> int:
        try:
            return self.values[key]
        except KeyError:
            raise AttributeError(key) from None


def load_costs(graph: Graph) -> Costs:
    rows = run(graph, "costs", COSTS_QUERY, read_only=True)
    return Costs({k: int(v) for k, v in rows[0]["costs"].items()})


def _strategy(a: Assessment) -> Strategy:
    if a.positioning is not None:
        return Strategy.POSITIONING
    return Strategy.RESERVE_CALLOUT if a.on_reserve else Strategy.DAY_OFF_CALLOUT


def _stage(a: Assessment) -> str:
    failed = {v.rule_id for v in a.verdicts if v.failed}
    for stage, rule_ids, _ in FUNNEL:
        if failed & set(rule_ids):
            return stage
    return "within limits"


def _assess(ctx: Context, cand: Candidate, pairing: PairingView, delay: float, delay_date: str | None) -> Assessment:
    """``assess``, plus a departure slip when someone called now cannot reach the airport by
    report time."""
    a = assess_candidate(ctx, cand, pairing, delay, delay_date)
    if not a.legal or a.positioning is not None or delay:
        return a
    ready = ctx.now_ts + cand.reach_minutes * 60
    late = (ready - pairing.days[0].report_ts) / 60
    if late <= 0:
        return a
    minutes = -(-late // REACH_ROUNDING_MINUTES) * REACH_ROUNDING_MINUTES
    slipped = assess_candidate(ctx, cand, pairing, minutes / 60)
    return replace(slipped, reach_slip_hours=round(minutes / 60, 2))


def find_options(
    graph: Graph,
    pairing_id: str,
    role: str,
    delay_hours: float = 0.0,
    delay_date: str | None = None,
    ctx: Context | None = None,
) -> OptionsResult:
    """Every legal way to fill ``role`` on ``pairing_id``, cheapest first, cancellation last."""
    ctx = ctx or Context.open(graph)
    pairing = ctx.pairing(pairing_id)
    dates = [d.date for d in pairing.days]
    flights = run(graph, "pairing_flights", PAIRING_FLIGHTS_QUERY, {"pairing": pairing_id}, read_only=True)[0]
    if flights["cancelled"]:
        raise TripCancelled(f"{pairing_id} is cancelled: there is nothing to re-crew")
    if pairing.days[0].report_ts < ctx.now_ts:
        raise AlreadyStarted(f"{pairing_id} reported before now; it can no longer be re-crewed")
    costs = load_costs(graph)

    def pool(rank: str, command: bool = False, right_seat: bool = False) -> list[str]:
        rows = run(
            graph,
            "candidate_pool",
            CANDIDATE_POOL_QUERY,
            {"pairing": pairing_id, "rank": rank, "command": command, "right_seat": right_seat},
            read_only=True,
        )
        return [r["id"] for r in rows]

    reserve_pool: dict[tuple[str, str], int] = {}

    def resilience(a: Assessment, cand: Candidate) -> float:
        if not a.on_reserve:
            return 100.0
        key = (cand.state.rank, cand.state.base)
        if key not in reserve_pool:
            reserve_pool[key] = run(
                graph,
                "reserve_pool",
                RESERVE_POOL_QUERY,
                {"rank": key[0], "base": key[1], "date": a.days[0].date},
                read_only=True,
            )[0]["n"]
        n = reserve_pool[key]
        return 100.0 if n <= 0 else round(100.0 * (n - 1) / n, 1)

    def row(a: Assessment, cand: Candidate, strategy: Strategy) -> tuple:
        rank = cand.state.rank
        breakdown = {"callout": costs.callout(rank, a.on_reserve)}
        if allowance := costs.seniority_allowance(rank, cand.seniority):
            breakdown["seniority_allowance"] = allowance
        if a.positioning is not None:
            breakdown["positioning"] = costs.deadhead_positioning
        elif travel := costs.travel(cand.reach_minutes):
            breakdown["travel"] = travel
        if a.departure_slip_hours:
            breakdown["delay"] = round(a.departure_slip_hours * costs.delay_cost_per_duty_hour)
        return (sum(breakdown.values()), a.departure_slip_hours, a, cand, breakdown, strategy)

    own_ids = pool(role)
    cands = ctx.candidates(own_ids, dates)
    assessed = [_assess(ctx, cands[c], pairing, delay_hours, delay_date) for c in own_ids]
    legal = [a for a in assessed if a.legal]
    illegal = [a for a in assessed if not a.legal]
    rows = [row(a, cands[a.crew_id], _strategy(a)) for a in legal]
    rows.sort(key=lambda r: (r[0], r[1], r[2].crew_id))

    all_assessed = {a.crew_id: a for a in assessed}
    substitute = {
        "Captain": ("First Officer", True, False, Strategy.COMMAND_UPGRADE),
        "First Officer": ("Captain", False, True, Strategy.RIGHT_SEAT),
    }.get(role)
    if substitute and len(rows) < SUBSTITUTE_BELOW:
        other, command, right_seat, strategy = substitute
        sub_ids = pool(other, command, right_seat)
        sub_cands = ctx.candidates(sub_ids, dates)
        cands |= sub_cands
        extra = [_assess(ctx, sub_cands[c], pairing, delay_hours, delay_date) for c in sub_ids]
        all_assessed |= {a.crew_id: a for a in extra}
        more = [row(a, sub_cands[a.crew_id], strategy) for a in extra if a.legal]
        more.sort(key=lambda r: (-r[3].experience_years, r[0], r[2].crew_id))
        rows += more

    options: list[Option] = []
    for i, (cost, slip, a, cand, breakdown, strategy) in enumerate(rows, start=1):
        options.append(
            Option(
                rank=i,
                strategy=strategy,
                crew_id=a.crew_id,
                name=cand.state.name,
                crew_rank=cand.state.rank,
                base=cand.state.base,
                cost_inr=cost,
                cost_breakdown=breakdown,
                delay_hours=slip,
                positioning_flight=a.positioning.flight_id if a.positioning else None,
                reach_minutes=cand.reach_minutes,
                seniority=cand.seniority,
                risk_score=cand.risk,
                resilience=resilience(a, cand),
                experience_years=cand.experience_years,
            )
        )

    if role == "Cabin Crew" and _may_operate_short(graph, pairing, dates, flights["seats"]):
        options.append(
            Option(len(options) + 1, Strategy.SHORT_CREW, None, "Operate one cabin crew short", None, None, 0, {}, 0.0)
        )

    cancel_cost = flights["flights"] * costs.cancellation_per_flight
    options.append(
        Option(
            len(options) + 1,
            Strategy.CANCEL,
            None,
            f"Cancel all {flights['flights']} flights of the pairing",
            None,
            None,
            cancel_cost,
            {"cancellation": cancel_cost},
            0.0,
        )
    )

    crew_options = [o for o in options if o.crew_id is not None]
    policies: dict[str, Option] = {}
    if crew_options:
        policies["cheapest"] = crew_options[0]
        policies["least_delay"] = min(crew_options, key=lambda o: (o.delay_hours, o.cost_inr, o.crew_id))
        policies["keeps_most_reserve_cover"] = min(
            crew_options, key=lambda o: (-(o.resilience or 0), o.cost_inr, o.crew_id)
        )

    excluded = tuple(
        Exclusion(
            crew_id=a.crew_id,
            name=cands[a.crew_id].state.name,
            stage=_stage(a),
            rule_ids=tuple(dict.fromkeys(v.rule_id for v in a.blocking)),
            reasons=tuple(v.detail for v in a.blocking),
        )
        for a in illegal
    )
    dropped = Counter(e.stage for e in excluded)
    funnel = [FunnelStage("considered", len(own_ids), 0, "")]
    remaining = len(own_ids)
    for stage, _, reason in FUNNEL:
        if n := dropped.get(stage, 0):
            remaining -= n
            funnel.append(FunnelStage(stage, remaining, n, reason))
    funnel.append(FunnelStage("legal", remaining, 0, ""))

    return OptionsResult(
        pairing_id=pairing_id,
        role=role,
        options=tuple(options),
        excluded=excluded,
        funnel=tuple(funnel),
        assessments=all_assessed,
        policies=policies,
    )


def cabin_minimum(seats: int) -> int:
    """One cabin crew per 50 seats or part of 50, on the trip's largest aircraft."""
    return -(-seats // SEATS_PER_CABIN_CREW)


def _may_operate_short(graph: Graph, pairing: PairingView, dates: list[str], seats: int) -> bool:
    """Does the trip keep its cabin minimum without the missing member(s)?"""
    rows = run(
        graph,
        "cabin_present",
        CABIN_PRESENT_QUERY,
        {"pairing": pairing.pairing_id, "cabin": list(CABIN_RANKS), "open": OPEN_STATUSES, "dates": dates},
        read_only=True,
    )
    cabin = len(rows)
    missing = max(1, sum(1 for r in rows if r["grounded"]))
    return cabin - missing >= cabin_minimum(seats)
