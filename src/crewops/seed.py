"""Load the seed week into the operation graph.

    uv run python -m crewops.seed --replace

Also derives two edge types used a lot by the agents: ``NEXT_LEG`` (each aircraft's
rotation) and ``ROUTE`` (the station network)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from falkordb import Graph

from crewops.config import DATA, SEED_NOW
from crewops.graph import Graphs, run
from crewops.schema import ensure_fulltext, ensure_indexes
from crewops.timeutil import ts

SEED = DATA / "seed"
TEAM = DATA / "team" / "demo_team.json"

DESKS = (
    ("desk-1", ("VT-DXA", "VT-DXB")),
    ("desk-2", ("VT-DXC", "VT-DXD")),
    ("desk-3", ("VT-DXE", "VT-DXF")),
)
"""Controller desks and the aircraft each one watches."""

BATCH = 500


def _json(name: str, folder: Path = SEED) -> Any:
    return json.loads((folder / name).read_text())


def _batches(rows: list[dict[str, Any]]):
    for i in range(0, len(rows), BATCH):
        yield rows[i : i + BATCH]


def _write(graph: Graph, name: str, cypher: str, rows: list[dict[str, Any]]) -> None:
    for chunk in _batches(rows):
        run(graph, name, cypher, {"rows": chunk})


# --- Cypher ---------------------------------------------------------------------------------

LOAD_AIRLINES = """
UNWIND $rows AS r
MERGE (al:Airline {code: r.code}) SET al.name = r.name, al.color = r.color
WITH al, r
UNWIND r.aircraft AS tail
MERGE (ac:Aircraft {tail: tail})
MERGE (ac)-[:OPERATED_BY]->(al)
"""

LOAD_STATIONS = "UNWIND $rows AS r MERGE (:Station {code: r.code})"

LOAD_FLIGHTS = """
UNWIND $rows AS r
MATCH (dep:Station {code: r.dep}), (arr:Station {code: r.arr}), (ac:Aircraft {tail: r.aircraft})
MERGE (t:AircraftType {name: r.type})
MERGE (ac)-[:OF_TYPE]->(t)
SET ac.type = r.type
CREATE (f:Flight {id: r.id, no: r.no, date: r.date, dep_ts: r.dep_ts, arr_ts: r.arr_ts,
                  block_hours: r.block_hours, seats: r.seats, booked: r.booked,
                  aircraft_type: r.type, status: 'scheduled'})
CREATE (f)-[:FROM]->(dep), (f)-[:TO]->(arr), (f)-[:ON]->(ac)
"""

LINK_ROTATIONS = """
MATCH (ac:Aircraft)<-[:ON]-(f:Flight)
WITH ac, f ORDER BY f.dep_ts
WITH ac, collect(f) AS legs
UNWIND range(0, size(legs) - 2) AS i
WITH legs[i] AS a, legs[i + 1] AS b
CREATE (a)-[:NEXT_LEG {turn_minutes: (b.dep_ts - a.arr_ts) / 60}]->(b)
"""

LINK_ROUTES = """
MATCH (a:Station)<-[:FROM]-(f:Flight)-[:TO]->(b:Station)
WITH a, b, count(f) AS flights, sum(f.block_hours) AS block
CREATE (a)-[:ROUTE {flights: flights, block_hours: block / flights}]->(b)
"""

LOAD_PAIRINGS = """
UNWIND $rows AS r
MATCH (ac:Aircraft {tail: r.aircraft})
CREATE (p:Pairing {id: r.id})-[:FLOWN_BY]->(ac)
WITH p, r
UNWIND r.days AS d
CREATE (p)-[:HAS_DAY {seq: d.seq}]->(dd:DutyDay {id: d.id, date: d.date, report_ts: d.report_ts,
        release_ts: d.release_ts, sectors: size(d.flights), pairing: r.id})
WITH dd, d
UNWIND range(0, size(d.flights) - 1) AS i
MATCH (f:Flight {id: d.flights[i]})
CREATE (dd)-[:LEG {seq: i}]->(f)
"""

DERIVE_DUTY_DAYS = """
MATCH (dd:DutyDay)-[l:LEG]->(f:Flight)-[:FROM]->(s:Station)
WITH dd, f, s ORDER BY l.seq
WITH dd, collect(f) AS legs, collect(s.code) AS deps
SET dd.block_hours = round(reduce(b = 0.0, x IN legs | b + x.block_hours) * 100) / 100.0,
    dd.aircraft_type = legs[0].aircraft_type,
    dd.dep_station = deps[0]
"""

LOAD_CREW = """
UNWIND $rows AS r
MATCH (al:Airline {code: r.airline}), (base:Station {code: r.base})
CREATE (c:Crew {id: r.id, name: r.name, rank: r.rank, status: r.status, seniority: r.seniority,
                reach_minutes: r.reach, experience_years: r.experience,
                command_qualified: r.cq, right_seat_qualified: r.rsq})
CREATE (c)-[:EMPLOYED_BY]->(al), (c)-[:BASED_AT]->(base)
WITH c, r
UNWIND r.ratings AS rating
MERGE (t:AircraftType {name: rating})
CREATE (c)-[:RATED_ON]->(t)
"""

LOAD_ROSTER = """
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew}), (p:Pairing {id: r.pairing})
CREATE (c)-[:ASSIGNED {role: r.role}]->(p)
"""

LOAD_CERTS = """
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew})
CREATE (c)-[:HOLDS]->(:Certification {type: r.type, valid_from: r.valid_from, valid_to: r.valid_to})
"""

LOAD_CLOCKS = """
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew})
SET c.last_rest_ended_ts = r.rest_ts
WITH c, r
UNWIND r.days AS d
CREATE (c)-[:LOGGED]->(:DutyLog {date: d.date, duty_hours: d.duty_hours, flight_hours: d.flight_hours})
"""

LOAD_RESERVES = """
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew}), (s:Station {code: r.base})
CREATE (c)-[:ON_CALL {date: r.date, start_ts: r.start_ts, end_ts: r.end_ts}]->(s)
"""

LOAD_RISK = """
UNWIND $rows AS r
MATCH (c:Crew {id: r.crew})
SET c.risk = r.score, c.risk_drivers = r.drivers
"""

LOAD_GATES = """
UNWIND $rows AS r
MATCH (s:Station {code: r.station})
MERGE (g:Gate {id: r.id})
SET g.emergency = r.emergency
MERGE (g)-[:AT]->(s)
"""

LOAD_BOARDING = """
UNWIND $rows AS r
MATCH (f:Flight {id: r.flight}), (g:Gate {id: r.gate})
CREATE (f)-[:BOARDS_AT {start_ts: r.start_ts, end_ts: r.end_ts}]->(g)
"""

LOAD_CONTACTS = """
UNWIND $rows AS r
CREATE (k:Contact {team: r.team, scope: r.scope, name: r.name, title: r.title})
WITH k, r
OPTIONAL MATCH (s:Station {code: r.scope})
FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | CREATE (k)-[:ON_DUTY_AT]->(s))
"""

LOAD_RULES = """
UNWIND $rows AS r
CREATE (rule:Rule {id: r.id, text: r.text})
SET rule += r.params
"""

LOAD_COSTS = "CREATE (c:CostTable) SET c += $costs"

LOAD_TEAM = """
UNWIND $rows AS r
CREATE (u:User {username: r.username, name: r.name, roles: r.roles})
WITH u, r
OPTIONAL MATCH (c:Crew {id: r.crew_id})
FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | CREATE (u)-[:IS_CREW]->(c))
"""

LOAD_DESKS = """
UNWIND $rows AS r
CREATE (d:Desk {id: r.id})
WITH d, r
UNWIND r.aircraft AS tail
MATCH (ac:Aircraft {tail: tail})
CREATE (d)-[:WATCHES]->(ac)
"""

STAFF_DESKS = """
UNWIND $rows AS r
MATCH (u:User {username: r.username}), (d:Desk {id: r.desk})
CREATE (u)-[:STAFFS]->(d)
"""

LOAD_META = "CREATE (:Meta {snapshot_ts: $snapshot, loaded_ts: timestamp() / 1000})"

COUNT = """
MATCH (n) WITH labels(n)[0] AS label, count(*) AS n
RETURN label, n ORDER BY label
"""


def _already_loaded(graph: Graph) -> bool:
    try:
        return bool(run(graph, "is_loaded", "MATCH (m:Meta) RETURN count(m) AS n", read_only=True)[0]["n"])
    except Exception:  # no such graph
        return False


def load(graph: Graph, *, replace: bool = False) -> dict[str, int]:
    """Write the seed into ``graph`` and return node counts by label."""
    if not replace and _already_loaded(graph):
        raise RuntimeError(f"graph {graph.name!r} already holds an operation; use --replace")
    if replace:
        try:
            graph.delete()
        except Exception as exc:  # the graph doesn't exist yet
            if "empty key" not in str(exc).lower() and "invalid graph" not in str(exc).lower():
                raise

    run(graph, "load_meta", LOAD_META, {"snapshot": ts(SEED_NOW)})
    ensure_indexes(graph)
    ensure_fulltext(graph)

    airlines = _json("airlines.json")
    _write(graph, "load_airlines", LOAD_AIRLINES, airlines)

    flights = _json("flights.json")
    stations = sorted({f["dep_station"] for f in flights} | {f["arr_station"] for f in flights})
    _write(graph, "load_stations", LOAD_STATIONS, [{"code": s} for s in stations])
    _write(
        graph,
        "load_flights",
        LOAD_FLIGHTS,
        [
            {
                "id": f["flight_id"],
                "no": f["flight_no"],
                "date": f["date"],
                "dep": f["dep_station"],
                "arr": f["arr_station"],
                "dep_ts": ts(f["dep_utc"]),
                "arr_ts": ts(f["arr_utc"]),
                "block_hours": f["block_hours"],
                "aircraft": f["aircraft"],
                "type": f["aircraft_type"],
                "seats": f["seats"],
                "booked": f.get("booked") if f.get("booked") is not None else f["seats"],
            }
            for f in flights
        ],
    )
    run(graph, "link_rotations", LINK_ROTATIONS)
    run(graph, "link_routes", LINK_ROUTES)

    rosters = _json("rosters.json")
    _write(
        graph,
        "load_pairings",
        LOAD_PAIRINGS,
        [
            {
                "id": p["pairing_id"],
                "aircraft": p["aircraft"],
                "days": [
                    {
                        "id": f"{p['pairing_id']}/{d['date']}",
                        "seq": i,
                        "date": d["date"],
                        "report_ts": ts(d["report_utc"]),
                        "release_ts": ts(d["release_utc"]),
                        "flights": d["flights"],
                    }
                    for i, d in enumerate(p["days"])
                ],
            }
            for p in rosters["pairings"]
        ],
    )
    run(graph, "derive_duty_days", DERIVE_DUTY_DAYS)

    crew = _json("crew.json")
    _write(
        graph,
        "load_crew",
        LOAD_CREW,
        [
            {
                "id": c["crew_id"],
                "name": c["name"],
                "rank": c["rank"],
                "status": c["status"],
                "seniority": c["seniority"],
                "reach": c["reachability_minutes"],
                "experience": c.get("experience_years") or 0,
                "cq": bool(c.get("command_qualified")),
                "rsq": bool(c.get("right_seat_qualified")),
                "airline": c["airline"],
                "base": c["base"],
                "ratings": c["ratings"],
            }
            for c in crew
        ],
    )
    _write(
        graph,
        "load_roster",
        LOAD_ROSTER,
        [
            {"crew": s["crew_id"], "pairing": p["pairing_id"], "role": s["role"]}
            for p in rosters["pairings"]
            for s in p["crew"]
        ],
    )
    _write(
        graph,
        "load_certs",
        LOAD_CERTS,
        [
            {"crew": c["crew_id"], "type": c["cert_type"], "valid_from": c["valid_from"], "valid_to": c["valid_to"]}
            for c in _json("certifications.json")
        ],
    )
    _write(
        graph,
        "load_clocks",
        LOAD_CLOCKS,
        [
            {
                "crew": c["crew_id"],
                "rest_ts": ts(c["last_rest_ended"]) if c.get("last_rest_ended") else None,
                "days": c["daily_history"],
            }
            for c in _json("duty_clocks.json")
        ],
    )
    _write(
        graph,
        "load_reserves",
        LOAD_RESERVES,
        [
            {
                "crew": r["crew_id"],
                "base": r["base"],
                "date": d,
                "start_ts": ts(f"{d}T{r['oncall_window_utc']['start']}:00Z"),
                "end_ts": ts(f"{d}T{r['oncall_window_utc']['end']}:00Z"),
            }
            for r in _json("reserve_pool.json")
            for d in r["dates"]
        ],
    )
    _write(
        graph,
        "load_risk",
        LOAD_RISK,
        [
            {"crew": r["crew_id"], "score": r["disruption_risk_score"], "drivers": r.get("drivers", [])}
            for r in _json("risk_signals.json")
        ],
    )

    boarding = _json("boarding_gates.json")
    gates = {b["boarding_gate_number"]: False for b in boarding}
    gates |= {g["gate"]: True for g in _json("emergency_gates.json")}
    _write(
        graph,
        "load_gates",
        LOAD_GATES,
        [{"id": g, "station": g.split("-")[0], "emergency": e} for g, e in sorted(gates.items())],
    )
    _write(
        graph,
        "load_boarding",
        LOAD_BOARDING,
        [
            {
                "flight": b["flight_id"],
                "gate": b["boarding_gate_number"],
                "start_ts": ts(b["boarding_start_time"]),
                "end_ts": ts(b["boarding_end_time"]),
            }
            for b in boarding
        ],
    )
    _write(graph, "load_contacts", LOAD_CONTACTS, _json("contacts.json"))

    rules = _json("rules.json")["rules"]
    _write(
        graph,
        "load_rules",
        LOAD_RULES,
        [{"id": r["rule_id"], "text": r["text"], "params": r.get("params", {})} for r in rules],
    )
    costs = {k: v for k, v in _json("costs.json").items() if isinstance(v, int | float)}
    run(graph, "load_costs", LOAD_COSTS, {"costs": costs})

    team = json.loads(TEAM.read_text())["members"]
    _write(
        graph,
        "load_team",
        LOAD_TEAM,
        [
            {"username": m["username"], "name": m["name"], "roles": m["roles"], "crew_id": m.get("crew_id")}
            for m in team
        ],
    )
    _write(graph, "load_desks", LOAD_DESKS, [{"id": d, "aircraft": list(a)} for d, a in DESKS])
    _write(
        graph,
        "staff_desks",
        STAFF_DESKS,
        [{"username": m["username"], "desk": m["desk"]} for m in team if m.get("desk")],
    )
    return {r["label"]: r["n"] for r in run(graph, "count_nodes", COUNT, read_only=True)}


def summarize(graph: Graph) -> dict[str, int]:
    return {r["label"]: r["n"] for r in run(graph, "count_nodes", COUNT, read_only=True)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Load the seed operation into FalkorDB")
    parser.add_argument("--replace", action="store_true", help="drop the graph first")
    args = parser.parse_args(argv)
    gs = Graphs()
    counts = load(gs.ops, replace=args.replace)
    print(f"Loaded into graph {gs.ops_name!r}:")
    for label, n in counts.items():
        print(f"  {label:<14} {n:>6}")


if __name__ == "__main__":
    main()
