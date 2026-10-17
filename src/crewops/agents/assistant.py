"""Assistant for one user.

Uses Claude with the graph tools when ANTHROPIC_API_KEY is set, otherwise (or if the API
call fails) a rule-based planner that calls the same tools. Reads and writes the user's
memory graph on every turn."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from crewops.agents import tools as registry
from crewops.agents.tools import Caller
from crewops.config import settings
from crewops.graph import Graphs
from crewops.legality import Context
from crewops.memory import Memory, extract_entities
from crewops.timeutil import add_days, day_of

SYSTEM_PROMPT = """You are CrewOps, the crew-control assistant for three airlines (Deccan Express, \
Konkan Skyways, Nilgiri Connect) working from a FalkorDB graph of the operation.

How to work:
- Answer from tool results only. Legality, costs, hours and rankings come from the tools; never \
work them out yourself, and quote crew ids, costs (INR) and flight ids exactly as the tools return them.
- Prefer one or two well-chosen tool calls. For "who covers X" find the seat first (blast_radius for a \
crew member, get_disruption for a disruption), then find_cover.
- When a tool returns an error, say what is missing or not allowed rather than guessing.
- Keep answers short enough to read aloud: lead with the recommendation, then the cost, then one line \
on why the next options lost. Say which rules ruled people out when it matters.
- You can only read and report through these tools; approvals need a human approver and you cannot \
bypass that. Never claim an action happened unless a tool result says it did."""

MAX_TOOL_ROUNDS = 6


@dataclass
class Answer:
    text: str
    intent: str
    engine: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    turn: int = 0
    focus: list[dict[str, str]] = field(default_factory=list)
    """What the answer is about (the recommended crew member, say), recorded last so a
    follow-up's "she" or "it" resolves to it."""

    @property
    def evidence(self) -> list[dict[str, Any]]:
        return [q for c in self.tool_calls for q in c.get("evidence", [])]

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.text,
            "intent": self.intent,
            "engine": self.engine,
            "session_id": self.session_id,
            "turn": self.turn,
            "tool_calls": [
                {
                    "tool": c["tool"],
                    "input": c["input"],
                    "ok": c["ok"],
                    "queries": [q["query"] for q in c.get("evidence", [])],
                }
                for c in self.tool_calls
            ],
            "evidence": self.evidence,
        }


class Assistant:
    def __init__(self, gs: Graphs, username: str, session_id: str | None = None, use_model: bool | None = None) -> None:
        self.gs = gs
        self.caller = Caller.load(gs, username)
        self.memory = Memory(gs, username)
        self.now_ts = Context.open(gs.ops).now_ts
        self.new_session = session_id is None
        self.session_id = session_id or self.memory.start_session(self.now_ts)
        cfg = settings()
        self.use_model = bool(cfg.anthropic_api_key) if use_model is None else use_model
        self.model = cfg.model

    # --- context from memory -----------------------------------------------------------------

    def focus(self) -> dict[str, str | None]:
        return {k: self.memory.last_entity(k, self.session_id) for k in ("crew", "pairing", "disruption", "plan")}

    def _context_block(self) -> str:
        lines = [
            f"Signed in: {self.caller.name} ({self.caller.username}), roles {', '.join(sorted(self.caller.roles))}"
            + (f", desk {self.caller.desk}" if self.caller.desk else "")
            + (f", crew id {self.caller.crew_id}" if self.caller.crew_id else "")
        ]
        lines.append(f"Operation clock: {_iso(self.now_ts)}")
        focus = {k: v for k, v in self.focus().items() if v}
        if focus:
            lines.append("Last mentioned in this conversation: " + ", ".join(f"{k} {v}" for k, v in focus.items()))
        facts = self.memory.facts()
        if facts:
            lines.append("Stored preferences: " + ", ".join(f"{k}={v}" for k, v in facts.items()))
        recent = self.memory.recent_turns(self.session_id, 4)
        if recent:
            lines.append(
                "Recent turns:\n" + "\n".join(f"- Q: {t['question']}\n  A: {t['answer'][:300]}" for t in recent)
            )
        return "\n".join(lines)

    # --- the turn ------------------------------------------------------------------------------

    def ask(self, question: str) -> Answer:
        question = question.strip()
        answer: Answer | None = None
        if self.use_model:
            try:
                answer = self._ask_model(question)
            except Exception as exc:  # fall back to the planner if the API call fails
                answer = self._ask_planner(question)
                answer.engine = f"planner (model unavailable: {type(exc).__name__})"
        if answer is None:
            answer = self._ask_planner(question)
        entities = extract_entities(question, *[json.dumps(c["input"]) for c in answer.tool_calls], answer.text)
        focus = [e for e in answer.focus if e.get("ref")]
        entities = [e for e in entities if e not in focus] + focus
        answer.session_id = self.session_id
        answer.turn = self.memory.record_turn(
            self.session_id,
            question,
            answer.text,
            self.now_ts,
            intent=answer.intent,
            tools=[c["tool"] for c in answer.tool_calls],
            entities=entities,
        )
        self.new_session = False
        return answer

    def _call(self, calls: list[dict[str, Any]], name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = registry.call(self.gs, self.caller, name, args)
        calls.append(
            {
                "tool": name,
                "input": args,
                "ok": out["ok"],
                "result": out.get("result", out),
                "evidence": out.get("evidence", []),
            }
        )
        return out

    # --- Claude --------------------------------------------------------------------------------

    def _ask_model(self, question: str) -> Answer:
        import anthropic

        client = anthropic.Anthropic()
        tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema(), "strict": True}
            for t in registry.available(self.caller, include_writes=False)
        ]
        system = f"{SYSTEM_PROMPT}\n\n<context>\n{self._context_block()}\n</context>"
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        calls: list[dict[str, Any]] = []
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("model declined")
            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text").strip()
                return Answer(text or "I could not find an answer in the graph.", _intent_of(calls), self.model, calls)
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                out = self._call(calls, block.name, dict(block.input))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(out.get("result", out), default=str)[:20000],
                        "is_error": not out["ok"],
                    }
                )
            messages.append({"role": "user", "content": results})
        raise RuntimeError("tool loop did not finish")

    # --- the deterministic planner -------------------------------------------------------------

    def _ask_planner(self, question: str) -> Answer:
        q = question.lower()
        refs = {e["kind"]: e["ref"] for e in reversed(extract_entities(question))}
        focus = self.focus()
        crew = refs.get("crew") or (focus["crew"] if _PRONOUN.search(q) else None)
        pairing = refs.get("pairing") or (
            focus["pairing"] if re.search(r"\b(it|that|this|the) (trip|pairing)\b|\bit\b", q) else None
        )
        disruption = refs.get("disruption") or focus["disruption"]
        calls: list[dict[str, Any]] = []
        self._focus: list[dict[str, str]] = []

        def done(text: str, intent: str) -> Answer:
            return Answer(text, intent, "planner", calls, focus=self._focus)

        if re.search(r"\b(inbox|waiting for me|anything (new|waiting)|what('s| is) new|my queue)\b", q) or (
            self.new_session and re.search(r"^(hi|hello|hey|good (morning|evening))\b", q)
        ):
            out = self._call(calls, "my_inbox", {})
            text = _render_inbox(out["result"])
            if self.new_session:
                prev = self.memory.previous_session(self.session_id)
                if prev and prev["turns"]:
                    text = f'Welcome back. Last time you asked: "{prev["turns"][-1]["question"]}". ' + text
            return done(text, "inbox")

        if m := re.search(r"\b(prefer|remember that|from now on)\b(.+)", q):
            policy = next(
                (
                    p
                    for p in ("least_delay", "cheapest", "keeps_most_reserve_cover")
                    if p.replace("_", " ") in m.group(2) or p in m.group(2)
                ),
                None,
            )
            if policy or "delay" in m.group(2):
                policy = policy or "least_delay"
                self._call(calls, "remember_preference", {"key": "ranking_policy", "value": policy})
                return done(f"Noted: I will lead with the {policy.replace('_', ' ')} option from now on.", "remember")

        if re.search(r"\b(what did i ask|last time|recall|remind me)\b", q):
            out = self._call(calls, "recall", {"ref": crew or pairing or disruption})
            r = out["result"]
            if r.get("mentions"):
                m0 = r["mentions"][0]
                return done(
                    f'You last asked about {r["ref"]}: "{m0["question"]}". I answered: {m0["answer"][:200]}', "recall"
                )
            prev = self.memory.previous_session(self.session_id)
            if prev and prev["turns"]:
                qs = "; ".join(t["question"] for t in prev["turns"][-3:])
                return done(f"In your previous session you asked: {qs}", "recall")
            return done("I have no earlier conversation with you yet.", "recall")

        if re.search(r"\bwhy not\b|\bwhy (wasn't|isn't|was not|is not)\b", q) and crew and disruption:
            out = self._call(calls, "why_not", {"disruption_id": disruption, "crew_id": crew})
            return done(_render_why_not(out["result"]), "why_not")

        if re.search(r"\b(explain|decision|trail|audit|who approved|why did)\b", q) and disruption:
            out = self._call(calls, "explain_decision", {"disruption_id": disruption})
            return done(_render_trail(out["result"]) if out["ok"] else out["result"]["error"], "explain")

        if re.search(r"\b(who (do we|should we|to) tell|notify|notified|who needs to know)\b", q) and disruption:
            out = self._call(calls, "who_to_tell", {"disruption_id": disruption})
            return done(_render_recipients(out["result"]), "who_to_tell")

        if re.search(r"\b(legal|allowed|can (he|she|they)|is (he|she|they) ok)\b", q) and crew and pairing:
            out = self._call(
                calls, "check_legality", {"crew_id": crew, "pairing_id": pairing, "delay_hours": _hours(q)}
            )
            return done(_render_legality(out["result"]) if out["ok"] else out["result"]["error"], "legality")

        if re.search(r"\b(delay|late|slip)\b", q) and (refs.get("flight") or refs.get("aircraft")) and _hours(q):
            out = self._call(
                calls,
                "delay_impact",
                {
                    "delay_hours": _hours(q),
                    "flight_id": refs.get("flight"),
                    "aircraft": refs.get("aircraft"),
                    "flight_date": _date_in(q),
                },
            )
            return done(_render_delay(out["result"]) if out["ok"] else out["result"]["error"], "delay")

        if re.search(r"\b(reserve|on call|standby)\b", q):
            out = self._call(
                calls, "reserves", {"on_date": _date_in(q), "base": refs.get("station"), "rank": _rank_in(q)}
            )
            return done(_render_reserves(out["result"]), "reserves")

        if re.search(r"\b(critical|hub|single point|most important station)\b", q):
            out = self._call(calls, "station_criticality", {})
            top = out["result"]["stations"][:3]
            return done(
                "Most critical stations: "
                + "; ".join(
                    f"{s['station']} (betweenness {s['betweenness']:g}, {s['movements']} movements, {s['crew_based']} crew based)"
                    for s in top
                )
                + ".",
                "criticality",
            )

        if (m := re.search(r"\bfrom ([a-z]{3}) to ([a-z]{3})\b", q)) and re.search(r"\b(route|position|get)\b", q):
            out = self._call(calls, "positioning_routes", {"origin": m.group(1), "dest": m.group(2)})
            routes = out["result"]["routes"]
            return done(
                "Fastest routes: "
                + "; ".join(f"{' > '.join(r['stations'])} ({r['block_hours']:g}h block)" for r in routes)
                if routes
                else "No route between those stations.",
                "routes",
            )

        if re.search(r"\bwho (knows|has flown)\b", q):
            out = self._call(
                calls,
                "who_knows",
                {
                    "station": refs.get("station"),
                    "aircraft_type": "ATR72" if "atr" in q else ("A320" if "a320" in q else None),
                },
            )
            if not out["ok"]:
                return done("Name a station or an aircraft type.", "who_knows")
            return done(
                "Crew who know it best: "
                + ", ".join(f"{c['name']} ({c['crew_id']}, {c['rank']})" for c in out["result"]["crew"])
                + ".",
                "who_knows",
            )

        if re.search(r"\b(cover|replace|replacement|who (do i|can i|should i) use|who covers|fill)\b", q):
            if pairing and not refs.get("crew"):
                role = _rank_in(q) or "Captain"
                out = self._call(calls, "find_cover", {"pairing_id": pairing, "role": role, "delay_hours": None})
                return done(self._render_cover(out, role, pairing), "cover")
            if crew:
                start = _date_in(q)
                # no date named: the seats they leave empty over the coming week
                window = (
                    {"from_date": start, "to_date": None}
                    if start
                    else {"from_date": day_of(self.now_ts), "to_date": add_days(day_of(self.now_ts), 6)}
                )
                out = self._call(calls, "blast_radius", {"crew_id": crew, **window})
                seats = out["result"].get("seats", []) if out["ok"] else []
                if pairing:
                    seats = [s for s in seats if s["pairing_id"] == pairing] or seats
                if not seats:
                    return done(
                        f"{crew} has no rostered duty that has not started on {out['result'].get('from', 'that day')}.",
                        "cover",
                    )
                parts = []
                for s in seats:
                    cover = self._call(
                        calls, "find_cover", {"pairing_id": s["pairing_id"], "role": s["role"], "delay_hours": None}
                    )
                    parts.append(
                        f"{s['role']} seat on {s['pairing_id']} ({len(s['uncovered'])} flights, {s['passengers']} passengers): "
                        + self._render_cover(cover, s["role"], s["pairing_id"])
                    )
                return done(" ".join(parts), "cover")
            if disruption:
                out = self._call(calls, "get_disruption", {"disruption_id": disruption})
                return done(_render_disruption(out["result"]) if out["ok"] else out["result"]["error"], "disruption")

        if re.search(r"\b(blast radius|what breaks|impact|affected)\b", q) and crew:
            out = self._call(calls, "blast_radius", {"crew_id": crew, "from_date": _date_in(q), "to_date": None})
            seats = out["result"].get("seats", [])
            if not seats:
                return done(f"Nothing rostered for {crew} on {out['result'].get('from')} is affected.", "blast_radius")
            return done(
                " ".join(
                    f"{s['pairing_id']} ({s['role']}): {len(s['uncovered'])} flights uncovered "
                    f"({', '.join(s['uncovered'])}), {s['passengers']} passengers."
                    for s in seats
                ),
                "blast_radius",
            )

        if disruption and re.search(r"\b(status|what('s| is) happening|tell me about|summary|d-\d{4})\b", q):
            out = self._call(calls, "get_disruption", {"disruption_id": disruption})
            return done(_render_disruption(out["result"]) if out["ok"] else out["result"]["error"], "disruption")

        if refs.get("crew") or re.search(r"\bwho is\b", q):
            target = refs.get("crew") or q.split("who is", 1)[-1].strip(" ?")
            out = self._call(calls, "crew_profile", {"crew": target})
            return done(_render_profile(out["result"]) if out["ok"] else out["result"]["error"], "profile")

        if re.search(r"\b(disruptions|open issues|what('s| is) open)\b", q):
            out = self._call(calls, "list_disruptions", {"status": None})
            items = out["result"]["disruptions"][:6]
            return done(
                "Latest disruptions: " + "; ".join(f"{d['id']} {d['status']} - {d['summary']}" for d in items)
                if items
                else "No disruptions on record.",
                "list",
            )

        return done(
            'I can find cover ("who covers C-1042 on 13 Oct?"), check legality ("is C-3310 legal for P-2291?"), '
            "show a disruption's blast radius, follow a delay down the rotation, explain a decision or why someone "
            "was not picked, list reserves, and tell you what is waiting for you.",
            "help",
        )

    def _render_cover(self, out: dict[str, Any], role: str, pairing: str) -> str:
        if not out["ok"]:
            return out["result"]["error"]
        r = out["result"]
        policy = self.memory.facts().get("ranking_policy", "cheapest")
        crew_opts = [o for o in r["options"] if o["crew_id"]]
        if not crew_opts:
            return f"No legal {role} for {pairing}. Fallback: {r['options'][-1]['action']} (INR {r['options'][-1]['cost_inr']:,})."
        pick_id = r["policies"].get(policy) or crew_opts[0]["crew_id"]
        pick = next(o for o in crew_opts if o["crew_id"] == pick_id)
        self._focus += [{"kind": "pairing", "ref": pairing}, {"kind": "crew", "ref": pick_id}]
        text = f"{pick['action']}, INR {pick['cost_inr']:,}"
        if pick["delay_hours"]:
            text += f", first departure ~{pick['delay_hours']:g}h late"
        if policy != "cheapest":
            text += f" (your {policy.replace('_', ' ')} preference)"
        others = [o for o in crew_opts if o["crew_id"] != pick_id][:2]
        if others:
            text += ". Next: " + "; ".join(f"{o['name']} ({o['crew_id']}) INR {o['cost_inr']:,}" for o in others)
        stages = [
            f"{s['dropped']} {s['stage'].replace('within limits', 'over limits')}" for s in r["funnel"] if s["dropped"]
        ]
        if stages:
            text += f". Ruled out: {', '.join(stages)}"
        if r["cancellation_multiple"]:
            text += f". Cancelling would cost {r['cancellation_multiple']}x more"
        return text + "."


_PRONOUN = re.compile(r"\b(she|he|they|her|him|them|that crew|this crew)\b")


def _iso(seconds: int) -> str:
    from crewops.timeutil import iso

    return iso(seconds)


def _intent_of(calls: list[dict[str, Any]]) -> str:
    return calls[-1]["tool"] if calls else "chat"


def _hours(q: str) -> float | None:
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:h\b|hours?|hrs?)", q):
        return float(m.group(1))
    if m := re.search(r"(\d+)\s*(?:min|minutes)", q):
        return round(int(m.group(1)) / 60, 2)
    return None


_MONTHS = {"oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}


def _date_in(q: str) -> str | None:
    if m := re.search(r"\b(2026-\d{2}-\d{2})\b", q):
        return m.group(1)
    if m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(oct|october|nov|november|dec|december)\b", q):
        return f"2026-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    if "tomorrow" in q:
        return "tomorrow"
    return None


def _rank_in(q: str) -> str | None:
    for words, rank in (
        (("first officer", "fo ", "co-pilot", "copilot"), "First Officer"),
        (("senior cabin", "scc", "purser"), "Senior Cabin Crew"),
        (("cabin", "flight attendant"), "Cabin Crew"),
        (("captain", "cpt"), "Captain"),
    ):
        if any(w in q for w in words):
            return rank
    return None


def _render_legality(r: dict[str, Any]) -> str:
    if r["legal"]:
        extra = " (on reserve)" if r["on_reserve"] else ""
        if r["positioning"]:
            extra += f", positioning on {r['positioning']['flight']}"
        return f"Yes. {r['crew_id']} is legal for {r['pairing_id']}{extra}: all {len({c['rule'] for c in r['checks']})} rules pass."
    reasons = "; ".join(f"{b['rule']}: {b['detail']}" for b in r["blocking"][:3])
    return f"No. {r['crew_id']} is not legal for {r['pairing_id']}. {reasons}."


def _render_delay(r: dict[str, Any]) -> str:
    text = f"A {r['delay_hours']:g}h delay carries to {len(r['legs'])} legs ({r['passengers_delayed']} passengers)."
    for d in r["duty_days"]:
        if d["breach"]:
            text += f" {d['pairing_id']} on {d['date']}: FDP {d['fdp_hours']}h breaks the {d['fdp_limit']}h limit" + (
                f"; drop {', '.join(d['legs_to_drop'])} (FDP then {d['fdp_after_drop']}h)."
                if d["legs_to_drop"]
                else "."
            )
    if not any(d["breach"] for d in r["duty_days"]):
        text += " Every crew duty stays legal."
    return text


def _render_reserves(r: dict[str, Any]) -> str:
    free = [x for x in r["reserves"] if not x["grounded"] and not x["already_flying"]]
    if not r["reserves"]:
        return f"Nobody is on call on {r['date']} for that filter."
    return (
        f"{len(free)} of {len(r['reserves'])} reserves free on {r['date']}: "
        + "; ".join(
            f"{x['rank']} {x['name']} ({x['crew_id']}, {x['airline']}, {x['base']} {x['window']})" for x in free[:8]
        )
        + "."
    )


def _render_inbox(r: dict[str, Any]) -> str:
    items = r["items"]
    parts = []
    if desk := items.get("desk"):
        parts.append(
            f"{len(desk)} open on your desk: "
            + "; ".join(f"{d['disruption']} {d['status']} - {d['summary']}" for d in desk[:4])
        )
    if approvals := items.get("approvals"):
        parts.append(
            f"{len(approvals)} plan(s) to approve: "
            + "; ".join(
                f"{a['plan']} for {a['disruption']} (INR {a['cost']:,}, from {a['submitted_by']})"
                for a in approvals[:4]
            )
        )
    if reports := items.get("my_reports"):
        parts.append("Your reports: " + "; ".join(f"{d['disruption']} {d['status']}" for d in reports[:4]))
    if crew := items.get("crew"):
        if crew["new_trips"]:
            parts.append(
                "New trips for you: "
                + "; ".join(f"{t['pairing']} as {t['role']} ({t['plan']})" for t in crew["new_trips"])
            )
        if crew["notices"]:
            parts.append("Notices: " + "; ".join(f"{n['disruption']}: {n['summary']}" for n in crew["notices"][:3]))
    return " ".join(parts) + "." if parts else "Nothing is waiting for you."


def _render_disruption(r: dict[str, Any]) -> str:
    text = f"{r['id']} ({r['type'].replace('_', ' ')}) is {r['status']}"
    if r.get("desk"):
        text += f" on {r['desk']}"
    text += f". {r['headline'] or r['summary']}"
    if r.get("joint_plan"):
        text += (
            f" Joint plan: INR {r['joint_plan']['total_cost_inr']:,} ("
            + ", ".join(a["crew_id"] for a in r["joint_plan"]["assignments"])
            + ")."
        )
    return text


def _render_trail(t: dict[str, Any]) -> str:
    d = t["disruption"]
    text = f"{d['id']} was reported by {t['reported_by']} and went to {t['desk']} ({t['controller']})."
    for g in t["gaps"]:
        if g["top"]:
            top = g["top"][0]
            text += f" For the {g['role']} seat on {g['pairing']}, {top['name']} ({top['crew']}) ranked first at INR {top['cost']:,}"
            if g["breaches"]:
                text += "; most others were ruled out by " + ", ".join(
                    f"{b['rule']} ({b['excluded']})" for b in g["breaches"][:3]
                )
            text += "."
    for p in t["plans"]:
        text += f" Plan {p['plan']} ({p['status']}) was submitted by {p['submitted_by']}"
        if p["approved_by"]:
            text += f" and approved by {p['approved_by']}, seal {p['seal'][:12]}"
        if p["rejected_by"]:
            text += f" and rejected by {p['rejected_by']}: {p['reject_reason']}"
        text += "."
    return text


def _render_why_not(r: dict[str, Any]) -> str:
    if r["legal"] is False:
        x = r["exclusions"][0]
        return f"{x['name']} ({r['crew_id']}) was ruled out at the {x['stage']} stage: {'; '.join(x['reasons'][:2])}."
    if r["legal"]:
        o = r["options"][0]
        above = ", ".join(f"{b['name']} ({b['crew']}, INR {b['cost']:,})" for b in o["ranked_above"][:3])
        return (
            f"{o['name']} ({r['crew_id']}) is legal and ranked {o['rank']} at INR {o['cost']:,}; ranked above: {above}."
        )
    return r["note"]


def _render_recipients(r: dict[str, Any]) -> str:
    rec = r["recipients"]
    if not rec:
        return "Nobody has been notified yet; the disruption may not be analysed."
    contacts = [x for x in rec if x["kind"] == "Contact"]
    crew = [x for x in rec if x["kind"] == "Crew"]
    text = "Tell " + "; ".join(f"{c['who']} ({c['title']})" for c in contacts)
    if crew:
        text += f", and the {len(crew)} crew on the trip"
    sent = sum(1 for x in rec if x["sent"])
    return text + f". {sent} of {len(rec)} notices delivered."


def _render_profile(r: dict[str, Any]) -> str:
    if "matches" in r:
        return (
            "Several crew match: " + ", ".join(f"{m['name']} ({m['crew_id']}, {m['rank']})" for m in r["matches"])
            if r["matches"]
            else "No crew by that name."
        )
    text = (
        f"{r['name']} ({r['crew_id']}) is a {r['rank']} with {r['airline']}, based at {r['base']}, rated on "
        f"{', '.join(r['ratings'])}, status {r['status']}."
    )
    if r["duties"]:
        text += " Rostered: " + ", ".join(f"{d['pairing']} on {d['date']}" for d in r["duties"][:5]) + "."
    if r["on_call"]:
        text += f" On call {len(r['on_call'])} days."
    if r["grounded_by"]:
        text += f" Grounded by {', '.join(r['grounded_by'])}."
    return text
