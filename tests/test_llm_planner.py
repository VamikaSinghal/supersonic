"""Step 9: LLM planner skeleton (no network: the client is faked)."""
import inspect

import pytest

from sonic.agent import TOOLS
from sonic.llm_planner import TOOL_SCHEMAS, LLMPlanner
from sonic.planner import Action, Done, Step


def response(*blocks):
    return {"content": list(blocks), "stop_reason": "tool_use"}


# 28
def test_llm_planner_maps_tool_use_and_text_and_builds_history():
    sent = []
    replies = [
        response({"type": "text", "text": "Let me look."},
                 {"type": "tool_use", "id": "toolu_x", "name": "read_file", "input": {"path": "a.txt"}}),
        response({"type": "text", "text": "All done."}),
    ]
    planner = LLMPlanner(client=lambda body: (sent.append(body), replies[len(sent) - 1])[1])

    a = planner.next_action("summarise a.txt", [])
    assert a == Action("read_file", {"path": "a.txt"})
    d = planner.next_action("summarise a.txt", [Step(a, True, "hello")])
    assert d == Done("All done.")

    body = sent[1]
    assert body["model"] and body["max_tokens"] and body["system"] and body["tools"] == TOOL_SCHEMAS
    msgs = body["messages"]
    assert msgs[0] == {"role": "user", "content": "summarise a.txt"}
    use = [b for b in msgs[1]["content"] if b["type"] == "tool_use"][0]
    result = msgs[2]["content"][0]
    assert msgs[1]["role"] == "assistant" and msgs[2]["role"] == "user"
    assert use["name"] == "read_file" and use["input"] == {"path": "a.txt"}
    assert result["type"] == "tool_result" and result["tool_use_id"] == use["id"]
    assert result["content"] == "hello" and result["is_error"] is False


# 29
def test_tool_schemas_match_real_tool_signatures():
    assert {t["name"] for t in TOOL_SCHEMAS} == set(TOOLS)
    for schema in TOOL_SCHEMAS:
        params = list(inspect.signature(TOOLS[schema["name"]]).parameters.values())[1:]  # skip ws
        required = {p.name for p in params if p.default is inspect.Parameter.empty}
        assert set(schema["input_schema"]["required"]) == required
        assert set(schema["input_schema"]["properties"]) <= {p.name for p in params}
        assert schema["description"]


# 30
def test_from_env_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        LLMPlanner.from_env()
