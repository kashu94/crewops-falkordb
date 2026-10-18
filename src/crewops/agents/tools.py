"""Tool registry shared by the assistant and the MCP server.

``call`` checks the caller's role, runs the tool and returns the result together with the
Cypher queries it ran. Write tools apply the same rules as the console."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from crewops import algorithms, disruptions, explain, impact, legality, options, plans
from crewops.agents import coordinator
from crewops.disruptions import NotAllowed, user
from crewops.graph import Graphs, run, tracing
from crewops.legality import Context
from crewops.memory import Memory
from crewops.timeutil import day_of

DESK_ROLES = {"controller", "approver", "reporter", "auditor", "admin", "super_admin"}


@dataclass(frozen=True, slots=True)
class Caller:
    username: str
    name: str
    roles: frozenset[str]
    desk: str | None = None
    crew_id: str | None = None

    @classmethod
    def load(cls, gs: Graphs, username: str) -> Caller:
        u = user(gs.ops, username)
        return cls(u["username"], u["name"], frozenset(u["roles"]), u.get("desk"), u.get("crew_id"))


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    type: type
    description: str
    required: bool = True
    enum: tuple[str, ...] | None = None

    def schema(self) -> dict[str, Any]:
        json_type = {str: "string", int: "integer", float: "number", bool: "boolean"}[self.type]
        s: dict[str, Any] = {
            "type": json_type if self.required else [json_type, "null"],
            "description": self.description,
        }
        if self.enum:
            s["enum"] = list(self.enum) + ([] if self.required else [None])
        return s


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    params: tuple[Param, ...]
    handler: Callable[..., dict[str, Any]]
    roles: frozenset[str] = field(default_factory=lambda: frozenset(DESK_ROLES))
    writes: bool = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {p.name: p.schema() for p in self.params},
            "required": [p.name for p in self.params],
            "additionalProperties": False,
        }


def _now(gs: Graphs) -> int:
    return Context.open(gs.ops).now_ts


def _date(value: str | None, gs: Graphs) -> str:
    if not value or value.lower() == "today":
        return day_of(_now(gs))
    if value.lower() == "tomorrow":
        return day_of(_now(gs) + 86400)
    return date.fromisoformat(value).isoformat()


# --- handlers -------------------------------------------------------------------------------


def _find_cover(gs, caller, pairing_id, role, delay_hours=None):
    result = options.find_options(gs.ops, pairing_id.upper(), role, delay_hours or 0.0)
    return result.as_dict(limit=8) | {"excluded": [e.as_dict() for e in result.excluded[:12]]}


def _check_legality(gs, caller, crew_id, pairing_id, delay_hours=None):
    return legality.assess(gs.ops, crew_id.upper(), pairing_id.upper(), delay_hours or 0.0).as_dict()


def _blast_radius(gs, caller, crew_id, from_date=None, to_date=None):
    start = _date(from_date, gs)
    end = _date(to_date, gs) if to_date else start
    seats = impact.crew_blast_radius(gs.ops, crew_id.upper(), start, end, _now(gs))
    return {"crew_id": crew_id.upper(), "from": start, "to": end, "seats": [vars_(s) for s in seats]}


def vars_(obj: Any) -> dict[str, Any]:
    return {k: getattr(obj, k) for k in obj.__slots__}


def _delay_impact(gs, caller, delay_hours, flight_id=None, aircraft=None, flight_date=None):
    effect = impact.delay_ripple(
        gs.ops,
        delay_hours,
        flight_id.upper() if flight_id else None,
        aircraft.upper() if aircraft else None,
        _date(flight_date, gs) if aircraft else None,
    )
    return effect.as_dict()


def _disruption(gs, caller, disruption_id):
    d = disruptions.get(gs.ops, disruption_id.upper())
    analysis = d.get("analysis") or {}
    return {
        "id": d["id"],
        "type": d["type"],
        "status": d["status"],
        "summary": d["summary"],
        "desk": d.get("desk"),
        "reported_by": d["reporter"],
        "headline": analysis.get("headline"),
        "gaps": [
            {
                k: g[k]
                for k in ("gap", "pairing_id", "role", "vacated_by", "passengers", "recommended", "excluded_count")
            }
            for g in analysis.get("gaps", [])
        ],
        "joint_plan": analysis.get("joint_plan"),
        "contention": analysis.get("contention"),
    }


LIST_DISRUPTIONS = """
MATCH (d:Disruption)
WHERE $status IS NULL OR d.status = $status
OPTIONAL MATCH (d)-[:ASSIGNED_TO]->(k:Desk)
RETURN d.id AS id, d.type AS type, d.status AS status, d.summary AS summary, k.id AS desk
ORDER BY id DESC LIMIT 25
"""


def _list_disruptions(gs, caller, status=None):
    return {"disruptions": run(gs.ops, "list_disruptions", LIST_DISRUPTIONS, {"status": status}, read_only=True)}


def _explain(gs, caller, disruption_id):
    return explain.decision_trail(gs.ops, disruption_id.upper())


def _why_not(gs, caller, disruption_id, crew_id):
    return explain.why_not(gs.ops, disruption_id.upper(), crew_id)


NOTIFIED = """
MATCH (d:Disruption {id: $id})-[n:NOTIFIES]->(t)
RETURN labels(t)[0] AS kind, coalesce(t.name, t.id) AS who, t.title AS title, t.team AS team,
       n.reason AS reason, n.sent_ts IS NOT NULL AS sent
ORDER BY kind, who
"""


def _who_to_tell(gs, caller, disruption_id):
    return {"recipients": run(gs.ops, "notified", NOTIFIED, {"id": disruption_id.upper()}, read_only=True)}


CREW_PROFILE = """
MATCH (c:Crew {id: $id})-[:BASED_AT]->(b:Station)
MATCH (c)-[:EMPLOYED_BY]->(al:Airline)
OPTIONAL MATCH (c)-[:RATED_ON]->(t:AircraftType)
WITH c, b, al, collect(t.name) AS ratings
OPTIONAL MATCH (c)-[a:ASSIGNED]->(p:Pairing)-[:HAS_DAY]->(d:DutyDay)
WITH c, b, al, ratings, d, p, a ORDER BY d.date
WITH c, b, al, ratings, collect(CASE WHEN d IS NULL THEN NULL ELSE {date: d.date, pairing: p.id, role: a.role} END) AS duties
OPTIONAL MATCH (c)-[oc:ON_CALL]->(:Station)
WITH c, b, al, ratings, duties, collect(oc.date) AS on_call
OPTIONAL MATCH (dis:Disruption)-[g:GROUNDS]->(c) WHERE dis.status IN $open
RETURN c.id AS crew_id, c.name AS name, c.rank AS rank, c.status AS status, b.code AS base,
       al.name AS airline, ratings, c.seniority AS seniority, c.experience_years AS experience_years,
       c.command_qualified AS command_qualified, c.right_seat_qualified AS right_seat_qualified,
       duties, on_call, collect(DISTINCT dis.id) AS grounded_by
"""

CREW_BY_NAME = """
CALL db.idx.fulltext.queryNodes('Crew', $q) YIELD node
RETURN node.id AS crew_id, node.name AS name, node.rank AS rank LIMIT 5
"""


def _crew_profile(gs, caller, crew):
    crew = crew.strip()
    if caller.roles == {"crew"} and crew.upper() != caller.crew_id:
        raise NotAllowed("crew may only look up their own profile")
    ids = [crew.upper()] if crew[:2].upper() == "C-" else []
    if not ids:
        matches = run(gs.ops, "crew_by_name", CREW_BY_NAME, {"q": crew.replace(".", " ")}, read_only=True)
        if len(matches) != 1:
            return {"matches": matches, "note": "name more precisely or use the crew id"}
        ids = [matches[0]["crew_id"]]
    rows = run(gs.ops, "crew_profile", CREW_PROFILE, {"id": ids[0], "open": legality.OPEN_STATUSES}, read_only=True)
    if not rows:
        raise legality.UnknownEntity("crew", ids[0])
    profile = rows[0]
    profile["duties"] = [d for d in profile["duties"] if d]
    return profile


SCHEDULE_LEGS = """
MATCH (c:Crew {id: $id})-[a:ASSIGNED]->(p:Pairing)-[:HAS_DAY]->(d:DutyDay)-[l:LEG]->(f:Flight)
MATCH (f)-[:FROM]->(o:Station), (f)-[:TO]->(t:Station), (f)-[:ON]->(ac:Aircraft)
OPTIONAL MATCH (f)-[:BOARDS_AT]->(g:Gate)
RETURN d.date AS date, p.id AS pairing, a.role AS role, a.via_plan AS via_plan,
       d.report_ts AS report_ts, d.release_ts AS release_ts, l.seq AS seq,
       f.id AS flight_id, f.no AS flight_no, o.code AS origin, t.code AS dest,
       f.dep_ts AS dep_ts, f.arr_ts AS arr_ts, f.status AS status, ac.tail AS aircraft, g.id AS gate
ORDER BY dep_ts
"""

SCHEDULE_RESERVE = """
MATCH (c:Crew {id: $id})-[oc:ON_CALL]->(s:Station)
RETURN oc.date AS date, oc.start_ts AS start_ts, oc.end_ts AS end_ts, s.code AS base
ORDER BY date
"""


def _crew_schedule(gs, caller, crew):
    from crewops.timeutil import hhmm

    profile = _crew_profile(gs, caller, crew)
    if "matches" in profile:
        return profile
    cid = profile["crew_id"]
    days: dict[tuple[str, str], dict[str, Any]] = {}
    for r in run(gs.ops, "schedule_legs", SCHEDULE_LEGS, {"id": cid}, read_only=True):
        day = days.setdefault(
            (r["date"], r["pairing"]),
            {
                "date": r["date"],
                "pairing": r["pairing"],
                "role": r["role"],
                "via_plan": r["via_plan"],
                "report": hhmm(r["report_ts"]),
                "release": hhmm(r["release_ts"]),
                "duty_hours": round((r["release_ts"] - r["report_ts"]) / 3600, 2),
                "flights": [],
            },
        )
        day["flights"].append(
            {
                "flight": r["flight_no"],
                "flight_id": r["flight_id"],
                "from": r["origin"],
                "to": r["dest"],
                "departs": hhmm(r["dep_ts"]),
                "arrives": hhmm(r["arr_ts"]),
                "aircraft": r["aircraft"],
                "gate": r["gate"],
                "status": r["status"],
            }
        )
    reserve = [
        {"date": r["date"], "window": f"{hhmm(r['start_ts'])}-{hhmm(r['end_ts'])}", "base": r["base"]}
        for r in run(gs.ops, "schedule_reserve", SCHEDULE_RESERVE, {"id": cid}, read_only=True)
    ]
    keep = ("crew_id", "name", "rank", "status", "base", "airline", "ratings", "grounded_by")
    return {k: profile[k] for k in keep} | {
        "duty_days": sorted(days.values(), key=lambda d: d["date"]),
        "reserve": reserve,
    }


RESERVES = """
MATCH (c:Crew)-[oc:ON_CALL]->(s:Station)
WHERE oc.date = $date AND ($base IS NULL OR s.code = $base) AND ($rank IS NULL OR c.rank = $rank)
MATCH (c)-[:EMPLOYED_BY]->(al:Airline)
OPTIONAL MATCH (dis:Disruption)-[:GROUNDS {date: $date}]->(c) WHERE dis.status IN $open
OPTIONAL MATCH (c)-[a:ASSIGNED]->(:Pairing)-[:HAS_DAY]->(d:DutyDay {date: $date})
RETURN c.id AS crew_id, c.name AS name, c.rank AS rank, s.code AS base, al.code AS airline,
       oc.start_ts AS window_start, oc.end_ts AS window_end,
       count(dis) > 0 AS grounded, count(d) > 0 AS already_flying
ORDER BY rank, crew_id
"""


def _reserves(gs, caller, on_date=None, base=None, rank=None):
    from crewops.timeutil import hhmm

    day = _date(on_date, gs)
    rows = run(
        gs.ops,
        "reserves",
        RESERVES,
        {"date": day, "base": base.upper() if base else None, "rank": rank, "open": legality.OPEN_STATUSES},
        read_only=True,
    )
    for r in rows:
        r["window"] = f"{hhmm(r.pop('window_start'))}-{hhmm(r.pop('window_end'))}Z"
    return {"date": day, "reserves": rows}


def _report(
    gs,
    caller,
    kind,
    crew_id=None,
    from_date=None,
    to_date=None,
    reason=None,
    flight_id=None,
    aircraft=None,
    flight_date=None,
    delay_hours=None,
    station=None,
    gate=None,
    start=None,
    end=None,
):
    details = {
        k: v
        for k, v in {
            "crew_id": crew_id,
            "from_date": from_date,
            "to_date": to_date,
            "reason": reason,
            "flight_id": flight_id,
            "aircraft": aircraft,
            "date": flight_date,
            "delay_hours": delay_hours,
            "station": station,
            "gate": gate,
            "start": start,
            "end": end,
        }.items()
        if v not in (None, "")
    }
    d = disruptions.report(gs.ops, kind, details, caller.username)
    return {"disruption_id": d, "status": "REPORTED", "note": "the triage agent will analyse it"}


def _submit(gs, caller, disruption_id, note=None):
    return plans.submit(gs.ops, disruption_id.upper(), caller.username, note=note or "")


def _approve(gs, caller, plan_id):
    return plans.approve(gs.ops, plan_id.upper(), caller.username)


def _reject(gs, caller, plan_id, reason):
    return plans.reject(gs.ops, plan_id.upper(), caller.username, reason)


def _inbox(gs, caller):
    return coordinator.inbox(gs.ops, caller.username)


def _criticality(gs, caller):
    return {"stations": algorithms.station_criticality(gs.ops)}


def _routes(gs, caller, origin, dest):
    return {"routes": algorithms.positioning_routes(gs.ops, origin, dest)}


def _who_knows(gs, caller, station=None, aircraft_type=None):
    return {"crew": algorithms.who_knows(gs.ops, station, aircraft_type)}


def _knock_on(gs, caller, on_date=None):
    return {"flights": algorithms.knock_on(gs.ops, _date(on_date, gs))}


def _rule_pressure(gs, caller):
    return {"rules": explain.rule_pressure(gs.ops)}


def _playbook(gs, caller, kind=None):
    return {"playbook": Memory(gs, caller.username).playbook(kind)}


def _recall(gs, caller, ref=None):
    m = Memory(gs, caller.username)
    if ref:
        return {"ref": ref.upper(), "mentions": m.mentions_of(ref), "related": m.related(ref)}
    latest = m.latest_session()
    return {"facts": m.facts(), "stats": m.stats(), "latest_session": latest}


def _remember(gs, caller, key, value):
    Memory(gs, caller.username).remember(key, value, _now(gs))
    return {"remembered": {key: value}}


ROLES = ("Captain", "First Officer", "Senior Cabin Crew", "Cabin Crew")
D = Param("disruption_id", str, "Disruption id, e.g. D-0001")

TOOLS: tuple[Tool, ...] = (
    Tool(
        "find_cover",
        "Rank every legal crew member who could fill a seat on a pairing, cheapest first, with "
        "why everyone else is ruled out. Cancellation is always the last option.",
        (
            Param("pairing_id", str, "Pairing id, e.g. P-2291"),
            Param("role", str, "Seat to fill", enum=ROLES),
            Param("delay_hours", float, "Judge against a first departure this many hours late", required=False),
        ),
        _find_cover,
    ),
    Tool(
        "check_legality",
        "Check one crew member against all seven legality rules for one pairing.",
        (
            Param("crew_id", str, "Crew id, e.g. C-3310"),
            Param("pairing_id", str, "Pairing id"),
            Param("delay_hours", float, "What-if delay on day one", required=False),
        ),
        _check_legality,
    ),
    Tool(
        "blast_radius",
        "Seats a crew member leaves empty between two dates: pairings, flights, passengers.",
        (
            Param("crew_id", str, "Crew id"),
            Param("from_date", str, "YYYY-MM-DD, 'today' or 'tomorrow'", required=False),
            Param("to_date", str, "YYYY-MM-DD; defaults to from_date", required=False),
        ),
        _blast_radius,
    ),
    Tool(
        "delay_impact",
        "Follow a delay down the aircraft's rotation and into each crew duty day: who breaks "
        "their duty limit and which legs would have to come off.",
        (
            Param("delay_hours", float, "Hours late"),
            Param("flight_id", str, "First late flight id", required=False),
            Param("aircraft", str, "Tail, e.g. VT-DXA, when the flight is not named", required=False),
            Param("flight_date", str, "Date of the aircraft's day, with aircraft", required=False),
        ),
        _delay_impact,
    ),
    Tool(
        "get_disruption",
        "A disruption's status, headline, empty seats, recommendations and joint plan.",
        (D,),
        _disruption,
    ),
    Tool(
        "list_disruptions",
        "Recent disruptions, optionally only those with one status.",
        (
            Param(
                "status",
                str,
                "Filter",
                required=False,
                enum=("REPORTED", "ANALYSING", "OPTIONS_READY", "PLAN_SUBMITTED", "RESOLVED", "CLOSED", "PARKED"),
            ),
        ),
        _list_disruptions,
    ),
    Tool(
        "explain_decision",
        "The decision trail for a disruption: reporter, desk, options, plan, who submitted "
        "and approved it, fingerprint, seal and event log.",
        (D,),
        _explain,
    ),
    Tool(
        "why_not",
        "Why a crew member was not chosen for a disruption: the rules they breach, or who ranked above them.",
        (D, Param("crew_id", str, "Crew id")),
        _why_not,
    ),
    Tool(
        "who_to_tell",
        "Everyone the disruption notifies: station and network contacts, crew on the trip.",
        (D,),
        _who_to_tell,
    ),
    Tool(
        "crew_schedule",
        "A crew member's schedule: each duty day with report and release times and every flight "
        "(route, times in UTC, aircraft, gate), plus reserve days. Accepts an id or a name.",
        (Param("crew", str, "Crew id or name"),),
        _crew_schedule,
        roles=frozenset(DESK_ROLES | {"crew"}),
    ),
    Tool(
        "crew_profile",
        "A crew member's rank, base, ratings, rostered duties, reserve days and groundings. Accepts an id or a name.",
        (Param("crew", str, "Crew id or name"),),
        _crew_profile,
        roles=frozenset(DESK_ROLES | {"crew"}),
    ),
    Tool(
        "reserves",
        "Who is on call on a date, optionally at one base and rank, and whether they are free.",
        (
            Param("on_date", str, "YYYY-MM-DD, 'today' or 'tomorrow'", required=False),
            Param("base", str, "Station code", required=False),
            Param("rank", str, "Rank", required=False, enum=ROLES),
        ),
        _reserves,
    ),
    Tool(
        "my_inbox",
        "What is waiting for the signed-in user: desk work, approvals, their reports, crew notices.",
        (),
        _inbox,
        roles=frozenset(DESK_ROLES | {"crew"}),
    ),
    Tool(
        "station_criticality",
        "PageRank and betweenness over the route network: which station's loss hurts most.",
        (),
        _criticality,
    ),
    Tool(
        "positioning_routes",
        "Shortest routes between two stations by block hours, for positioning crew.",
        (Param("origin", str, "From station"), Param("dest", str, "To station")),
        _routes,
    ),
    Tool(
        "who_knows",
        "Crew who know a station (legs flown into it) or an aircraft type (rated, most experienced).",
        (
            Param("station", str, "Station code", required=False),
            Param("aircraft_type", str, "A320 or ATR72", required=False),
        ),
        _who_knows,
    ),
    Tool(
        "knock_on",
        "Flights whose delay would carry to the most later legs of the same day.",
        (Param("on_date", str, "YYYY-MM-DD", required=False),),
        _knock_on,
    ),
    Tool("rule_pressure", "Which legality rules rule out the most candidates across disruptions.", (), _rule_pressure),
    Tool(
        "playbook",
        "Shared procedural memory: the strategies approvers chose before, by disruption type and seat.",
        (Param("kind", str, "Disruption type", required=False),),
        _playbook,
    ),
    Tool(
        "recall",
        "The user's own memory: past questions about an id, or their stored preferences.",
        (Param("ref", str, "An id to recall, e.g. C-3310", required=False),),
        _recall,
        roles=frozenset(DESK_ROLES | {"crew"}),
    ),
    Tool(
        "remember_preference",
        "Store a preference in the user's memory, e.g. ranking_policy = least_delay.",
        (Param("key", str, "Preference name"), Param("value", str, "Value")),
        _remember,
        roles=frozenset(DESK_ROLES | {"crew"}),
    ),
    Tool(
        "report_disruption",
        "Record a disruption for the triage agent to analyse.",
        (
            Param("kind", str, "Type", enum=disruptions.KINDS),
            Param("crew_id", str, "Crew unavailable", required=False),
            Param("from_date", str, "First date", required=False),
            Param("to_date", str, "Last date", required=False),
            Param("reason", str, "Why", required=False),
            Param("flight_id", str, "Late flight", required=False),
            Param("aircraft", str, "Late aircraft", required=False),
            Param("flight_date", str, "Aircraft's date", required=False),
            Param("delay_hours", float, "Hours late", required=False),
            Param("station", str, "Closed station", required=False),
            Param("gate", str, "Closed gate", required=False),
            Param("start", str, "Closure start, ISO UTC", required=False),
            Param("end", str, "Closure end, ISO UTC", required=False),
        ),
        _report,
        roles=frozenset({"reporter", "controller", "crew"}),
        writes=True,
    ),
    Tool(
        "submit_plan",
        "Submit the analysed recommendation for a disruption on the caller's desk for approval.",
        (D, Param("note", str, "Note for the approver", required=False)),
        _submit,
        roles=frozenset({"controller"}),
        writes=True,
    ),
    Tool(
        "approve_plan",
        "Approve a submitted plan: re-checks legality and the fingerprint, then commits the roster. "
        "Never the submitter.",
        (Param("plan_id", str, "Plan id, e.g. PL-0001"),),
        _approve,
        roles=frozenset({"approver"}),
        writes=True,
    ),
    Tool(
        "reject_plan",
        "Reject a submitted plan with a reason.",
        (Param("plan_id", str, "Plan id"), Param("reason", str, "Why")),
        _reject,
        roles=frozenset({"approver"}),
        writes=True,
    ),
)

BY_NAME = {t.name: t for t in TOOLS}


def available(caller: Caller, include_writes: bool = True) -> list[Tool]:
    return [t for t in TOOLS if caller.roles & t.roles and (include_writes or not t.writes)]


def call(gs: Graphs, caller: Caller, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool as ``caller``. Errors come back as data so an agent can recover."""
    tool = BY_NAME.get(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    if not caller.roles & tool.roles:
        return {"ok": False, "error": f"{caller.username} ({', '.join(sorted(caller.roles))}) may not use {name}"}
    known = {p.name for p in tool.params}
    clean = {k: v for k, v in args.items() if k in known and v is not None}
    missing = [p.name for p in tool.params if p.required and p.name not in clean]
    if missing:
        return {"ok": False, "error": f"missing {', '.join(missing)}"}
    with tracing() as trace:
        try:
            result = tool.handler(gs, caller, **clean)
            ok = True
        except (LookupError, ValueError, PermissionError) as exc:
            result, ok = {"error": str(exc)}, False
    return {"ok": ok, "tool": name, "result": result, "evidence": trace.summary()}


def mcp_function(gs: Graphs, caller_of: Callable[[], Caller], tool: Tool) -> Callable[..., dict[str, Any]]:
    """A plain function with a real signature, so an MCP server can derive the tool schema."""

    def fn(**kwargs: Any) -> dict[str, Any]:
        return call(gs, caller_of(), tool.name, kwargs)

    parameters = []
    for p in tool.params:
        base = Literal[p.enum] if p.enum else p.type  # type: ignore[valid-type]
        annotation = Annotated[base if p.required else (base | None), Field(description=p.description)]
        parameters.append(
            inspect.Parameter(
                p.name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=inspect.Parameter.empty if p.required else None,
            )
        )
    fn.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    fn.__annotations__ = {p.name: p.annotation for p in parameters} | {"return": dict[str, Any]}
    fn.__name__ = tool.name
    fn.__doc__ = tool.description
    return fn
