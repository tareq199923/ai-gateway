import json

import httpx
import pytest

from invincible.compat.anthropic import (
    anthropic_to_internal,
    build_error,
    flatten_content_blocks,
    translate_finish_reason,
)
from invincible.main import app
from tests.conftest import provider_body, sse_body, stream_chunk

AUTH = {"Authorization": "Bearer test-gateway-key"}
ANTHROPIC_BODY = {
    "model": "claude-sonnet-4",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "hi"}],
}


def _anthropic_events(response):
    """Parse an Anthropic SSE response into [(event, payload), ...]."""
    events = []
    for block in response.text.split("\n\n"):
        event = None
        payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
        if event is not None:
            events.append((event, payload))
    return events


class _FailingStream(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes):
        self._prefix = prefix

    async def __aiter__(self):
        yield self._prefix
        raise httpx.StreamError("connection dropped mid-stream")

    async def aclose(self):
        pass


# ---------------------------------------------------------------- root probes


async def test_head_root_returns_200(client):
    response = await client.request("HEAD", "/")
    assert response.status_code == 200
    assert response.content == b""


async def test_get_health_detail(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "Invincible"
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


# ----------------------------------------------------- non-streaming messages


async def test_anthropic_completion_success(client, router_setter):
    alpha_body = provider_body("alpha", content="Hello world")
    router_setter(handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)})
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["id"].startswith("msg_")
    assert body["model"] == "claude-sonnet-4"
    assert body["content"] == [{"type": "text", "text": "Hello world"}]
    assert body["stop_reason"] == "end_turn"
    assert body["stop_sequence"] is None
    assert body["usage"]["input_tokens"] >= 1
    assert body["usage"]["output_tokens"] >= 1


async def test_anthropic_echoes_requested_model_as_hint(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200, json=provider_body("alpha", content="ok")
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["model"] == "claude-opus-4-8"


async def test_anthropic_system_and_blocks_are_flattened(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "system": [{"type": "text", "text": "Be concise."}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Explore "},
                        {"type": "image", "source": {"type": "base64", "data": "x"}},
                        {"type": "text", "text": "this"},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing[0] == {"role": "system", "content": "Be concise."}
    assert outgoing[1] == {"role": "user", "content": "Explore this"}


async def test_anthropic_tool_blocks_are_degraded_not_dropped(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="Searched."))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "search",
                            "input": {"query": "x"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": "result text"}],
                        }
                    ],
                },
            ],
        },
    )
    assert response.status_code == 200
    combined = "".join(m["content"] for m in captured[0]["messages"])
    assert "[tool_use: search]" in combined
    assert "result text" in combined


# ------------------------------------------------------------------- streaming


async def test_anthropic_streaming_returns_event_stream(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk("alpha", {"content": "Hi"}),
                    stream_chunk("alpha", {}, finish_reason="stop"),
                ),
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_anthropic_streaming_emits_canonical_sequence(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk("alpha", {"content": "Hel"}),
                    stream_chunk("alpha", {"content": "lo!"}),
                    stream_chunk("alpha", {}, finish_reason="stop"),
                ),
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    events = _anthropic_events(response)
    assert [e for e, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]

    start_payload = events[0][1]
    assert start_payload["type"] == "message_start"
    assert start_payload["message"]["type"] == "message"
    assert start_payload["message"]["role"] == "assistant"
    assert start_payload["message"]["model"] == "claude-sonnet-4"
    assert start_payload["message"]["content"] == []

    deltas = [p["delta"]["text"] for e, p in events if e == "content_block_delta"]
    assert "".join(deltas) == "Hello!"

    message_delta = [p for e, p in events if e == "message_delta"][0]
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["delta"]["stop_sequence"] is None
    assert message_delta["usage"]["output_tokens"] >= 1
    assert events[-1] == ("message_stop", {"type": "message_stop"})


async def test_anthropic_streamed_reply_persisted(client, router_setter):
    received_payloads = []

    def alpha_handler(request: httpx.Request):
        received_payloads.append(json.loads(request.read()))
        return httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"content": "Hello"}),
                stream_chunk("alpha", {"content": " world"}),
                stream_chunk("alpha", {}, finish_reason="stop"),
            ),
        )

    router_setter({"alpha.example.com": alpha_handler})

    headers = {
        "Authorization": "Bearer test-gateway-key",
        "X-Session-Id": "shared-convo",
    }
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200

    history = await app.state.sessions.load("shared-convo")
    assistant_messages = [m for m in history if m["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] == "Hello world"

    await client.post(
        "/v1/messages",
        headers=headers,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    second_outgoing = received_payloads[1]["messages"]
    assert [m["content"] for m in second_outgoing] == ["hi", "Hello world", "hi"]


async def test_cross_protocol_session_sharing(client, router_setter):
    """An OpenAI client on the same session id sees an Anthropic reply, and
    vice versa - because both protocols persist the same internal model."""
    def alpha_handler(request: httpx.Request):
        return httpx.Response(200, json=provider_body("alpha", content="Hello world"))

    router_setter({"alpha.example.com": alpha_handler})
    headers = {"Authorization": "Bearer test-gateway-key", "X-Session-Id": "shared"}

    await client.post(
        "/v1/messages",
        headers=headers,
        json={"messages": [{"role": "user", "content": "introduce yourself"}]},
    )

    history = await app.state.sessions.load("shared")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["content"] == "Hello world"

    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="hi"))

    router_setter({"alpha.example.com": recording_handler})
    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "morning"}]},
    )
    contents = [m["content"] for m in received[0]["messages"]]
    assert "Hello world" in contents


async def test_anthropic_midstream_error_terminates_cleanly(client, router_setter):
    stream_fail = _FailingStream(
        sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "partial"}),
            done=False,
        ).encode()
    )
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                stream=stream_fail,
                headers={"content-type": "text/event-stream"},
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200
    events = _anthropic_events(response)
    event_names = [e for e, _ in events]
    assert "message_start" in event_names
    assert "content_block_delta" in event_names
    assert event_names[-1] == "error"
    error_payload = events[-1][1]
    assert error_payload["type"] == "error"
    assert error_payload["error"]["type"] == "api_error"
    assert not response.text.rstrip().endswith("message_stop")


# ------------------------------------------------------------------- failover


async def test_streaming_failover_before_first_chunk(client, router_setter):
    router = router_setter(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("beta", {"role": "assistant"}),
                    stream_chunk("beta", {"content": "hi"}),
                    stream_chunk("beta", {}, finish_reason="stop"),
                ),
            ),
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200
    events = _anthropic_events(response)
    assert events[0][0] == "message_start"
    assert not router.health_tracker.is_available("alpha")


async def test_streaming_all_providers_fail_returns_503(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(500),
            "gamma.example.com": httpx.Response(429),
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "overloaded_error"


async def test_nonstreaming_failover_to_next_tier(client, router_setter):
    router = router_setter(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200, json=provider_body("beta", "hello")
            ),
        }
    )
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 200
    assert response.json()["model"] == "claude-sonnet-4"
    assert not router.health_tracker.is_available("alpha")


async def test_all_providers_fail_returns_overloaded(client, router_setter):
    router_setter(handlers={"alpha.example.com": httpx.Response(429)})
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 503
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "overloaded_error",
            "message": "All providers failed or are in cooldown.",
        },
    }


async def test_upstream_400_is_mapped_and_sanitized(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                400, json={"error": {"message": "internal secret"}}
            )
        }
    )
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "internal secret" not in json.dumps(body)


# ------------------------------------------------------------------- auth


async def test_anthropic_auth_missing_returns_401(client):
    response = await client.post("/v1/messages", json=ANTHROPIC_BODY)
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_anthropic_auth_invalid_returns_401(client):
    response = await client.post(
        "/v1/messages",
        headers={"Authorization": "Bearer wrong-key"},
        json=ANTHROPIC_BODY,
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_anthropic_auth_open_when_key_unset(client, router_setter, monkeypatch):
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))}
    )
    monkeypatch.delenv("GATEWAY_API_KEY")
    response = await client.post("/v1/messages", json=ANTHROPIC_BODY)
    assert response.status_code == 200


# ------------------------------------------------------------- invalid input


async def test_missing_messages_returns_422(client):
    response = await client.post("/v1/messages", headers=AUTH, json={"model": "x"})
    assert response.status_code == 422


async def test_non_list_messages_returns_422(client):
    response = await client.post(
        "/v1/messages", headers=AUTH, json={"messages": "not a list"}
    )
    assert response.status_code == 422


async def test_empty_messages_returns_400(client):
    response = await client.post("/v1/messages", headers=AUTH, json={"messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_useless_text_returns_400(client):
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={"messages": [{"role": "user", "content": ""}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_unknown_role_returns_400(client):
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={"messages": [{"role": "admin", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_developer_and_foo_roles_still_rejected(client):
    for role in ("developer", "foo"):
        response = await client.post(
            "/v1/messages",
            headers=AUTH,
            json={"messages": [{"role": role, "content": "hi"}]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"


async def test_system_role_inside_messages_is_accepted(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "system", "content": "Be precise."},
                {"role": "user", "content": "hi"},
            ],
        },
    )
    assert response.status_code == 200
    assert captured[0]["messages"][0] == {
        "role": "system",
        "content": "Be precise.",
    }


async def test_top_level_and_messages_system_combined(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "system": "Top system.",
            "messages": [
                {"role": "system", "content": "Inner system."},
                {"role": "user", "content": "hi"},
            ],
        },
    )
    assert response.status_code == 200
    systems = [
        m["content"] for m in captured[0]["messages"] if m["role"] == "system"
    ]
    assert systems == ["Top system.", "Inner system."]


async def test_ignored_optional_anthropic_fields(client, router_setter):
    """tools / tool_choice / metadata / temperature / top_p / top_k /
    stop_sequences / unknown fields / beta query must never produce a 422."""
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(200, json=provider_body("alpha"))
        }
    )
    response = await client.post(
        "/v1/messages?beta=true",
        headers={
            **AUTH,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "tools-2024-04-04",
        },
        json={
            "model": "claude-sonnet-4",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "tools": [{"name": "search", "description": "Search", "input_schema": {}}],
            "tool_choice": {"type": "auto"},
            "metadata": {"user_id": "abc"},
            "temperature": 0.5,
            "top_p": 0.9,
            "top_k": 5,
            "stop_sequences": ["\\n"],
        },
    )
    assert response.status_code == 200
    assert response.json()["type"] == "message"


# ------------------------------------------------------------- pure helpers


async def test_translate_finish_reason_mapping():
    assert translate_finish_reason("stop") == "end_turn"
    assert translate_finish_reason("length") == "max_tokens"
    assert translate_finish_reason("tool_calls") == "tool_use"
    assert translate_finish_reason(None) == "end_turn"
    assert translate_finish_reason("bogus") == "end_turn"


async def test_flatten_content_blocks_matrix():
    assert flatten_content_blocks("plain", "user") == "plain"
    assert (
        flatten_content_blocks(
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "user"
        )
        == "ab"
    )
    assert (
        flatten_content_blocks(
            [{"type": "tool_use", "name": "search", "input": {}}], "assistant"
        )
        == "[tool_use: search]"
    )
    assert (
        flatten_content_blocks([{"type": "tool_result", "content": "out"}], "user")
        == "out"
    )
    assert flatten_content_blocks(123, "user") == ""
    assert flatten_content_blocks(None, "user") == ""


async def test_build_error_mapping():
    assert build_error(400, "x")[0] == 400
    assert build_error(401, "x")[1]["error"]["type"] == "authentication_error"
    assert build_error(403, "x")[1]["error"]["type"] == "permission_error"
    assert build_error(404, "x")[1]["error"]["type"] == "not_found_error"
    assert build_error(429, "x")[1]["error"]["type"] == "rate_limit_error"
    assert build_error(500, "x")[1]["error"]["type"] == "api_error"
    assert build_error(503, "x")[1]["error"]["type"] == "overloaded_error"
    assert build_error(599, "x")[1]["error"]["type"] == "api_error"


async def test_anthropic_to_internal_promotes_system():
    internal = anthropic_to_internal(
        [{"role": "user", "content": "hi"}], system="Be nice."
    )
    assert internal[0] == {"role": "system", "content": "Be nice."}
    assert internal[1] == {"role": "user", "content": "hi"}


async def test_anthropic_to_internal_accepts_system_role_in_messages():
    internal = anthropic_to_internal(
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "hi"},
        ]
    )
    assert internal[0] == {"role": "system", "content": "Be precise."}
    assert internal[1] == {"role": "user", "content": "hi"}


async def test_anthropic_to_internal_rejects_empty():
    with pytest.raises(ValueError):
        anthropic_to_internal([])
    with pytest.raises(ValueError):
        anthropic_to_internal([{"role": "user", "content": ""}])
