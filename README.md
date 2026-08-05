# Invincible — AI Continuity Gateway & Local MCP Agent

> The Python package is named `invincible` (the repository directory is
> `ai-gateway`). Throughout this repo the project is referred to as
> **Invincible**.

---

## What is Invincible?

A local, Python (FastAPI) server that runs on your development machine and
serves two roles in one process:

1. **Local Failover Proxy** — an OpenAI-compatible `/v1/chat/completions`
   endpoint that fans requests across tiered upstream providers (Groq,
   Gemini, OpenRouter) and transparently fails over on rate limits (429) and
   server errors, so a free-tier 429 no longer kills an agent's workflow.
2. **Local MCP Tool Server** — a JSON-RPC 2.0 `/mcp` endpoint exposing
   `read_file`, `execute_bash`, and `write_file` to a cloud-hosted AI that
   reaches your machine through a tunnel, letting it read local files, write
   code, and run commands on your box.

### Why it exists

- **The 429 problem.** AI coding agents using free/open-source providers
  (Groq, Gemini, OpenRouter) get killed when they hit a rate limit. Invincible
  sits between the agent and the providers; on a 429 (or 5xx) it records the
  failure, puts the provider in a short cooldown, and retries the next
  provider in the tier order. The agent sees a single, stable endpoint.
- **The cloud-to-local gap.** Cloud AI tools (e.g. the Claude web/mobile app)
  can reason well but cannot read your local files, write to disk, or run
  terminal commands. Invincible's MCP server exposes those capabilities over
  HTTP, so a remote model can act on the local machine — under operator
  confirmation for anything destructive.

---

## Features

| Feature | What it gives you |
|---|---|
| **Tiered failover** | Providers sorted by `tier`, tried in order; 429/5xx → cooldown + next tier; 401/403 → permanent disable; network errors → next tier. All providers down → HTTP 503. |
| **Exponential cooldown** | 30s → 60s → 120s → 240s → capped at 300s; a success resets the counter (in-memory, process-scoped). |
| **Conversation memory** | SQLite-backed, keyed by the `X-Session-Id` header (default `default`). History is merged into every request and the assistant reply is persisted back. |
| **Context trimming** | Per-provider `max_context`; system messages always kept; everything else dropped as atomic *turns* (an assistant `tool_calls` is never separated from its tool results); the most recent turn is always sent. |
| **Per-provider timeouts** | Split connect/read/write/pool with sane defaults and per-provider overrides (Gemini gets 90s read, the free OpenRouter fallback 20s). |
| **MCP tool server** | `read_file` (no confirmation), `execute_bash` and `write_file` (interactive y/N at the server terminal), guarded by denylists and a separate `MCP_SHARED_SECRET` auth. |

---

## Installation

Requires Python 3.10+.

```bash
python -m venv venv
source venv/bin/activate            # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Optional: install the package itself so the `invincible` / `inv` commands
work from anywhere:

```bash
pip install -e .
invincible --version                 # verify
```

---

## Quick Start

```bash
cp .env.example .env                # then fill in API keys

invincible setup                    # generates missing secrets, prompts for keys
invincible start                    # http://127.0.0.1:8000
```

`invincible setup` writes missing secret values (`GATEWAY_API_KEY`,
`MCP_SHARED_SECRET`) as random `secrets.token_urlsafe(32)` tokens and prompts
for the provider keys, preserving your existing `.env` comments and values.

See [Examples](#examples) for ready-to-run `curl` calls, or continue reading
for the full configuration, API, and tooling reference.

---

## Configuration

Everything is environment variables plus one YAML file — no other config.

### `.env` variables

| Variable | Required by | Purpose |
|---|---|---|
| `GATEWAY_API_KEY` | `/v1/*` | Bearer token for the chat endpoint. **If unset, the endpoint is open (no auth).** |
| `MCP_SHARED_SECRET` | `/mcp` | Value of the `X-MCP-Secret` header for tool calls. **If unset, `/mcp` returns 503.** |
| `GEMINI_API_KEY` | provider tier 1 | Gemini Flash. |
| `GROQ_API_KEY` | provider tier 2 | Groq Llama 70B. |
| `OPENROUTER_API_KEY` | provider tier 3 | OpenRouter free fallback. |
| `INVINCIBLE_CONFIG_PATH` | startup | Path to a custom `providers.yaml` (set by CLI `--config`). |
| `INVINCIBLE_DB_PATH` | startup | Path to the session database (set by CLI `--db-path`). |

The two secrets are **independent**: a leaked tunnel URL alone is not enough
to reach tool execution, and rotating one secret never affects the other.

### `providers.yaml`

Defines the upstream providers: `tier` (failover order, ascending), `base_url`
(OpenAI-compatible), `api_key_env` (env var *name*, never the key itself),
`model_id`, `max_context`, and optional per-provider `timeout` overrides. The
canonical copy is packaged at `invincible/providers.yaml` (a deprecated copy
at the repo root is only a fallback).

Full reference — schema, validation rules, timeout resolution:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

---

## CLI Commands

Two commands, both exposed as `invincible` and `inv`:

| Command | Purpose |
|---|---|
| `invincible setup` | Create/update `.env`: generates missing secrets (`token_urlsafe(32)`, never echoed), prompts for provider keys, preserves existing comments/values. `--force` re-prompts existing values. |
| `invincible start` | Start the server. Options: `--host` (default `127.0.0.1`), `--port` (default `8000`), `--reload`, `--log-level`, `--env-file`, `--config` (custom providers.yaml), `--db-path` (session database). |

```bash
invincible setup --force
invincible start --port 9000 --config ./my-providers.yaml
```

Full CLI reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md) → *CLI reference*.

---

## API

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | Health check → `{"status": "healthy"}` |
| `GET` | `/v1/models` | `Authorization: Bearer <GATEWAY_API_KEY>` | OpenAI-compatible model list from `providers.yaml` |
| `POST` | `/v1/chat/completions` | `Authorization: Bearer <GATEWAY_API_KEY>` | Chat completion with tiered failover |

### Chat request

- **Body**: `{"messages": [...], "stream": false}` — OpenAI message format.
  Only `messages` and `stream` are accepted; `stream: true` → **400** (no
  streaming). Other OpenAI fields are rejected with **422**.
- **Sessions**: history is loaded from SQLite keyed by the `X-Session-Id`
  header (default `default`), prepended to your messages, and the assistant
  reply is persisted back. `session_id` is a partition key, not a credential.
- **Response**: the upstream provider's JSON is forwarded **verbatim**.

### Status codes

| Status | When |
|---|---|
| `200` | Upstream success (body forwarded verbatim) |
| `400` | `stream: true` |
| `401` | Missing/invalid `GATEWAY_API_KEY` (when set) |
| `422` | Body fails validation (missing `messages`, extra fields) |
| `4xx` | Upstream returned a non-failover error (e.g. 400) — forwarded verbatim |
| `503` | All providers failed or are in cooldown |

Full contract — sessions, trimming, timeout semantics:
[docs/API_REFERENCE.md](docs/API_REFERENCE.md).

---

## MCP Support

`POST /mcp` implements a minimal JSON-RPC 2.0 subset: `initialize`,
`tools/list`, and `tools/call`. Protocol version: `2025-06-18`.

- **Auth**: header `X-MCP-Secret: <MCP_SHARED_SECRET>` (timing-safe
  comparison). Wrong/missing → `401`; secret unset on the server → `503`.
- **Notifications**: a request without an `id` still runs its side effect
  but the server replies `204 No Content` with no body.

### Tools

| Tool | Arguments | Confirmation | Gate |
|---|---|---|---|
| `read_file` | `path` | **No** | Blocks only real secrets/state: `.env*`, `sessions.db`, `.git/`. **Allows** `invincible/`, `tests/`, `providers.yaml`. |
| `execute_bash` | `command` | **Yes** — y/N at the server terminal | Blocks high-blast-radius commands (`rm -rf /`, fork bombs, `dd of=/dev/`, `mkfs`, `sudo`, `curl \| sh`, `rd /s C:\`, …). 30s execution timeout. |
| `write_file` | `path`, `content` | **Yes** — y/N at the server terminal | Blocks writes to `.env*`, `providers.yaml`, `sessions.db`, `invincible/`, `tests/`, `.git/`. Creates parent directories. |

Security model, full denylist inventory, and known limits:
[docs/SECURITY.md](docs/SECURITY.md).

---

## Provider Routing

Providers are tried in **`tier` ascending order** (1 first). Per attempt:

| Upstream status | Router behavior |
|---|---|
| `200` | `record_success` (resets cooldown) → return body |
| `429` / `5xx` | `record_failure` → cooldown → **try next provider** |
| `401` / `403` | `disable` (permanent for process lifetime) → **try next provider** |
| Other `4xx` (e.g. `400`) | **Abort** — forward the provider's status and body |
| Network error | `record_failure` → cooldown → **try next provider** |
| In cooldown / missing API key | Skipped silently (log only) |

All providers exhausted → **HTTP 503**. Cooldowns follow
`30 * 2**(failures-1)`, capped at **300s**; all health state is in-memory and
resets on restart.

Shipped tier order:

| Tier | Provider | Model | Max context |
|---|---|---|---|
| 1 | `gemini-flash` | `gemini-2.5-flash` | 1 000 000 |
| 2 | `groq-llama` | `llama-3.3-70b-versatile` | 128 000 |
| 3 | `openrouter-fallback` | `meta-llama/llama-3.1-8b-instruct:free` | 32 000 |

Deep dive (failover state machine, context trimming): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Examples

### 1. Health check

```bash
curl http://127.0.0.1:8000/
# {"status": "healthy"}
```

### 2. List models

```bash
curl http://127.0.0.1:8000/v1/models
# {
#   "object": "list",
#   "data": [
#     {"id": "gemini-2.5-flash", "object": "model", "owned_by": "invincible"},
#     {"id": "llama-3.3-70b-versatile", "object": "model", "owned_by": "invincible"},
#     {"id": "meta-llama/llama-3.1-8b-instruct:free", "object": "model", "owned_by": "invincible"}
#   ]
# }
```

The list is built from the running gateway's provider configuration, so it
reflects exactly what the gateway can route to.

### 3. Chat with session memory

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: my-conversation" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

The assistant reply is stored under `my-conversation` and will be included in
your next request with the same `X-Session-Id` — the model remembers the
conversation.

### 4. List MCP tools

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "X-MCP-Secret: $MCP_SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### 5. Run a command via MCP

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "X-MCP-Secret: $MCP_SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"execute_bash","arguments":{"command":"git status"}}}'
```

Expect a `[y/N]` prompt at the server terminal before it runs.

### 6. Expose to a cloud AI over a tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# → https://random-name.trycloudflare.com  — call /mcp on this URL
```

The tunnel URL alone is useless without `MCP_SHARED_SECRET`.

More MCP protocol details: [docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md).

---

## Architecture

```
                         ┌──────────────────────────────┐
  OpenAI-compatible      │  invincible/main.py          │
  agent  ─── /v1/chat ─► │  (FastAPI)                   │
                         │                              │
  Cloud AI      ─── /mcp ─►  openai_compat │ mcp routers │
  (via tunnel)           │                              │
                         └──────┬────────────────┬──────┘
                                │                │
                  ┌─────────────▼──┐    ┌─────────▼──────────┐
                  │ core/router.py │    │ core/tool_executor  │
                  │ tiered failover│    │ (denylist + confirm)│
                  │ + ctx trimming │    └─────────────────────┘
                  └───────┬────────┘
                          │
            ┌─────────────▼──────────────┐
            │ core/provider_health.py   │
            │ core/session_store.py     │
            │ (SQLite conversation mem) │
            └────────────────────────────┘
```

### Package layout

| Path | Role |
|---|---|
| `invincible/main.py` | FastAPI app, lifespan, two auth dependencies, router wiring. |
| `invincible/endpoints/openai_compat.py` | `POST /v1/chat/completions` (session merge + upstream call); `GET /v1/models`. |
| `invincible/endpoints/mcp.py` | `POST /mcp`; JSON-RPC 2.0 dispatch, `tools/list`, `tools/call`. |
| `invincible/core/router.py` | Provider loading, tiered failover, response trimming, timeouts. |
| `invincible/core/provider_health.py` | Per-provider failure counts + exponential cooldowns. |
| `invincible/core/session_store.py` | SQLite-backed conversation memory, partitioned by session id. |
| `invincible/core/tool_executor.py` | Denylists, interactive confirmation, tool execution. |
| `invincible/cli.py` | Click CLI: `setup` (env file wizard) and `start` (uvicorn wrapper). |
| `invincible/providers.yaml` | Canonical provider configuration (packaged, authoritative). |

---

## Documentation

| Doc | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, request flows, context-trimming deep dive, failover state machine. |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | The `/v1/chat/completions` contract: request, response, status codes, failover semantics. |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `.env` variables, `providers.yaml` schema, timeouts, session database, CLI reference. |
| [docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md) | Client-facing `/mcp` spec: JSON-RPC shape, tools, notifications, tunnel setup. |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, auth realms, denylist inventory, confirmation UX, known limits. |
| [docs/TESTING.md](docs/TESTING.md) | How tests work, fixtures, per-file coverage map. |

---

## Known limits (tl;dr)

- OpenAI-compatible **chat completions only** — no Anthropic Messages API
  translation.
- **No streaming** — `stream: true` is rejected with HTTP 400.
- Denylists are **text-pattern matches, not shell parsers** — wrappers like
  `powershell -Command` can smuggle commands past them; the interactive
  confirmation prompt is the real safety boundary.
- **Single-user, local-only** — confirmation is a terminal prompt; no web UI.
- Sessions are stored **plaintext** in SQLite; cooldowns and provider
  disables are **in-memory only**.

Full details: [docs/SECURITY.md](docs/SECURITY.md) → *Known limits*.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [docs/TESTING.md](docs/TESTING.md).
