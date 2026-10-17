"""Triage and notifier agents.

    uv run python -m crewops.agents.triage --workers 2
    uv run python -m crewops.agents.triage --once

A failed analysis is released for retry and parked after three attempts."""

from __future__ import annotations

import argparse
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from falkordb import Graph

from crewops.agents import coordinator
from crewops.disruptions import analyse
from crewops.graph import Graphs, run, tracing
from crewops.legality import Context

PENDING_NOTICES = """
MATCH (d:Disruption)-[n:NOTIFIES]->(t)
WHERE n.sent_ts IS NULL
RETURN d.id AS disruption, d.summary AS summary, labels(t)[0] AS kind,
       coalesce(t.name, t.id) AS recipient, n.reason AS reason
ORDER BY disruption
LIMIT $limit
"""

MARK_SENT = """
MATCH (d:Disruption {id: $id})-[n:NOTIFIES]->(t)
WHERE n.sent_ts IS NULL
SET n.sent_ts = $now, n.sent_by = $agent
RETURN count(n) AS sent
"""


@dataclass
class TriageAgent:
    graph: Graph
    name: str = "triage-1"
    log: list[dict[str, Any]] = field(default_factory=list)

    def step(self) -> dict[str, Any] | None:
        """Claim and analyse one disruption. None when nothing is waiting."""
        ctx = Context.open(self.graph)
        coordinator.register(self.graph, self.name, "triage", ctx.now_ts)
        claim = coordinator.claim_next(self.graph, self.name, ctx.now_ts)
        if claim is None:
            return None
        with tracing() as trace:
            try:
                result = analyse(self.graph, claim["id"], actor=self.name, ctx=ctx)
            except Exception as exc:  # released for another agent; parked after MAX_ATTEMPTS
                status = coordinator.release_failed(self.graph, claim["id"], f"{type(exc).__name__}: {exc}")
                entry = {"disruption": claim["id"], "error": str(exc), "status": status}
                self.log.append(entry)
                return entry
        coordinator.handoff(
            self.graph, self.name, claim["id"], result["headline"][:300], ctx.now_ts, desk=result.get("desk")
        )
        entry = {
            "disruption": claim["id"],
            "desk": result.get("desk"),
            "headline": result["headline"],
            "queries": len(trace.queries),
        }
        self.log.append(entry)
        return entry

    def drain(self) -> list[dict[str, Any]]:
        out = []
        while (entry := self.step()) is not None:
            out.append(entry)
        return out


@dataclass
class NotifierAgent:
    graph: Graph
    name: str = "notifier-1"

    def step(self, limit: int = 200) -> list[dict[str, Any]]:
        ctx = Context.open(self.graph)
        coordinator.register(self.graph, self.name, "notifier", ctx.now_ts)
        pending = run(self.graph, "pending_notices", PENDING_NOTICES, {"limit": limit}, read_only=True)
        for disruption in sorted({p["disruption"] for p in pending}):
            run(self.graph, "mark_sent", MARK_SENT, {"id": disruption, "now": ctx.now_ts, "agent": self.name})
        return pending


def run_workers(gs: Graphs, workers: int, once: bool, poll_seconds: float = 2.0) -> None:
    stop = threading.Event()

    def loop(i: int) -> None:
        agent = TriageAgent(gs.ops, f"triage-{i}")
        while not stop.is_set():
            entry = agent.step()
            if entry:
                print(f"[{agent.name}] {entry['disruption']}: {entry.get('headline') or entry.get('error')}")
            elif once:
                return
            else:
                stop.wait(poll_seconds)

    threads = [threading.Thread(target=loop, args=(i + 1,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    notifier = NotifierAgent(gs.ops)
    try:
        while any(t.is_alive() for t in threads):
            for n in notifier.step():
                print(f"[{notifier.name}] {n['disruption']} -> {n['kind']} {n['recipient']}")
            time.sleep(0.2 if once else poll_seconds)
        for n in notifier.step():
            print(f"[{notifier.name}] {n['disruption']} -> {n['kind']} {n['recipient']}")
    except KeyboardInterrupt:
        stop.set()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the triage and notifier agents")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    args = parser.parse_args(argv)
    run_workers(Graphs(), args.workers, args.once)


if __name__ == "__main__":
    main()
