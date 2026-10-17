from crewops.config import SEED_NOW, Settings, parse_utc
from crewops.graph import memory_graph_name, run, tracing


def test_settings_default_to_the_seed_clock(monkeypatch, tmp_path):
    monkeypatch.delenv("CREWOPS_NOW", raising=False)
    cfg = Settings.from_env(tmp_path / "missing.env")
    assert cfg.now == SEED_NOW
    assert cfg.ops_graph == "crewops"


def test_env_file_is_read_and_shell_wins(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("FALKORDB_PORT=7000\nCREWOPS_OPS_GRAPH=from_file\n")
    monkeypatch.setenv("CREWOPS_OPS_GRAPH", "from_shell")
    monkeypatch.delenv("FALKORDB_PORT", raising=False)
    cfg = Settings.from_env(env)
    assert cfg.falkordb_port == 7000
    assert cfg.ops_graph == "from_shell"


def test_parse_utc_accepts_z():
    assert parse_utc("2026-10-12T18:00:00Z") == SEED_NOW


def test_memory_graph_names_are_safe():
    assert memory_graph_name("Ananya.Iyer", "m_") == "m_ananya_iyer"


def test_run_records_queries_on_the_trace(gs):
    g = gs.db.select_graph("crewops_test_probe")
    with tracing() as trace:
        rows = run(g, "probe", "RETURN $x AS x, {a: 1} AS m", {"x": 2})
    assert rows == [{"x": 2, "m": {"a": 1}}]
    assert trace.summary()[0]["query"] == "probe"
