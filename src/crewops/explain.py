"""Explanations read back from the graph: decision trail, why a crew member was not
picked, which rules exclude the most candidates, and a subgraph for the UI."""

from __future__ import annotations

import json
from typing import Any

from falkordb import Graph

from crewops.graph import run
from crewops.legality import OPEN_STATUSES

TRAIL_QUERY = """
MATCH (d:Disruption {id: $id})-[:REPORTED_BY]->(r:User)
OPTIONAL MATCH (d)-[:ASSIGNED_TO]->(desk:Desk)<-[:STAFFS]-(ctl:User)
OPTIONAL MATCH (d)-[:LOGGED]->(e:Event)
WITH d, r, desk, ctl, e ORDER BY e.seq
WITH d, r, desk, ctl, collect(e{.seq, .kind, .ts, .actor, .note}) AS events
OPTIONAL MATCH (pl:Plan)-[:FOR]->(d)
OPTIONAL MATCH (pl)-[:SUBMITTED_BY]->(s:User)
OPTIONAL MATCH (pl)-[:APPROVED_BY]->(a:User)
OPTIONAL MATCH (pl)-[:REJECTED_BY]->(rj:User)
OPTIONAL MATCH (pl)-[:HAS]->(x:Assignment)-[:ASSIGNS]->(c:Crew)
WITH d, r, desk, ctl, events, pl, s, a, rj,
     collect(CASE WHEN x IS NULL THEN NULL ELSE {gap: x.gap, crew: c.id, name: c.name,
             action: x.action, cost: x.cost, strategy: x.strategy} END) AS assigned
RETURN d{.id, .type, .status, .summary, .reported_ts} AS disruption,
       r.name AS reported_by, desk.id AS desk, ctl.name AS controller, events,
       collect(CASE WHEN pl IS NULL THEN NULL ELSE {
         plan: pl.id, status: pl.status, fingerprint: pl.fingerprint, seal: pl.seal,
         total_cost: pl.total_cost, submitted_by: s.name, approved_by: a.name,
         rejected_by: rj.name, reject_reason: pl.reject_reason, assignments: assigned} END) AS plans
"""

GAP_SUMMARY_QUERY = """
MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap)-[:VACATED_BY]->(v:Crew)
OPTIONAL MATCH (g)-[o:OPTION]->(c:Crew)
WITH g, v, o, c ORDER BY o.rank
WITH g, v, collect(CASE WHEN c IS NULL THEN NULL ELSE {rank: o.rank, crew: c.id, name: c.name,
     cost: o.cost, strategy: o.strategy} END)[0..3] AS top
OPTIONAL MATCH (g)-[:RULED_OUT]->(x:Exclusion)-[:BREACHES]->(rule:Rule)
WITH g, v, top, rule.id AS rule, count(x) AS n
WITH g, v, top, collect(CASE WHEN rule IS NULL THEN NULL ELSE {rule: rule, excluded: n} END) AS breaches
RETURN g.id AS gap, g.pairing AS pairing, g.role AS role, v.id AS vacated_by, top, breaches
ORDER BY gap
"""

WHY_NOT_EXCLUDED = """
MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap)-[:RULED_OUT]->(x:Exclusion)-[:CANDIDATE]->(c:Crew {id: $crew})
OPTIONAL MATCH (x)-[:BREACHES]->(r:Rule)
RETURN g.id AS gap, g.pairing AS pairing, c.name AS name, x.stage AS stage, x.reasons AS reasons,
       collect({rule: r.id, text: r.text}) AS rules
"""

WHY_NOT_OPTION = """
MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap)-[o:OPTION]->(c:Crew {id: $crew})
OPTIONAL MATCH (g)-[better:OPTION]->(b:Crew) WHERE better.rank < o.rank
WITH g, o, c, better, b ORDER BY better.rank
RETURN g.id AS gap, g.pairing AS pairing, c.name AS name, o.rank AS rank, o.cost AS cost,
       o.action AS action,
       collect(CASE WHEN b IS NULL THEN NULL ELSE {rank: better.rank, crew: b.id, name: b.name,
               cost: better.cost} END) AS ranked_above
"""

RULE_PRESSURE_QUERY = """
MATCH (d:Disruption)-[:HAS_GAP]->(:Gap)-[:RULED_OUT]->(x:Exclusion)-[:BREACHES]->(r:Rule)
WHERE d.status IN $statuses
RETURN r.id AS rule, r.text AS text, count(DISTINCT x) AS exclusions, count(DISTINCT d) AS disruptions
ORDER BY exclusions DESC
"""

SUBGRAPH_QUERIES = (
    (
        "MATCH (d:Disruption {id: $id})-[r:REPORTED_BY|ASSIGNED_TO|CONCERNS|HAS_GAP|UNCOVERS|DELAYS|MOVES|NOTIFIES]->(n) "
        "RETURN d AS a, type(r) AS rel, n AS b LIMIT 200"
    ),
    ("MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap)-[r:IN|VACATED_BY]->(n) RETURN g AS a, type(r) AS rel, n AS b"),
    (
        "MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap)-[r:OPTION]->(n) WHERE r.rank <= 3 "
        "RETURN g AS a, 'OPTION #' + toString(r.rank) AS rel, n AS b"
    ),
    ("MATCH (pl:Plan)-[r:FOR]->(d:Disruption {id: $id}) RETURN pl AS a, type(r) AS rel, d AS b"),
    (
        "MATCH (pl:Plan)-[:FOR]->(:Disruption {id: $id}) MATCH (pl)-[r:SUBMITTED_BY|APPROVED_BY|REJECTED_BY]->(u) "
        "RETURN pl AS a, type(r) AS rel, u AS b"
    ),
    (
        "MATCH (pl:Plan)-[:FOR]->(:Disruption {id: $id}) MATCH (pl)-[:HAS]->(x:Assignment)-[:ASSIGNS]->(c) "
        "RETURN pl AS a, 'ASSIGNS' AS rel, c AS b"
    ),
)

_KEYS = {
    "Disruption": "id",
    "Gap": "id",
    "Plan": "id",
    "Crew": "id",
    "Flight": "id",
    "Pairing": "id",
    "User": "username",
    "Desk": "id",
    "Station": "code",
    "Gate": "id",
    "Aircraft": "tail",
    "Contact": "name",
}


def decision_trail(graph: Graph, disruption_id: str) -> dict[str, Any]:
    rows = run(graph, "decision_trail", TRAIL_QUERY, {"id": disruption_id}, read_only=True)
    if not rows:
        raise LookupError(f"no disruption {disruption_id!r}")
    trail = rows[0]
    trail["plans"] = [p for p in trail["plans"] if p]
    trail["gaps"] = run(graph, "gap_summary", GAP_SUMMARY_QUERY, {"id": disruption_id}, read_only=True)
    for gap in trail["gaps"]:
        gap["top"] = [t for t in gap["top"] if t]
        gap["breaches"] = sorted((b for b in gap["breaches"] if b), key=lambda b: -b["excluded"])
    return trail


def why_not(graph: Graph, disruption_id: str, crew_id: str) -> dict[str, Any]:
    crew_id = crew_id.upper()
    excluded = run(graph, "why_not_excluded", WHY_NOT_EXCLUDED, {"id": disruption_id, "crew": crew_id}, read_only=True)
    if excluded:
        return {"crew_id": crew_id, "legal": False, "exclusions": excluded}
    options = run(graph, "why_not_option", WHY_NOT_OPTION, {"id": disruption_id, "crew": crew_id}, read_only=True)
    if options:
        for o in options:
            o["ranked_above"] = [b for b in o["ranked_above"] if b]
        return {"crew_id": crew_id, "legal": True, "options": options}
    return {
        "crew_id": crew_id,
        "legal": None,
        "note": f"{crew_id} was not a candidate for {disruption_id}: wrong rank or airline, or already on the trip",
    }


def rule_pressure(graph: Graph, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    return run(
        graph,
        "rule_pressure",
        RULE_PRESSURE_QUERY,
        {"statuses": statuses or [*OPEN_STATUSES, "RESOLVED"]},
        read_only=True,
    )


def _node(n: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    label = n["_labels"][0]
    key = n.get(_KEYS.get(label, "id")) or n.get("id") or n.get("name")
    caption = {
        "Crew": f"{n.get('id')} {n.get('name', '')}",
        "User": n.get("name"),
        "Gap": f"{n.get('role')} seat {n.get('pairing')}",
        "Contact": f"{n.get('name')} ({n.get('team')})",
        "Disruption": f"{n.get('id')} {n.get('type', '')}",
    }.get(label, str(key))
    node_id = f"{label}:{key}"
    props = {
        k: v for k, v in n.items() if not k.startswith("_") and k not in ("analysis", "details", "funnel", "fallbacks")
    }
    return node_id, {"id": node_id, "label": caption, "group": label, "props": props}


def subgraph(graph: Graph, disruption_id: str) -> dict[str, list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for i, q in enumerate(SUBGRAPH_QUERIES):
        for row in run(graph, f"subgraph_{i}", q, {"id": disruption_id}, read_only=True):
            a_id, a = _node(row["a"])
            b_id, b = _node(row["b"])
            nodes.setdefault(a_id, a)
            nodes.setdefault(b_id, b)
            edges.append({"from": a_id, "to": b_id, "label": row["rel"]})
    seen = set()
    unique = []
    for e in edges:
        k = (e["from"], e["to"], e["label"])
        if k not in seen:
            seen.add(k)
            unique.append(e)
    return {"nodes": list(nodes.values()), "edges": unique}


def analysis_of(graph: Graph, disruption_id: str) -> dict[str, Any] | None:
    rows = run(
        graph,
        "analysis_of",
        "MATCH (d:Disruption {id: $id}) RETURN d.analysis AS a",
        {"id": disruption_id},
        read_only=True,
    )
    return json.loads(rows[0]["a"]) if rows and rows[0]["a"] else None
