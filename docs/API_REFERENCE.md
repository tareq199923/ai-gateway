# API Reference — `/v1/chat/completions`

The OpenAI-compatible chat surface. A single endpoint, plus a health check.
The provider behind it is chosen by the router, not by the client.

---

## 1. Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | Health check → `{"status": "healthy"}` |
| `POST` | `/v1/chat/completions` | `Authorization: Bearer <GATEWAY_API_KEY>` | Chat completion with failover |

Auth details: if `GATEWAY_API_KEY` is **unset**, the chat endpoint is open
(no auth enforced). Wrong/missing key with the key set → `401`.

---

## 2. Request

```
POST /v1/chat/completions
Authorization: Bearer <GATEWAY_API_KEY>          # required if key is set
X-Session-Id: <id>                               # optional, default "default"
Content-Type: application/json
```

### Body (`ChatRequest`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `messages` | array of objects | **yes** | Standard OpenAI messages (`role`, `content`, optional `tool_calls`/`tool_call_id`). |
| `stream` | boolean | no | **Must be `false`/absent** — `true` → `400`. Streaming is not supported. |

Other OpenAI fields (`model`, `temperature`, `max_tokens`, …) are **not
accepted** — `ChatRequest` only defines `messages` and `stream`, so sending
extra fields yields `422 Unprocessable Entity` from Pydantic (strict by
default). The upstream `model` is set per-provider from `providers.yaml`,
never from the client.

### Sessions

- History is loaded from SQLite keyed by `X-Session-Id` (default
  `default`), **prepended** to the request's `messages`, sent upstream.
- On a successful reply, the assistant message (`choices[0].message`) is
  appended and the full conversation persisted back (upsert).
- `session_id` is a **partition key, not a credential** — any authenticated
  caller may read/write any session id.
- Trimming happens per-provider at send time; the stored history is
  untrimmed (the raw conversation accumulates in the DB).

---

## 3. Response

**Success (`200`)**: the upstream provider's JSON is forwarded **verbatim**
— no normalization. Shape is standard OpenAI:

```json
{
  "id": "cmpl-...",
  "model": "gemini-2.5-flash",
  "choices": [
    {"message": {"role": "assistant", "content": "Hello!"}}
  ]
}
```

Session persistence only happens if `choices[0].message` exists.

---

## 4. Status codes & error semantics

| Status | When | Body shape |
|---|---|---|
| `200` | Upstream success | Upstream body verbatim |
| `400` | `stream: true` | `{"error": {"message": "Streaming is not currently supported by this server.", "type": "invalid_request_error"}}` |
| `401` | Missing/invalid `GATEWAY_API_KEY` | `{"detail": {"error": {"message": "...", "type": "auth_error"}}}` (FastAPI HTTPException) |
| `422` | Body fails Pydantic validation (missing `messages`, extra fields) | FastAPI validation detail |
| `4xx` (forwarded) | Upstream returned a non-failover error (see below) | **Upstream's own error body**, status copied |
| `503` | All providers failed / in cooldown, or an unexpected exception | `{"error": {"message": "All providers failed or are in cooldown.", "type": "gateway_error"}}` |

### Failover semantics (per upstream response)

The router (`invincible/core/router.py::route_request`) tries providers in
`tier` ascending order. Per attempt:

| Upstream status | Router behavior |
|---|---|
| `200` | `record_success` (resets cooldown), return body |
| `429` or `5xx` | `record_failure` → cooldown → **try next provider** |
| `401` / `403` | `disable` (permanent for process lifetime) → **try next provider** |
| Other `4xx` (e.g. `400`) | **Abort immediately** — raise `UpstreamClientError`, which the endpoint forwards with the provider's status and body. No failover. |
| Network error (`httpx.RequestError`) | `record_failure` → cooldown → **try next provider** |
| Provider in cooldown / no API key | Skipped silently (log only) |

Exhausted all providers (including all in cooldown) → the `503` above.

### Cooldown curve

`record_failure` sets `cooldown_until = now + min(30 * 2**(failures-1), 300)`:
**30s → 60s → 120s → 240s → 300s (cap).** `record_success` resets the
counter and clears the cooldown. `disable` (401/403) blocks the provider
forever — both cooldowns and disables are **in-memory only** and reset on
process restart.

---

## 5. Context trimming (per provider)

Before each upstream call the conversation is trimmed to the provider's
`max_context` (default `32000` tokens):

- All `system` messages are always kept.
- The remaining messages are grouped into **turns** (a new turn starts at
  each `user` message) and dropped oldest-first as atomic units, so an
  assistant `tool_calls` is never separated from its tool results.
- A `1000`-token reserve is subtracted for the provider's response.
- The most recent turn is **always** sent, even if it alone exceeds the
  budget.
- Token estimation is a heuristic: `len(json.dumps(message)) // 4`
  (~4 chars/token).

Full detail: [docs/ARCHITECTURE.md](ARCHITECTURE.md) → *Context trimming*.

---

## 6. Timeouts

Per-provider split timeouts (defaults `connect 5s / read 60s / write 5s /
pool 2s`, overridable in `providers.yaml`). The shipped config gives Gemini
`90s`, Groq `45s`, and the OpenRouter fallback `20s` reads. See
[docs/CONFIGURATION.md](CONFIGURATION.md).
