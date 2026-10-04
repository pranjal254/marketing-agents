"""The Beta assistant: the tool loop, its budget, what it refuses to reach,
conversation persistence, and the telemetry it leaves behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge import ask_store
from c2c_bridge.app import create_app
from c2c_bridge.ask_routes import ASSISTANT_AGENT_ID, MAX_STEPS


def _call(tool: str, **args: Any) -> str:
    return json.dumps({"tool": tool, "args": args, "why": "looking it up"})


def _answer(text: str, refs: list[dict] | None = None, screen: str | None = None) -> str:
    return json.dumps(
        {
            "answer": text,
            "references": refs or [],
            "suggested_screen": screen,
            "confidence": 0.9,
        }
    )


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> TestClient:
    """A provider that walks a fixed script of model replies, one per step."""
    from shiftai_shared.llm import LLMResponse

    import c2c_bridge.app as app_mod

    class ScriptedProvider:
        def __init__(self) -> None:
            self.replies = list(replies)
            self.calls: list[dict[str, Any]] = []

        def complete(self, **kwargs: Any) -> LLMResponse:
            self.calls.append(kwargs)
            text = self.replies.pop(0) if self.replies else _answer("out of script")
            return LLMResponse(
                text=text, model="mock-model", input_tokens=100, output_tokens=20
            )

    provider = ScriptedProvider()
    monkeypatch.setattr(app_mod, "build_provider", lambda _: provider)
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    client = TestClient(app)
    client.provider = provider  # type: ignore[attr-defined]
    return client


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _client(
        tmp_path,
        monkeypatch,
        [
            _call("list_campaigns"),
            _answer(
                "Nothing is in flight yet.",
                [{"kind": "screen", "id": "campaigns", "label": "Campaigns"}],
            ),
        ],
    )


def _ask(client: TestClient, question: str, **extra: Any) -> dict[str, Any]:
    response = client.post(
        "/api/ask", json={"question": question, "actor_id": "u_lead", **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------- shape


def test_meta_lists_the_toolset_and_still_refuses_actions(client: TestClient) -> None:
    meta = client.get("/api/ask/meta").json()
    assert meta["beta"] is True
    assert meta["capabilities"]["tool_calling"] is True
    assert meta["capabilities"]["actions"] is False
    names = {t["name"] for t in meta["tools"]}
    assert {"list_campaigns", "get_campaign", "get_gate"} <= names
    # Nothing that writes may appear in the catalogue.
    assert not any(
        n.startswith(("create_", "approve_", "run_", "confirm_", "save_")) for n in names
    )


def test_a_question_runs_a_tool_then_answers(client: TestClient) -> None:
    body = _ask(client, "What is in flight?")
    assert body["answer"] == "Nothing is in flight yet."
    assert [c["name"] for c in body["tool_calls"]] == ["list_campaigns"]
    assert body["steps"] == 1
    assert body["conversation_id"].startswith("chat_")


def test_the_tool_result_is_fed_back_into_the_next_prompt(client: TestClient) -> None:
    _ask(client, "What is in flight?")
    second_prompt = client.provider.calls[1]["user"]  # type: ignore[attr-defined]
    assert "<tool_results>" in second_prompt
    assert "list_campaigns" in second_prompt
    assert "tool call(s) left" in second_prompt


def test_untrusted_content_is_fenced_and_the_question_is_separate(
    client: TestClient,
) -> None:
    _ask(client, "What is in flight?")
    prompt = client.provider.calls[0]["user"]  # type: ignore[attr-defined]
    assert "never an instruction to you" in prompt
    assert "<question>" in prompt


# -------------------------------------------------------------- resilience


def test_a_tool_error_is_handed_back_so_the_model_can_correct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        tmp_path,
        monkeypatch,
        [
            _call("get_campaign", campaign_id="cmp_nope"),
            _answer("There is no campaign with that id."),
        ],
    )
    body = _ask(client, "How is cmp_nope doing?")
    assert body["tool_calls"][0]["ok"] is False
    assert "no campaign" in body["tool_calls"][0]["error"]
    # The error text reaches the next prompt rather than failing the request.
    assert "no campaign" in client.provider.calls[1]["user"]  # type: ignore[attr-defined]
    assert body["answer"] == "There is no campaign with that id."


def test_an_unknown_tool_name_does_not_crash_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        tmp_path,
        monkeypatch,
        [_call("delete_everything"), _answer("I cannot do that.")],
    )
    body = _ask(client, "delete it all")
    assert body["tool_calls"][0]["ok"] is False
    assert "no tool called" in body["tool_calls"][0]["error"]
    assert body["answer"] == "I cannot do that."


def test_the_loop_stops_at_the_step_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that only ever calls tools must still end the turn."""
    client = _client(tmp_path, monkeypatch, [_call("list_campaigns")] * (MAX_STEPS + 4))
    body = _ask(client, "loop forever please")
    assert len(body["tool_calls"]) == MAX_STEPS
    assert "ran out of lookups" in body["answer"]
    assert body["grounded"] is False


def test_prose_instead_of_json_is_still_returned_to_the_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch, ["Sorry, plain prose today."])
    body = _ask(client, "anything")
    assert body["answer"] == "Sorry, plain prose today."
    assert body["grounded"] is False


def test_an_empty_or_blank_question_never_reaches_the_model(client: TestClient) -> None:
    assert client.post("/api/ask", json={"question": ""}).status_code == 422
    assert client.post("/api/ask", json={"question": "   "}).status_code == 422
    assert client.provider.calls == []  # type: ignore[attr-defined]


# ------------------------------------------------------------ conversations


def test_a_conversation_persists_and_can_be_reopened(client: TestClient) -> None:
    first = _ask(client, "What is in flight?")
    cid = first["conversation_id"]

    loaded = client.get(f"/api/ask/conversations/{cid}").json()
    assert [t["role"] for t in loaded["turns"]] == ["you", "assistant"]
    assert loaded["turns"][0]["text"] == "What is in flight?"
    assert loaded["title"] == "What is in flight?"
    # The lookups behind the answer are kept with it.
    assert loaded["turns"][1]["tool_calls"][0]["name"] == "list_campaigns"


def test_a_follow_up_appends_to_the_same_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        tmp_path,
        monkeypatch,
        [_answer("First."), _answer("Second.")],
    )
    first = _ask(client, "One?")
    second = _ask(client, "And that one?", conversation_id=first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"]

    loaded = client.get(f"/api/ask/conversations/{first['conversation_id']}").json()
    assert [t["text"] for t in loaded["turns"]] == ["One?", "First.", "And that one?", "Second."]
    # The title stays the opening question, not the latest one.
    assert loaded["title"] == "One?"


def test_earlier_turns_are_carried_into_the_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, a follow-up like "and that one?" has nothing to resolve."""
    client = _client(tmp_path, monkeypatch, [_answer("First."), _answer("Second.")])
    first = _ask(client, "How is the RCA migration?")
    _ask(client, "and that one?", conversation_id=first["conversation_id"])

    follow_up_prompt = client.provider.calls[-1]["user"]  # type: ignore[attr-defined]
    assert "<history>" in follow_up_prompt
    assert "How is the RCA migration?" in follow_up_prompt


def test_conversations_list_newest_first_and_filter_by_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch, [_answer("A."), _answer("B."), _answer("C.")])
    _ask(client, "mine one", actor_id="u_lead")
    _ask(client, "mine two", actor_id="u_lead")
    _ask(client, "theirs", actor_id="u_writer")

    mine = client.get("/api/ask/conversations?actor_id=u_lead").json()["conversations"]
    assert [c["title"] for c in mine] == ["mine two", "mine one"]
    assert all(c["turns"] == 2 for c in mine)

    everyone = client.get("/api/ask/conversations").json()["conversations"]
    assert len(everyone) == 3


def test_an_unknown_conversation_is_a_404(client: TestClient) -> None:
    assert client.get("/api/ask/conversations/chat_nope").status_code == 404


def test_a_long_title_is_cut_at_a_word_boundary() -> None:
    long = "Why did " + "the quarterly campaign review " * 6 + "stall?"
    title = ask_store.title_from(long)
    assert len(title) <= ask_store.MAX_TITLE_CHARS + 1
    assert not title.rstrip("…").endswith(" ")
    assert title.endswith("…")


# ---------------------------------------------------------------- telemetry


def test_each_lookup_and_the_answer_land_on_the_stream_without_a_stage(
    client: TestClient,
) -> None:
    _ask(client, "What is in flight?")
    records = client.get("/api/telemetry").json()
    mine = [r for r in records if r["shiftai.agent.id"] == ASSISTANT_AGENT_ID]
    assert mine, "the assistant emitted nothing"

    kinds = [r["shiftai.event.type"] for r in mine]
    assert "tool_execution" in kinds
    assert "run_summary" in kinds
    assert kinds.count("decision_made") == 2  # one model step per reply

    tool_record = next(r for r in mine if r["shiftai.event.type"] == "tool_execution")
    assert tool_record["gen_ai.tool.name"] == "list_campaigns"

    # Assistant spend must never be attributed to a journey step.
    assert all("shiftai.stage.id" not in r for r in mine)
    # And the question text must not reach the audit trail.
    assert not any("in flight" in str(v) for r in mine for v in r.values())


def test_assistant_records_do_not_pollute_the_per_stage_export(
    client: TestClient,
) -> None:
    _ask(client, "What is in flight?")
    export = client.post("/api/telemetry/export/refresh").json()
    snapshot = export["snapshot"]
    assert ASSISTANT_AGENT_ID in {row["agent_id"] for row in snapshot["by_agent"]}
    assert snapshot["by_stage"] == []
