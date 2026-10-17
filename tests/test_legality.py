"""Legality checks, compared with scenario S2 in data/eval/scenarios.json."""

from crewops.legality import Context, assess
from crewops.rules import DutyDay, RuleBook, calendar_window, check_fdp


def test_fdp_limit_shrinks_with_sectors():
    book = RuleBook()
    assert book.fdp_limit(2) == 13.0
    assert book.fdp_limit(4) == 12.0


def test_calendar_window_includes_the_duty_date():
    assert calendar_window("2026-10-14", 7) == ("2026-10-08", "2026-10-14")


def test_fdp_breach_reads_out_the_overrun():
    day = DutyDay("2026-10-14", 0, int(12.5 * 3600), 4, 7.5, "A320", "BLR")
    verdict = check_fdp(day, RuleBook())
    assert verdict.failed
    assert "by 0h30m" in verdict.detail


def test_rule_thresholds_come_from_the_graph(loaded):
    ctx = Context.open(loaded.ops)
    assert ctx.book.max_duty_hours == 60
    assert ctx.book.min_rest_hours == 12


def test_reserve_captain_is_legal_for_the_flagship_pairing(loaded):
    a = assess(loaded.ops, "C-3310", "P-2291")
    assert a.legal
    assert a.on_reserve


def test_exclusions_match_the_answer_key(loaded, scenarios):
    ctx = Context.open(loaded.ops)
    for expected in scenarios["S2"]["answer_key"]["excluded_candidates"]:
        a = assess(loaded.ops, expected["crew_id"], "P-2291", ctx=ctx)
        assert not a.legal
        rule, _, detail = expected["reason"].partition(": ")
        assert a.blocking[0].rule_id == rule
        assert a.blocking[0].detail == detail


def test_wrong_airline_is_not_legal(loaded):
    # C-3315 flies ATR72s for Nilgiri Connect; P-2291 is a Konkan Skyways A320 trip
    a = assess(loaded.ops, "C-3315", "P-2291")
    assert not a.legal
    assert any(v.rule_id == "RULE-QUAL-05" for v in a.blocking)
