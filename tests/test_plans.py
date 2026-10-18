import pytest

from crewops.disruptions import NotAllowed, analyse, get, report
from crewops.graph import run
from crewops.plans import PlanError, Tampered, approve, pending_for, reject, submit


def _sick_captain(g):
    d = report(
        g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"}, "imran.sheikh"
    )
    analyse(g, d)
    return d


def _captain_of(g, pairing):
    return run(
        g,
        "captain",
        "MATCH (c:Crew)-[a:ASSIGNED {role: 'Captain'}]->(:Pairing {id: $p}) RETURN c.id AS c, a.via_plan AS plan",
        {"p": pairing},
    )


def test_four_eyes_commit_swaps_the_roster_in_one_write(fresh):
    g = fresh.ops
    d = _sick_captain(g)
    plan = submit(g, d, "ananya.iyer")
    assert get(g, d)["status"] == "PLAN_SUBMITTED"
    assert pending_for(g, "meera.bhatt")[0]["plan"] == plan["plan_id"]
    result = approve(g, plan["plan_id"], "meera.bhatt")
    assert result["status"] == "COMMITTED"
    assert _captain_of(g, "P-2291") == [{"c": "C-3310", "plan": plan["plan_id"]}]
    assert get(g, d)["status"] == "RESOLVED"


def test_only_the_owning_desk_submits_and_only_approvers_approve(fresh):
    g = fresh.ops
    d = _sick_captain(g)  # goes to desk-1 (Ananya)
    with pytest.raises(NotAllowed):
        submit(g, d, "rohit.malhotra")
    plan = submit(g, d, "ananya.iyer")
    with pytest.raises(NotAllowed):
        approve(g, plan["plan_id"], "ananya.iyer")


def test_the_submitter_can_never_approve(fresh):
    g = fresh.ops
    run(g, "dual_role", "MATCH (u:User {username: 'ananya.iyer'}) SET u.roles = ['controller', 'approver']")
    d = _sick_captain(g)
    plan = submit(g, d, "ananya.iyer")
    with pytest.raises(NotAllowed, match="four eyes"):
        approve(g, plan["plan_id"], "ananya.iyer")


def test_a_roster_change_after_submission_is_tampering(fresh):
    g = fresh.ops
    d = _sick_captain(g)
    plan = submit(g, d, "ananya.iyer")
    run(
        g,
        "sneak_change",
        "MATCH (c:Crew {id: 'C-2087'}), (p:Pairing {id: 'P-2291'}) CREATE (c)-[:ASSIGNED {role: 'Observer'}]->(p)",
    )
    with pytest.raises(Tampered):
        approve(g, plan["plan_id"], "meera.bhatt")
    assert _captain_of(g, "P-2291")[0]["c"] == "C-1042"


def test_only_offered_options_can_be_planned(fresh):
    g = fresh.ops
    d = _sick_captain(g)
    gap = f"{d}/P-2291/C-1042"
    with pytest.raises(PlanError, match="not a legal option"):
        submit(g, d, "ananya.iyer", assignments=[{"gap": gap, "crew_id": "C-2087"}])


def test_rejection_returns_the_disruption_to_the_desk(fresh):
    g = fresh.ops
    d = _sick_captain(g)
    plan = submit(g, d, "ananya.iyer")
    reject(g, plan["plan_id"], "meera.bhatt", "use the cheaper day-off crew")
    assert get(g, d)["status"] == "OPTIONS_READY"


def test_approval_updates_the_shared_playbook(fresh):
    g = fresh.ops
    d = _sick_captain(g)
    approve(g, submit(g, d, "ananya.iyer")["plan_id"], "meera.bhatt")
    rows = run(
        g, "playbook", "MATCH (:Playbook {role: 'Captain'})-[c:CHOSE]->(s:Strategy) RETURN s.name AS s, c.count AS n"
    )
    assert rows == [{"s": "reserve_callout", "n": 1}]


def test_gate_closure_plan_moves_the_flight(fresh):
    g = fresh.ops
    d = report(
        g,
        "gate_closure",
        {"gate": "BLR-G1", "start": "2026-10-13T00:00:00Z", "end": "2026-10-13T06:00:00Z"},
        "imran.sheikh",
    )
    analyse(g, d)
    desk = get(g, d)["desk"]
    controller = run(g, "ctl", "MATCH (u:User)-[:STAFFS]->(:Desk {id: $d}) RETURN u.username AS u", {"d": desk})[0]["u"]
    approve(g, submit(g, d, controller)["plan_id"], "karan.mehta")
    rows = run(g, "gate", "MATCH (:Flight {id: 'NG451-2026-10-13'})-[:BOARDS_AT]->(x:Gate) RETURN x.id AS gate")
    assert rows == [{"gate": "BLR-E1"}]


def test_delay_plan_cancels_the_legs_the_crew_cannot_fly(fresh):
    g = fresh.ops
    d = report(g, "flight_delay", {"aircraft": "VT-DXA", "date": "2026-10-14", "delay_hours": 1.5}, "imran.sheikh")
    result = analyse(g, d)
    assert [c["flight"] for c in result["proposed_cancellations"]] == ["DX404-2026-10-14"]
    desk = get(g, d)["desk"]
    controller = run(g, "ctl", "MATCH (u:User)-[:STAFFS]->(:Desk {id: $d}) RETURN u.username AS u", {"d": desk})[0]["u"]
    plan = submit(g, d, controller)
    assert plan["cancel_flights"] == ["DX404-2026-10-14"] and plan["total_cost_inr"] == 250_000
    approve(g, plan["plan_id"], "meera.bhatt")
    rows = run(g, "status", "MATCH (f:Flight {id: 'DX404-2026-10-14'}) RETURN f.status AS s")
    assert rows == [{"s": "cancelled"}]


def test_controller_can_choose_another_option_or_cancel(fresh):
    g = fresh.ops
    d = _sick_captain(g)
    gap = f"{d}/P-2291/C-1042"
    chosen = submit(g, d, "ananya.iyer", assignments=[{"gap": gap, "crew_id": "C-3983"}])
    assert chosen["assignments"][0]["crew"] == "C-3983" and chosen["total_cost_inr"] == 30660
    reject(g, chosen["plan_id"], "meera.bhatt", "try cancelling")
    cancel = submit(g, d, "ananya.iyer", assignments=[], cancel_pairings=["P-2291"])
    assert len(cancel["cancel_flights"]) == 6 and cancel["total_cost_inr"] == 6 * 250_000


def test_a_plan_must_change_something(fresh):
    g = fresh.ops
    d = _sick_captain(g)
    with pytest.raises(PlanError, match="nothing to plan"):
        submit(g, d, "ananya.iyer", assignments=[])


def test_approved_hold_moves_the_trips_departures(fresh):
    g = fresh.ops
    d = report(g, "certification_lapse", {"crew_id": "C-5417", "from_date": "2026-10-16"}, "imran.sheikh")
    gap = analyse(g, d)["gaps"][0]["gap"]
    desk = get(g, d)["desk"]
    controller = run(g, "ctl", "MATCH (u:User)-[:STAFFS]->(:Desk {id: $d}) RETURN u.username AS u", {"d": desk})[0]["u"]
    before = run(g, "dep", "MATCH (f:Flight {id: 'DX421-2026-10-17'}) RETURN f.dep_ts AS t")[0]["t"]
    plan = submit(g, d, controller, assignments=[{"gap": gap, "crew_id": "C-1329"}])
    assert plan["assignments"][0]["strategy"] == "delay_departure"
    approve(g, plan["plan_id"], "meera.bhatt")
    after = run(g, "dep", "MATCH (f:Flight {id: 'DX421-2026-10-17'}) RETURN f.dep_ts AS t, f.delay_minutes AS m")[0]
    assert after == {"t": before + 2 * 3600, "m": 120}
