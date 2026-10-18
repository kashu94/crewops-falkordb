import asyncio

from crewops.agents import tools
from crewops.agents.tools import Caller
from crewops.mcp_server import build


def test_every_tool_returns_its_cypher_evidence(loaded):
    caller = Caller.load(loaded, "ananya.iyer")
    out = tools.call(loaded, caller, "find_cover", {"pairing_id": "p-2291", "role": "Captain"})
    assert out["ok"]
    assert out["result"]["recommended"]["crew_id"] == "C-3310"
    names = {q["query"] for q in out["evidence"]}
    assert {"candidate_pool", "crew_context"} <= names
    assert all("MATCH" in q["cypher"] or "CALL" in q["cypher"] for q in out["evidence"])


def test_roles_gate_tools_in_code(loaded):
    crew = Caller.load(loaded, "c1042.nair")
    assert not tools.call(loaded, crew, "find_cover", {"pairing_id": "P-2291", "role": "Captain"})["ok"]
    assert tools.call(loaded, crew, "crew_profile", {"crew": "C-1042"})["ok"]
    assert not tools.call(loaded, crew, "crew_profile", {"crew": "C-3310"})["ok"]
    approver = Caller.load(loaded, "meera.bhatt")
    assert "submit_plan" not in {t.name for t in tools.available(approver)}
    assert "approve_plan" in {t.name for t in tools.available(approver)}


def test_errors_come_back_as_data(loaded):
    caller = Caller.load(loaded, "ananya.iyer")
    out = tools.call(loaded, caller, "check_legality", {"crew_id": "C-9999", "pairing_id": "P-2291"})
    assert out["ok"] is False and "C-9999" in out["result"]["error"]
    assert tools.call(loaded, caller, "find_cover", {"role": "Captain"})["error"] == "missing pairing_id"


def test_strict_schemas_list_every_property_as_required():
    for tool in tools.TOOLS:
        schema = tool.input_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_name_search_uses_the_fulltext_index(loaded):
    caller = Caller.load(loaded, "ananya.iyer")
    out = tools.call(loaded, caller, "crew_profile", {"crew": "Pillai"})
    assert len(out["result"]["matches"]) > 1


def test_mcp_server_exposes_the_callers_tools(loaded):
    server = build("ananya.iyer", loaded)

    async def go():
        listed = {t.name: t for t in await server.list_tools()}
        assert "submit_plan" in listed and "approve_plan" not in listed
        assert listed["find_cover"].input_schema["properties"]["role"]["enum"][0] == "Captain"
        return await server.call_tool("check_legality", {"crew_id": "C-3310", "pairing_id": "P-2291"})

    result = asyncio.run(go())
    assert '"legal": true' in result.content[0].text


def test_crew_schedule_lists_flights_and_is_private_to_crew(loaded):
    reporter = Caller.load(loaded, "imran.sheikh")
    out = tools.call(loaded, reporter, "crew_schedule", {"crew": "C-1042"})
    day = out["result"]["duty_days"][0]
    assert (day["date"], day["pairing"], day["report"]) == ("2026-10-13", "P-2291", "06:00")
    assert [f["flight"] for f in day["flights"]] == ["KS412", "KS413", "KS588"]
    assert day["flights"][0]["from"] == "BLR" and day["flights"][0]["gate"]
    crew = Caller.load(loaded, "c1042.nair")
    assert tools.call(loaded, crew, "crew_schedule", {"crew": "C-1042"})["ok"]
    assert not tools.call(loaded, crew, "crew_schedule", {"crew": "C-3310"})["ok"]


def test_schedule_explains_a_handed_over_trip(fresh):
    from crewops.disruptions import analyse, report
    from crewops.plans import approve, submit

    g = fresh.ops
    d = report(g, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13"}, "imran.sheikh")
    analyse(g, d)
    approve(g, submit(g, d, "ananya.iyer")["plan_id"], "meera.bhatt")
    crew = Caller.load(fresh, "c1042.nair")
    out = tools.call(fresh, crew, "crew_schedule", {"crew": "C-1042"})
    assert out["ok"], out
    sched = out["result"]
    assert sched["duty_days"] == []
    assert sched["handed_over"] == [
        {"pairing": "P-2291", "plan": "PL-0001", "covered_by": "C-3310", "covered_by_name": "D. Reddy"}
    ]
