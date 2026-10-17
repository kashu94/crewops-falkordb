from crewops.disruptions import analyse, report
from crewops.explain import decision_trail, rule_pressure, subgraph, why_not
from crewops.plans import approve, submit


def test_trail_runs_from_reporter_to_approver(fresh):
    g = fresh.ops
    d = report(
        g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"}, "imran.sheikh"
    )
    analyse(g, d)
    approve(g, submit(g, d, "ananya.iyer")["plan_id"], "meera.bhatt")
    trail = decision_trail(g, d)
    assert trail["reported_by"] == "Imran Sheikh"
    assert trail["controller"] == "Ananya Iyer"
    plan = trail["plans"][0]
    assert (plan["submitted_by"], plan["approved_by"], plan["status"]) == ("Ananya Iyer", "Meera Bhatt", "COMMITTED")
    assert plan["assignments"][0]["crew"] == "C-3310"
    assert [e["kind"] for e in trail["events"]] == [
        "reported",
        "analysed",
        "assigned",
        "plan_submitted",
        "plan_committed",
    ]


def test_why_not_names_the_rule(fresh):
    g = fresh.ops
    d = report(
        g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"}, "imran.sheikh"
    )
    analyse(g, d)
    excluded = why_not(g, d, "c-2087")
    assert excluded["legal"] is False
    assert excluded["exclusions"][0]["rules"][0]["rule"] == "RULE-DUTY-02"
    beaten = why_not(g, d, "C-3983")
    assert beaten["legal"] is True
    assert beaten["options"][0]["ranked_above"][0]["crew"] == "C-3310"
    assert why_not(g, d, "C-3315")["legal"] is None  # an ATR captain at another airline


def test_rule_pressure_and_subgraph(fresh):
    g = fresh.ops
    d = report(
        g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"}, "imran.sheikh"
    )
    analyse(g, d)
    assert rule_pressure(g)[0]["rule"] == "RULE-REST-04"
    sg = subgraph(g, d)
    groups = {n["group"] for n in sg["nodes"]}
    assert {"Disruption", "Gap", "Crew", "Flight", "User", "Desk"} <= groups
    assert any(e["label"] == "OPTION #1" for e in sg["edges"])
