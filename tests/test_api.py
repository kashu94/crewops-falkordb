"""Console API."""

import pytest
from fastapi.testclient import TestClient

from crewops import api


@pytest.fixture
def client(fresh, monkeypatch):
    monkeypatch.setattr(api, "graphs", lambda: fresh)
    api._assistants.clear()
    return TestClient(api.app)


def _as(client, username):
    assert client.post("/api/login", json={"username": username}).status_code == 200


def test_report_triage_plan_approve_through_the_api(client):
    _as(client, "imran.sheikh")
    d = client.post(
        "/api/disruptions",
        json={
            "kind": "crew_unavailable",
            "details": {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"},
        },
    ).json()["id"]
    assert client.post("/api/triage").json()["handled"][0]["disruption"] == d
    _as(client, "ananya.iyer")
    plan = client.post(f"/api/disruptions/{d}/plan", json={}).json()
    assert plan["total_cost_inr"] == 23090
    assert client.post(f"/api/plans/{plan['plan_id']}/approve").status_code == 403  # not an approver
    _as(client, "meera.bhatt")
    assert client.post(f"/api/plans/{plan['plan_id']}/approve").json()["status"] == "COMMITTED"
    detail = client.get(f"/api/disruptions/{d}").json()
    assert detail["status"] == "RESOLVED"
    assert detail["trail"]["plans"][0]["approved_by"] == "Meera Bhatt"
    graph = client.get(f"/api/disruptions/{d}/graph").json()
    assert any(e["label"] == "APPROVED_BY" for e in graph["edges"])


def test_assistant_keeps_its_session(client):
    _as(client, "ananya.iyer")
    first = client.post("/api/ask", json={"question": "Who covers P-2291 captain?"}).json()
    follow = client.post(
        "/api/ask", json={"question": "Is she legal for it?", "session_id": first["session_id"]}
    ).json()
    assert follow["session_id"] == first["session_id"] and follow["turn"] == 2
    assert follow["answer"].startswith("Yes. C-3310")
    assert follow["evidence"]


def test_signed_out_and_bad_input(client):
    assert client.get("/api/me").status_code == 401
    _as(client, "nikhil.agarwal")  # auditor
    r = client.post("/api/disruptions", json={"kind": "crew_unavailable", "details": {"crew_id": "C-1042"}})
    assert r.status_code == 403
    _as(client, "imran.sheikh")
    assert client.post("/api/disruptions", json={"kind": "volcano", "details": {}}).status_code == 400
    assert client.get("/api/disruptions/D-9999").status_code == 404


def test_header_sign_in_works_without_cookies(client):
    client.cookies.clear()
    me = client.get("/api/me", headers={"X-CrewOps-User": "meera.bhatt"}).json()
    assert me["username"] == "meera.bhatt" and "approver" in me["roles"]
