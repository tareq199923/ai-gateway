# MCP Protocol — client-facing spec

This is the contract for anything that wants to call Invincible's tools over
HTTP: a cloud-hosted AI reaching your machine through a tunnel, a script, or
a manual `curl`. The server speaks a **minimal JSON-RPC 2.0 subset** over a
single `POST /mcp` — it is not a general-purpose MCP transport (no
streaming/SSE, no subscriptions, no batch).

---

## 1. Transport & auth

```
POST /mcp
Content-Type: application/json
X-MCP-Secret: <MCP_SHARED_SECRET>
```

- **Auth**: the `X-MCP-Secret` header must equal `MCP_SHARED_SECRET`
  (timing-safe comparison). Wrong/missing → `401`. If the secret is unset on
  the server → `503` (disabled, never open).
- **One request per HTTP POST.** The body is a single JSON-RPC 2.0 object.
- Protocol version advertised: `2025-06-18`.

---

## 2. Methods

### `initialize`

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize"}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "serverInfo": {"name": "invincible-mcp", "version": "0.1.0"},
    "capabilities": {"tools": {}}
  }
}
```

### `tools/list`

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
```

Response: `result.tools` is an array of three tool descriptors:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "read_file",
        "description": "Read a file's contents from the host machine. ...",
        "inputSchema": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"]
        }
      },
      {
        "name": "execute_bash",
        "description": "Run a shell command on the host machine. ...",
        "inputSchema": {
          "type": "object",
          "properties": {"command": {"type": "string"}},
          "required": ["command"]
        }
      },
      {
        "name": "write_file",
        "description": "Write content to a file on the host machine. ...",
        "inputSchema": {
          "type": "object",
          "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
          "required": ["path", "content"]
        }
      }
    ]
  }
}
```

### `tools/call`

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {"name": "<tool>", "arguments": { ... }}
}
```

`arguments` is optional and defaults to `{}`.

#### `read_file`

```json
"arguments": {"path": "C:\\Users\\me\\project\\notes.txt"}
```

No confirmation. Result (success):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{'status': 'read', 'path': 'C:\\\\Users\\\\me\\\\project\\\\notes.txt', 'content': '...'}"}],
    "isError": false
  }
}
```

Result (error, e.g. missing file):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{'status': 'error', 'error': 'File not found: ...'}"}],
    "isError": true
  }
}
```

Denylisted target (`.env`, `sessions.db`, `.git/`):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "Blocked: read of Invincible's .env file (.env)"}],
    "isError": true
  }
}
```

#### `execute_bash`

```json
"arguments": {"command": "git status"}
```

If the command survives the denylist, the operator is prompted at the server
terminal (`Allow this command? [y/N]`).

- Operator approves → the command runs (30s timeout; on timeout the process
  is killed and you get `returncode: -1` and a timeout message in `stderr`).

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{'stdout': '...', 'stderr': '', 'returncode': 0}"}],
    "isError": false
  }
}
```

- Operator declines (or EOF) → `isError: true`, text `Declined by operator
  at the terminal.`
- Denylist hit → `isError: true`, text starts `Blocked: <reason>`. The
  prompt is never shown.

> Note: the result is the Python `dict.__str__()` output, so expect single
> quotes and Python escapes inside the JSON text field. `str(result)` is used
> for all three tools.

#### `write_file`

```json
"arguments": {"path": "C:\\Users\\me\\project\\scratch\\out.txt", "content": "hello"}
```

Like `execute_bash`: denylist first (path resolves inside the repo to
`.env*`, `providers.yaml`, `sessions.db`, `invincible/`, `tests/`, `.git/` →
`Blocked`, no prompt), then confirmation, then write. Parent directories are
created automatically (`os.makedirs(..., exist_ok=True)`).

Success:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{'status': 'written', 'path': '...', 'bytes': 5}"}],
    "isError": false
  }
}
```

Failure (e.g. permission denied) → `isError: true`, text
`{'status': 'error', 'error': '<exception>'}`.

---

## 3. Notifications (no `id`)

A request **without** an `id` field is a JSON-RPC 2.0 *notification*: the
server still performs the side effect (e.g. `tools/call` executes), but
replies `204 No Content` with an empty body — even when the call would have
errored.

```json
{"jsonrpc": "2.0", "method": "tools/call",
 "params": {"name": "execute_bash", "arguments": {"command": "ls"}}}
```

→ `HTTP 204`, empty body.

---

## 4. Error codes

| Code | Meaning | When |
|---|---|---|
| `-32700` | Parse error | Body is not valid JSON. `id: null`. |
| `-32600` | Invalid Request | Body is not an object (e.g. a JSON array). `id: null`. |
| `-32602` | Invalid params | `params` exists but is not an object. |
| `-32601` | Method not found | Unknown `method`, or unknown tool name in `tools/call` (message: `Unknown tool: <name>` / `Unknown method: <method>`). |

Protocol-level errors are returned as JSON-RPC errors:

```json
{"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "Parse error"}}
```

Tool-level failures (blocked/declined/missing file) are **not** JSON-RPC
errors — they are successful calls whose `result.isError` is `true`.

---

## 5. End-to-end example (tunnel)

Expose the local server with a tunnel, e.g. Cloudflare:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# → https://random-name.trycloudflare.com
```

Then a remote AI calls:

```bash
curl -X POST https://random-name.trycloudflare.com/mcp \
  -H "X-MCP-Secret: $MCP_SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
```

Security notes for this setup:

- The tunnel URL alone is useless without the MCP secret (independent from
  `GATEWAY_API_KEY`).
- `read_file` needs no confirmation; `execute_bash`/`write_file` will block
  until someone at the machine answers the y/N prompt.
- See [docs/SECURITY.md](SECURITY.md) for the full threat model.
