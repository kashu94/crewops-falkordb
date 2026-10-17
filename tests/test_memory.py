from crewops.memory import Memory, extract_entities


def test_entities_are_normalised():
    found = extract_entities("Is c3310 legal for P2291? and DX401-2026-10-14 at BLR, plan PL-0002")
    assert {"kind": "crew", "ref": "C-3310"} in found
    assert {"kind": "pairing", "ref": "P-2291"} in found
    assert {"kind": "flight", "ref": "DX401-2026-10-14"} in found
    assert {"kind": "station", "ref": "BLR"} in found
    assert {"kind": "plan", "ref": "PL-0002"} in found


def test_turns_chain_and_resolve_follow_ups(fresh):
    m = Memory(fresh, "ananya.iyer")
    s = m.start_session(1000)
    m.record_turn(s, "Who covers C-1042 on P-2291?", "C-3310 on reserve", 1001, intent="cover")
    m.record_turn(s, "Is she legal?", "Yes", 1002, intent="legality", entities=[])
    assert [t["seq"] for t in m.recent_turns(s)] == [1, 2]
    assert m.last_entity("crew", s) == "C-3310"  # the answer mentioned C-3310 last
    assert m.last_entity("pairing") == "P-2291"


def test_memory_survives_sessions_and_stays_per_user(fresh):
    m = Memory(fresh, "ananya.iyer")
    first = m.start_session(1000)
    m.record_turn(first, "Blast radius of D-0001?", "6 flights", 1001)
    second = m.start_session(2000)
    prev = m.previous_session(second)
    assert prev["session"] == first
    assert prev["turns"][0]["refs"] == ["D-0001"]
    other = Memory(fresh, "rohit.malhotra")
    other.start_session(1500)
    assert other.last_entity("disruption") is None
    assert {g for g in fresh.memory_graphs()} >= {
        "crewops_test_memory_ananya_iyer",
        "crewops_test_memory_rohit_malhotra",
    }


def test_facts_are_superseded_not_overwritten(fresh):
    m = Memory(fresh, "ananya.iyer")
    s = m.start_session(1000)
    seq = m.record_turn(s, "I prefer least delay", "Noted", 1001)
    m.remember("ranking_policy", "least_delay", 1001, s, seq)
    m.remember("ranking_policy", "cheapest", 1100)
    assert m.facts() == {"ranking_policy": "cheapest"}
    history = m.fact_history("ranking_policy")
    assert [h["value"] for h in history] == ["cheapest", "least_delay"]
    assert history[1]["learned_from"] == "I prefer least delay"


def test_forget_drops_only_that_users_graph(fresh):
    a, b = Memory(fresh, "ananya.iyer"), Memory(fresh, "divya.rao")
    a.record_turn(a.start_session(1), "hi", "hello", 2)
    b.record_turn(b.start_session(1), "hi", "hello", 2)
    a.forget()
    assert "crewops_test_memory_ananya_iyer" not in fresh.memory_graphs()
    assert b.stats()["turns"] == 1
