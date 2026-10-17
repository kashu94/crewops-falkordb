"""What a disruption affects.

- crew_blast_radius: seats a crew member leaves empty, with flights and passengers.
- delay_ripple: follows a delay along NEXT_LEG and checks each crew duty day it extends.
- station_closure / gate_closure: flights hit by a closure; free gates for a gate move.
- who_to_tell: station and network contacts, crew on the trip, and the desk controller."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from falkordb import Graph

from crewops.graph import run
from crewops.rules import RuleBook, load_rulebook
from crewops.timeutil import hours, iso

MIN_TURN_MINUTES = 45
"""Shortest turnaround: only slack beyond this, overnight, absorbs a delay."""

STATION_REOPEN_TURNAROUND_S = 30 * 60
"""After a station reopens, the first movements need this long."""

RELEASE_AFTER_LAST_ARRIVAL_S = 30 * 60
REPORT_BEFORE_FIRST_DEPARTURE_S = 60 * 60


# --- Cypher ---------------------------------------------------------------------------------

CREW_BLAST_QUERY = """
MATCH (c:Crew {id: $crew})-[a:ASSIGNED]->(p:Pairing)-[:HAS_DAY]->(d:DutyDay)
WHERE d.date >= $from AND d.date <= $to AND d.report_ts >= $now
WITH c, a, p, collect(d.date) AS dates, min(d.date) AS first
MATCH (p)-[:HAS_DAY]->(dd:DutyDay)-[l:LEG]->(f:Flight)
WITH c, a, p, dates, first, dd, l, f ORDER BY dd.date, l.seq
WITH c, a, p, dates, first, collect({id: f.id, date: dd.date, booked: f.booked}) AS legs
MATCH (p)-[:FLOWN_BY]->(ac:Aircraft)
RETURN p.id AS pairing, a.role AS role, dates, ac.tail AS aircraft,
       [x IN legs WHERE x.date IN dates | x.id] AS uncovered,
       [x IN legs WHERE x.date > first AND NOT x.date IN dates | x.id] AS at_risk,
       reduce(s = 0, x IN [y IN legs WHERE y.date IN dates] | s + x.booked) AS passengers
ORDER BY pairing
"""

ROTATION_QUERY = """
MATCH (f:Flight {id: $flight})
MATCH path = (f)-[:NEXT_LEG*0..12]->(g:Flight)
RETURN g.id AS id, g.no AS no, g.date AS date, g.dep_ts AS dep_ts, g.arr_ts AS arr_ts,
       g.booked AS booked, length(path) AS hop
ORDER BY hop
"""

FIRST_LEG_QUERY = """
MATCH (f:Flight)-[:ON]->(:Aircraft {tail: $aircraft})
WHERE f.date = $date
RETURN f.id AS id ORDER BY f.dep_ts LIMIT 1
"""

DUTY_DAYS_OF_FLIGHTS_QUERY = """
MATCH (dd:DutyDay)-[l:LEG]->(f:Flight)
WHERE f.id IN $flights
WITH DISTINCT dd
MATCH (dd)-[l2:LEG]->(leg:Flight)
WITH dd, leg ORDER BY l2.seq
WITH dd, collect({id: leg.id, dep_ts: leg.dep_ts, arr_ts: leg.arr_ts, booked: leg.booked}) AS legs
OPTIONAL MATCH (c:Crew)-[a:ASSIGNED]->(:Pairing {id: dd.pairing})
RETURN dd.id AS id, dd.pairing AS pairing, dd.date AS date, dd.report_ts AS report_ts,
       dd.release_ts AS release_ts, legs, collect({crew: c.id, role: a.role}) AS crew
ORDER BY dd.report_ts
"""

STATION_CLOSURE_QUERY = """
MATCH (s:Station {code: $station})
MATCH (f:Flight)-[r:FROM|TO]->(s)
WHERE (type(r) = 'FROM' AND f.dep_ts >= $start AND f.dep_ts < $end)
   OR (type(r) = 'TO' AND f.arr_ts >= $start AND f.arr_ts < $end)
RETURN f.id AS id, type(r) AS direction, f.dep_ts AS dep_ts, f.arr_ts AS arr_ts, f.booked AS booked
ORDER BY f.dep_ts
"""

GATE_CLOSURE_QUERY = """
MATCH (g:Gate {id: $gate})-[:AT]->(s:Station)
MATCH (f:Flight)-[b:BOARDS_AT]->(g)
WHERE b.start_ts < $end AND b.end_ts > $start
WITH s, f, b
OPTIONAL MATCH (alt:Gate)-[:AT]->(s)
WHERE alt.id <> $gate AND NOT alt.id IN $closed
OPTIONAL MATCH (other:Flight)-[ob:BOARDS_AT]->(alt)
WHERE ob.start_ts < b.end_ts AND ob.end_ts > b.start_ts
WITH f, b, alt, count(other) AS clashes
WHERE alt IS NULL OR clashes = 0
WITH f, b, alt ORDER BY alt.emergency, alt.id
RETURN f.id AS flight, b.start_ts AS start_ts, b.end_ts AS end_ts, f.booked AS booked,
       collect(alt.id) AS free_gates
ORDER BY start_ts
"""

STATION_CONTACTS_QUERY = """
MATCH (f:Flight)-[:FROM|TO]->(s:Station)<-[:ON_DUTY_AT]-(k:Contact)
WHERE f.id IN $flights AND k.team IN $teams
RETURN DISTINCT k.name AS name, k.team AS team, k.scope AS scope, k.title AS title
ORDER BY scope, team
"""

STATION_CONTACTS_BY_CODE_QUERY = """
MATCH (s:Station)<-[:ON_DUTY_AT]-(k:Contact)
WHERE s.code IN $stations AND k.team IN $teams
RETURN k.name AS name, k.team AS team, k.scope AS scope, k.title AS title
ORDER BY scope, team
"""

NETWORK_CONTACTS_QUERY = """
MATCH (k:Contact {scope: 'NETWORK'})
WHERE k.team IN $teams
RETURN k.name AS name, k.team AS team, k.scope AS scope, k.title AS title
ORDER BY team
"""

TRIP_CREW_QUERY = """
MATCH (c:Crew)-[a:ASSIGNED]->(p:Pairing)
WHERE p.id IN $pairings AND NOT c.id IN $exclude
RETURN c.id AS crew_id, c.name AS name, a.role AS role, p.id AS pairing
ORDER BY pairing, role
"""

DESK_CONTROLLER_QUERY = """
MATCH (u:User)-[:STAFFS]->(d:Desk)-[:WATCHES]->(ac:Aircraft)
WHERE ac.tail IN $aircraft
RETURN DISTINCT d.id AS desk, u.username AS username, u.name AS name
ORDER BY desk
"""


# --- crew unavailable -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeatImpact:
    pairing_id: str
    role: str
    aircraft: str
    dates: tuple[str, ...]
    uncovered: tuple[str, ...]
    at_risk: tuple[str, ...]
    passengers: int


def crew_blast_radius(graph: Graph, crew_id: str, from_date: str, to_date: str, now_ts: int) -> list[SeatImpact]:
    """Every seat a crew member leaves empty between two dates (trips not yet started)."""
    rows = run(
        graph,
        "crew_blast_radius",
        CREW_BLAST_QUERY,
        {"crew": crew_id, "from": from_date, "to": to_date, "now": now_ts},
        read_only=True,
    )
    return [
        SeatImpact(
            r["pairing"],
            r["role"],
            r["aircraft"],
            tuple(sorted(r["dates"])),
            tuple(r["uncovered"]),
            tuple(r["at_risk"]),
            r["passengers"],
        )
        for r in rows
    ]


# --- delays ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DelayedLeg:
    flight_id: str
    date: str
    delay_hours: float
    new_dep: str
    new_arr: str
    booked: int


@dataclass(frozen=True, slots=True)
class DutyDayEffect:
    duty_day: str
    pairing_id: str
    date: str
    fdp_hours: float
    fdp_limit: float
    breach: bool
    legs_to_drop: tuple[str, ...]
    fdp_after_drop: float | None
    crew: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class DelayEffect:
    first_flight: str
    delay_hours: float
    legs: tuple[DelayedLeg, ...]
    duty_days: tuple[DutyDayEffect, ...]
    cancellation_cost_inr: int = 0
    passengers_delayed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ripple(rotation: list[dict[str, Any]], delay_s: int) -> dict[str, int]:
    """Delay in seconds each leg inherits. The rest of the day's legs carry it in full (the
    published turns are the minimum); an overnight absorbs whatever slack it has beyond the
    minimum turn."""
    delays: dict[str, int] = {}
    prev_arr = None
    carried = delay_s
    first_date = rotation[0]["date"] if rotation else None
    for leg in rotation:
        if prev_arr is not None and leg["date"] != first_date:
            slack = leg["dep_ts"] - prev_arr - MIN_TURN_MINUTES * 60
            carried = max(0, carried - max(0, slack))
        if carried <= 0:
            break
        delays[leg["id"]] = carried
        prev_arr = leg["arr_ts"]
    return delays


def _day_effect(day: dict[str, Any], delays: dict[str, int], book: RuleBook) -> DutyDayEffect:
    legs = day["legs"]
    new_arr = [leg["arr_ts"] + delays.get(leg["id"], 0) for leg in legs]
    release = max(day["release_ts"], new_arr[-1] + RELEASE_AFTER_LAST_ARRIVAL_S)
    fdp = hours(day["report_ts"], release)
    limit = book.fdp_limit(len(legs))
    drop: list[str] = []
    after = None
    if fdp > limit:
        for keep in range(len(legs) - 1, 0, -1):
            rel = new_arr[keep - 1] + RELEASE_AFTER_LAST_ARRIVAL_S
            if hours(day["report_ts"], rel) <= book.fdp_limit(keep):
                drop = [leg["id"] for leg in legs[keep:]]
                after = hours(day["report_ts"], rel)
                break
    return DutyDayEffect(
        duty_day=day["id"],
        pairing_id=day["pairing"],
        date=day["date"],
        fdp_hours=fdp,
        fdp_limit=limit,
        breach=fdp > limit,
        legs_to_drop=tuple(drop),
        fdp_after_drop=after,
        crew=tuple(c for c in day["crew"] if c.get("crew")),
    )


def delay_ripple(
    graph: Graph,
    delay_hours: float,
    flight_id: str | None = None,
    aircraft: str | None = None,
    date: str | None = None,
    cancellation_per_flight: int = 250_000,
) -> DelayEffect:
    """Follow a delay down the aircraft's rotation and into each crew duty day."""
    if delay_hours <= 0:
        raise ValueError("delay_hours must be positive")
    if flight_id is None:
        rows = run(graph, "first_leg", FIRST_LEG_QUERY, {"aircraft": aircraft, "date": date}, read_only=True)
        if not rows:
            raise LookupError(f"{aircraft} has no flights on {date}")
        flight_id = rows[0]["id"]
    rotation = run(graph, "rotation", ROTATION_QUERY, {"flight": flight_id}, read_only=True)
    if not rotation:
        raise LookupError(f"no flight {flight_id!r}")
    delays = _ripple(rotation, round(delay_hours * 3600))
    legs = tuple(
        DelayedLeg(
            leg["id"],
            leg["date"],
            round(delays[leg["id"]] / 3600, 2),
            iso(leg["dep_ts"] + delays[leg["id"]]),
            iso(leg["arr_ts"] + delays[leg["id"]]),
            leg["booked"],
        )
        for leg in rotation
        if leg["id"] in delays
    )
    book = load_rulebook(graph)
    days = run(graph, "duty_days_of_flights", DUTY_DAYS_OF_FLIGHTS_QUERY, {"flights": list(delays)}, read_only=True)
    effects = tuple(_day_effect(d, delays, book) for d in days)
    dropped = sum(len(e.legs_to_drop) for e in effects)
    return DelayEffect(
        first_flight=flight_id,
        delay_hours=delay_hours,
        legs=legs,
        duty_days=effects,
        cancellation_cost_inr=dropped * cancellation_per_flight,
        passengers_delayed=sum(leg.booked for leg in legs),
    )


# --- closures -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClosureImpact:
    flight_id: str
    direction: str
    min_delay_hours: float
    booked: int
    duty: DutyDayEffect | None = None


def station_closure(graph: Graph, station: str, start_ts: int, end_ts: int) -> list[ClosureImpact]:
    """Flights leaving or landing at a closed station, the least delay each needs, and what
    that delay does to its crew's duty day."""
    rows = run(
        graph,
        "station_closure",
        STATION_CLOSURE_QUERY,
        {"station": station, "start": start_ts, "end": end_ts},
        read_only=True,
    )
    book = load_rulebook(graph)
    out = []
    for r in rows:
        at = r["dep_ts"] if r["direction"] == "FROM" else r["arr_ts"]
        delay_s = end_ts + STATION_REOPEN_TURNAROUND_S - at
        rotation = run(graph, "rotation", ROTATION_QUERY, {"flight": r["id"]}, read_only=True)
        delays = _ripple(rotation, delay_s)
        days = run(graph, "duty_days_of_flights", DUTY_DAYS_OF_FLIGHTS_QUERY, {"flights": [r["id"]]}, read_only=True)
        out.append(
            ClosureImpact(
                r["id"],
                "departure" if r["direction"] == "FROM" else "arrival",
                hours(0, delay_s),
                r["booked"],
                _day_effect(days[0], delays, book) if days else None,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class GateMove:
    flight_id: str
    boarding_from: str
    boarding_to: str
    booked: int
    free_gates: tuple[str, ...]

    @property
    def new_gate(self) -> str | None:
        return self.free_gates[0] if self.free_gates else None


def gate_closure(
    graph: Graph, gate: str, start_ts: int, end_ts: int, closed: list[str] | None = None
) -> list[GateMove]:
    """Flights holding a closed gate, each with the gates free for its whole boarding window:
    ordinary gates first, then the station's emergency gates."""
    rows = run(
        graph,
        "gate_closure",
        GATE_CLOSURE_QUERY,
        {"gate": gate, "start": start_ts, "end": end_ts, "closed": closed or []},
        read_only=True,
    )
    return [
        GateMove(r["flight"], iso(r["start_ts"]), iso(r["end_ts"]), r["booked"], tuple(r["free_gates"])) for r in rows
    ]


# --- who to tell ----------------------------------------------------------------------------

STATION_TEAMS = {
    "crew_unavailable": [],
    "certification_lapse": [],
    "flight_delay": ["station_ops"],
    "station_closure": ["station_ops", "gate_control"],
    "gate_closure": ["gate_control"],
}
NETWORK_TEAMS = {
    "crew_unavailable": ["ops_monitoring"],
    "certification_lapse": ["ops_monitoring", "crew_records"],
    "flight_delay": ["ops_monitoring", "flight_info", "customer_care", "maintenance_control"],
    "station_closure": ["ops_monitoring", "flight_info", "customer_care"],
    "gate_closure": ["flight_info"],
}


@dataclass(frozen=True, slots=True)
class Audience:
    contacts: tuple[dict[str, str], ...] = ()
    crew: tuple[dict[str, str], ...] = ()
    controllers: tuple[dict[str, str], ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"contacts": list(self.contacts), "crew": list(self.crew), "controllers": list(self.controllers)}


def who_to_tell(
    graph: Graph,
    kind: str,
    flights: list[str],
    pairings: list[str],
    aircraft: list[str],
    exclude_crew: list[str] | None = None,
    stations: list[str] | None = None,
) -> Audience:
    contacts: list[dict[str, str]] = []
    teams = STATION_TEAMS.get(kind, [])
    if teams and flights:
        contacts += run(
            graph, "station_contacts", STATION_CONTACTS_QUERY, {"flights": flights, "teams": teams}, read_only=True
        )
    if teams and stations:
        contacts += run(
            graph,
            "station_contacts_by_code",
            STATION_CONTACTS_BY_CODE_QUERY,
            {"stations": stations, "teams": teams},
            read_only=True,
        )
    contacts += run(
        graph, "network_contacts", NETWORK_CONTACTS_QUERY, {"teams": NETWORK_TEAMS.get(kind, [])}, read_only=True
    )
    seen: set[str] = set()
    unique = []
    for c in contacts:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)
    crew = (
        run(graph, "trip_crew", TRIP_CREW_QUERY, {"pairings": pairings, "exclude": exclude_crew or []}, read_only=True)
        if pairings
        else []
    )
    controllers = (
        run(graph, "desk_controllers", DESK_CONTROLLER_QUERY, {"aircraft": aircraft}, read_only=True)
        if aircraft
        else []
    )
    return Audience(tuple(unique), tuple(crew), tuple(controllers))
