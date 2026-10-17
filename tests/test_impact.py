"""Blast radius, delay ripple and closures, compared with data/eval/scenarios.json."""

from crewops.impact import crew_blast_radius, delay_ripple, gate_closure, station_closure, who_to_tell
from crewops.timeutil import ts


def test_sick_captain_leaves_both_days_of_the_trip_uncovered(loaded, scenarios):
    key = scenarios["S2"]["answer_key"]
    seats = crew_blast_radius(loaded.ops, "C-1042", "2026-10-13", "2026-10-14", ts("2026-10-13T05:00:00Z"))
    assert [s.pairing_id for s in seats] == ["P-2291"]
    assert set(seats[0].uncovered) == set(key["uncovered_flights_day1"]) | set(key["uncovered_flights_day2"])


def test_delay_carries_down_the_rotation_and_breaks_fdp(loaded, scenarios):
    key = scenarios["S4"]["answer_key"]
    effect = delay_ripple(loaded.ops, 1.5, aircraft="VT-DXA", date="2026-10-14")
    assert [leg.delay_hours for leg in effect.legs] == [1.5] * 4
    day = effect.duty_days[0]
    assert (day.fdp_hours, day.fdp_limit, day.breach) == (key["fdp_after_delay"], key["fdp_limit"], True)
    assert day.legs_to_drop == ("DX404-2026-10-14",)


def test_overnight_slack_absorbs_a_late_last_leg(loaded):
    effect = delay_ripple(loaded.ops, 2, flight_id="DX404-2026-10-14")
    assert [leg.flight_id for leg in effect.legs] == ["DX404-2026-10-14"]


def test_station_closure_matches_every_flight_in_the_answer_key(loaded, scenarios):
    key = {f["flight_id"]: f for f in scenarios["S3"]["answer_key"]["per_flight_assessment"]}
    impacts = station_closure(loaded.ops, "BLR", ts("2026-10-15T08:00:00Z"), ts("2026-10-15T14:00:00Z"))
    assert {i.flight_id for i in impacts} == set(scenarios["S3"]["answer_key"]["affected_flights"])
    for i in impacts:
        k = key[i.flight_id]
        assert i.min_delay_hours == k["min_delay_hours"]
        assert (i.duty.fdp_hours, i.duty.fdp_limit) == (k["crew_fdp_after_delay"], k["fdp_limit"])


def test_gate_closure_moves_to_a_free_gate_ordinary_first(loaded):
    moves = gate_closure(loaded.ops, "BLR-G1", ts("2026-10-13T00:00:00Z"), ts("2026-10-13T06:00:00Z"))
    assert [m.flight_id for m in moves] == ["NG451-2026-10-13"]
    assert moves[0].free_gates[-1].startswith("BLR-E")


def test_who_to_tell_reaches_station_network_crew_and_desk(loaded):
    audience = who_to_tell(
        loaded.ops, "station_closure", ["DX402-2026-10-15"], ["P-2204"], ["VT-DXA"], stations=["BLR"]
    )
    teams = {(c["team"], c["scope"]) for c in audience.contacts}
    assert ("gate_control", "BLR") in teams and ("flight_info", "NETWORK") in teams
    assert len(audience.crew) == 6
    assert audience.controllers[0]["username"] == "ananya.iyer"
