"""Plans and four-eyes approval.

A controller on the owning desk submits a plan; a different user with the approver role
approves it. Approval re-checks legality and the plan fingerprint, then commits the roster
change, cancellations and gate moves in a single Cypher query. Committed plans update the
shared playbook."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from falkordb import Graph

from crewops.disruptions import NotAllowed, get, log, next_id, user
from crewops.graph import run
from crewops.legality import Context, UnknownEntity, assess


class PlanError(ValueError):
    pass


class Tampered(PlanError):
    pass


# --- Cypher ---------------------------------------------------------------------------------

GAP_OPTION_QUERY = """
MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap {id: $gap})-[o:OPTION]->(c:Crew {id: $crew})
MATCH (g)-[:VACATED_BY]->(v:Crew)
RETURN g.pairing AS pairing, g.role AS role, v.id AS vacated, o.strategy AS strategy,
       o.cost AS cost, o.rank AS rank, o.action AS action
"""

GAPS_QUERY = """
MATCH (d:Disruption {id: $id})-[:HAS_GAP]->(g:Gap)
OPTIONAL MATCH (g)-[o:OPTION {rank: 1}]->(c:Crew)
RETURN g.id AS gap, c.id AS crew ORDER BY gap
"""

ROSTER_STATE_QUERY = """
MATCH (p:Pairing) WHERE p.id IN $pairings
OPTIONAL MATCH (c:Crew)-[a:ASSIGNED]->(p)
WITH p, c, a ORDER BY c.id
RETURN p.id AS pairing, collect(c.id + ':' + a.role) AS crew
ORDER BY pairing
"""

FLIGHT_STATE_QUERY = """
MATCH (f:Flight) WHERE f.id IN $flights
OPTIONAL MATCH (f)-[:BOARDS_AT]->(g:Gate)
RETURN f.id AS flight, f.status AS status, g.id AS gate ORDER BY flight
"""

CREATE_PLAN = """
MATCH (d:Disruption {id: $id}), (u:User {username: $user})
CREATE (pl:Plan {id: $plan, status: 'SUBMITTED', fingerprint: $fp, total_cost: $cost,
                 submitted_ts: $now, cancel_flights: $cancel, gate_moves: $moves, note: $note})
CREATE (pl)-[:FOR]->(d), (pl)-[:SUBMITTED_BY {ts: $now}]->(u)
SET d.status = 'PLAN_SUBMITTED'
WITH pl
UNWIND $assignments AS a
MATCH (g:Gap {id: a.gap}), (c:Crew {id: a.crew})
CREATE (pl)-[:HAS]->(x:Assignment {gap: a.gap, crew: a.crew, strategy: a.strategy, cost: a.cost,
                                   action: a.action, pairing: a.pairing, role: a.role,
                                   vacated: a.vacated})
CREATE (x)-[:FILLS]->(g), (x)-[:ASSIGNS]->(c)
"""

GET_PLAN = """
MATCH (pl:Plan {id: $plan})-[:FOR]->(d:Disruption)
MATCH (pl)-[:SUBMITTED_BY]->(s:User)
OPTIONAL MATCH (pl)-[:HAS]->(x:Assignment)
WITH pl, d, s, x ORDER BY x.gap
RETURN pl{.*} AS plan, d.id AS disruption, s.username AS submitted_by,
       collect(x{.*}) AS assignments
"""

COMMIT_PLAN = """
MATCH (pl:Plan {id: $plan}) WHERE pl.status = 'SUBMITTED' AND pl.fingerprint = $fp
MATCH (pl)-[:FOR]->(d:Disruption), (u:User {username: $user})
SET pl.status = 'COMMITTED', pl.approved_ts = $now, pl.seal = $seal, d.status = 'RESOLVED'
CREATE (pl)-[:APPROVED_BY {ts: $now}]->(u)
WITH pl
OPTIONAL MATCH (f:Flight) WHERE f.id IN $cancel
WITH pl, collect(f) AS cancelled
FOREACH (f IN cancelled | SET f.status = 'cancelled')
WITH pl
UNWIND (CASE WHEN size($moves) = 0 THEN [null] ELSE $moves END) AS m
OPTIONAL MATCH (mf:Flight {id: m.flight})-[old_gate:BOARDS_AT]->(:Gate)
OPTIONAL MATCH (ng:Gate {id: m.to_gate})
WITH pl, mf, ng, old_gate, old_gate.start_ts AS gs, old_gate.end_ts AS ge
DELETE old_gate
FOREACH (_ IN CASE WHEN mf IS NULL OR ng IS NULL THEN [] ELSE [1] END |
  CREATE (mf)-[:BOARDS_AT {start_ts: gs, end_ts: ge, via_plan: pl.id}]->(ng))
WITH pl, count(*) AS moved
UNWIND (CASE WHEN size($swaps) = 0 THEN [null] ELSE $swaps END) AS s
OPTIONAL MATCH (p:Pairing {id: s.pairing})
OPTIONAL MATCH (c:Crew {id: s.crew})
OPTIONAL MATCH (:Crew {id: s.vacated})-[old:ASSIGNED]->(p)
DELETE old
FOREACH (_ IN CASE WHEN p IS NULL OR c IS NULL THEN [] ELSE [1] END |
  CREATE (c)-[:ASSIGNED {role: s.role, via_plan: pl.id}]->(p))
WITH pl, moved, count(*) AS swapped
RETURN pl.status AS status, moved, swapped
"""

REJECT_PLAN = """
MATCH (pl:Plan {id: $plan}) WHERE pl.status = 'SUBMITTED'
MATCH (pl)-[:FOR]->(d:Disruption), (u:User {username: $user})
SET pl.status = 'REJECTED', pl.rejected_ts = $now, pl.reject_reason = $reason,
    d.status = 'OPTIONS_READY'
CREATE (pl)-[:REJECTED_BY {ts: $now, reason: $reason}]->(u)
RETURN pl.status AS status
"""

CLOSE_DISRUPTION = """
MATCH (d:Disruption {id: $id}) WHERE NOT d.status IN ['RESOLVED', 'CLOSED']
SET d.status = 'CLOSED', d.closed_ts = $now, d.close_reason = $reason
RETURN d.status AS status
"""

LEARN_PLAYBOOK = """
MATCH (pl:Plan {id: $plan})-[:FOR]->(d:Disruption)
MATCH (pl)-[:HAS]->(x:Assignment)
MERGE (b:Playbook {type: d.type, role: x.role})
MERGE (s:Strategy {name: x.strategy})
MERGE (b)-[c:CHOSE]->(s)
ON CREATE SET c.count = 0
SET c.count = c.count + 1, c.last_plan = pl.id
"""

PENDING_FOR_APPROVER = """
MATCH (pl:Plan {status: 'SUBMITTED'})-[:FOR]->(d:Disruption)
MATCH (pl)-[:SUBMITTED_BY]->(s:User)
WHERE s.username <> $user
RETURN pl.id AS plan, d.id AS disruption, d.summary AS summary, pl.total_cost AS cost,
       s.name AS submitted_by, pl.submitted_ts AS submitted_ts
ORDER BY submitted_ts
"""


# --- fingerprint ----------------------------------------------------------------------------


def _fingerprint(
    graph: Graph,
    disruption_id: str,
    assignments: list[dict[str, Any]],
    cancel: list[str],
    moves: list[dict[str, Any]],
    submitter: str,
    submitted_ts: int,
) -> str:
    """SHA-256 over exactly what decides the plan, including the roster and flight state it
    was made against, read from the graph."""
    pairings = sorted({a["pairing"] for a in assignments})
    flights = sorted(set(cancel) | {m["flight"] for m in moves})
    roster = run(graph, "roster_state", ROSTER_STATE_QUERY, {"pairings": pairings}, read_only=True) if pairings else []
    flight_state = (
        run(graph, "flight_state", FLIGHT_STATE_QUERY, {"flights": flights}, read_only=True) if flights else []
    )
    body = {
        "disruption": disruption_id,
        "assignments": sorted(
            ({k: a[k] for k in ("gap", "crew", "strategy", "cost", "pairing", "role", "vacated")} for a in assignments),
            key=lambda a: a["gap"],
        ),
        "cancel": sorted(cancel),
        "moves": sorted(({"flight": m["flight"], "to_gate": m["to_gate"]} for m in moves), key=lambda m: m["flight"]),
        "roster": roster,
        "flights": flight_state,
        "submitted_by": submitter,
        "submitted_ts": submitted_ts,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- submit / approve / reject --------------------------------------------------------------


def submit(
    graph: Graph,
    disruption_id: str,
    username: str,
    assignments: list[dict[str, str]] | None = None,
    cancel_flights: list[str] | None = None,
    gate_moves: list[dict[str, str]] | None = None,
    note: str = "",
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Submit a plan. Without ``assignments`` the analysis's own choice is used: the joint plan
    when several seats are open, otherwise each gap's top option."""
    ctx = Context.open(graph, now_ts)
    who = user(graph, username)
    d = get(graph, disruption_id)
    if "controller" not in who["roles"]:
        raise NotAllowed(f"{username} is not a controller")
    if d.get("desk") and who.get("desk") != d["desk"]:
        raise NotAllowed(f"{disruption_id} belongs to {d['desk']}; {username} staffs {who.get('desk')}")
    if d["status"] != "OPTIONS_READY":
        raise PlanError(f"{disruption_id} is {d['status']}; a plan needs OPTIONS_READY")

    analysis = d.get("analysis") or {}
    if assignments is None:
        joint = analysis.get("joint_plan")
        if joint:
            assignments = [{"gap": a["gap"], "crew_id": a["crew_id"]} for a in joint["assignments"]]
        else:
            rows = run(graph, "gaps", GAPS_QUERY, {"id": disruption_id}, read_only=True)
            assignments = [{"gap": r["gap"], "crew_id": r["crew"]} for r in rows if r["crew"]]
    if gate_moves is None and analysis.get("gate_moves"):
        gate_moves = [
            {"flight": m["flight_id"], "to_gate": m["to_gate"]} for m in analysis["gate_moves"] if m["to_gate"]
        ]
    cancel_flights = sorted(set(cancel_flights or []))
    gate_moves = gate_moves or []
    if not assignments and not cancel_flights and not gate_moves:
        raise PlanError("the plan changes nothing")

    resolved: list[dict[str, Any]] = []
    for a in assignments:
        crew = a["crew_id"].upper()
        rows = run(
            graph, "gap_option", GAP_OPTION_QUERY, {"id": disruption_id, "gap": a["gap"], "crew": crew}, read_only=True
        )
        if not rows:
            raise PlanError(f"{crew} is not a legal option for {a['gap']}")
        r = rows[0]
        check = assess(graph, crew, r["pairing"], ctx=ctx)
        if not check.legal:
            raise PlanError(f"{crew} is no longer legal for {r['pairing']}: {check.blocking[0].detail}")
        resolved.append(
            {"gap": a["gap"], "crew": crew}
            | {k: r[k] for k in ("pairing", "role", "vacated", "strategy", "cost", "action")}
        )
    crew_ids = [a["crew"] for a in resolved]
    if len(set(crew_ids)) != len(crew_ids):
        raise PlanError("the same crew member cannot fill two seats")

    total = sum(a["cost"] for a in resolved)
    plan_id = next_id(graph, "plan", "PL")
    fp = _fingerprint(graph, disruption_id, resolved, cancel_flights, gate_moves, username, ctx.now_ts)
    run(
        graph,
        "create_plan",
        CREATE_PLAN,
        {
            "id": disruption_id,
            "user": username,
            "plan": plan_id,
            "fp": fp,
            "cost": total,
            "now": ctx.now_ts,
            "cancel": cancel_flights,
            "moves": json.dumps(gate_moves),
            "note": note,
            "assignments": resolved,
        },
    )
    log(
        graph, disruption_id, "plan_submitted", username, f"{plan_id}: INR {total:,}, fingerprint {fp[:12]}", ctx.now_ts
    )
    return {
        "plan_id": plan_id,
        "fingerprint": fp,
        "total_cost_inr": total,
        "assignments": resolved,
        "cancel_flights": cancel_flights,
        "gate_moves": gate_moves,
        "status": "SUBMITTED",
    }


def get_plan(graph: Graph, plan_id: str) -> dict[str, Any]:
    rows = run(graph, "get_plan", GET_PLAN, {"plan": plan_id}, read_only=True)
    if not rows:
        raise UnknownEntity("plan", plan_id)
    r = rows[0]
    plan = r["plan"]
    plan["gate_moves"] = json.loads(plan.get("gate_moves") or "[]")
    return plan | {"disruption": r["disruption"], "submitted_by": r["submitted_by"], "assignments": r["assignments"]}


def approve(graph: Graph, plan_id: str, username: str, now_ts: int | None = None) -> dict[str, Any]:
    """Approve a plan: re-check legality and the fingerprint, then commit."""
    ctx = Context.open(graph, now_ts)
    who = user(graph, username)
    if "approver" not in who["roles"]:
        raise NotAllowed(f"{username} is not an approver")
    plan = get_plan(graph, plan_id)
    if plan["submitted_by"] == username:
        raise NotAllowed("four eyes: the submitter cannot approve their own plan")
    if plan["status"] != "SUBMITTED":
        raise PlanError(f"{plan_id} is {plan['status']}")

    fp = _fingerprint(
        graph,
        plan["disruption"],
        plan["assignments"],
        plan.get("cancel_flights") or [],
        plan["gate_moves"],
        plan["submitted_by"],
        plan["submitted_ts"],
    )
    if fp != plan["fingerprint"]:
        log(graph, plan["disruption"], "tampered", username, f"{plan_id} changed since submission", ctx.now_ts)
        raise Tampered(f"TAMPERED: {plan_id} or the roster it was made against changed since submission")
    for a in plan["assignments"]:
        check = assess(graph, a["crew"], a["pairing"], ctx=ctx)
        if not check.legal:
            raise PlanError(f"{a['crew']} is no longer legal for {a['pairing']}: {check.blocking[0].detail}")

    seal = hashlib.sha256(f"{fp}|{username}|{ctx.now_ts}".encode()).hexdigest()
    rows = run(
        graph,
        "commit_plan",
        COMMIT_PLAN,
        {
            "plan": plan_id,
            "fp": fp,
            "user": username,
            "now": ctx.now_ts,
            "seal": seal,
            "cancel": plan.get("cancel_flights") or [],
            "moves": plan["gate_moves"],
            "swaps": [
                {"pairing": a["pairing"], "crew": a["crew"], "vacated": a["vacated"], "role": a["role"]}
                for a in plan["assignments"]
            ],
        },
    )
    if not rows or rows[0]["status"] != "COMMITTED":
        raise Tampered(f"{plan_id} changed while it was being approved")
    if plan["assignments"]:
        run(graph, "learn_playbook", LEARN_PLAYBOOK, {"plan": plan_id})
    log(graph, plan["disruption"], "plan_committed", username, f"{plan_id} approved, seal {seal[:12]}", ctx.now_ts)
    return {"plan_id": plan_id, "status": "COMMITTED", "seal": seal, "fingerprint": fp}


def reject(graph: Graph, plan_id: str, username: str, reason: str, now_ts: int | None = None) -> dict[str, Any]:
    ctx = Context.open(graph, now_ts)
    who = user(graph, username)
    if "approver" not in who["roles"]:
        raise NotAllowed(f"{username} is not an approver")
    plan = get_plan(graph, plan_id)
    rows = run(
        graph, "reject_plan", REJECT_PLAN, {"plan": plan_id, "user": username, "now": ctx.now_ts, "reason": reason}
    )
    if not rows:
        raise PlanError(f"{plan_id} is not awaiting approval")
    log(graph, plan["disruption"], "plan_rejected", username, f"{plan_id}: {reason}", ctx.now_ts)
    return {"plan_id": plan_id, "status": "REJECTED"}


def close(graph: Graph, disruption_id: str, username: str, reason: str, now_ts: int | None = None) -> dict[str, Any]:
    """Close a disruption that needs no plan (a delay crew can absorb, a cancelled trip)."""
    ctx = Context.open(graph, now_ts)
    who = user(graph, username)
    if "controller" not in who["roles"]:
        raise NotAllowed(f"{username} is not a controller")
    rows = run(graph, "close_disruption", CLOSE_DISRUPTION, {"id": disruption_id, "now": ctx.now_ts, "reason": reason})
    if not rows:
        raise PlanError(f"{disruption_id} is already closed or resolved")
    log(graph, disruption_id, "closed", username, reason, ctx.now_ts)
    return {"disruption": disruption_id, "status": "CLOSED"}


def pending_for(graph: Graph, username: str) -> list[dict[str, Any]]:
    return run(graph, "pending_plans", PENDING_FOR_APPROVER, {"user": username}, read_only=True)
