"""FalkorDB access.

The operation lives in one graph (``crewops``). Each user gets a separate memory graph
(``crewops_memory_<user>``). All queries go through ``run``, which records each query on the
active ``Trace`` so answers can list the Cypher behind them."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from falkordb import FalkorDB, Graph
from falkordb.edge import Edge
from falkordb.node import Node
from falkordb.path import Path

from crewops.config import Settings, settings


@dataclass(slots=True)
class QueryRecord:
    name: str
    graph: str
    cypher: str
    params: dict[str, Any]
    rows: int
    ms: float


@dataclass(slots=True)
class Trace:
    """Queries run while answering one question or running one agent step."""

    queries: list[QueryRecord] = field(default_factory=list)

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "query": q.name,
                "graph": q.graph,
                "rows": q.rows,
                "ms": round(q.ms, 2),
                "cypher": " ".join(q.cypher.split()),
            }
            for q in self.queries
        ]


_trace: ContextVar[Trace | None] = ContextVar("crewops_trace", default=None)


@contextmanager
def tracing() -> Iterator[Trace]:
    """Collect every query run inside the block."""
    trace = Trace()
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        _trace.reset(token)


def _plain(value: Any) -> Any:
    """Graph values to plain Python: nodes and edges become their properties."""
    if isinstance(value, Node):
        return {"_labels": list(value.labels), **value.properties}
    if isinstance(value, Edge):
        return {"_type": value.relation, **value.properties}
    if isinstance(value, Path):
        return {"nodes": [_plain(n) for n in value.nodes()], "edges": [_plain(e) for e in value.edges()]}
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def run(
    graph: Graph,
    name: str,
    cypher: str,
    params: Mapping[str, Any] | None = None,
    *,
    read_only: bool = False,
) -> list[dict[str, Any]]:
    """Run one named query and return its rows as dicts keyed by column name."""
    started = time.perf_counter()
    params = dict(params or {})
    result = graph.ro_query(cypher, params) if read_only else graph.query(cypher, params)
    columns = [h[1] for h in result.header] if result.header else []
    rows = [dict(zip(columns, (_plain(v) for v in row), strict=True)) for row in result.result_set]
    trace = _trace.get()
    if trace is not None:
        trace.queries.append(
            QueryRecord(name, graph.name, cypher, params, len(rows), (time.perf_counter() - started) * 1000)
        )
    return rows


_SAFE = re.compile(r"[^a-z0-9_]")


def memory_graph_name(username: str, prefix: str) -> str:
    """``crewops_memory_ananya_iyer`` for ``ananya.iyer``."""
    return prefix + _SAFE.sub("_", username.lower())


class Graphs:
    """The FalkorDB connection and the graphs this app uses."""

    def __init__(
        self,
        cfg: Settings | None = None,
        *,
        ops_graph: str | None = None,
        memory_prefix: str | None = None,
    ) -> None:
        self.cfg = cfg or settings()
        self.db = FalkorDB(
            host=self.cfg.falkordb_host,
            port=self.cfg.falkordb_port,
            username=self.cfg.falkordb_username,
            password=self.cfg.falkordb_password,
        )
        self.ops_name = ops_graph or self.cfg.ops_graph
        self.memory_prefix = memory_prefix or self.cfg.memory_prefix

    @property
    def ops(self) -> Graph:
        return self.db.select_graph(self.ops_name)

    def memory(self, username: str) -> Graph:
        return self.db.select_graph(memory_graph_name(username, self.memory_prefix))

    def memory_graphs(self) -> list[str]:
        return sorted(g for g in self.db.list_graphs() if g.startswith(self.memory_prefix))

    def ping(self) -> bool:
        try:
            return bool(self.db.connection.ping())
        except Exception:
            return False


@lru_cache(maxsize=1)
def graphs() -> Graphs:
    return Graphs()
