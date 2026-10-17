"""The graph data model and its indexes.

Operation graph (``crewops``)
=============================

The airline::

    (:Airline {code, name, color})
    (:Aircraft {tail, type})-[:OPERATED_BY]->(:Airline)
    (:Aircraft)-[:OF_TYPE]->(:AircraftType {name})
    (:Station {code})-[:ROUTE {flights, block_hours}]->(:Station)     one per city pair flown
    (:Gate {id, emergency})-[:AT]->(:Station)

Flying::

    (:Flight {id, no, date, dep_ts, arr_ts, block_hours, seats, booked, aircraft_type, status})
    (:Flight)-[:FROM]->(:Station)   (:Flight)-[:TO]->(:Station)   (:Flight)-[:ON]->(:Aircraft)
    (:Flight)-[:NEXT_LEG {turn_minutes}]->(:Flight)                   the aircraft's rotation
    (:Flight)-[:BOARDS_AT {start_ts, end_ts}]->(:Gate)
    (:Pairing {id})-[:FLOWN_BY]->(:Aircraft)
    (:Pairing)-[:HAS_DAY {seq}]->(:DutyDay {id, date, report_ts, release_ts, sectors,
                                             block_hours, aircraft_type, dep_station})
    (:DutyDay)-[:LEG {seq}]->(:Flight)

People::

    (:Crew {id, name, rank, status, seniority, reach_minutes, experience_years,
            command_qualified, right_seat_qualified, last_rest_ended_ts, risk, risk_drivers})
    (:Crew)-[:EMPLOYED_BY]->(:Airline)    (:Crew)-[:BASED_AT]->(:Station)
    (:Crew)-[:RATED_ON]->(:AircraftType)
    (:Crew)-[:HOLDS]->(:Certification {type, valid_from, valid_to})
    (:Crew)-[:LOGGED]->(:DutyLog {date, duty_hours, flight_hours})   28 days of history
    (:Crew)-[:ON_CALL {date, start_ts, end_ts}]->(:Station)          reserve duty
    (:Crew)-[:ASSIGNED {role}]->(:Pairing)                           the live roster
    (:User {username, name, roles})-[:IS_CREW]->(:Crew)
    (:User)-[:STAFFS]->(:Desk {id})-[:WATCHES]->(:Aircraft)
    (:Contact {team, scope, name, title})-[:ON_DUTY_AT]->(:Station)  scope NETWORK has no edge

Rules and money::

    (:Rule {id, text, ...params})        thresholds the legality checks read
    (:CostTable {...})                   one node: callouts, allowances, delay, cancellation
    (:Meta {snapshot_ts, loaded_ts})

Disruptions, decisions and agents (written at run time)::

    (:Disruption {id, type, status, reported_ts, summary, details})
        -[:REPORTED_BY]->(:User)  -[:CONCERNS]->(:Crew|:Flight|:Station|:Gate|:Aircraft)
        -[:GROUNDS {date}]->(:Crew)  -[:UNCOVERS]->(:Flight)  -[:AT_RISK]->(:Flight)
        -[:ASSIGNED_TO]->(:Desk)  -[:NOTIFIES {reason}]->(:Contact)
        -[:HAS_GAP]->(:Gap {id, role, dates})-[:IN]->(:Pairing)
                     (:Gap)-[:VACATED_BY]->(:Crew)
                     (:Gap)-[:OPTION {rank, strategy, cost, delay_hours, ...}]->(:Crew)
                     (:Gap)-[:RULED_OUT]->(:Exclusion {stage, reasons})-[:CANDIDATE]->(:Crew)
                                          (:Exclusion)-[:BREACHES]->(:Rule)
    (:Plan {id, status, fingerprint, total_cost})-[:FOR]->(:Disruption)
        -[:SUBMITTED_BY]->(:User)  -[:APPROVED_BY|REJECTED_BY]->(:User)
        -[:HAS]->(:Assignment {strategy, cost})-[:FILLS]->(:Gap)
                  (:Assignment)-[:ASSIGNS]->(:Crew)
    (:Disruption)-[:LOGGED]->(:Event {seq, kind, ts, actor, note})
    (:Agent {name, kind})-[:CLAIMED {ts}]->(:Disruption)
    (:Handoff {ts, note})-[:FROM]->(:Agent)  (:Handoff)-[:TO]->(:Desk|:User)
                          (:Handoff)-[:ABOUT]->(:Disruption)
    (:Playbook {type, role})-[:CHOSE {count}]->(:Strategy {name})     shared procedural memory

Memory graphs (``crewops_memory_<user>``, one per user)
=======================================================

::

    (:Session {id, started_ts, last_ts})-[:HAS]->(:Turn {seq, ts, question, answer, intent})
    (:Turn)-[:NEXT]->(:Turn)
    (:Turn)-[:MENTIONS]->(:Entity {kind, ref})
    (:Fact {key, value, ts, active})-[:SUPERSEDES]->(:Fact)
    (:Fact)-[:LEARNED_IN]->(:Turn)

Times are Unix seconds (``*_ts``) so Cypher can compare and subtract them; dates are
``YYYY-MM-DD`` strings, which order correctly as text.
"""

from __future__ import annotations

from falkordb import Graph

from crewops.graph import run

OPS_INDEXES: tuple[tuple[str, str], ...] = (
    ("Airline", "code"),
    ("Aircraft", "tail"),
    ("AircraftType", "name"),
    ("Station", "code"),
    ("Gate", "id"),
    ("Flight", "id"),
    ("Flight", "date"),
    ("Pairing", "id"),
    ("DutyDay", "id"),
    ("DutyDay", "date"),
    ("Crew", "id"),
    ("Crew", "rank"),
    ("DutyLog", "date"),
    ("User", "username"),
    ("Desk", "id"),
    ("Rule", "id"),
    ("Disruption", "id"),
    ("Disruption", "status"),
    ("Gap", "id"),
    ("Plan", "id"),
    ("Agent", "name"),
    ("Contact", "team"),
)

MEMORY_INDEXES: tuple[tuple[str, str], ...] = (
    ("Session", "id"),
    ("Turn", "seq"),
    ("Entity", "ref"),
    ("Fact", "key"),
)


def _existing(graph: Graph) -> set[tuple[str, str]]:
    rows = run(graph, "list_indexes", "CALL db.indexes() YIELD label, properties RETURN label, properties")
    return {(r["label"], p) for r in rows for p in r["properties"]}


def ensure_indexes(graph: Graph, indexes: tuple[tuple[str, str], ...] = OPS_INDEXES) -> None:
    """Create any missing range index. Safe to call repeatedly."""
    have = _existing(graph)
    for label, prop in indexes:
        if (label, prop) not in have:
            run(graph, "create_index", f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")


def ensure_fulltext(graph: Graph) -> None:
    """Full-text search over crew names, so 'Pillai' finds C-3315."""
    try:
        run(graph, "create_fulltext", "CALL db.idx.fulltext.createNodeIndex('Crew', 'name')")
    except Exception as exc:  # already exists
        if "already" not in str(exc).lower():
            raise
