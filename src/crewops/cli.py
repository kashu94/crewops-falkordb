"""CrewOps command line.

crewops load --replace                         load the seed operation
crewops report crew_unavailable --as imran.sheikh --details '{"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14"}'
crewops triage --once                          run the triage and notifier agents
crewops ask --as ananya.iyer "Who covers C-1042 on 13 Oct?"
crewops ask --as ananya.iyer                   interactive, one session
crewops plan submit D-0001 --as ananya.iyer
crewops plan approve PL-0001 --as meera.bhatt
crewops explain D-0001
crewops demo                                   run the full demo
crewops mcp --user ananya.iyer                 MCP server on stdio
crewops serve                                  web console on :8000
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from crewops.graph import Graphs, graphs, tracing


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_load(args: argparse.Namespace) -> None:
    from crewops.seed import main as seed_main

    seed_main(["--replace"] if args.replace else [])


def cmd_report(args: argparse.Namespace) -> None:
    from crewops.disruptions import report

    print(report(graphs().ops, args.kind, json.loads(args.details), args.as_user))


def cmd_triage(args: argparse.Namespace) -> None:
    from crewops.agents.triage import run_workers

    run_workers(graphs(), args.workers, args.once)


def cmd_ask(args: argparse.Namespace) -> None:
    from crewops.agents.assistant import Assistant

    bot = Assistant(graphs(), args.as_user, session_id=args.session, use_model=None if not args.no_model else False)

    def one(question: str) -> None:
        answer = bot.ask(question)
        print(answer.text)
        if args.evidence:
            for q in answer.evidence:
                print(f"  - {q['query']} ({q['rows']} rows, {q['ms']} ms)")
        print(f"  [{answer.engine}; tools: {', '.join(c['tool'] for c in answer.tool_calls) or 'none'}]")

    if args.question:
        one(" ".join(args.question))
        return
    print(f"CrewOps as {bot.caller.name} (session {bot.session_id}). Ctrl-D to leave.")
    for line in sys.stdin:
        if line.strip():
            one(line)


def cmd_plan(args: argparse.Namespace) -> None:
    from crewops import plans

    g = graphs().ops
    if args.action == "submit":
        _print(plans.submit(g, args.target, args.as_user, note=args.note or ""))
    elif args.action == "approve":
        _print(plans.approve(g, args.target, args.as_user))
    elif args.action == "reject":
        _print(plans.reject(g, args.target, args.as_user, args.note or "rejected"))
    elif args.action == "close":
        _print(plans.close(g, args.target, args.as_user, args.note or "closed"))


def cmd_explain(args: argparse.Namespace) -> None:
    from crewops.explain import decision_trail, why_not

    g = graphs().ops
    _print(why_not(g, args.disruption, args.why_not) if args.why_not else decision_trail(g, args.disruption))


def cmd_mcp(args: argparse.Namespace) -> None:
    from crewops.mcp_server import main as mcp_main

    mcp_main(["--user", args.user] + (["--http", "--port", str(args.port)] if args.http else []))


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("crewops.api:app", host=args.host, port=args.port, reload=False)


def cmd_demo(args: argparse.Namespace) -> None:
    """Full demo on a fresh graph."""
    from crewops.agents.assistant import Assistant
    from crewops.agents.triage import NotifierAgent, TriageAgent
    from crewops.disruptions import report
    from crewops.plans import approve, submit
    from crewops.seed import load

    gs: Graphs = graphs()
    step = 0

    def say(title: str) -> None:
        nonlocal step
        step += 1
        print(f"\n== {step}. {title}")

    say("Load the operation into FalkorDB")
    counts = load(gs.ops, replace=True)
    for name in gs.memory_graphs():
        gs.db.select_graph(name).delete()
    print(", ".join(f"{n} {label}" for label, n in counts.items()))

    say("01:30. Imran (reporter) logs Captain C-1042 sick for 13-14 Oct")
    d = report(
        gs.ops,
        "crew_unavailable",
        {"crew_id": "C-1042", "from_date": "2026-10-13", "to_date": "2026-10-14", "reason": "sick"},
        "imran.sheikh",
    )
    print(f"{d} REPORTED")

    say("The triage agent claims it, traverses the graph, writes options back, hands off to a desk")
    for entry in TriageAgent(gs.ops).drain():
        print(f"{entry['disruption']} -> {entry['desk']} ({entry['queries']} Cypher queries)\n{entry['headline']}")
    for n in NotifierAgent(gs.ops).step():
        print(f"  notified {n['kind']}: {n['recipient']}")

    ananya = Assistant(gs, "ananya.iyer", use_model=False if args.no_model else None)
    for q in (
        "Anything waiting for me?",
        "Captain C-1042 is sick for P-2291. Who do I use?",
        "Is she legal for it?",
        f"Why not C-2087 for {d}?",
    ):
        say(f"Ananya asks: {q}")
        with tracing():
            answer = ananya.ask(q)
        print(answer.text)
        print(
            f"  [{answer.engine}; {len(answer.evidence)} queries: {', '.join(sorted({e['query'] for e in answer.evidence}))}]"
        )

    say("Ananya submits the plan; Meera (a different person, an approver) approves it")
    plan = submit(gs.ops, d, "ananya.iyer")
    print(f"{plan['plan_id']} submitted, fingerprint {plan['fingerprint'][:16]}")
    result = approve(gs.ops, plan["plan_id"], "meera.bhatt")
    print(f"{result['plan_id']} {result['status']}, seal {result['seal'][:16]}: the roster changed in one Cypher write")

    say("A new session the next morning: Ananya's memory graph remembers")
    later = Assistant(gs, "ananya.iyer", use_model=False if args.no_model else None)
    print(later.ask("Hello").text)
    print(later.ask(f"Explain the decision on {d}").text)

    say("Graph algorithms as tools")
    print(later.ask("Which station is most critical?").text)
    print(later.ask("Best route from GOI to CCU to position a crew?").text)
    print(f"\nMemory graphs: {', '.join(gs.memory_graphs())}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="crewops", description="Crew-control agents on FalkorDB")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="load the seed operation")
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("report", help="report a disruption")
    p.add_argument("kind")
    p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--details", required=True, help="JSON")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("triage", help="run the triage and notifier agents")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("ask", help="ask the assistant")
    p.add_argument("question", nargs="*")
    p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--session")
    p.add_argument("--evidence", action="store_true", help="print the Cypher behind the answer")
    p.add_argument("--no-model", action="store_true", help="planner only, even with an API key")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("plan", help="submit, approve, reject or close")
    p.add_argument("action", choices=["submit", "approve", "reject", "close"])
    p.add_argument("target", help="disruption id (submit, close) or plan id (approve, reject)")
    p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("explain", help="decision trail, or why one crew member was not picked")
    p.add_argument("disruption")
    p.add_argument("--why-not")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("demo", help="run the whole story end to end")
    p.add_argument("--no-model", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("mcp", help="serve the tools over MCP")
    p.add_argument("--user", required=True)
    p.add_argument("--http", action="store_true")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("serve", help="web console")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
