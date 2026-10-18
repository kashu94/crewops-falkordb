"""Assistant: planner answers, memory follow-ups and the Claude tool loop."""

from types import SimpleNamespace

from crewops.agents.assistant import Assistant
from crewops.agents.triage import TriageAgent
from crewops.disruptions import report


def test_cover_then_follow_up_resolves_from_memory(fresh):
    bot = Assistant(fresh, "ananya.iyer", use_model=False)
    first = bot.ask("Captain C-1042 is sick for P-2291. Who do I use?")
    assert "C-3310" in first.text and "23,090" in first.text
    follow = bot.ask("Is she legal for it?")
    assert follow.text.startswith("Yes. C-3310 is legal for P-2291")
    assert follow.tool_calls[0]["input"] == {"crew_id": "C-3310", "pairing_id": "P-2291", "delay_hours": None}


def test_preferences_persist_and_change_the_answer(fresh):
    bot = Assistant(fresh, "ananya.iyer", use_model=False)
    bot.ask("I prefer least delay from now on")
    again = Assistant(fresh, "ananya.iyer", use_model=False)  # a new session
    assert "least delay preference" in again.ask("Who covers P-2291 captain?").text


def test_new_session_welcomes_back_with_inbox(fresh):
    report(fresh.ops, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-10-13"}, "imran.sheikh")
    TriageAgent(fresh.ops).drain()
    Assistant(fresh, "ananya.iyer", use_model=False).ask("Which station is most critical?")
    hello = Assistant(fresh, "ananya.iyer", use_model=False).ask("Hello")
    assert hello.text.startswith("Welcome back.")
    assert "D-0001" in hello.text


def test_answers_carry_evidence(fresh):
    answer = Assistant(fresh, "ananya.iyer", use_model=False).ask("Is C-3310 legal for P-2291?")
    assert answer.evidence and answer.as_dict()["tool_calls"][0]["tool"] == "check_legality"


class _FakeMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs["model"] == "claude-opus-5-5"
        assert all(t["strict"] for t in kwargs["tools"])
        if self.calls == 1:
            block = SimpleNamespace(
                type="tool_use",
                id="t1",
                name="check_legality",
                input={"crew_id": "C-3310", "pairing_id": "P-2291", "delay_hours": None},
            )
            return SimpleNamespace(stop_reason="tool_use", content=[block])
        last = kwargs["messages"][-1]["content"][0]
        assert last["type"] == "tool_result" and '"legal": true' in last["content"]
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Yes, C-3310 is legal.")]
        )


def test_claude_drives_the_graph_tools(fresh, monkeypatch):
    fake = _FakeMessages()
    monkeypatch.setattr("anthropic.Anthropic", lambda: SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    answer = Assistant(fresh, "ananya.iyer", use_model=True).ask("Is C-3310 legal for P-2291?")
    assert answer.text == "Yes, C-3310 is legal."
    assert answer.engine == "claude-opus-5-5"
    assert answer.tool_calls[0]["tool"] == "check_legality"


def test_model_outage_falls_back_to_the_planner(fresh, monkeypatch):
    def broken():
        raise ConnectionError("no network")

    monkeypatch.setattr("anthropic.Anthropic", broken)
    answer = Assistant(fresh, "ananya.iyer", use_model=True).ask("Is C-3310 legal for P-2291?")
    assert answer.engine.startswith("planner (model unavailable")
    assert answer.text.startswith("Yes.")


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_open_model_api_drives_the_graph_tools(fresh, monkeypatch):
    from crewops.config import Settings

    sent = []

    def fake_post(url, headers, json, timeout):
        sent.append(json)
        assert url == "https://api.groq.com/openai/v1/chat/completions"
        assert headers["Authorization"] == "Bearer test-key"
        if len(sent) == 1:
            call = {
                "id": "c1",
                "type": "function",
                "function": {"name": "check_legality", "arguments": '{"crew_id": "C-3310", "pairing_id": "P-2291"}'},
            }
            return _FakeResponse({"choices": [{"message": {"content": "", "tool_calls": [call]}}]})
        assert json["messages"][-1]["role"] == "tool" and '"legal":true' in json["messages"][-1]["content"]
        return _FakeResponse({"choices": [{"message": {"content": "<think>x</think>Yes, C-3310 is legal."}}]})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr("crewops.agents.assistant.settings", lambda: Settings(llm_api_key="test-key"))
    answer = Assistant(fresh, "ananya.iyer").ask("Is C-3310 legal for P-2291?")
    assert answer.text == "Yes, C-3310 is legal."
    assert answer.engine == "qwen/qwen3.8-27b"
    assert sent[0]["tools"][0]["type"] == "function"


def test_rate_limited_model_falls_through_to_the_next(fresh, monkeypatch):
    from crewops.config import Settings

    used = []

    def fake_post(url, headers, json, timeout):
        used.append(json["model"])
        if json["model"] == "first":
            return _FakeResponse({"error": "rate limit"}, status_code=429)
        return _FakeResponse({"choices": [{"message": {"content": "Hello from the second model."}}]})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(
        "crewops.agents.assistant.settings", lambda: Settings(llm_api_key="k", llm_model="first, second")
    )
    answer = Assistant(fresh, "ananya.iyer").ask("hello there")
    assert used == ["first", "second"]
    assert answer.engine == "second"


def test_crew_can_ask_for_their_next_flight(fresh):
    answer = Assistant(fresh, "c1042.nair", use_model=False).ask("When is my next flight?")
    assert answer.text.startswith("A. Nair (C-1042): next duty 2026-10-13 on P-2291")
    assert "KS412 BLR-BOM 07:00" in answer.text
