"""Per-user memory, stored in a separate graph for each user.

- Sessions and turns, with MENTIONS edges to the ids each turn talked about.
  This is how "is she legal?" finds the last crew member mentioned.
- Facts (preferences). A new value supersedes the old one instead of overwriting it.
- The playbook (shared, in the operation graph): strategies chosen in approved plans."""

from __future__ import annotations

import re
import uuid
from typing import Any

from falkordb import Graph

from crewops.graph import Graphs, run
from crewops.schema import MEMORY_INDEXES, ensure_indexes

ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("plan", re.compile(r"\bPL-\d{4}\b", re.I)),
    ("disruption", re.compile(r"\bD-\d{4}\b", re.I)),
    ("crew", re.compile(r"\bC-?\d{4}\b", re.I)),
    ("pairing", re.compile(r"\bP-?\d{4}\b", re.I)),
    ("flight", re.compile(r"\b(?:DX|KS|NG)\d{3}(?:-\d{4}-\d{2}-\d{2})?\b", re.I)),
    ("aircraft", re.compile(r"\bVT-DX[A-F]\b", re.I)),
    ("station", re.compile(r"\b(?:BLR|BOM|CCU|COK|DEL|GOI|HYD|MAA)\b")),
)


def _normalise(kind: str, ref: str) -> str:
    ref = ref.upper()
    if kind in ("crew", "pairing") and "-" not in ref:
        return f"{ref[0]}-{ref[1:]}"
    return ref


def extract_entities(*texts: str) -> list[dict[str, str]]:
    """Ids named in a question or answer, in order of first mention (question first), so
    the last one is the most recent thing said."""
    seen: dict[tuple[str, str], None] = {}
    for text in texts:
        if not text:
            continue
        for kind, pattern in ENTITY_PATTERNS:
            for m in pattern.finditer(text):
                seen.setdefault((kind, _normalise(kind, m.group(0))), None)
    return [{"kind": k, "ref": r} for k, r in seen]


# --- Cypher ---------------------------------------------------------------------------------

START_SESSION = "CREATE (s:Session {id: $id, started_ts: $now, last_ts: $now}) RETURN s.id AS id"

LATEST_SESSION = "MATCH (s:Session) RETURN s.id AS id, s.last_ts AS last_ts ORDER BY s.last_ts DESC LIMIT 1"

RECORD_TURN = """
MATCH (s:Session {id: $session})
OPTIONAL MATCH (s)-[:HAS]->(t:Turn)
WITH s, count(t) AS n
CREATE (s)-[:HAS]->(nt:Turn {seq: n + 1, ts: $now, question: $question, answer: $answer,
                             intent: $intent, tools: $tools, session: $session})
SET s.last_ts = $now
WITH s, nt
OPTIONAL MATCH (s)-[:HAS]->(prev:Turn {seq: nt.seq - 1})
FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:NEXT]->(nt))
WITH nt
UNWIND (CASE WHEN size($entities) = 0 THEN [null] ELSE $entities END) AS e
FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
  MERGE (en:Entity {kind: e.kind, ref: e.ref})
  MERGE (nt)-[m:MENTIONS]->(en)
  SET m.pos = e.pos)
RETURN DISTINCT nt.seq AS seq
"""

RECENT_TURNS = """
MATCH (s:Session {id: $session})-[:HAS]->(t:Turn)
RETURN t.seq AS seq, t.ts AS ts, t.question AS question, t.answer AS answer, t.intent AS intent
ORDER BY seq DESC LIMIT $limit
"""

LAST_ENTITY = """
MATCH (t:Turn)-[m:MENTIONS]->(e:Entity {kind: $kind})
WHERE $session IS NULL OR t.session = $session
RETURN e.ref AS ref, t.ts AS ts, t.seq AS seq, m.pos AS pos
ORDER BY ts DESC, seq DESC, pos DESC LIMIT 1
"""

PREVIOUS_SESSION = """
MATCH (s:Session) WHERE s.id <> $session
WITH s ORDER BY s.last_ts DESC LIMIT 1
MATCH (s)-[:HAS]->(t:Turn)
OPTIONAL MATCH (t)-[:MENTIONS]->(e:Entity)
WITH s, t, collect(e.ref) AS refs ORDER BY t.seq
RETURN s.id AS session, s.last_ts AS last_ts,
       collect({seq: t.seq, question: t.question, intent: t.intent, refs: refs}) AS turns
"""

REMEMBER_FACT = """
OPTIONAL MATCH (old:Fact {key: $key, active: true})
WITH collect(old) AS olds
CREATE (f:Fact {key: $key, value: $value, ts: $now, active: true, source: $source})
FOREACH (o IN olds | SET o.active = false CREATE (f)-[:SUPERSEDES]->(o))
WITH f
OPTIONAL MATCH (t:Turn {session: $session, seq: $seq})
FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | CREATE (f)-[:LEARNED_IN]->(t))
RETURN f.value AS value
"""

ACTIVE_FACTS = "MATCH (f:Fact {active: true}) RETURN f.key AS key, f.value AS value, f.ts AS ts ORDER BY key"

FACT_HISTORY = """
MATCH (f:Fact {key: $key})
OPTIONAL MATCH (f)-[:LEARNED_IN]->(t:Turn)
RETURN f.value AS value, f.ts AS ts, f.active AS active, t.question AS learned_from
ORDER BY ts DESC
"""

MENTIONS_OF = """
MATCH (t:Turn)-[:MENTIONS]->(:Entity {ref: $ref})
RETURN t.session AS session, t.seq AS seq, t.ts AS ts, t.question AS question, t.answer AS answer
ORDER BY ts DESC LIMIT $limit
"""

RELATED_ENTITIES = """
MATCH (:Entity {ref: $ref})<-[:MENTIONS]-(t:Turn)-[:MENTIONS]->(other:Entity)
WHERE other.ref <> $ref
RETURN other.kind AS kind, other.ref AS ref, count(t) AS together
ORDER BY together DESC LIMIT 10
"""

STATS = """
OPTIONAL MATCH (s:Session) WITH count(s) AS sessions
OPTIONAL MATCH (t:Turn) WITH sessions, count(t) AS turns
OPTIONAL MATCH (e:Entity) WITH sessions, turns, count(e) AS entities
OPTIONAL MATCH (f:Fact {active: true})
RETURN sessions, turns, entities, count(f) AS facts
"""

PLAYBOOK = """
MATCH (b:Playbook)-[c:CHOSE]->(s:Strategy)
WHERE $type IS NULL OR b.type = $type
RETURN b.type AS type, b.role AS role, s.name AS strategy, c.count AS times
ORDER BY type, role, times DESC
"""


class Memory:
    """One user's memory graph."""

    def __init__(self, gs: Graphs, username: str) -> None:
        self.username = username
        self.graph: Graph = gs.memory(username)
        self._ops = gs.ops
        self._ready = False

    def _ensure(self) -> None:
        if not self._ready:
            run(self.graph, "memory_init", "MERGE (o:Owner {username: $u})", {"u": self.username})
            ensure_indexes(self.graph, MEMORY_INDEXES)
            self._ready = True

    def start_session(self, now_ts: int, session_id: str | None = None) -> str:
        self._ensure()
        sid = session_id or uuid.uuid4().hex[:12]
        run(self.graph, "start_session", START_SESSION, {"id": sid, "now": now_ts})
        return sid

    def latest_session(self) -> dict[str, Any] | None:
        self._ensure()
        rows = run(self.graph, "latest_session", LATEST_SESSION, read_only=True)
        return rows[0] if rows else None

    def record_turn(
        self,
        session_id: str,
        question: str,
        answer: str,
        now_ts: int,
        intent: str = "",
        tools: list[str] | None = None,
        entities: list[dict[str, str]] | None = None,
    ) -> int:
        self._ensure()
        entities = entities if entities is not None else extract_entities(question, answer)
        entities = [{"kind": e["kind"], "ref": e["ref"], "pos": i} for i, e in enumerate(entities)]
        rows = run(
            self.graph,
            "record_turn",
            RECORD_TURN,
            {
                "session": session_id,
                "question": question,
                "answer": answer,
                "now": now_ts,
                "intent": intent,
                "tools": tools or [],
                "entities": entities,
            },
        )
        return rows[0]["seq"]

    def recent_turns(self, session_id: str, limit: int = 6) -> list[dict[str, Any]]:
        self._ensure()
        rows = run(self.graph, "recent_turns", RECENT_TURNS, {"session": session_id, "limit": limit}, read_only=True)
        return list(reversed(rows))

    def last_entity(self, kind: str, session_id: str | None = None) -> str | None:
        self._ensure()
        rows = run(self.graph, "last_entity", LAST_ENTITY, {"kind": kind, "session": session_id}, read_only=True)
        return rows[0]["ref"] if rows else None

    def previous_session(self, current: str) -> dict[str, Any] | None:
        self._ensure()
        rows = run(self.graph, "previous_session", PREVIOUS_SESSION, {"session": current}, read_only=True)
        return rows[0] if rows else None

    def remember(
        self, key: str, value: str, now_ts: int, session_id: str | None = None, seq: int | None = None
    ) -> None:
        self._ensure()
        run(
            self.graph,
            "remember_fact",
            REMEMBER_FACT,
            {
                "key": key,
                "value": value,
                "now": now_ts,
                "source": f"{session_id}#{seq}" if session_id else "",
                "session": session_id,
                "seq": seq,
            },
        )

    def facts(self) -> dict[str, str]:
        self._ensure()
        return {r["key"]: r["value"] for r in run(self.graph, "active_facts", ACTIVE_FACTS, read_only=True)}

    def fact_history(self, key: str) -> list[dict[str, Any]]:
        self._ensure()
        return run(self.graph, "fact_history", FACT_HISTORY, {"key": key}, read_only=True)

    def mentions_of(self, ref: str, limit: int = 5) -> list[dict[str, Any]]:
        self._ensure()
        return run(self.graph, "mentions_of", MENTIONS_OF, {"ref": ref.upper(), "limit": limit}, read_only=True)

    def related(self, ref: str) -> list[dict[str, Any]]:
        self._ensure()
        return run(self.graph, "related_entities", RELATED_ENTITIES, {"ref": ref.upper()}, read_only=True)

    def stats(self) -> dict[str, int]:
        self._ensure()
        return run(self.graph, "memory_stats", STATS, read_only=True)[0]

    def playbook(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Shared procedural memory: what approvers chose before, by gap kind and role."""
        return run(self._ops, "playbook", PLAYBOOK, {"type": kind}, read_only=True)

    def forget(self) -> None:
        """Drop this user's memory graph entirely."""
        try:
            self.graph.delete()
        except Exception as exc:
            if "empty key" not in str(exc).lower() and "invalid graph" not in str(exc).lower():
                raise
        self._ready = False
