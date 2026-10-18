"""Cover options, compared with the expected results in data/eval/scenarios.json."""

import pytest

from crewops.options import AlreadyStarted, Strategy, find_options

CASES = [("S1", "P-2224", "Captain"), ("S2", "P-2291", "Captain"), ("S5", "P-2213", "Cabin Crew")]


@pytest.mark.parametrize(("sid", "pairing", "role"), CASES)
def test_ranked_options_match_answer_key(loaded, scenarios, sid, pairing, role):
    result = find_options(loaded.ops, pairing, role)
    key = scenarios[sid]["answer_key"]["options"]
    # the scenarios predate the delay option, which is checked separately below
    ranked = [o for o in result.options if o.strategy is not Strategy.DELAY]
    assert [(o.crew_id, o.cost_inr) for o in ranked] == [(o.get("crew_id"), o.get("cost_inr")) for o in key]
    excluded = {e.crew_id for e in result.excluded}
    assert {e["crew_id"] for e in scenarios[sid]["answer_key"].get("excluded_candidates", [])} <= excluded


def test_cancellation_is_always_last(loaded):
    result = find_options(loaded.ops, "P-2291", "Captain")
    assert result.cancellation.strategy is Strategy.CANCEL
    assert result.cancellation.cost_inr == 6 * 250_000
    assert result.cancellation_multiple > 10


def test_cost_breakdown_adds_up(loaded):
    rec = find_options(loaded.ops, "P-2224", "Captain").recommended
    assert rec.crew_id == "C-3315"
    assert rec.cost_breakdown == {"callout": 18500, "seniority_allowance": 2250, "travel": 360}
    assert sum(rec.cost_breakdown.values()) == rec.cost_inr


def test_funnel_accounts_for_every_candidate(loaded):
    result = find_options(loaded.ops, "P-2291", "Captain")
    considered = result.funnel[0].remaining
    legal = result.funnel[-1].remaining
    assert considered == legal + len(result.excluded)


def test_a_started_pairing_cannot_be_recrewed(loaded):
    with pytest.raises(AlreadyStarted):
        find_options(loaded.ops, "P-2201", "Captain")  # flew on 12 Oct, before the snapshot


def test_holding_the_trip_is_offered_when_it_frees_someone(loaded):
    result = find_options(loaded.ops, "P-2213", "Cabin Crew")
    delay = [o for o in result.options if o.strategy is Strategy.DELAY]
    assert len(delay) == 1
    assert (delay[0].crew_id, delay[0].delay_hours) == ("C-1329", 2.0)
    assert delay[0].cost_breakdown["delay"] == 2 * 5400
    assert result.options[-1].strategy is Strategy.CANCEL
    assert result.recommended.strategy is not Strategy.DELAY


def test_no_hold_option_when_no_delay_helps(loaded):
    result = find_options(loaded.ops, "P-2291", "Captain")
    assert result.delay_checked
    assert not [o for o in result.options if o.strategy is Strategy.DELAY]
