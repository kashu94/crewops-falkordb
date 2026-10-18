"""Web console API.

Loads the seed if the graph is empty and runs the triage and notifier agents in a background
thread (disable with CREWOPS_AUTO_TRIAGE=0). Sign-in is a demo user picker, not real
authentication; actions are still role-checked."""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crewops import algorithms, disruptions, explain, plans
from crewops.agents import coordinator
from crewops.agents import tools as registry
from crewops.agents.assistant import Assistant
from crewops.agents.tools import Caller
from crewops.agents.triage import NotifierAgent, TriageAgent
from crewops.graph import graphs, run
from crewops.legality import UnknownEntity
from crewops.memory import Memory
from crewops.seed import load, summarize
from crewops.timeutil import iso

WEB = Path(__file__).parent / "web"
COOKIE = "crewops_user"
_stop = threading.Event()


def _agents_loop() -> None:
    gs = graphs()
    triage, notifier = TriageAgent(gs.ops, "triage-web"), NotifierAgent(gs.ops, "notifier-web")
    while not _stop.is_set():
        try:
            busy = triage.step() is not None
            notifier.step()
        except Exception:  # keep the loop alive; the claim is released inside step()
            busy = False
        _stop.wait(0.2 if busy else 2.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    gs = graphs()
    loaded = run(gs.ops, "is_loaded", "OPTIONAL MATCH (m:Meta) RETURN count(m) AS n")[0]["n"]
    if not loaded:
        load(gs.ops)
    worker = None
    if os.environ.get("CREWOPS_AUTO_TRIAGE", "1") != "0":
        worker = threading.Thread(target=_agents_loop, daemon=True)
        worker.start()
    yield
    _stop.set()


app = FastAPI(title="CrewOps on FalkorDB", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")

_assistants: dict[tuple[str, str], Assistant] = {}


def signed_in(request: Request) -> str | None:
    """The user picked in the console. Sent as a header too, because browsers block the
    cookie when the console is embedded in another site."""
    return request.headers.get("x-crewops-user") or request.cookies.get(COOKIE)


def _caller(username: str | None) -> Caller:
    if not username:
        raise HTTPException(401, "pick a team member first")
    try:
        return Caller.load(graphs(), username)
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (UnknownEntity, LookupError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    gs = graphs()
    from crewops.legality import Context

    return {
        "falkordb": gs.ping(),
        "graph": gs.ops_name,
        "nodes": summarize(gs.ops),
        "clock": iso(Context.open(gs.ops).now_ts),
    }


TEAM = "MATCH (u:User) OPTIONAL MATCH (u)-[:STAFFS]->(d:Desk) RETURN u.username AS username, u.name AS name, u.roles AS roles, d.id AS desk ORDER BY username"


@app.get("/api/team")
def team() -> list[dict[str, Any]]:
    return [u for u in run(graphs().ops, "team", TEAM, read_only=True) if "speaker" not in u["roles"]]


class Login(BaseModel):
    username: str


@app.post("/api/login")
def login(body: Login, response: Response) -> dict[str, Any]:
    caller = _caller(body.username)
    response.set_cookie(COOKIE, caller.username, httponly=True, samesite="lax")
    return {"username": caller.username, "name": caller.name, "roles": sorted(caller.roles)}


@app.get("/api/me")
def me(crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    gs = graphs()
    return {
        "username": caller.username,
        "name": caller.name,
        "roles": sorted(caller.roles),
        "desk": caller.desk,
        "crew_id": caller.crew_id,
        "inbox": coordinator.inbox(gs.ops, caller.username),
        "tools": [t.name for t in registry.available(caller)],
    }


LIST = """
MATCH (d:Disruption)
OPTIONAL MATCH (d)-[:ASSIGNED_TO]->(k:Desk)
OPTIONAL MATCH (d)-[:REPORTED_BY]->(u:User)
RETURN d.id AS id, d.type AS type, d.status AS status, d.summary AS summary, k.id AS desk,
       u.name AS reported_by, d.reported_ts AS reported_ts
ORDER BY id DESC LIMIT 50
"""


@app.get("/api/disruptions")
def list_disruptions(crewops_user: str | None = Depends(signed_in)) -> list[dict[str, Any]]:
    _caller(crewops_user)
    rows = run(graphs().ops, "list_disruptions_web", LIST, read_only=True)
    for r in rows:
        r["reported_at"] = iso(r["reported_ts"])
    return rows


@app.get("/api/disruptions/{disruption_id}")
def disruption(disruption_id: str, crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    _caller(crewops_user)
    gs = graphs()
    d = _guard(disruptions.get, gs.ops, disruption_id)
    trail = _guard(explain.decision_trail, gs.ops, disruption_id)
    return d | {"trail": trail}


@app.get("/api/disruptions/{disruption_id}/graph")
def disruption_graph(disruption_id: str, crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    _caller(crewops_user)
    return explain.subgraph(graphs().ops, disruption_id)


@app.get("/api/disruptions/{disruption_id}/why-not/{crew_id}")
def disruption_why_not(
    disruption_id: str, crew_id: str, crewops_user: str | None = Depends(signed_in)
) -> dict[str, Any]:
    _caller(crewops_user)
    return explain.why_not(graphs().ops, disruption_id, crew_id)


class NewReport(BaseModel):
    kind: str
    details: dict[str, Any]


@app.post("/api/disruptions")
def new_report(body: NewReport, crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    return {"id": _guard(disruptions.report, graphs().ops, body.kind, body.details, caller.username)}


@app.post("/api/triage")
def triage_now(crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    _caller(crewops_user)
    gs = graphs()
    handled = TriageAgent(gs.ops, "triage-manual").drain()
    sent = NotifierAgent(gs.ops, "notifier-manual").step()
    return {"handled": handled, "notified": len(sent)}


class PlanBody(BaseModel):
    note: str | None = None
    assignments: list[dict[str, str]] | None = None
    cancel_flights: list[str] | None = None
    cancel_pairings: list[str] | None = None
    short_gaps: list[str] | None = None


@app.post("/api/disruptions/{disruption_id}/plan")
def submit_plan(disruption_id: str, body: PlanBody, crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    return _guard(
        plans.submit,
        graphs().ops,
        disruption_id,
        caller.username,
        assignments=body.assignments,
        cancel_flights=body.cancel_flights,
        note=body.note or "",
        cancel_pairings=body.cancel_pairings,
        short_gaps=body.short_gaps,
    )


@app.post("/api/disruptions/{disruption_id}/close")
def close_disruption(
    disruption_id: str, body: PlanBody, crewops_user: str | None = Depends(signed_in)
) -> dict[str, Any]:
    caller = _caller(crewops_user)
    return _guard(plans.close, graphs().ops, disruption_id, caller.username, body.note or "closed from the console")


@app.post("/api/plans/{plan_id}/approve")
def approve_plan(plan_id: str, crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    return _guard(plans.approve, graphs().ops, plan_id, caller.username)


@app.post("/api/plans/{plan_id}/reject")
def reject_plan(plan_id: str, body: PlanBody, crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    return _guard(plans.reject, graphs().ops, plan_id, caller.username, body.note or "rejected")


class Question(BaseModel):
    question: str
    session_id: str | None = None


@app.post("/api/ask")
def ask(body: Question, crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    key = (caller.username, body.session_id or "")
    bot = _assistants.get(key)
    if bot is None:
        bot = Assistant(graphs(), caller.username, session_id=body.session_id)
        _assistants[(caller.username, bot.session_id)] = bot
    return bot.ask(body.question).as_dict()


@app.get("/api/memory")
def memory(crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    gs = graphs()
    m = Memory(gs, caller.username)
    return {
        "graph": m.graph.name,
        "stats": m.stats(),
        "facts": m.facts(),
        "playbook": m.playbook(),
        "all_memory_graphs": gs.memory_graphs(),
    }


@app.get("/api/insights")
def insights(crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    _caller(crewops_user)
    g = graphs().ops
    return {
        "stations": algorithms.station_criticality(g),
        "rule_pressure": explain.rule_pressure(g),
        "agents": coordinator.activity(g),
    }


SUMMARY = """
OPTIONAL MATCH (d:Disruption)
WITH collect(d) AS all
RETURN size(all) AS total,
       size([x IN all WHERE x.status IN $open]) AS open,
       size([x IN all WHERE x.status = 'OPTIONS_READY']) AS needs_plan,
       size([x IN all WHERE x.status = 'PLAN_SUBMITTED']) AS awaiting_approval,
       size([x IN all WHERE x.status IN ['RESOLVED', 'CLOSED']]) AS resolved
"""

PASSENGERS = """
MATCH (d:Disruption)-[:HAS_GAP]->(g:Gap)
WHERE d.status IN $open
RETURN coalesce(sum(g.passengers), 0) AS passengers
"""


@app.get("/api/summary")
def summary(crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    _caller(crewops_user)
    g = graphs().ops
    from crewops.legality import OPEN_STATUSES, Context

    counts = run(g, "summary", SUMMARY, {"open": OPEN_STATUSES}, read_only=True)[0]
    passengers = run(g, "open_passengers", PASSENGERS, {"open": OPEN_STATUSES}, read_only=True)[0]["passengers"]
    return counts | {"passengers": passengers, "clock": iso(Context.open(g).now_ts)}


@app.post("/api/tools/{name}")
def call_tool(name: str, body: dict[str, Any], crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    caller = _caller(crewops_user)
    return registry.call(graphs(), caller, name, body)


FORM_OPTIONS = """
MATCH (c:Crew)-[:EMPLOYED_BY]->(al:Airline), (c)-[:BASED_AT]->(b:Station)
WITH c, al, b ORDER BY c.id
WITH collect({id: c.id, name: c.name, rank: c.rank, airline: al.code, base: b.code}) AS crew
MATCH (f:Flight)-[:FROM]->(o:Station), (f)-[:TO]->(t:Station)
WITH crew, f, o, t ORDER BY f.dep_ts
WITH crew, collect({id: f.id, no: f.no, date: f.date, from: o.code, to: t.code, dep_ts: f.dep_ts}) AS flights
MATCH (g:Gate)-[:AT]->(s:Station)
WITH crew, flights, g, s ORDER BY s.code, g.id
WITH crew, flights, collect({id: g.id, station: s.code, emergency: g.emergency}) AS gates
MATCH (a:Aircraft)-[:OPERATED_BY]->(al:Airline)
WITH crew, flights, gates, a, al ORDER BY a.tail
RETURN crew, flights, gates, collect({tail: a.tail, type: a.type, airline: al.code}) AS aircraft
"""


@app.get("/api/form-options")
def form_options(crewops_user: str | None = Depends(signed_in)) -> dict[str, Any]:
    """Choices for the report form: crew, flights, gates, aircraft, stations and the week."""
    _caller(crewops_user)
    from crewops.timeutil import hhmm

    g = graphs().ops
    row = run(g, "form_options", FORM_OPTIONS, read_only=True)[0]
    for f in row["flights"]:
        f["departs"] = hhmm(f.pop("dep_ts"))
    dates = sorted({f["date"] for f in row["flights"]})
    stations = sorted({f["from"] for f in row["flights"]} | {f["to"] for f in row["flights"]})
    return row | {"stations": stations, "first_date": dates[0], "last_date": dates[-1]}


BOARD = """
MATCH (f:Flight)-[:FROM]->(o:Station), (f)-[:TO]->(t:Station), (f)-[:ON]->(:Aircraft)-[:OPERATED_BY]->(al:Airline)
WHERE f.dep_ts >= $now
OPTIONAL MATCH (f)-[:BOARDS_AT]->(g:Gate)
RETURN f.no AS flight, al.name AS airline, al.color AS color, o.code AS origin, t.code AS dest,
       f.dep_ts AS dep_ts, f.status AS status, coalesce(f.delay_minutes, 0) AS delay_minutes, g.id AS gate
ORDER BY dep_ts
LIMIT $limit
"""


@app.get("/api/board")
def board(limit: int = 8) -> list[dict[str, Any]]:
    """Next departures, for the sign-in page. Fictional data, no sign-in needed."""
    from crewops.legality import Context
    from crewops.timeutil import hhmm

    g = graphs().ops
    rows = run(g, "departures_board", BOARD, {"now": Context.open(g).now_ts, "limit": min(limit, 20)}, read_only=True)
    for r in rows:
        r["departs"] = hhmm(r.pop("dep_ts"))
    return rows
