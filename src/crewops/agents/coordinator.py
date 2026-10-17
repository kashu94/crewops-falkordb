"""Shared state between agents: claims, handoffs and each user's inbox.

``claim_next`` moves a REPORTED disruption to ANALYSING in one write query. FalkorDB runs
write queries on a graph one at a time, so two agents cannot claim the same one."""

from __future__ import annotations

from typing import Any

from falkordb import Graph

from crewops.graph import run
from crewops.legality import OPEN_STATUSES

MAX_ATTEMPTS = 3

REGISTER_AGENT = """
MERGE (a:Agent {name: $name})
ON CREATE SET a.kind = $kind, a.started_ts = $now
SET a.seen_ts = $now
"""

CLAIM_NEXT = """
MATCH (d:Disruption {status: 'REPORTED'})
WITH d ORDER BY d.reported_ts, d.id LIMIT 1
SET d.status = 'ANALYSING', d.claimed_by = $agent, d.claimed_ts = $now,
    d.attempts = coalesce(d.attempts, 0) + 1
WITH d
MERGE (a:Agent {name: $agent})
CREATE (a)-[:CLAIMED {ts: $now}]->(d)
RETURN d.id AS id, d.type AS type, d.attempts AS attempts
"""

RELEASE_FAILED = """
MATCH (d:Disruption {id: $id})
SET d.status = CASE WHEN d.attempts >= $max THEN 'PARKED' ELSE 'REPORTED' END,
    d.last_error = $error
RETURN d.status AS status
"""

HANDOFF = """
MATCH (d:Disruption {id: $id}), (a:Agent {name: $agent})
OPTIONAL MATCH (desk:Desk {id: $desk})
OPTIONAL MATCH (u:User {username: $user})
CREATE (h:Handoff {ts: $now, note: $note, kind: $kind})
CREATE (h)-[:FROM]->(a), (h)-[:ABOUT]->(d)
FOREACH (_ IN CASE WHEN desk IS NULL THEN [] ELSE [1] END | CREATE (h)-[:TO]->(desk))
FOREACH (_ IN CASE WHEN u IS NULL THEN [] ELSE [1] END | CREATE (h)-[:TO]->(u))
"""

ACK_HANDOFFS = """
OPTIONAL MATCH (:User {username: $user})-[:STAFFS]->(desk:Desk)
WITH collect(desk.id) AS desks
MATCH (h:Handoff)-[:TO]->(t)
WHERE h.acked_ts IS NULL
  AND ((t:User AND t.username = $user) OR (t:Desk AND t.id IN desks))
SET h.acked_ts = $now, h.acked_by = $user
RETURN count(h) AS acked
"""

INBOX_CONTROLLER = """
MATCH (u:User {username: $user})-[:STAFFS]->(desk:Desk)<-[:ASSIGNED_TO]-(d:Disruption)
WHERE d.status IN $open
OPTIONAL MATCH (h:Handoff)-[:ABOUT]->(d) WHERE h.acked_ts IS NULL
OPTIONAL MATCH (pl:Plan {status: 'REJECTED'})-[:FOR]->(d)
RETURN d.id AS disruption, d.type AS type, d.status AS status, d.summary AS summary,
       d.reported_ts AS reported_ts, count(DISTINCT h) AS new_handoffs,
       collect(DISTINCT pl.reject_reason) AS rejected
ORDER BY reported_ts
"""

INBOX_APPROVER = """
MATCH (pl:Plan {status: 'SUBMITTED'})-[:FOR]->(d:Disruption)
MATCH (pl)-[:SUBMITTED_BY]->(s:User)
WHERE s.username <> $user
RETURN pl.id AS plan, d.id AS disruption, d.summary AS summary, pl.total_cost AS cost,
       s.name AS submitted_by, pl.submitted_ts AS submitted_ts
ORDER BY submitted_ts
"""

INBOX_REPORTER = """
MATCH (d:Disruption)-[:REPORTED_BY]->(:User {username: $user})
OPTIONAL MATCH (d)-[:ASSIGNED_TO]->(desk:Desk)
RETURN d.id AS disruption, d.status AS status, d.summary AS summary, desk.id AS desk,
       d.reported_ts AS reported_ts
ORDER BY reported_ts DESC LIMIT 10
"""

INBOX_CREW = """
MATCH (:User {username: $user})-[:IS_CREW]->(c:Crew)
OPTIONAL MATCH (d:Disruption)-[n:NOTIFIES]->(c)
OPTIONAL MATCH (c)-[a:ASSIGNED]->(p:Pairing) WHERE a.via_plan IS NOT NULL
RETURN c.id AS crew_id,
       collect(DISTINCT CASE WHEN d IS NULL THEN NULL ELSE {disruption: d.id, summary: d.summary, reason: n.reason} END) AS notices,
       collect(DISTINCT CASE WHEN p IS NULL THEN NULL ELSE {pairing: p.id, plan: a.via_plan, role: a.role} END) AS new_trips
"""

AGENT_ACTIVITY = """
MATCH (a:Agent)
OPTIONAL MATCH (a)-[c:CLAIMED]->(d:Disruption)
OPTIONAL MATCH (h:Handoff)-[:FROM]->(a)
RETURN a.name AS agent, a.kind AS kind, a.seen_ts AS seen_ts, count(DISTINCT d) AS claimed,
       count(DISTINCT h) AS handoffs
ORDER BY agent
"""

USER_ROLES = "MATCH (u:User {username: $user}) RETURN u.roles AS roles, u.name AS name"


def register(graph: Graph, name: str, kind: str, now_ts: int) -> None:
    run(graph, "register_agent", REGISTER_AGENT, {"name": name, "kind": kind, "now": now_ts})


def claim_next(graph: Graph, agent: str, now_ts: int) -> dict[str, Any] | None:
    rows = run(graph, "claim_next", CLAIM_NEXT, {"agent": agent, "now": now_ts})
    return rows[0] if rows else None


def release_failed(graph: Graph, disruption_id: str, error: str) -> str:
    rows = run(
        graph, "release_failed", RELEASE_FAILED, {"id": disruption_id, "error": error[:500], "max": MAX_ATTEMPTS}
    )
    return rows[0]["status"] if rows else "UNKNOWN"


def handoff(
    graph: Graph,
    agent: str,
    disruption_id: str,
    note: str,
    now_ts: int,
    desk: str | None = None,
    user: str | None = None,
    kind: str = "analysis_ready",
) -> None:
    run(
        graph,
        "handoff",
        HANDOFF,
        {"id": disruption_id, "agent": agent, "desk": desk, "user": user, "note": note, "now": now_ts, "kind": kind},
    )


def ack_handoffs(graph: Graph, username: str, now_ts: int) -> int:
    return run(graph, "ack_handoffs", ACK_HANDOFFS, {"user": username, "now": now_ts})[0]["acked"]


def inbox(graph: Graph, username: str) -> dict[str, Any]:
    """Everything waiting for this person, by each role they hold."""
    rows = run(graph, "user_roles", USER_ROLES, {"user": username}, read_only=True)
    if not rows:
        return {"user": username, "roles": [], "items": {}}
    roles = set(rows[0]["roles"])
    items: dict[str, Any] = {}
    if "controller" in roles:
        items["desk"] = run(
            graph, "inbox_controller", INBOX_CONTROLLER, {"user": username, "open": OPEN_STATUSES}, read_only=True
        )
    if "approver" in roles:
        items["approvals"] = run(graph, "inbox_approver", INBOX_APPROVER, {"user": username}, read_only=True)
    if "reporter" in roles:
        items["my_reports"] = run(graph, "inbox_reporter", INBOX_REPORTER, {"user": username}, read_only=True)
    if "crew" in roles:
        crew = run(graph, "inbox_crew", INBOX_CREW, {"user": username}, read_only=True)
        if crew:
            items["crew"] = {
                "crew_id": crew[0]["crew_id"],
                "notices": [n for n in crew[0]["notices"] if n],
                "new_trips": [t for t in crew[0]["new_trips"] if t],
            }
    return {"user": username, "name": rows[0]["name"], "roles": sorted(roles), "items": items}


def activity(graph: Graph) -> list[dict[str, Any]]:
    return run(graph, "agent_activity", AGENT_ACTIVITY, read_only=True)
