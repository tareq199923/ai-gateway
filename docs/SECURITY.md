# Security Model

Invincible exposes two attack-relevant surfaces: a chat proxy that calls
upstream AI providers, and an MCP tool server that can **run shell commands
and write files on the host machine**. This document describes exactly what
guards what, where the boundaries are, and — explicitly — where they are not.

---

## 1. Two independent auth realms

The two endpoints authenticate **independently**, with separate secrets.
Rotating one never affects the other, and a leaked tunnel URL alone is not
enough to reach tool execution.

### `/v1/*` — `GATEWAY_API_KEY`

| Aspect | Value |
|---|---|
| Header | `Authorization: Bearer <key>` |
| Comparison | plain `==` string comparison (not timing-safe) |
| If unset | **Endpoint is open** — no auth enforced at all |
| Failure | HTTP 401, body `{"error": {"message": "...", "type": "auth_error"}}` |

Implemented in `invincible/main.py::require_auth`. Note the open-if-unset
behavior: on a dev box without a key, anyone who can reach the port can use
your provider credits. Set the key.

### `/mcp` — `MCP_SHARED_SECRET`

| Aspect | Value |
|---|---|
| Header | `X-MCP-Secret: <secret>` |
| Comparison | `secrets.compare_digest` (timing-safe — no byte-by-byte guessing) |
| If unset | **HTTP 503 — endpoint disabled** (never open) |
| Failure | HTTP 401, body `{"error": {"message": "...", "type": "auth_error"}}` |

Implemented in `invincible/endpoints/mcp.py::require_mcp_auth`.

### The layering principle

`tool_executor.py` (the code that actually runs commands and writes files)
**assumes the caller is already authenticated**. It decides only whether a
specific action is safe and approved — never who is allowed to ask. Auth is
entirely the endpoint dependency's job, one layer up.

---

## 2. The MCP tool security stack

For `execute_bash` and `write_file`, three gates run in order:

```
authenticated caller (MCP_SHARED_SECRET)
        │
        ▼
1. Denylist  ── matches? ──► ToolBlocked  → "Blocked: <reason>"  (no prompt)
        │ no
        ▼
2. Confirmation ── operator says n / EOF? ──► ToolDeclined → "Declined by operator"
        │ yes
        ▼
3. Execution (with 30s timeout for commands)
```

`read_file` has no confirmation step (reading is non-destructive); its
denylist is the only gate.

### 2.1 `execute_bash` denylist — full inventory

Matched against the **full command string**, case-insensitive
(`re.I`). These are text-pattern matches, **not** shell parsing — see
[Known limits](#6-known-limits).

| Pattern (abridged) | Reason |
|---|---|
| `rm` with `-r`+`-f` flags targeting `/`, `~`, or `$HOME` | Recursive force-delete of home or root |
| `rm -r` targeting `/` alone | Recursive delete starting at filesystem root |
| `:(){ :|:& };:` | Fork bomb |
| `dd ... of=/dev/...` | Raw write to a block device |
| `mkfs` / `mkfs.ext4` / any `mkfs.*` | Filesystem format command |
| `> /dev/sd*|nvme*|hd*|disk*` | Redirect writing directly to a disk device |
| `shutdown`, `reboot`, `halt`, `poweroff` (word-boundary) | System power/shutdown command |
| `sudo` (word-boundary) | Privilege escalation via sudo |
| `chmod -R 777 /` (or `chmod 777 /`) | World-writable permissions on filesystem root |
| `chown -R <user> /` | Recursive ownership change on filesystem root |
| `curl|wget ... \| (sudo )?sh|bash|zsh` | Piping a remote download straight into a shell |
| `kill -9 -1` | Kill all processes |
| `> /etc/passwd|shadow|sudoers` | Overwrite of a core system credentials file |
| `rd`/`rmdir`/`del`/`erase` with `/s` flag **and** a drive-root target (`C:\`, `C:\*`, `C:\*.*`) | Recursive delete targeting a Windows drive root |
| `format <letter>:` | Formatting a Windows drive |

Windows notes: flags can appear in either order around the target (`del /s /q
C:\*.*` vs `del /q /s C:\*.*`) — the regexes use lookaheads that scan the
whole command rather than anchoring to a fixed position. A **subdirectory**
target (`rd /s C:\build`, `rm -rf ./build`, `rm -rf /home/user`) deliberately
does **not** match — that is the Windows/Unix equivalent of a local cleanup
and is left to the confirmation prompt, same as any other command.

### 2.2 `write_file` path denylist — full inventory

Blocks writes outright (confirmation never reached) to paths that resolve
**inside the repo root** and match:

| Pattern (relative, case-insensitive) | Reason |
|---|---|
| `.env` / `.env.*` | Invincible's own secrets file |
| `providers.yaml` | Provider configuration |
| `sessions.db` | The session database |
| `invincible/` (any file under it) | Invincible's own source code |
| `tests/` (any file under it) | The test suite |
| `.git/` (any file under it) | Git internals |

### 2.3 `read_file` denylist — full inventory

Narrower than the write list **on purpose**: allowing a cloud AI to *see* the
source code is the entire point of the tool, and `providers.yaml` only holds
`api_key_env` **names**, not actual key values, so it is not a secret. Only
things that would leak an actual credential or sensitive local state over the
tunnel are blocked:

| Pattern (relative, case-insensitive) | Reason |
|---|---|
| `.env` / `.env.*` | Invincible's own secrets file |
| `sessions.db` | The session database (contains plaintext conversation history) |
| `.git/` (any file under it) | Git internals (history may contain secrets) |

Everything else — including `invincible/`, `tests/`, and `providers.yaml` —
**is** readable without confirmation.

### 2.4 Path resolution rules

- The repo root is resolved from `tool_executor.py`'s own location (three
  `dirname()` calls up), so it works from a checkout, an editable install, or
  a wheel.
- A candidate path is `os.path.abspath()`-ed and relativized to the repo
  root:
  - **Inside the repo** → patterns matched against the relative path.
  - **Outside the repo** (relpath starts with `..`) → not denied; for
    writes, the confirmation prompt is the gate (explicitly a different risk
    profile).
  - **Different Windows drive** (`ValueError` from `relpath`) → not inside
    the repo, not denied.
- Matching is case-insensitive on purpose: Windows treats `.env` and `.ENV`
  as the same file, so a differently-cased target must not slip past.
- A trailing `/` on a pattern like `invincible/` only matters for the
  relative path prefix — `invincible\main.py` works because the relativized
  path has separators normalized to `/` first.

---

## 3. The confirmation prompt

Every `execute_bash` and `write_file` call that survives the denylist prints
what the cloud AI wants to do and blocks until the operator answers at the
**terminal running the server**:

```
[MCP] Cloud AI wants to run:
  $ rm -rf ./build
Allow this command? [y/N]:

[MCP] Cloud AI wants to write 12345 bytes to:
  C:\Users\me\project\scratch\notes.txt
Allow this write? [y/N]:
```

- Accepts `y` or `yes` (case-insensitive); anything else — including plain
  Enter and EOF — declines.
- `input()` runs on a worker thread via `asyncio.to_thread`, so the event
  loop stays free for other in-flight requests while waiting on the operator.
- Denylist hits short-circuit **before** any prompt (verified by tests).

---

## 4. The chat endpoint's security posture

- **Auth**: `GATEWAY_API_KEY` (see above). Unset = open.
- **Sessions**: `session_id` (from `X-Session-Id`) is a **partition key, not
  a credential**. Anyone authenticated to the endpoint can read/write any
  session id. History is stored as **plaintext JSON in SQLite**
  (`sessions.db`, gitignored).
- **Upstream keys**: API keys are read from the environment by *name*
  (`api_key_env`), never stored in `providers.yaml`.
- **Failure data**: a provider's `401/403` response body is never forwarded
  to the client (the provider is silently disabled instead); other upstream
  errors are forwarded verbatim.

---

## 5. Operational hardening (JSON-RPC layer)

- Malformed JSON body → `-32700 Parse error` (id `null`).
- Non-object body → `-32600 Invalid Request`.
- Non-dict `params` → `-32602 Invalid params`.
- Unknown method/tool → `-32601`.
- Requests **without an `id`** are JSON-RPC *notifications*: the side effect
  (if any) still runs, but the server replies `204 No Content` with no body —
  even on error. See [docs/MCP_PROTOCOL.md](MCP_PROTOCOL.md).

---

## 6. Known limits

These are design decisions, documented so nobody mistakes the denylist for a
sandbox:

1. **The denylist is a text match, not a shell parser.** `powershell -Command
   "..."`, `cmd /c "..."`, encoding tricks, or any wrapper can smuggle an
   arbitrary command past every pattern. The denylist exists to catch the
   obvious, high-blast-radius cases without a prompt — **the interactive
   confirmation prompt is the genuine safety boundary. Read what you
   approve.**
2. **Confirmation is single-user and local-only.** It is a synchronous
   terminal prompt addressed to whoever is sitting at the machine. There is
   no web UI, no second HTTP round-trip approval surface, no audit log.
3. **`/v1/*` is unauthenticated if `GATEWAY_API_KEY` is unset.** Forgetting
   the key opens your provider credits to anyone who can reach the port.
4. **Auth is a shared secret, not identity.** No per-user model; anyone with
   the MCP secret is the operator for every session.
5. **Sessions persist plaintext.** `sessions.db` contains full conversation
   history unencrypted; the `.env` and `sessions.db` denylist entries exist
   precisely so a remote AI cannot exfiltrate them.
6. **Provider disable is process-scoped.** A provider disabled by a 401/403
   stays disabled until the process restarts; cooldowns are in-memory only.
7. **`/v1` auth comparison is not timing-safe.** `GATEWAY_API_KEY` uses plain
   `==`; the MCP secret uses `secrets.compare_digest`. The chat key protects
   provider credits, not tool execution — but if you want defense in depth,
   prefer long random tokens (the CLI generates `token_urlsafe(32)`).
