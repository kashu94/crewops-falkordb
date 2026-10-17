import pytest

from crewops.graph import run
from crewops.seed import load, summarize


def test_seed_counts(loaded):
    counts = summarize(loaded.ops)
    assert counts["Crew"] == 150
    assert counts["Flight"] == 147
    assert counts["Pairing"] == 39
    assert counts["DutyLog"] == 150 * 28


def test_every_pairing_is_fully_crewed_on_its_aircraft(loaded):
    rows = run(
        loaded.ops,
        "pairings_without_captain",
        """
        MATCH (p:Pairing)
        WHERE NOT (:Crew)-[:ASSIGNED {role: 'Captain'}]->(p)
           OR NOT (p)-[:FLOWN_BY]->(:Aircraft)
        RETURN p.id AS id
        """,
    )
    assert rows == []


def test_rotations_link_each_aircraft_in_departure_order(loaded):
    rows = run(
        loaded.ops,
        "rotation_order",
        """
        MATCH (a:Flight)-[:NEXT_LEG]->(b:Flight)
        WHERE b.dep_ts < a.arr_ts OR NOT (a)-[:ON]->(:Aircraft)<-[:ON]-(b)
        RETURN count(*) AS bad
        """,
    )
    assert rows[0]["bad"] == 0


def test_duty_days_carry_their_legs(loaded):
    rows = run(
        loaded.ops,
        "duty_day",
        "MATCH (d:DutyDay {id: 'P-2201/2026-10-12'}) RETURN d.sectors AS s, d.dep_station AS dep",
    )
    assert rows == [{"s": 4, "dep": "BLR"}]


def test_loading_twice_needs_replace(loaded):
    with pytest.raises(RuntimeError):
        load(loaded.ops)
