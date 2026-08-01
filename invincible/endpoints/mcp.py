# invincible/endpoints/mcp.py
"""Minimal MCP (Model Context Protocol) tool server.

Exposed over HTTP so a cloud-hosted AI reaching this machine through a
tunnel can call execute_bash and write_file. Speaks the JSON-RPC 2.0 shape
MCP clients expect for initialize / tools/list / tools/call - just enough
surface for this server's own use, not a general-purpose transport.

Auth is a separate MCP_SHARED_SECRET, independent of GATEWAY_API_KEY, so a
leaked tunnel URL alone isn't enough to reach these tools - and so rotating
one secret never silently affects the other. The comparison uses
secrets.compare_digest rather than `==` so a timing side-channel can't be
used to guess the secret one byte at a time.
"""
import json
import os
import secrets
from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import JSONResponse
from invincible.core import tool_executor

router = APIRouter()

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a file's contents from the host machine. Reads of files "
            "that contain secrets or sensitive state (.env, sessions.db, "
            ".git/) are rejected outright. No confirmation is required for "
            "other files since reading is non-destructive."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "execute_bash",
        "description": (
            "Run a shell command on the host machine. Commands matching the "
            "denylist (destructive filesystem ops, privilege escalation, "
            "power commands, etc.) are rejected outright. Everything else "
            "blocks and requires the operator to approve it interactively "
            "at the terminal running this server before it runs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file on the host machine. Writes to files "
            "this server depends on for its own security or state (.env, "
            "providers.yaml, sessions.db, its own source/tests, .git/) are "
            "rejected outright. Everything else blocks and requires the "
            "operator to approve it interactively at the terminal running "
            "this server before it runs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
]


async def require_mcp_auth(request: Request):
    secret = os.getenv("MCP_SHARED_SECRET")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": {
                "message": "MCP_SHARED_SECRET is not configured; MCP endpoint is disabled.",
                "type": "config_error",
            }},
        )
    provided = request.headers.get("X-MCP-Secret")
    if provided is None or not secrets.compare_digest(provided, secret):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing or invalid MCP secret", "type": "auth_error"}},
        )


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


async def _dispatch(method, rpc_id, params):
    if method == "initialize":
        return _result(rpc_id, {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "invincible-mcp", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        })

    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}

        try:
            if name == "read_file":
                result = await tool_executor.read_file(args.get("path", ""))
                return _result(rpc_id, _tool_content(str(result)))

            if name == "execute_bash":
                result = await tool_executor.execute_bash(args.get("command", ""))
                return _result(rpc_id, _tool_content(str(result)))

            if name == "write_file":
                result = await tool_executor.write_file(
                    args.get("path", ""), args.get("content", "")
                )
                return _result(rpc_id, _tool_content(str(result)))

            return _error(rpc_id, -32601, f"Unknown tool: {name}")

        except tool_executor.ToolBlocked as e:
            return _result(rpc_id, _tool_content(f"Blocked: {e.reason}", is_error=True))
        except tool_executor.ToolDeclined:
            return _result(rpc_id, _tool_content(
                "Declined by operator at the terminal.", is_error=True
            ))

    return _error(rpc_id, -32601, f"Unknown method: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Can't recover an id from unparseable input - JSON-RPC 2.0 says
        # send id: null for parse errors.
        return JSONResponse(_error(None, -32700, "Parse error"))

    if not isinstance(body, dict):
        return JSONResponse(_error(None, -32600, "Invalid Request"))

    method = body.get("method")
    params = body.get("params") or {}
    is_notification = "id" not in body
    rpc_id = body.get("id")

    if not isinstance(params, dict):
        if is_notification:
            # Notifications never get a response body, even on error.
            return Response(status_code=204)
        return JSONResponse(_error(rpc_id, -32602, "Invalid params"))

    response = await _dispatch(method, rpc_id, params)

    if is_notification:
        # JSON-RPC 2.0: a request with no "id" is a notification - the
        # side effect (if any) still runs via _dispatch above, but the
        # spec says the server MUST NOT reply with a body.
        return Response(status_code=204)

    return JSONResponse(response)
