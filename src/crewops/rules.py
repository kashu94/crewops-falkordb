"""The seven legality rules.

Thresholds come from the ``(:Rule)`` nodes. Each check returns a verdict with the numbers
behind it (used, limit, headroom) so a failure can be explained.

Duty and flight-hour limits use calendar-day windows (UTC dates, including the duty date).
The FDP limit is 13h minus 0.5h for each sector after the second."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from falkordb import Graph

from crewops.graph import run
from crewops.timeutil import add_days, format_hours, hours, iso

FDP = "RULE-FDP-01"
DUTY = "RULE-DUTY-02"
FLIGHT = "RULE-FLT-03"
REST = "RULE-REST-04"
QUAL = "RULE-QUAL-05"
CERT = "RULE-CERT-06"
BASE = "RULE-BASE-07"

ALL_RULES = (FDP, DUTY, FLIGHT, REST, QUAL, CERT, BASE)

PILOT_RANKS = ("Captain", "First Officer")
CABIN_RANKS = ("Senior Cabin Crew", "Cabin Crew")


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    rule_id: str
    passed: bool
    detail: str
    used: float | None = None
    limit: float | None = None
    headroom: float | None = None
    date: str | None = None

    @property
    def failed(self) -> bool:
        return not self.passed

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_id,
            "passed": self.passed,
            "detail": self.detail,
            **{k: v for k in ("used", "limit", "headroom", "date") if (v := getattr(self, k)) is not None},
        }


def ok(rule_id: str, detail: str, **numbers: Any) -> RuleVerdict:
    return RuleVerdict(rule_id, True, detail, **numbers)


def fail(rule_id: str, detail: str, **numbers: Any) -> RuleVerdict:
    return RuleVerdict(rule_id, False, detail, **numbers)


@dataclass(frozen=True, slots=True)
class RuleBook:
    base_fdp_hours: float = 13.0
    reduction_per_extra_sector_hours: float = 0.5
    free_sectors: int = 2
    max_duty_hours: float = 60.0
    duty_window_days: int = 7
    max_flight_hours: float = 100.0
    flight_window_days: int = 28
    min_rest_hours: float = 12.0

    def fdp_limit(self, sectors: int) -> float:
        extra = max(0, sectors - self.free_sectors)
        return self.base_fdp_hours - self.reduction_per_extra_sector_hours * extra


RULEBOOK_QUERY = "MATCH (r:Rule) RETURN r.id AS id, properties(r) AS props"


def load_rulebook(graph: Graph) -> RuleBook:
    """Thresholds from the ``(:Rule)`` nodes; anything absent keeps its default."""
    by_id: Mapping[str, Mapping[str, Any]] = {
        r["id"]: r["props"] for r in run(graph, "rulebook", RULEBOOK_QUERY, read_only=True)
    }

    def param(rule_id: str, key: str, default: float) -> float:
        return float(by_id.get(rule_id, {}).get(key, default))

    d = RuleBook()
    return RuleBook(
        base_fdp_hours=param(FDP, "base_fdp_hours", d.base_fdp_hours),
        reduction_per_extra_sector_hours=param(
            FDP, "reduction_per_extra_sector_hours", d.reduction_per_extra_sector_hours
        ),
        free_sectors=int(param(FDP, "free_sectors", d.free_sectors)),
        max_duty_hours=param(DUTY, "max_duty_hours", d.max_duty_hours),
        duty_window_days=int(param(DUTY, "window_days", d.duty_window_days)),
        max_flight_hours=param(FLIGHT, "max_flight_hours", d.max_flight_hours),
        flight_window_days=int(param(FLIGHT, "window_days", d.flight_window_days)),
        min_rest_hours=param(REST, "min_rest_hours", d.min_rest_hours),
    )


@dataclass(frozen=True, slots=True)
class DutyDay:
    """One day of a pairing, as the cover would fly it."""

    date: str
    report_ts: int
    release_ts: int
    sectors: int
    block_hours: float
    aircraft_type: str
    dep_station: str

    @property
    def fdp_hours(self) -> float:
        return hours(self.report_ts, self.release_ts)

    def delayed(self, h: float) -> DutyDay:
        """A late departure: crew still report on time, so only the release moves."""
        return replace(self, release_ts=self.release_ts + round(h * 3600))

    def shifted(self, h: float) -> DutyDay:
        """The whole day later (crew positioning in). Length unchanged."""
        s = round(h * 3600)
        return replace(self, report_ts=self.report_ts + s, release_ts=self.release_ts + s)


@dataclass(frozen=True, slots=True)
class Duty:
    """A duty day already on someone's roster."""

    pairing_id: str
    date: str
    report_ts: int
    release_ts: int
    block_hours: float

    @property
    def duty_hours(self) -> float:
        return hours(self.report_ts, self.release_ts)


@dataclass(frozen=True, slots=True)
class CrewState:
    crew_id: str
    name: str
    rank: str
    base: str
    airline: str | None
    ratings: tuple[str, ...]
    status: str
    last_rest_ended_ts: int | None
    daily_duty: Mapping[str, float] = field(default_factory=dict)
    daily_flight: Mapping[str, float] = field(default_factory=dict)
    certs: tuple[tuple[str, str, str], ...] = ()
    duties: tuple[Duty, ...] = ()

    def _per_day(self, history: Mapping[str, float], hours_of, exclude: str | None) -> dict[str, float]:
        """Hours per day from history or the roster, never both: the larger of the two."""
        rostered: dict[str, float] = {}
        for d in self.duties:
            if d.pairing_id != exclude:
                rostered[d.date] = rostered.get(d.date, 0.0) + hours_of(d)
        days = set(history) | set(rostered)
        return {d: max(history.get(d, 0.0), round(rostered.get(d, 0.0), 2)) for d in days}

    def duty_in_window(self, start: str, end: str, exclude: str | None = None) -> float:
        per_day = self._per_day(self.daily_duty, lambda d: d.duty_hours, exclude)
        return round(sum(h for d, h in per_day.items() if start <= d <= end), 2)

    def flight_in_window(self, start: str, end: str, exclude: str | None = None) -> float:
        per_day = self._per_day(self.daily_flight, lambda d: d.block_hours, exclude)
        return round(sum(h for d, h in per_day.items() if start <= d <= end), 2)

    def rostered_dates(self, exclude: str | None = None) -> set[str]:
        return {d.date for d in self.duties if d.pairing_id != exclude}


def calendar_window(end: str, days: int) -> tuple[str, str]:
    """Seven days ending the 14th run from the 8th to the 14th."""
    return add_days(end, -(days - 1)), end


def check_fdp(day: DutyDay, book: RuleBook) -> RuleVerdict:
    used, limit = day.fdp_hours, book.fdp_limit(day.sectors)
    numbers = {"used": used, "limit": limit, "headroom": round(limit - used, 2), "date": day.date}
    if used > limit:
        return fail(
            FDP,
            f"FDP {used}h exceeds {limit}h limit ({day.sectors} sectors) by {format_hours(used - limit)}",
            **numbers,
        )
    return ok(FDP, f"FDP {used}h of {limit}h ({day.sectors} sectors)", **numbers)


def _worst_window(crew, day, earlier, window_days, in_window, hours_of, exclude) -> tuple[float, str]:
    """Largest total over every window the new duty can push over the limit: the one ending
    on the duty date and each ending on a later rostered day inside the horizon."""
    new_days = [*earlier, day]
    horizon = add_days(day.date, window_days - 1)
    ends = {day.date} | {d for d in crew.rostered_dates(exclude) if day.date < d <= horizon}
    worst: tuple[float, str] | None = None
    for end in sorted(ends):
        start, _ = calendar_window(end, window_days)
        total = round(
            in_window(start, end, exclude) + sum(hours_of(d) for d in new_days if start <= d.date <= end),
            2,
        )
        if worst is None or total > worst[0]:
            worst = (total, end)
    assert worst is not None
    return worst


def _window_verdict(rule_id, total, end, day, limit, window_days) -> RuleVerdict:
    numbers = {"used": total, "limit": limit, "headroom": round(limit - total, 2), "date": day.date}
    window = f"{limit:g}h/{window_days}d"
    where = f"on {day.date}" if end == day.date else f"in the {window_days} days ending {end}"
    if total > limit:
        return fail(
            rule_id,
            f"would exceed {window} by {format_hours(total - limit)} {where} (total {total}h)",
            **numbers,
        )
    return ok(rule_id, f"{total}h of {window} {where}", **numbers)


def check_duty_window(crew, day, book, earlier=(), exclude=None) -> RuleVerdict:
    total, end = _worst_window(
        crew, day, earlier, book.duty_window_days, crew.duty_in_window, lambda d: d.fdp_hours, exclude
    )
    return _window_verdict(DUTY, total, end, day, book.max_duty_hours, book.duty_window_days)


def check_flight_window(crew, day, book, earlier=(), exclude=None) -> RuleVerdict:
    total, end = _worst_window(
        crew, day, earlier, book.flight_window_days, crew.flight_in_window, lambda d: d.block_hours, exclude
    )
    return _window_verdict(FLIGHT, total, end, day, book.max_flight_hours, book.flight_window_days)


def check_rest(crew: CrewState, day: DutyDay, book: RuleBook, exclude: str | None = None) -> RuleVerdict:
    """Minimum rest before this duty and before whatever they fly next."""
    minimum = book.min_rest_hours
    if crew.last_rest_ended_ts is not None and day.report_ts < crew.last_rest_ended_ts:
        short = hours(day.report_ts, crew.last_rest_ended_ts)
        return fail(
            REST,
            f"still resting until {iso(crew.last_rest_ended_ts)[:16].replace('T', ' ')}Z, "
            f"{format_hours(short)} after report on {day.date}",
            limit=minimum,
            date=day.date,
        )
    duties = [d for d in crew.duties if d.pairing_id != exclude]
    for duty in duties:
        if duty.report_ts < day.release_ts and day.report_ts < duty.release_ts:
            return fail(REST, f"double-booked: {duty.pairing_id} overlaps on {day.date}", date=day.date)
    for duty in duties:
        if duty.report_ts >= day.release_ts:
            rest, where = hours(day.release_ts, duty.report_ts), "before"
        elif duty.release_ts <= day.report_ts:
            rest, where = hours(duty.release_ts, day.report_ts), "after"
        else:
            continue
        if rest < minimum:
            return fail(
                REST,
                f"only {rest}h rest {where} {duty.pairing_id} on {duty.date}",
                used=rest,
                limit=minimum,
                headroom=round(rest - minimum, 2),
                date=day.date,
            )
    return ok(REST, f"at least {minimum:g}h rest either side", limit=minimum, date=day.date)


def check_qualification(crew: CrewState, day: DutyDay) -> RuleVerdict:
    if day.aircraft_type not in crew.ratings:
        return fail(QUAL, f"not rated on {day.aircraft_type}", date=day.date)
    if crew.status != "active":
        return fail(QUAL, f"status is {crew.status}, not active", date=day.date)
    return ok(QUAL, f"{day.aircraft_type} rated and active", date=day.date)


def check_certifications(crew: CrewState, day: DutyDay) -> RuleVerdict:
    """Every certificate still valid on the duty date (expiry only: some licence records in
    the data carry a future ``valid_from``)."""
    for cert_type, _valid_from, valid_to in crew.certs:
        if day.date > valid_to:
            return fail(CERT, f"{cert_type} expired {valid_to}, invalid on {day.date}", date=day.date)
    return ok(CERT, f"all {len(crew.certs)} certifications valid on {day.date}", date=day.date)


def check_base(crew: CrewState, day: DutyDay, positioned: bool) -> RuleVerdict:
    if crew.base == day.dep_station:
        return ok(BASE, f"own base ({crew.base})", date=day.date)
    if positioned:
        return ok(BASE, f"positioning from {crew.base} to {day.dep_station}", date=day.date)
    return fail(
        BASE,
        f"based at {crew.base}, duty starts at {day.dep_station} with no positioning flight",
        date=day.date,
    )


def evaluate(
    crew: CrewState,
    days: list[DutyDay],
    book: RuleBook,
    exclude: str | None = None,
    positioned: bool = False,
) -> list[RuleVerdict]:
    """All rules against every day; the base rule only on day one."""
    verdicts: list[RuleVerdict] = []
    for i, day in enumerate(days):
        verdicts += [
            check_qualification(crew, day),
            check_certifications(crew, day),
            check_fdp(day, book),
            check_duty_window(crew, day, book, days[:i], exclude),
            check_flight_window(crew, day, book, days[:i], exclude),
            check_rest(crew, day, book, exclude),
        ]
        if i == 0:
            verdicts.append(check_base(crew, day, positioned))
    return verdicts


def blocking(verdicts: Iterable[RuleVerdict]) -> list[RuleVerdict]:
    """The failures, each distinct rule and detail once."""
    seen: set[tuple[str, str]] = set()
    out = []
    for v in verdicts:
        if v.failed and (v.rule_id, v.detail) not in seen:
            seen.add((v.rule_id, v.detail))
            out.append(v)
    return out
