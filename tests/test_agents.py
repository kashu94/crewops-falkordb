"""Agents coordinating through the graph."""

import threading

from crewops.agents import coordinator
from crewops.agents.triage import NotifierAgent, TriageAgent
from crewops.disruptions import get, report
from crewops.graph import run
from crewops.plans import submit

SICK = ["C-1042", "C-3231", "C-1938", "C-3940", "C-5417", "C-2087"]


def _report_all(g):
    return [report(g, "crew_unavailable", {"crew_id": c, "from_date": "2026-10-14"}, "imran.sheikh") for c in SICK]


def test_parallel_triage_agents_never_claim_the_same_disruption(fresh):
    g = fresh.ops
    ids = _report_all(g)
    agents = [TriageAgent(g, f"triage-{i}") for i in range(4)]
    threads = [threading.Thread(target=a.drain) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    handled = [e["disruption"] for a in agents for e in a.log]
    assert sorted(handled) == sorted(ids)
    claims = run(g, "claims", "MATCH (:Agent)-[c:CLAIMED]->(d:Disruption) RETURN d.id AS d, count(c) AS n")
    assert all(r["n"] == 1 for r in claims) and len(claims) == len(ids)
    assert {get(g, d)["status"] for d in ids} == {"OPTIONS_READY"}


def test_handoff_reaches_the_controller_inbox_and_plan_reaches_approvers(fresh):
    g = fresh.ops
    d = report(
        g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"}, "imran.sheikh"
    )
    TriageAgent(g).drain()
    box = coordinator.inbox(g, "ananya.iyer")["items"]["desk"]
    assert box[0]["disruption"] == d and box[0]["new_handoffs"] == 1
    assert coordinator.ack_handoffs(g, "ananya.iyer", 1) == 1
    plan = submit(g, d, "ananya.iyer")
    approvals = coordinator.inbox(g, "meera.bhatt")["items"]["approvals"]
    assert approvals[0]["plan"] == plan["plan_id"]
    assert coordinator.inbox(g, "imran.sheikh")["items"]["my_reports"][0]["status"] == "PLAN_SUBMITTED"


def test_notifier_delivers_what_triage_wrote(fresh):
    g = fresh.ops
    report(g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13"}, "imran.sheikh")
    TriageAgent(g).drain()
    sent = NotifierAgent(g).step()
    assert {s["kind"] for s in sent} >= {"Contact", "Crew"}
    assert NotifierAgent(g).step() == []
    crew_box = coordinator.inbox(g, "c3310.reddy")["items"]["crew"]
    assert crew_box["crew_id"] == "C-3310"


def test_a_failing_analysis_is_retried_then_parked(fresh, monkeypatch):
    g = fresh.ops
    d = report(g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13"}, "imran.sheikh")

    def boom(*args, **kwargs):
        raise RuntimeError("model of the world exploded")

    monkeypatch.setattr("crewops.agents.triage.analyse", boom)
    agent = TriageAgent(g)
    statuses = [agent.step()["status"] for _ in range(3)]
    assert statuses == ["REPORTED", "REPORTED", "PARKED"]
    assert agent.step() is None
    assert get(g, d)["status"] == "PARKED"
