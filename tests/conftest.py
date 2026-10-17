"""Tests need a running FalkorDB and use their own graphs (crewops_test*).
They are skipped if FalkorDB is not reachable."""

from __future__ import annotations

import pytest

from crewops.config import Settings
from crewops.graph import Graphs

TEST_OPS = "crewops_test"
TEST_MEMORY = "crewops_test_memory_"


def _graphs() -> Graphs:
    return Graphs(Settings.from_env(), ops_graph=TEST_OPS, memory_prefix=TEST_MEMORY)


@pytest.fixture(scope="session")
def gs() -> Graphs:
    g = _graphs()
    if not g.ping():
        pytest.skip("FalkorDB is not reachable (set FALKORDB_HOST / FALKORDB_PORT)")
    return g


@pytest.fixture(scope="session")
def loaded(gs: Graphs) -> Graphs:
    """The seed operation, loaded once per test session."""
    from crewops.seed import load

    load(gs.ops, replace=True)
    return gs


@pytest.fixture
def fresh(gs: Graphs) -> Graphs:
    """A freshly loaded operation for tests that change it."""
    from crewops.seed import load

    load(gs.ops, replace=True)
    for name in gs.memory_graphs():
        gs.db.select_graph(name).delete()
    return gs


@pytest.fixture(scope="session")
def scenarios() -> dict:
    """Scenarios with expected results (data/eval/scenarios.json)."""
    import json

    from crewops.config import DATA

    return {s["scenario_id"]: s for s in json.loads((DATA / "eval" / "scenarios.json").read_text())}
