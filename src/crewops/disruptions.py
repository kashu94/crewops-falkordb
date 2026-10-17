"""Report and analyse disruptions.

``report`` creates the ``(:Disruption)`` node and its edges; unavailable crew get a ``GROUNDS``
edge per date so later legality checks see it. ``analyse`` writes the results back into the
graph: gaps with ranked options, exclusions linked to the rules they break, affected
flights, the assigned desk and who to notify. Each step adds an ``(:Event)``."""

from __future__ import annotations

import itertools
import json
from typing import Any

from falkordb import Graph

from crewops.graph import run
from crewops.impact import (
    crew_blast_radius,
    delay_ripple,
    gate_closure,
    station_closure,
    who_to_tell,
)
from crewops.legality import OPEN_STATUSES, Context, UnknownEntity
from crewops.options import AlreadyStarted, OptionsResult, find_options, load_costs
from crewops.timeutil import add_days, day_of, iso, ts

KINDS = ("crew_unavailable", "certification_lapse", "flight_delay", "station_closure", "gate_closure")
REPORTING_ROLES = {"reporter", "controller", "crew"}
OPTIONS_KEPT = 10
JOINT_OPTIONS_PER_SEAT = 8


class NotAllowed(PermissionError):
    pass


class BadReport(ValueError):
    pass


# --- Cypher ---------------------------------------------------------------------------------

USER_QUERY = """
MATCH (u:User {username: $username})
OPTIONAL MATCH (u)-[:IS_CREW]->(c:Crew)
OPTIONAL MATCH (u)-[:STAFFS]->(d:Desk)
RETURN u.username AS username, u.name AS name, u.roles AS roles, c.id AS crew_id, d.id AS desk
"""

NEXT_ID = """
MERGE (k:Counter {name: $name})
ON CREATE SET k.n = 0
SET k.n = k.n + 1
RETURN k.n AS n
"""

CREATE_DISRUPTION = """
MATCH (u:User {username: $reporter})
CREATE (d:Disruption {id: $id, type: $type, status: 'REPORTED', reported_ts: $now,
                      summary: $summary, details: $details})
CREATE (d)-[:REPORTED_BY]->(u)
RETURN d.id AS id
"""

CONCERNS_CREW = """
MATCH (d:Disruption {id: $id})
UNWIND $crew AS cid
MATCH (c:Crew {id: cid})
CREATE (d)-[:CONCERNS]->(c)
WITH d, c
UNWIND $dates AS day
CREATE (d)-[:GROUNDS {date: day}]->(c)
"""

CONCERNS_NODE = """
MATCH (d:Disruption {id: $id})
MATCH (n) WHERE (n:Flight AND n.id = $ref) OR (n:Station AND n.code = $ref)
             OR (n:Gate AND n.id = $ref) OR (n:Aircraft AND n.tail = $ref)
CREATE (d)-[:CONCERNS]->(n)
"""

LOG_EVENT = """
MATCH (d:Disruption {id: $id})
OPTIONAL MATCH (d)-[:LOGGED]->(prev:Event)
WITH d, count(prev) AS n
CREATE (d)-[:LOGGED]->(:Event {seq: n + 1, kind: $kind, ts: $now, actor: $actor, note: $note})
"""

GET_DISRUPTION = """
MATCH (d:Disruption {id: $id})-[:REPORTED_BY]->(u:User)
OPTIONAL MATCH (d)-[:ASSIGNED_TO]->(desk:Desk)
RETURN d{.*} AS d, u.username AS reporter, desk.id AS desk
"""

SET_STATUS = "MATCH (d:Disruption {id: $id}) SET d.status = $status RETURN d.status AS status"

CREATE_GAP = """
MATCH (d:Disruption {id: $id}), (p:Pairing {id: $pairing}), (v:Crew {id: $vacated})
CREATE (d)-[:HAS_GAP]->(g:Gap {id: $gap, role: $role, dates: $dates, pairing: $pairing,
                               passengers: $passengers, fallbacks: $fallbacks, funnel: $funnel})
CREATE (g)-[:IN]->(p), (g)-[:VACATED_BY]->(v)
"""

LINK_FLIGHTS = """
MATCH (d:Disruption {id: $id})
UNWIND $rows AS r
MATCH (f:Flight {id: r.flight})
MERGE (d)-[e:UNCOVERS]->(f)
"""

LINK_AT_RISK = """
MATCH (d:Disruption {id: $id})
UNWIND $flights AS fid
MATCH (f:Flight {id: fid})
MERGE (d)-[:AT_RISK]->(f)
"""

WRITE_OPTIONS = """
MATCH (g:Gap {id: $gap})
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew})
CREATE (g)-[:OPTION {rank: r.rank, strategy: r.strategy, cost: r.cost, delay_hours: r.delay,
                     action: r.action, breakdown: r.breakdown, resilience: r.resilience}]->(c)
"""

WRITE_EXCLUSIONS = """
MATCH (g:Gap {id: $gap})
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew})
CREATE (g)-[:RULED_OUT]->(x:Exclusion {stage: r.stage, reasons: r.reasons, rules: r.rules})
       -[:CANDIDATE]->(c)
WITH x, r
UNWIND r.rules AS rid
MATCH (rule:Rule {id: rid})
CREATE (x)-[:BREACHES]->(rule)
"""

WRITE_DELAYS = """
MATCH (d:Disruption {id: $id})
UNWIND $rows AS r
MATCH (f:Flight {id: r.flight})
MERGE (d)-[e:DELAYS]->(f)
SET e.hours = r.hours
"""

WRITE_FDP_BREACHES = """
MATCH (d:Disruption {id: $id})
UNWIND $rows AS r
MATCH (dd:DutyDay {id: r.day})
MERGE (d)-[e:BREACHES_FDP]->(dd)
SET e.fdp_hours = r.fdp, e.limit = r.limit, e.drop = r.drop
"""

WRITE_GATE_MOVES = """
MATCH (d:Disruption {id: $id})
UNWIND $rows AS r
MATCH (f:Flight {id: r.flight})
MERGE (d)-[e:MOVES]->(f)
SET e.to_gate = r.to_gate, e.free_gates = r.free
"""

LEAST_LOADED_DESK = """
MATCH (k:Desk)
OPTIONAL MATCH (open:Disruption)-[:ASSIGNED_TO]->(k) WHERE open.status IN $open
WITH k, count(open) AS open_n
OPTIONAL MATCH (any:Disruption)-[:ASSIGNED_TO]->(k)
WITH k, open_n, count(any) AS total
RETURN k.id AS desk, open_n, total
ORDER BY open_n, total, desk
LIMIT 1
"""

ASSIGN_DESK = """
MATCH (d:Disruption {id: $id}), (k:Desk {id: $desk})
MERGE (d)-[:ASSIGNED_TO]->(k)
"""

WRITE_NOTIFY_CONTACTS = """
MATCH (d:Disruption {id: $id})
UNWIND $rows AS r
MATCH (k:Contact {name: r.name})
MERGE (d)-[e:NOTIFIES]->(k)
SET e.reason = r.reason
"""

WRITE_NOTIFY_CREW = """
MATCH (d:Disruption {id: $id})
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew})
MERGE (d)-[e:NOTIFIES]->(c)
SET e.reason = r.reason
"""

CONTENTION_QUERY = """
MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap)-[o:OPTION {rank: 1}]->(c:Crew)
MATCH (other:Disruption)-[:HAS_GAP]->(g2:Gap)-[o2:OPTION]->(c)
WHERE other.id <> d.id AND other.status IN $open AND o2.rank <= 3
RETURN c.id AS crew, g.id AS gap, other.id AS other, g2.id AS other_gap, o2.rank AS other_rank
"""

SAVE_ANALYSIS = """
MATCH (d:Disruption {id: $id})
SET d.analysis = $analysis, d.analysed_ts = $now, d.status = 'OPTIONS_READY'
"""


# --- helpers --------------------------------------------------------------------------------


def user(graph: Graph, username: str) -> dict[str, Any]:
    rows = run(graph, "user", USER_QUERY, {"username": username}, read_only=True)
    if not rows:
        raise NotAllowed(f"unknown user {username!r}")
    return rows[0]


def next_id(graph: Graph, name: str, prefix: str) -> str:
    n = run(graph, "next_id", NEXT_ID, {"name": name})[0]["n"]
    return f"{prefix}-{n:04d}"


def log(graph: Graph, disruption_id: str, kind: str, actor: str, note: str, now_ts: int) -> None:
    run(graph, "log_event", LOG_EVENT, {"id": disruption_id, "kind": kind, "actor": actor, "note": note, "now": now_ts})


def get(graph: Graph, disruption_id: str) -> dict[str, Any]:
    rows = run(graph, "get_disruption", GET_DISRUPTION, {"id": disruption_id}, read_only=True)
    if not rows:
        raise UnknownEntity("disruption", disruption_id)
    d = rows[0]["d"]
    d["details"] = json.loads(d.get("details") or "{}")
    d["analysis"] = json.loads(d["analysis"]) if d.get("analysis") else None
    d["reporter"] = rows[0]["reporter"]
    d["desk"] = rows[0]["desk"]
    return d


def _crew_ids(details: dict[str, Any]) -> list[str]:
    ids = details.get("crew_ids") or ([details["crew_id"]] if details.get("crew_id") else [])
    return [i.strip().upper() for i in ids]


def _dates(start: str, end: str) -> list[str]:
    out, day = [], start
    while day <= end:
        out.append(day)
        day = add_days(day, 1)
    return out


# --- report ---------------------------------------------------------------------------------


def report(graph: Graph, kind: str, details: dict[str, Any], reporter: str, now_ts: int | None = None) -> str:
    """Record a disruption. Checks the reporter's role."""
    if kind not in KINDS:
        raise BadReport(f"unknown disruption type {kind!r}; expected one of {', '.join(KINDS)}")
    now_ts = now_ts or Context.open(graph).now_ts
    who = user(graph, reporter)
    roles = set(who["roles"])
    if not roles & REPORTING_ROLES:
        raise NotAllowed(f"{reporter} ({', '.join(sorted(roles))}) may not report disruptions")
    details = dict(details)
    crew: list[str] = []
    dates: list[str] = []
    concerns: list[str] = []

    if kind in ("crew_unavailable", "certification_lapse"):
        crew = _crew_ids(details)
        if not crew:
            raise BadReport("name the crew member (crew_id or crew_ids)")
        if roles == {"crew"} and crew != [who["crew_id"]]:
            raise NotAllowed("crew may only report their own unavailability")
        start = details.get("from_date") or day_of(now_ts)
        end = details.get("to_date") or (add_days(start, 6) if kind == "certification_lapse" else start)
        if end < start:
            raise BadReport("to_date is before from_date")
        dates = _dates(start, end)
        details |= {"crew_ids": crew, "from_date": start, "to_date": end}
        summary = (
            f"{', '.join(crew)} unavailable {start}"
            + (f" to {end}" if end != start else "")
            + (f" ({details['reason']})" if details.get("reason") else "")
            if kind == "crew_unavailable"
            else f"{', '.join(crew)}: {details.get('cert_type', 'certificate')} lapsed, grounded from {start}"
        )
    elif roles == {"crew"}:
        raise NotAllowed("crew may only report their own unavailability")
    elif kind == "flight_delay":
        hours = float(details.get("delay_hours") or 0)
        if hours <= 0:
            raise BadReport("delay_hours must be positive")
        if not details.get("flight_id") and not (details.get("aircraft") and details.get("date")):
            raise BadReport("name the flight_id, or the aircraft and date")
        concerns = [details.get("flight_id") or details["aircraft"]]
        summary = f"{concerns[0]} delayed {hours:g}h" + (f" ({details['cause']})" if details.get("cause") else "")
    elif kind in ("station_closure", "gate_closure"):
        key = "station" if kind == "station_closure" else "gate"
        if not details.get(key) or not details.get("start") or not details.get("end"):
            raise BadReport(f"name the {key}, start and end")
        if ts(details["end"]) <= ts(details["start"]):
            raise BadReport("end must be after start")
        concerns = [details[key].upper()]
        summary = f"{concerns[0]} closed {details['start']} to {details['end']}"
    else:  # pragma: no cover
        raise BadReport(kind)

    disruption_id = next_id(graph, "disruption", "D")
    run(
        graph,
        "create_disruption",
        CREATE_DISRUPTION,
        {
            "id": disruption_id,
            "type": kind,
            "reporter": reporter,
            "now": now_ts,
            "summary": summary,
            "details": json.dumps(details, sort_keys=True),
        },
    )
    if crew:
        run(graph, "ground_crew", CONCERNS_CREW, {"id": disruption_id, "crew": crew, "dates": dates})
    for ref in concerns:
        run(graph, "concerns", CONCERNS_NODE, {"id": disruption_id, "ref": ref})
    log(graph, disruption_id, "reported", reporter, summary, now_ts)
    return disruption_id


# --- analyse --------------------------------------------------------------------------------


def _joint_plan(results: list[tuple[str, OptionsResult]]) -> dict[str, Any] | None:
    """Cheapest cover for several seats at once without giving anyone two seats."""
    per_gap = [(gap, [o for o in r.crew_options[:JOINT_OPTIONS_PER_SEAT]]) for gap, r in results]
    if any(not opts for _, opts in per_gap):
        return None
    best = None
    for combo in itertools.product(*(opts for _, opts in per_gap)):
        ids = [o.crew_id for o in combo]
        if len(set(ids)) < len(ids):
            continue
        total = sum(o.cost_inr for o in combo)
        if best is None or total < best[0]:
            best = (total, combo)
    if best is None:
        return None
    return {
        "total_cost_inr": best[0],
        "assignments": [
            {"gap": gap, "crew_id": o.crew_id, "action": o.action, "cost_inr": o.cost_inr, "rank": o.rank}
            for (gap, _), o in zip(per_gap, best[1], strict=True)
        ],
    }


def _analyse_crew(graph: Graph, d: dict[str, Any], ctx: Context) -> dict[str, Any]:
    details = d["details"]
    gaps: list[dict[str, Any]] = []
    results: list[tuple[str, OptionsResult]] = []
    skipped: list[str] = []
    pairings: list[str] = []
    aircraft: list[str] = []
    flights: list[str] = []
    for crew_id in details["crew_ids"]:
        seats = crew_blast_radius(graph, crew_id, details["from_date"], details["to_date"], ctx.now_ts)
        for seat in seats:
            gap_id = f"{d['id']}/{seat.pairing_id}/{crew_id}"
            try:
                result = find_options(graph, seat.pairing_id, seat.role, ctx=ctx)
            except AlreadyStarted as exc:
                skipped.append(str(exc))
                continue
            fallbacks = [o.as_dict() for o in result.options if o.crew_id is None]
            run(
                graph,
                "create_gap",
                CREATE_GAP,
                {
                    "id": d["id"],
                    "pairing": seat.pairing_id,
                    "vacated": crew_id,
                    "gap": gap_id,
                    "role": seat.role,
                    "dates": list(seat.dates),
                    "passengers": seat.passengers,
                    "fallbacks": json.dumps(fallbacks),
                    "funnel": json.dumps(result.as_dict()["funnel"]),
                },
            )
            run(graph, "link_uncovered", LINK_FLIGHTS, {"id": d["id"], "rows": [{"flight": f} for f in seat.uncovered]})
            if seat.at_risk:
                run(graph, "link_at_risk", LINK_AT_RISK, {"id": d["id"], "flights": list(seat.at_risk)})
            run(
                graph,
                "write_options",
                WRITE_OPTIONS,
                {
                    "gap": gap_id,
                    "rows": [
                        {
                            "crew": o.crew_id,
                            "rank": o.rank,
                            "strategy": o.strategy.value,
                            "cost": o.cost_inr,
                            "delay": o.delay_hours,
                            "action": o.action,
                            "breakdown": json.dumps(o.cost_breakdown),
                            "resilience": o.resilience,
                        }
                        for o in result.crew_options[:OPTIONS_KEPT]
                    ],
                },
            )
            run(
                graph,
                "write_exclusions",
                WRITE_EXCLUSIONS,
                {
                    "gap": gap_id,
                    "rows": [
                        {"crew": e.crew_id, "stage": e.stage, "reasons": list(e.reasons), "rules": list(e.rule_ids)}
                        for e in result.excluded
                    ],
                },
            )
            results.append((gap_id, result))
            pairings.append(seat.pairing_id)
            aircraft.append(seat.aircraft)
            flights += seat.uncovered
            rec = result.recommended
            gaps.append(
                {
                    "gap": gap_id,
                    "pairing_id": seat.pairing_id,
                    "role": seat.role,
                    "vacated_by": crew_id,
                    "dates": list(seat.dates),
                    "uncovered_flights": list(seat.uncovered),
                    "at_risk_flights": list(seat.at_risk),
                    "passengers": seat.passengers,
                    "recommended": rec.as_dict() if rec else None,
                    "options": [o.as_dict() for o in result.options[:OPTIONS_KEPT]]
                    + ([result.cancellation.as_dict()] if len(result.options) > OPTIONS_KEPT else []),
                    "excluded_count": len(result.excluded),
                    "funnel": result.as_dict()["funnel"],
                }
            )
    joint = _joint_plan(results) if len(results) > 1 else None
    contention = run(graph, "contention", CONTENTION_QUERY, {"id": d["id"], "open": OPEN_STATUSES}, read_only=True)
    audience = who_to_tell(
        graph, d["type"], flights, sorted(set(pairings)), sorted(set(aircraft)), exclude_crew=details["crew_ids"]
    )
    passengers = sum(g["passengers"] for g in gaps)
    if gaps:
        lines = [
            f"{g['role']} seat on {g['pairing_id']} ({len(g['uncovered_flights'])} flights, "
            f"{g['passengers']} passengers): "
            + (
                f"recommend {g['recommended']['action']} for INR {g['recommended']['cost_inr']:,}"
                if g["recommended"]
                else "no legal crew; cancel or operate short"
            )
            for g in gaps
        ]
        headline = f"{len(gaps)} seat(s) to cover, {passengers} passengers. " + " ".join(lines)
    else:
        headline = "No rostered duty is affected in that period."
    return {
        "headline": headline,
        "gaps": gaps,
        "joint_plan": joint,
        "contention": contention,
        "skipped": skipped,
        "passengers": passengers,
        "audience": audience.as_dict(),
        "_notify": audience,
        "_aircraft": sorted(set(aircraft)),
    }


def _write_delays(graph: Graph, disruption_id: str, delays: list[dict[str, Any]], breaches: list[Any]) -> None:
    if delays:
        run(graph, "write_delays", WRITE_DELAYS, {"id": disruption_id, "rows": delays})
    if breaches:
        run(
            graph,
            "write_fdp_breaches",
            WRITE_FDP_BREACHES,
            {
                "id": disruption_id,
                "rows": [
                    {"day": b.duty_day, "fdp": b.fdp_hours, "limit": b.fdp_limit, "drop": list(b.legs_to_drop)}
                    for b in breaches
                ],
            },
        )


def _analyse_delay(graph: Graph, d: dict[str, Any], ctx: Context) -> dict[str, Any]:
    det = d["details"]
    costs = load_costs(graph)
    effect = delay_ripple(
        graph,
        float(det["delay_hours"]),
        det.get("flight_id"),
        det.get("aircraft"),
        det.get("date"),
        cancellation_per_flight=costs.cancellation_per_flight,
    )
    breaches = [e for e in effect.duty_days if e.breach]
    _write_delays(
        graph, d["id"], [{"flight": leg.flight_id, "hours": leg.delay_hours} for leg in effect.legs], breaches
    )
    pairings = sorted({e.pairing_id for e in effect.duty_days})
    flights = [leg.flight_id for leg in effect.legs]
    aircraft = [
        r["tail"]
        for r in run(
            graph,
            "aircraft_of",
            "MATCH (f:Flight {id: $f})-[:ON]->(a:Aircraft) RETURN a.tail AS tail",
            {"f": effect.first_flight},
            read_only=True,
        )
    ]
    audience = who_to_tell(graph, d["type"], flights, pairings, aircraft)
    if breaches:
        parts = [
            f"{b.pairing_id} on {b.date}: FDP {b.fdp_hours}h exceeds {b.fdp_limit}h; "
            + (
                f"take {', '.join(b.legs_to_drop)} off this crew (FDP then {b.fdp_after_drop}h) and re-crew or cancel it"
                if b.legs_to_drop
                else "no leg can be dropped to make it legal"
            )
            for b in breaches
        ]
        headline = f"Delay ripples to {len(effect.legs)} legs, {effect.passengers_delayed} passengers. " + " ".join(
            parts
        )
    else:
        headline = (
            f"Delay ripples to {len(effect.legs)} legs ({effect.passengers_delayed} passengers); "
            "every crew duty stays within its limit."
        )
    return {
        "headline": headline,
        "delay": effect.as_dict(),
        "audience": audience.as_dict(),
        "_notify": audience,
        "_aircraft": aircraft,
    }


def _analyse_station(graph: Graph, d: dict[str, Any], ctx: Context) -> dict[str, Any]:
    det = d["details"]
    impacts = station_closure(graph, det["station"].upper(), ts(det["start"]), ts(det["end"]))
    breaches = [i.duty for i in impacts if i.duty and i.duty.breach]
    _write_delays(graph, d["id"], [{"flight": i.flight_id, "hours": i.min_delay_hours} for i in impacts], breaches)
    flights = [i.flight_id for i in impacts]
    pairings = sorted({i.duty.pairing_id for i in impacts if i.duty})
    aircraft = [
        r["tail"]
        for r in run(
            graph,
            "aircraft_of_flights",
            "MATCH (f:Flight)-[:ON]->(a:Aircraft) WHERE f.id IN $f RETURN DISTINCT a.tail AS tail",
            {"f": flights},
            read_only=True,
        )
    ]
    audience = who_to_tell(graph, d["type"], flights, pairings, aircraft, stations=[det["station"].upper()])
    per_flight = [
        {
            "flight_id": i.flight_id,
            "direction": i.direction,
            "min_delay_hours": i.min_delay_hours,
            "pairing_id": i.duty.pairing_id if i.duty else None,
            "crew_fdp_after_delay": i.duty.fdp_hours if i.duty else None,
            "fdp_limit": i.duty.fdp_limit if i.duty else None,
            "action": "delay (crew legal)"
            if i.duty and not i.duty.breach
            else "delay exceeds crew FDP: re-crew tail legs from reserves or cancel",
        }
        for i in impacts
    ]
    headline = (
        f"{len(impacts)} movements at {det['station'].upper()} fall in the closure; "
        f"{sum(i.booked for i in impacts)} passengers. {len(breaches)} crew duty days would breach FDP."
    )
    return {
        "headline": headline,
        "flights": per_flight,
        "audience": audience.as_dict(),
        "_notify": audience,
        "_aircraft": aircraft,
    }


def _analyse_gate(graph: Graph, d: dict[str, Any], ctx: Context) -> dict[str, Any]:
    det = d["details"]
    moves = gate_closure(graph, det["gate"].upper(), ts(det["start"]), ts(det["end"]))
    run(
        graph,
        "write_gate_moves",
        WRITE_GATE_MOVES,
        {
            "id": d["id"],
            "rows": [{"flight": m.flight_id, "to_gate": m.new_gate, "free": list(m.free_gates)} for m in moves],
        },
    )
    flights = [m.flight_id for m in moves]
    audience = who_to_tell(graph, d["type"], flights, [], [], stations=[det["gate"].split("-")[0].upper()])
    no_gate = [m.flight_id for m in moves if not m.new_gate]
    headline = (
        f"{len(moves)} departures hold {det['gate'].upper()} during the closure; "
        + ", ".join(f"{m.flight_id} to {m.new_gate or 'a remote stand'}" for m in moves)
        + (f". No gate free for {', '.join(no_gate)}" if no_gate else "")
        if moves
        else f"No departure holds {det['gate'].upper()} during the closure."
    )
    return {
        "headline": headline,
        "gate_moves": [
            {"flight_id": m.flight_id, "to_gate": m.new_gate, "free_gates": list(m.free_gates), "booked": m.booked}
            for m in moves
        ],
        "audience": audience.as_dict(),
        "_notify": audience,
        "_aircraft": [],
    }


ANALYSERS = {
    "crew_unavailable": _analyse_crew,
    "certification_lapse": _analyse_crew,
    "flight_delay": _analyse_delay,
    "station_closure": _analyse_station,
    "gate_closure": _analyse_gate,
}


def analyse(graph: Graph, disruption_id: str, actor: str = "triage", ctx: Context | None = None) -> dict[str, Any]:
    """Work out a disruption's consequences and write them into the graph."""
    ctx = ctx or Context.open(graph)
    d = get(graph, disruption_id)
    result = ANALYSERS[d["type"]](graph, d, ctx)
    audience = result.pop("_notify")
    result.pop("_aircraft", None)

    desk = d.get("desk")
    if desk is None:
        row = run(graph, "least_loaded_desk", LEAST_LOADED_DESK, {"open": OPEN_STATUSES}, read_only=True)
        desk = row[0]["desk"] if row else None
        if desk:
            run(graph, "assign_desk", ASSIGN_DESK, {"id": disruption_id, "desk": desk})
    result["desk"] = desk

    reason = f"{d['type'].replace('_', ' ')}: {d['summary']}"
    if audience.contacts:
        run(
            graph,
            "notify_contacts",
            WRITE_NOTIFY_CONTACTS,
            {"id": disruption_id, "rows": [{"name": c["name"], "reason": reason} for c in audience.contacts]},
        )
    if audience.crew:
        run(
            graph,
            "notify_crew",
            WRITE_NOTIFY_CREW,
            {"id": disruption_id, "rows": [{"crew": c["crew_id"], "reason": "trip affected"} for c in audience.crew]},
        )

    result["analysed_at"] = iso(ctx.now_ts)
    run(graph, "save_analysis", SAVE_ANALYSIS, {"id": disruption_id, "analysis": json.dumps(result), "now": ctx.now_ts})
    log(graph, disruption_id, "analysed", actor, result["headline"][:500], ctx.now_ts)
    if desk:
        log(graph, disruption_id, "assigned", actor, f"to {desk}", ctx.now_ts)
    return result


def set_status(graph: Graph, disruption_id: str, status: str) -> None:
    run(graph, "set_status", SET_STATUS, {"id": disruption_id, "status": status})
