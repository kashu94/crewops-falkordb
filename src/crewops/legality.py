"""Legality checks for one crew member on one pairing.

The pairing's duty days come from ``(:Pairing)-[:HAS_DAY]->(:DutyDay)``. One batched query
fetches each candidate's ratings, certificates, rostered duties, duty log, reserve windows
and open groundings. Positioning flights are looked up by route. The rules in
``crewops.rules`` then run on that data."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from falkordb import Graph

from crewops.config import settings
from crewops.graph import run
from crewops.rules import (
    BASE,
    QUAL,
    REST,
    CrewState,
    Duty,
    DutyDay,
    RuleBook,
    RuleVerdict,
    blocking,
    evaluate,
    fail,
    load_rulebook,
)
from crewops.timeutil import hhmm, hours, ts

POSITIONING_REPORT_BUFFER_S = 15 * 60
"""A positioned crew member reports this long after their positioning flight lands."""

OPEN_STATUSES = ["REPORTED", "ANALYSING", "OPTIONS_READY", "PLAN_SUBMITTED"]


class UnknownEntity(LookupError):
    def __init__(self, kind: str, value: str) -> None:
        super().__init__(f"no {kind} {value!r}")
        self.kind = kind
        self.value = value


# --- Cypher ---------------------------------------------------------------------------------

PAIRING_QUERY = """
MATCH (p:Pairing {id: $pairing})-[h:HAS_DAY]->(d:DutyDay)
WITH p, d ORDER BY h.seq
WITH p, collect(d{.date, .report_ts, .release_ts, .sectors, .block_hours, .aircraft_type,
                  .dep_station}) AS days
MATCH (p)-[:FLOWN_BY]->(ac:Aircraft)-[:OPERATED_BY]->(al:Airline)
OPTIONAL MATCH (c:Crew)-[a:ASSIGNED]->(p)
RETURN p.id AS id, ac.tail AS aircraft, al.code AS airline, al.name AS airline_name, days,
       collect(CASE WHEN c IS NULL THEN NULL ELSE {crew: c.id, role: a.role} END) AS crew
"""

CREW_CONTEXT_QUERY = """
UNWIND $ids AS cid
MATCH (c:Crew {id: cid})-[:BASED_AT]->(b:Station)
MATCH (c)-[:EMPLOYED_BY]->(al:Airline)
OPTIONAL MATCH (c)-[:RATED_ON]->(t:AircraftType)
WITH c, b, al, collect(t.name) AS ratings
OPTIONAL MATCH (c)-[:HOLDS]->(x:Certification)
WITH c, b, al, ratings, collect(x{.type, .valid_from, .valid_to}) AS certs
OPTIONAL MATCH (c)-[:ASSIGNED]->(:Pairing)-[:HAS_DAY]->(od:DutyDay)
WITH c, b, al, ratings, certs,
     collect(od{.pairing, .date, .report_ts, .release_ts, .block_hours}) AS duties
OPTIONAL MATCH (c)-[:LOGGED]->(l:DutyLog)
WITH c, b, al, ratings, certs, duties, collect(l{.date, .duty_hours, .flight_hours}) AS logs
OPTIONAL MATCH (c)-[oc:ON_CALL]->(rs:Station) WHERE oc.date IN $dates
WITH c, b, al, ratings, certs, duties, logs,
     collect(oc{.date, .start_ts, .end_ts, station: rs.code}) AS oncall
OPTIONAL MATCH (dis:Disruption)-[g:GROUNDS]->(c)
WHERE dis.status IN $open AND g.date IN $dates
RETURN c{.*} AS crew, b.code AS base, al.code AS airline, ratings, certs, duties, logs, oncall,
       collect(CASE WHEN dis IS NULL THEN NULL ELSE {date: g.date, disruption: dis.id} END)
         AS grounded
"""

POSITIONING_QUERY = """
MATCH (:Station {code: $origin})<-[:FROM]-(f:Flight)-[:TO]->(:Station {code: $dest})
WHERE f.date = $date AND f.dep_ts >= $now AND f.status <> 'cancelled'
RETURN f.id AS id, f.arr_ts AS arr_ts
ORDER BY f.arr_ts
"""

NOW_QUERY = "MATCH (m:Meta) RETURN m.snapshot_ts AS snapshot"


@dataclass(frozen=True, slots=True)
class PairingView:
    pairing_id: str
    aircraft: str
    airline: str
    airline_name: str
    days: tuple[DutyDay, ...]
    crew: tuple[tuple[str, str], ...]
    """(crew id, role) rostered on it."""

    @property
    def on_board(self) -> set[str]:
        return {c for c, _ in self.crew}


@dataclass(frozen=True, slots=True)
class Positioning:
    flight_id: str
    from_station: str
    to_station: str
    arrives_ts: int
    delay_hours: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight": self.flight_id,
            "from": self.from_station,
            "to": self.to_station,
            "delay_hours": self.delay_hours,
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    """A crew member as the graph returned them, ready for the rules."""

    state: CrewState
    seniority: int
    reach_minutes: int
    experience_years: int
    command_qualified: bool
    right_seat_qualified: bool
    risk: float | None
    oncall: tuple[dict[str, Any], ...]
    grounded: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class Assessment:
    crew_id: str
    pairing_id: str
    verdicts: tuple[RuleVerdict, ...]
    positioning: Positioning | None
    delay_hours: float
    on_reserve: bool
    days: tuple[DutyDay, ...]
    reach_slip_hours: float = 0.0

    @property
    def legal(self) -> bool:
        return not any(v.failed for v in self.verdicts)

    @property
    def blocking(self) -> list[RuleVerdict]:
        return blocking(self.verdicts)

    @property
    def departure_slip_hours(self) -> float:
        if self.positioning:
            return self.positioning.delay_hours
        return self.reach_slip_hours

    def as_dict(self) -> dict[str, Any]:
        return {
            "crew_id": self.crew_id,
            "pairing_id": self.pairing_id,
            "legal": self.legal,
            "on_reserve": self.on_reserve,
            "departure_slip_hours": self.departure_slip_hours,
            "positioning": self.positioning.as_dict() if self.positioning else None,
            "blocking": [v.as_dict() for v in self.blocking],
            "checks": [v.as_dict() for v in self.verdicts],
        }


@dataclass
class Context:
    """One graph, its rule book and clock, and caches for one analysis."""

    graph: Graph
    now_ts: int
    book: RuleBook
    _pairings: dict[str, PairingView] = field(default_factory=dict)
    _positioning: dict[tuple[str, str, str], list[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def open(cls, graph: Graph, now_ts: int | None = None) -> Context:
        if now_ts is None:
            snapshot = run(graph, "snapshot", NOW_QUERY, read_only=True)
            seed = snapshot[0]["snapshot"] if snapshot else ts(settings().now)
            now_ts = max(seed, ts(settings().now))
        return cls(graph, now_ts, load_rulebook(graph))

    def pairing(self, pairing_id: str) -> PairingView:
        if pairing_id not in self._pairings:
            rows = run(self.graph, "pairing", PAIRING_QUERY, {"pairing": pairing_id}, read_only=True)
            if not rows:
                raise UnknownEntity("pairing", pairing_id)
            r = rows[0]
            self._pairings[pairing_id] = PairingView(
                pairing_id=r["id"],
                aircraft=r["aircraft"],
                airline=r["airline"],
                airline_name=r["airline_name"],
                days=tuple(DutyDay(**d) for d in r["days"]),
                crew=tuple(sorted((c["crew"], c["role"]) for c in r["crew"] if c)),
            )
        return self._pairings[pairing_id]

    def candidates(self, crew_ids: Iterable[str], dates: Iterable[str]) -> dict[str, Candidate]:
        ids = sorted(set(crew_ids))
        if not ids:
            return {}
        rows = run(
            self.graph,
            "crew_context",
            CREW_CONTEXT_QUERY,
            {"ids": ids, "dates": sorted(set(dates)), "open": OPEN_STATUSES},
            read_only=True,
        )
        out: dict[str, Candidate] = {}
        for r in rows:
            c = r["crew"]
            state = CrewState(
                crew_id=c["id"],
                name=c["name"],
                rank=c["rank"],
                base=r["base"],
                airline=r["airline"],
                ratings=tuple(r["ratings"]),
                status=c["status"],
                last_rest_ended_ts=c.get("last_rest_ended_ts"),
                daily_duty={log["date"]: log["duty_hours"] for log in r["logs"]},
                daily_flight={log["date"]: log["flight_hours"] for log in r["logs"]},
                certs=tuple((x["type"], x["valid_from"], x["valid_to"]) for x in r["certs"]),
                duties=tuple(
                    sorted(
                        (
                            Duty(d["pairing"], d["date"], d["report_ts"], d["release_ts"], d["block_hours"])
                            for d in r["duties"]
                        ),
                        key=lambda d: d.report_ts,
                    )
                ),
            )
            out[c["id"]] = Candidate(
                state=state,
                seniority=c.get("seniority") or 0,
                reach_minutes=c.get("reach_minutes") or 0,
                experience_years=c.get("experience_years") or 0,
                command_qualified=bool(c.get("command_qualified")),
                right_seat_qualified=bool(c.get("right_seat_qualified")),
                risk=c.get("risk"),
                oncall=tuple(r["oncall"]),
                grounded=tuple(g for g in r["grounded"] if g),
            )
        missing = set(ids) - set(out)
        if missing:
            raise UnknownEntity("crew", sorted(missing)[0])
        return out

    def positioning(self, origin: str, day: DutyDay) -> Positioning | None:
        """Best same-day positioning flight from the crew base, or None if there is none."""
        key = (origin, day.dep_station, day.date)
        if key not in self._positioning:
            self._positioning[key] = run(
                self.graph,
                "positioning_flights",
                POSITIONING_QUERY,
                {"origin": origin, "dest": day.dep_station, "date": day.date, "now": self.now_ts},
                read_only=True,
            )
        best: Positioning | None = None
        for f in self._positioning[key]:
            ready = f["arr_ts"] + POSITIONING_REPORT_BUFFER_S
            slip = max(0.0, hours(day.report_ts, ready))
            if best is None or (slip, f["arr_ts"]) < (best.delay_hours, best.arrives_ts):
                best = Positioning(f["id"], origin, day.dep_station, f["arr_ts"], slip)
        return best


def _intra_pairing_rest(days: list[DutyDay], book: RuleBook) -> list[RuleVerdict]:
    """Rest between days of the same pairing (can break after a delay or positioning slip)."""
    out = []
    for prev, nxt in zip(days, days[1:], strict=False):
        rest = hours(prev.release_ts, nxt.report_ts)
        if rest < book.min_rest_hours:
            out.append(
                fail(
                    REST,
                    f"only {rest}h rest between {prev.date} and {nxt.date} of the pairing",
                    used=rest,
                    limit=book.min_rest_hours,
                    headroom=round(rest - book.min_rest_hours, 2),
                    date=nxt.date,
                )
            )
    return out


def assess_candidate(
    ctx: Context,
    cand: Candidate,
    pairing: PairingView,
    delay_hours: float = 0.0,
    delay_date: str | None = None,
) -> Assessment:
    """All seven rules for one crew member on every day of one pairing."""
    if delay_hours < 0:
        raise ValueError("delay_hours cannot be negative")
    crew = cand.state
    days = list(pairing.days)
    first = days[0]

    own = [d for d in crew.duties if d.pairing_id == pairing.pairing_id]
    if crew.last_rest_ended_ts and any(d.release_ts <= crew.last_rest_ended_ts for d in own):
        crew = replace(crew, last_rest_ended_ts=None)
    rostered_here = bool(own)

    positioning = None
    if crew.base != first.dep_station and not rostered_here:
        positioning = ctx.positioning(crew.base, first)
        if positioning and positioning.delay_hours:
            days[0] = days[0].shifted(positioning.delay_hours)

    if delay_hours:
        index = 0
        if delay_date is not None:
            matching = [i for i, d in enumerate(days) if d.date == delay_date]
            if not matching:
                raise ValueError(f"{pairing.pairing_id} has no duty day on {delay_date}")
            index = matching[0]
        days[index] = days[index].delayed(delay_hours)

    verdicts = evaluate(
        crew,
        days,
        ctx.book,
        exclude=pairing.pairing_id,
        positioned=positioning is not None or rostered_here,
    )
    verdicts += _intra_pairing_rest(days, ctx.book)

    if crew.airline and crew.airline != pairing.airline:
        verdicts.append(
            fail(
                QUAL,
                f"works for {crew.airline}, not {pairing.airline_name}, which flies {pairing.aircraft}",
                date=days[0].date,
            )
        )

    duty_dates = {d.date for d in days}
    for g in cand.grounded:
        if g["date"] in duty_dates:
            verdicts.append(
                fail(QUAL, f"reported unavailable on {g['date']} (report {g['disruption']})", date=g["date"])
            )
            break

    rostered = [o for o in cand.oncall if o["date"] == first.date]
    on_reserve = any(o["start_ts"] <= days[0].report_ts <= o["end_ts"] for o in rostered)
    if rostered and not on_reserve:
        o = rostered[0]
        verdicts.append(
            fail(
                BASE,
                f"on-call window {hhmm(o['start_ts'])}-{hhmm(o['end_ts'])}Z does not cover "
                f"report at {hhmm(days[0].report_ts)}Z",
                date=first.date,
            )
        )

    return Assessment(
        crew_id=crew.crew_id,
        pairing_id=pairing.pairing_id,
        verdicts=tuple(verdicts),
        positioning=positioning,
        delay_hours=delay_hours,
        on_reserve=on_reserve,
        days=tuple(days),
    )


def assess(
    graph: Graph,
    crew_id: str,
    pairing_id: str,
    delay_hours: float = 0.0,
    delay_date: str | None = None,
    ctx: Context | None = None,
) -> Assessment:
    """Legality of one crew member for one pairing, straight from the graph."""
    ctx = ctx or Context.open(graph)
    pairing = ctx.pairing(pairing_id)
    cand = ctx.candidates([crew_id], [d.date for d in pairing.days])[crew_id]
    return assess_candidate(ctx, cand, pairing, delay_hours, delay_date)
