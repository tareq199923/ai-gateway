import httpx
import pytest

from tests.conftest import provider_body


@pytest.mark.asyncio
async def test_session_history_is_replayed_on_second_request(router_setter, client):
    """Proves the gateway remembers prior turns within the same session_id,
    and does NOT leak history across a different session_id."""

    received_payloads = []

    def alpha_handler(request: httpx.Request):
        received_payloads.append(request.read())
        return httpx.Response(200, json=provider_body("alpha", content="Nice to meet you!"))

    router_setter({"alpha.example.com": alpha_handler})

    headers = {
        "Authorization": "Bearer test-gateway-key",
        "X-Session-Id": "conversation-1",
    }

    resp1 = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "My name is TestUser"}]},
    )
    assert resp1.status_code == 200

    resp2 = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "What is my name?"}]},
    )
    assert resp2.status_code == 200

    import json
    second_outgoing = json.loads(received_payloads[1])
    outgoing_contents = [m["content"] for m in second_outgoing["messages"]]

    assert "My name is TestUser" in outgoing_contents
    assert "Nice to meet you!" in outgoing_contents
    assert "What is my name?" in outgoing_contents


@pytest.mark.asyncio
async def test_different_session_ids_do_not_share_history(router_setter, client):
    def alpha_handler(request: httpx.Request):
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})

    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-gateway-key", "X-Session-Id": "session-a"},
        json={"messages": [{"role": "user", "content": "secret: banana"}]},
    )

    received = []

    def alpha_handler_2(request: httpx.Request):
        received.append(request.read())
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler_2})

    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-gateway-key", "X-Session-Id": "session-b"},
        json={"messages": [{"role": "user", "content": "what is the secret?"}]},
    )

    import json
    payload = json.loads(received[0])
    contents = [m["content"] for m in payload["messages"]]
    assert "secret: banana" not in contents
