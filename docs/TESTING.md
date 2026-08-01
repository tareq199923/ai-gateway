# Testing

The suite is pytest + pytest-asyncio. **No real provider is ever called** —
upstream HTTP is faked with `httpx.MockTransport`, and the whole FastAPI app
is exercised in-process with `httpx.ASGITransport`.

---

## 1. Running

```bash
pip install -r requirements.txt   # or: pip install -e ".[dev]"
pytest
```

- `pytest.ini` sets `asyncio_mode = auto`, so async tests need no explicit
  markers (the two `.asyncio` markers that exist in test files are
  redundant but harmless).
- No `.env` or API keys are required — every fixture injects its own.

---

## 2. Test doubles & fixtures (`tests/conftest.py`)

- **`provider_config(tmp_path)`** — writes a temp `providers.yaml` from a
  provider dict list and returns its path.
- **`make_router`** — builds a real `Router` against a temp config with
  `httpx.MockTransport`. It sets a fake API key in the environment for every
  provider (unless the key is in `missing_keys`) and routes mock responses
  **by hostname**:

  ```python
  handlers = {
      "alpha.example.com": httpx.Response(200, json=provider_body("alpha")),
      "beta.example.com":  my_callable,      # callable gets the httpx.Request
  }
  ```

  A handler can be a static `httpx.Response` or a function that returns one
  (to record calls, inspect `request.read()`, or raise
  `httpx.ConnectError`). Unknown hosts → `500` so a typo surfaces loudly.
- **`router_setter`** — replaces `app.state.router` (the test app is the
  real `invincible.main.app`), tracking every router so the fixture can
  close its httpx client afterwards.
- **`client`** — an `httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
  base_url="http://test")`; sets `GATEWAY_API_KEY=test-gateway-key`,
  `MCP_SHARED_SECRET` is set per-test with `monkeypatch`. Uses a
  `SessionStore(db_path=":memory:")` so tests are isolated.
- **`provider_body(name, content)`** — a canned OpenAI-shaped success body.

### Pattern: confirmation tests

`tool_executor.confirm` is monkeypatched with a fake returning `True`/`False`
(defined as module-level `_true()`/`_false()` in the test files). Denylist
tests use a fake that flips a `called` flag to prove the prompt was **never**
reached for blocked commands/writes.

### Pattern: fake clock

`test_health_tracker.py` monkeypatches `time.monotonic` with an advancing
closure (`fake_clock(seconds)`), so cooldown expiry is tested without
sleeping.

---

## 3. Coverage map

| File | What it pins down |
|---|---|
| `test_api.py` | Health check; chat success; streaming → 400; missing/invalid/absent auth (401 vs open); 422 on empty body; all-providers-fail → 503; upstream 400 forwarded verbatim. |
| `test_router.py` | Lowest-tier-first success; tier sorting; failover on 429 / 5xx / network error; skipping providers in cooldown or with missing keys; 401 → permanent disable (verified across a second request); non-failover 4xx aborts with `UpstreamClientError`; all-fail raises; required-field validation. |
| `test_health_tracker.py` | Exponential curve 30→60→120→240→300 (capped); success reset; cooldown expiry restores availability; disable survives any clock advance; providers tracked independently. |
| `test_context_trimming.py` | No-op under budget; oldest turns dropped but system kept; **tool_calls never split from tool results**; most-recent-turn kept even if oversized; turn grouping; token estimation bounds; per-provider `max_context` honored (payload sizes differ). |
| `test_session_store.py` | History replayed on second request within a session; **no cross-session leakage** (session-a's secret not visible in session-b). |
| `test_timeouts.py` | Defaults when no `timeout:` block; partial override merges with defaults; full override; **real shipped `providers.yaml` parses** with the expected read timeouts (guards YAML typos). |
| `test_mcp_endpoint.py` | MCP auth (missing/wrong → 401, unset secret → 503); `tools/list` names; blocked command → `isError`; declined command; approved command executes; unknown tool → -32601; read_file success / `.env` blocked / own-source allowed; write to protected path blocked **without prompting**; JSON-RPC hardening: parse error -32700, non-object -32600, bad params -32602; notifications (no `id`) → 204 with empty body, even on param errors. |
| `test_tool_executor.py` | Parameterized denylist sweep over ~24 dangerous commands (Unix + Windows) and ~10 safe ones (incl. `rm -rf ./build`, `rd /s C:\build`, `del C:\temp\out.txt`); blocked commands never prompt; declined doesn't run/write; approved runs/writes; write denylist blocks `.env*`, `providers.yaml`, `sessions.db`, `invincible/`, `tests/`, `.git/`; read denylist blocks only secrets but **allows** `providers.yaml`, `invincible/`, `tests/`; paths outside the repo are not write-denied; read errors are structured, not exceptions. |
| `test_cli.py` | CLI registration + version; both console scripts declared in pyproject; `setup` creates/updates `.env`, preserves existing values/comments, generates secrets only when missing; `start` port validation, env-file loading, config path handling. |

---

## 4. Writing a new test — quick recipe

```python
# 1. Router-level (no HTTP app):
router = make_router(
    handlers={"alpha.example.com": lambda req: httpx.Response(429)}
)
result = await router.route_request([{"role": "user", "content": "hi"}])

# 2. API-level:
async def test_something(client, router_setter):
    router_setter(handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))})
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-gateway-key"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200

# 3. MCP tool layer (bypasses HTTP entirely):
monkeypatch.setattr(tool_executor, "confirm", lambda prompt: _true())
result = await tool_executor.execute_bash("echo hi")
```
