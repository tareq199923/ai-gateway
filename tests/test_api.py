import httpx

from tests.conftest import provider_body

MESSAGES = [{"role": "user", "content": "hi"}]
AUTH = {"Authorization": "Bearer test-gateway-key"}


async def test_health_check(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


async def test_chat_completion_success(client, router_setter):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={"messages": MESSAGES}
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_streaming_rejected(client):
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 400
    assert "not currently supported" in response.json()["error"]["message"]


async def test_missing_auth_returns_401(client):
    response = await client.post(
        "/v1/chat/completions", json={"messages": MESSAGES}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_invalid_auth_returns_401(client):
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong-key"},
        json={"messages": MESSAGES},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_valid_auth_succeeds(client, router_setter):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES},
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_auth_optional_when_key_unset(client, router_setter, monkeypatch):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    monkeypatch.delenv("GATEWAY_API_KEY")
    response = await client.post(
        "/v1/chat/completions", json={"messages": MESSAGES}
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_missing_messages_returns_422(client):
    response = await client.post("/v1/chat/completions", headers=AUTH, json={})
    assert response.status_code == 422


async def test_all_providers_fail_returns_503(client, router_setter):
    router_setter(handlers={"alpha.example.com": httpx.Response(429)})
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={"messages": MESSAGES}
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "gateway_error"


async def test_upstream_error_forwarded(client, router_setter):
    error_body = {"error": {"message": "bad request"}}
    router_setter(
        handlers={"alpha.example.com": httpx.Response(400, json=error_body)}
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={"messages": MESSAGES}
    )
    assert response.status_code == 400
    assert response.json() == error_body
