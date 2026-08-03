import httpx
import pytest

from invincible.core.router import (
    AllProvidersFailedError,
    estimate_tokens,
    group_into_turns,
    trim_messages,
)


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


def system(content):
    return {"role": "system", "content": content}


def test_trim_is_noop_when_under_budget():
    messages = [system("be helpful"), user("hi"), assistant("hello!")]
    trimmed = trim_messages(messages, max_context=1_000_000)
    assert trimmed == messages


def test_trim_drops_oldest_turns_first_but_keeps_system():
    messages = [system("be helpful")]
    big = "x" * 2000
    for i in range(10):
        messages.append(user(f"turn {i}: {big}"))
        messages.append(assistant(f"reply {i}: {big}"))

    trimmed = trim_messages(messages, max_context=2000, reserve_tokens=100)

    assert trimmed[0] == system("be helpful")
    assert any("turn 9" in m["content"] for m in trimmed if m["role"] == "user")
    assert not any("turn 0" in m["content"] for m in trimmed if m["role"] == "user")


def test_trim_never_splits_a_tool_call_from_its_tool_result():
    tool_call_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    tool_result_msg = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "file contents here",
    }

    messages = [
        system("be helpful"),
        user("old turn, should get dropped " + "x" * 2000),
        assistant("old reply " + "x" * 2000),
        user("read this file for me"),
        tool_call_msg,
        tool_result_msg,
        assistant("here's a summary of the file"),
    ]

    trimmed = trim_messages(messages, max_context=1500, reserve_tokens=100)

    has_tool_call = any(m.get("tool_calls") for m in trimmed)
    has_tool_result = any(m.get("role") == "tool" for m in trimmed)
    assert has_tool_call == has_tool_result


def test_trim_keeps_most_recent_turn_even_if_it_alone_exceeds_budget():
    huge = "x" * 50_000
    messages = [user(f"huge turn: {huge}")]
    trimmed = trim_messages(messages, max_context=100, reserve_tokens=10)
    assert trimmed == messages


def test_group_into_turns_starts_new_group_on_each_user_message():
    messages = [user("a"), assistant("b"), user("c"), assistant("d")]
    turns = group_into_turns(messages)
    assert len(turns) == 2
    assert turns[0] == [user("a"), assistant("b")]
    assert turns[1] == [user("c"), assistant("d")]


def test_estimate_tokens_is_roughly_length_over_four():
    msg = {"role": "user", "content": "a" * 400}
    assert 90 <= estimate_tokens(msg) <= 110


@pytest.mark.asyncio
async def test_router_trims_to_each_providers_own_max_context(make_router):
    received = {}

    def make_handler(name):
        def handler(request: httpx.Request):
            received[name] = request.read()
            return httpx.Response(500)
        return handler

    providers = [
        {
            "name": "big",
            "tier": 1,
            "base_url": "https://alpha.example.com/v1",
            "api_key_env": "ALPHA_API_KEY",
            "model_id": "big-model",
            "max_context": 1_000_000,
        },
        {
            "name": "small",
            "tier": 2,
            "base_url": "https://beta.example.com/v1",
            "api_key_env": "BETA_API_KEY",
            "model_id": "small-model",
            "max_context": 500,
        },
    ]

    big_history = [user(f"message {i}: " + "x" * 500) for i in range(20)]

    router = make_router(
        providers=providers,
        handlers={
            "alpha.example.com": make_handler("big"),
            "beta.example.com": make_handler("small"),
        },
    )

    with pytest.raises(AllProvidersFailedError):
        await router.route_request(big_history)

    import json
    big_payload = json.loads(received["big"])
    small_payload = json.loads(received["small"])

    assert len(big_payload["messages"]) > len(small_payload["messages"])
