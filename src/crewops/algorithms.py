"""Graph algorithms used as tools: PageRank and betweenness over the route network
(betweenness uses a fixed seed so results repeat), weighted shortest paths for positioning,
connected components, knock-on depth along rotations, and who knows a station or type."""

from __future__ import annotations

from typing import Any

from falkordb import Graph

from crewops.graph import run

PAGERANK = """
CALL algo.pageRank('Station', 'ROUTE') YIELD node, score
RETURN node.code AS station, score
"""

BETWEENNESS = """
CALL algo.betweenness({nodeLabels: ['Station'], relationshipTypes: ['ROUTE'],
                       samplingSize: 1000, samplingSeed: 7}) YIELD node, score
RETURN node.code AS station, score
"""

STATION_TRAFFIC = """
MATCH (s:Station)
OPTIONAL MATCH (f:Flight)-[:FROM|TO]->(s)
WITH s, count(f) AS movements, sum(f.booked) AS passengers
OPTIONAL MATCH (c:Crew)-[:BASED_AT]->(s)
RETURN s.code AS station, movements, passengers, count(c) AS crew_based
"""

SP_PATHS = """
MATCH (a:Station {code: $origin}), (b:Station {code: $dest})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['ROUTE'], weightProp: 'block_hours',
                   maxLen: $max_legs, pathCount: $paths})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.code] AS stations, pathWeight AS block_hours
ORDER BY block_hours
"""

WCC = """
CALL algo.WCC({nodeLabels: ['Station'], relationshipTypes: ['ROUTE']}) YIELD node, componentId
RETURN componentId AS component, collect(node.code) AS stations
"""

KNOCK_ON = """
MATCH (f:Flight)
WHERE f.date = $date AND f.status <> 'cancelled'
OPTIONAL MATCH (f)-[:NEXT_LEG*1..8]->(g:Flight)
WHERE g.date = f.date
WITH f, count(g) AS downstream, sum(g.booked) AS passengers
MATCH (f)-[:ON]->(ac:Aircraft)
RETURN f.id AS flight, ac.tail AS aircraft, downstream, passengers
ORDER BY downstream DESC, passengers DESC
LIMIT $limit
"""

WHO_KNOWS_STATION = """
MATCH (c:Crew)-[:ASSIGNED]->(:Pairing)-[:HAS_DAY]->(:DutyDay)-[:LEG]->(f:Flight)-[:TO]->(:Station {code: $station})
RETURN c.id AS crew_id, c.name AS name, c.rank AS rank, count(f) AS legs
ORDER BY legs DESC, crew_id
LIMIT $limit
"""

WHO_KNOWS_TYPE = """
MATCH (c:Crew)-[:RATED_ON]->(:AircraftType {name: $type})
OPTIONAL MATCH (c)-[:ASSIGNED]->(:Pairing)-[:HAS_DAY]->(d:DutyDay)
RETURN c.id AS crew_id, c.name AS name, c.rank AS rank, c.experience_years AS years,
       count(d) AS duty_days
ORDER BY years DESC, duty_days DESC
LIMIT $limit
"""


def station_criticality(graph: Graph) -> list[dict[str, Any]]:
    pagerank = {r["station"]: r["score"] for r in run(graph, "pagerank_stations", PAGERANK, read_only=True)}
    between = {r["station"]: r["score"] for r in run(graph, "betweenness_stations", BETWEENNESS, read_only=True)}
    traffic = run(graph, "station_traffic", STATION_TRAFFIC, read_only=True)
    total = sum(pagerank.values()) or 1.0
    rows = [
        {
            **t,
            "pagerank": round(pagerank.get(t["station"], 0.0) / total, 3),
            "betweenness": between.get(t["station"], 0.0),
        }
        for t in traffic
    ]
    return sorted(rows, key=lambda r: (-r["betweenness"], -r["pagerank"], r["station"]))


def positioning_routes(graph: Graph, origin: str, dest: str, max_legs: int = 3, paths: int = 3) -> list[dict[str, Any]]:
    return run(
        graph,
        "sp_paths",
        SP_PATHS,
        {"origin": origin.upper(), "dest": dest.upper(), "max_legs": max_legs, "paths": paths},
        read_only=True,
    )


def network_components(graph: Graph) -> list[dict[str, Any]]:
    return run(graph, "wcc_stations", WCC, read_only=True)


def knock_on(graph: Graph, date: str, limit: int = 10) -> list[dict[str, Any]]:
    return run(graph, "knock_on", KNOCK_ON, {"date": date, "limit": limit}, read_only=True)


def who_knows(
    graph: Graph, station: str | None = None, aircraft_type: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    if station:
        return run(
            graph, "who_knows_station", WHO_KNOWS_STATION, {"station": station.upper(), "limit": limit}, read_only=True
        )
    if aircraft_type:
        return run(
            graph, "who_knows_type", WHO_KNOWS_TYPE, {"type": aircraft_type.upper(), "limit": limit}, read_only=True
        )
    raise ValueError("name a station or an aircraft type")
