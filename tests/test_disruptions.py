import pytest

from crewops.disruptions import BadReport, NotAllowed, analyse, get, report
from crewops.graph import run
from crewops.legality import assess


def test_report_and_analyse_a_sick_captain(fresh):
    g = fresh.ops
    d = report(
        g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"}, "imran.sheikh"
    )
    result = analyse(g, d)
    gap = result["gaps"][0]
    assert gap["recommended"]["crew_id"] == "C-3310"
    assert result["desk"] == "desk-1"
    assert get(g, d)["status"] == "OPTIONS_READY"
    # the graph now holds the decision context
    rows = run(
        g,
        "options_in_graph",
        "MATCH (:Disruption {id: $id})-[:HAS_GAP]->(:Gap)-[o:OPTION]->(c:Crew) RETURN c.id AS c ORDER BY o.rank LIMIT 1",
        {"id": d},
    )
    assert rows == [{"c": "C-3310"}]
    breaches = run(
        g,
        "exclusions_by_rule",
        "MATCH (:Disruption {id: $id})-[:HAS_GAP]->(:Gap)-[:RULED_OUT]->(:Exclusion)-[:BREACHES]->(r:Rule) "
        "RETURN r.id AS rule, count(*) AS n ORDER BY n DESC",
        {"id": d},
    )
    assert {b["rule"] for b in breaches} >= {"RULE-REST-04", "RULE-DUTY-02", "RULE-QUAL-05"}


def test_grounded_crew_are_no_longer_legal_anywhere_that_day(fresh):
    g = fresh.ops
    assert assess(g, "C-3310", "P-2291").legal
    report(g, "crew_unavailable", {"crew_id": "C-3310", "from_date": "2026-10-13"}, "imran.sheikh")
    a = assess(g, "C-3310", "P-2291")
    assert not a.legal
    assert "reported unavailable" in a.blocking[0].detail


def test_two_sick_captains_get_a_joint_plan(fresh, scenarios):
    key = scenarios["S6"]["answer_key"]["optimal_joint_plan"]
    g = fresh.ops
    d = report(g, "crew_unavailable", {"crew_ids": ["C-3940", "C-1938"], "from_date": "2026-10-16"}, "lakshmi.hegde")
    joint = analyse(g, d)["joint_plan"]
    assert joint["total_cost_inr"] == key["total_cost_inr"]
    assert {a["crew_id"] for a in joint["assignments"]} == {key["assign_dxa"]["crew_id"], key["assign_dxb"]["crew_id"]}


def test_desks_share_the_load(fresh):
    g = fresh.ops
    desks = []
    for crew in ("C-1042", "C-3231", "C-1938"):
        d = report(g, "crew_unavailable", {"crew_id": crew, "from_date": "2026-10-14"}, "imran.sheikh")
        desks.append(analyse(g, d)["desk"])
    assert sorted(desks) == ["desk-1", "desk-2", "desk-3"]


def test_roles_are_checked_in_code(fresh):
    g = fresh.ops
    with pytest.raises(NotAllowed):
        report(g, "crew_unavailable", {"crew_id": "C-1042"}, "nikhil.agarwal")  # auditor
    with pytest.raises(NotAllowed):
        report(g, "crew_unavailable", {"crew_id": "C-2087"}, "c1042.nair")  # someone else
    assert report(g, "crew_unavailable", {"crew_id": "C-1042"}, "c1042.nair")
    with pytest.raises(BadReport):
        report(g, "flight_delay", {"flight_id": "DX401-2026-10-14", "delay_hours": 0}, "imran.sheikh")


def test_delay_report_writes_ripple_and_breach_edges(fresh):
    g = fresh.ops
    d = report(g, "flight_delay", {"aircraft": "VT-DXA", "date": "2026-10-14", "delay_hours": 1.5}, "imran.sheikh")
    analyse(g, d)
    rows = run(g, "delay_edges", "MATCH (:Disruption {id: $id})-[e:DELAYS]->(f) RETURN count(f) AS n", {"id": d})
    assert rows[0]["n"] == 4
    rows = run(g, "fdp_edges", "MATCH (:Disruption {id: $id})-[e:BREACHES_FDP]->(dd) RETURN e.drop AS drop", {"id": d})
    assert rows[0]["drop"] == ["DX404-2026-10-14"]
