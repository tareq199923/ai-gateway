import os

from invincible.core import tool_executor

MCP_AUTH = {"X-MCP-Secret": "test-mcp-secret"}


async def test_mcp_missing_secret_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401


async def test_mcp_wrong_secret_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers={"X-MCP-Secret": "wrong"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401


async def test_mcp_disabled_when_secret_unset(client, monkeypatch):
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    response = await client.post(
        "/mcp", headers=MCP_AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 503


async def test_mcp_tools_list(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp", headers=MCP_AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names == {"read_file", "execute_bash", "write_file"}


async def test_mcp_call_blocked_command(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "execute_bash", "arguments": {"command": "sudo rm -rf /"}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]


async def test_mcp_call_declined_command(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    monkeypatch.setattr(tool_executor, "confirm", lambda prompt: _false())

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "execute_bash", "arguments": {"command": "echo hi"}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Declined" in body["result"]["content"][0]["text"]


async def test_mcp_call_approved_command(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    monkeypatch.setattr(tool_executor, "confirm", lambda prompt: _true())

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "execute_bash", "arguments": {"command": "echo hi"}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is False
    assert "hi" in body["result"]["content"][0]["text"]


async def test_mcp_unknown_tool(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "delete_everything", "arguments": {}},
        },
    )
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == -32601


async def test_mcp_call_read_file_success(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    target = tmp_path / "readable.txt"
    target.write_text("hello from disk")

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": str(target)}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is False
    assert "hello from disk" in body["result"]["content"][0]["text"]


async def test_mcp_call_read_env_file_blocked(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    target = os.path.join(tool_executor._REPO_ROOT, ".env")

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": target}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]


async def test_mcp_call_read_own_source_allowed(client, monkeypatch):
    """Unlike write_file, read_file must allow invincible/ and tests/ - seeing
    the code is the entire point of giving a cloud AI this tool."""
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    target = os.path.join(tool_executor._REPO_ROOT, "invincible", "main.py")

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": target}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is False


async def test_mcp_call_write_to_protected_path_blocked(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    called = False

    async def fake_confirm(prompt):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(tool_executor, "confirm", fake_confirm)

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": ".env", "content": "GATEWAY_API_KEY=stolen"},
            },
        },
    )
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]
    assert called is False  # never reached the confirmation prompt


# --- JSON-RPC protocol hardening ---

async def test_mcp_malformed_json_returns_parse_error(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers={**MCP_AUTH, "Content-Type": "application/json"},
        content=b"{not valid json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32700


async def test_mcp_non_object_body_returns_invalid_request(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post("/mcp", headers=MCP_AUTH, json=[1, 2, 3])
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32600


async def test_mcp_invalid_params_returns_invalid_params(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1, 2]},
    )
    body = response.json()
    assert body["id"] == 1
    assert body["error"]["code"] == -32602


async def test_mcp_notification_returns_no_body(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "method": "tools/list"},  # no "id" -> notification
    )
    assert response.status_code == 204
    assert response.content == b""


async def test_mcp_notification_invalid_params_still_no_body(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "method": "tools/call", "params": [1, 2]},
    )
    assert response.status_code == 204
    assert response.content == b""


async def _true():
    return True


async def _false():
    return False
