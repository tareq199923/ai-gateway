# invincible/core/router.py
import os
import json
import httpx
import yaml
import logging
from invincible.core.provider_health import HealthTracker

logger = logging.getLogger("invincible.router")

DEFAULT_TIMEOUT_CONFIG = {"connect": 5.0, "read": 60.0, "write": 5.0, "pool": 2.0}


def resolve_timeout(provider: dict) -> httpx.Timeout:
    """Build an httpx.Timeout for a provider, using its own `timeout:` block
    from providers.yaml where present, falling back to DEFAULT_TIMEOUT_CONFIG
    field-by-field for anything the provider doesn't override."""
    cfg = {**DEFAULT_TIMEOUT_CONFIG, **(provider.get("timeout") or {})}
    return httpx.Timeout(
        connect=cfg["connect"], read=cfg["read"], write=cfg["write"], pool=cfg["pool"]
    )

DEFAULT_MAX_CONTEXT = 32000
RESERVE_TOKENS = 1000  # headroom left for the provider's own response


def estimate_tokens(message: dict) -> int:
    """Rough token estimate: ~4 chars per token. This is a heuristic, not an
    exact tokenizer match - it will over/under-count on code-heavy content,
    but it's cheap and good enough to decide what to drop, not to bill by."""
    return max(1, len(json.dumps(message)) // 4)


def group_into_turns(messages: list) -> list:
    """Group non-system messages into turns, where a new turn starts at each
    user message. This keeps an assistant's tool_calls together with the
    tool result message(s) that answer them and the eventual assistant
    follow-up, since all of those belong to the same user turn and must
    never be split apart when trimming."""
    turns = []
    current = []
    for m in messages:
        if m.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(m)
    if current:
        turns.append(current)
    return turns


def trim_messages(messages: list, max_context: int, reserve_tokens: int = RESERVE_TOKENS) -> list:
    """Keep all system messages, then keep as many of the most recent turns
    as fit inside max_context (minus reserve_tokens for the response).
    Always keeps at least the single most recent turn, even if it alone
    exceeds budget - there's nothing better to send in that case.
    Turns are dropped as atomic units so a tool_call is never separated
    from its tool result."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    def turn_tokens(turn):
        return sum(estimate_tokens(m) for m in turn)

    system_tokens = sum(estimate_tokens(m) for m in system_msgs)
    budget = max(max_context - reserve_tokens - system_tokens, 0)

    turns = group_into_turns(rest)
    if not turns:
        return system_msgs

    kept = [turns[-1]]
    used = turn_tokens(turns[-1])

    for turn in reversed(turns[:-1]):
        t = turn_tokens(turn)
        if used + t > budget:
            break
        kept.insert(0, turn)
        used += t

    return system_msgs + [m for turn in kept for m in turn]

class UpstreamClientError(Exception):
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body
        super().__init__(str(body))

class Router:
    def __init__(self, config_path=None, transport=None):
        if config_path is None:
            # Resolve providers.yaml relative to this file's location
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            config_path = os.path.join(base_dir, "providers.yaml")
            
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        self.providers = config.get("providers", [])
        required_fields = {"name", "tier", "base_url", "api_key_env", "model_id"}
        for provider in self.providers:
            missing = required_fields - set(provider.keys())
            if missing:
                raise ValueError(
                    f"Provider '{provider.get('name', 'unnamed')}' is missing required "
                    f"field(s): {', '.join(sorted(missing))}"
                )
        self.providers.sort(key=lambda p: p["tier"])
        for provider in self.providers:
            if not os.getenv(provider["api_key_env"]):
                logger.warning(
                    f"Provider '{provider['name']}' has no API key set via "
                    f"{provider['api_key_env']}. It will be unavailable."
                )
        self.health_tracker = HealthTracker()
        self.client = httpx.AsyncClient(transport=transport)

    async def route_request(self, messages: list) -> dict:
        for provider in self.providers:
            name = provider["name"]
            
            if not self.health_tracker.is_available(name):
                logger.info(f"Provider {name} in cooldown. Skipping.")
                continue

            api_key = os.getenv(provider["api_key_env"])
            if not api_key:
                logger.warning(f"No API key found for {name} ({provider['api_key_env']}). Skipping.")
                continue

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            trimmed_messages = trim_messages(
                messages, provider.get("max_context", DEFAULT_MAX_CONTEXT)
            )
            payload = {
                "model": provider["model_id"],
                "messages": trimmed_messages
            }

            try:
                resp = await self.client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=resolve_timeout(provider)
                )
                
                if resp.status_code == 429 or resp.status_code >= 500:
                    logger.warning(f"Provider {name} returned {resp.status_code}. Triggering failover.")
                    self.health_tracker.record_failure(name)
                    continue
                    
                resp.raise_for_status()
                self.health_tracker.record_success(name)
                return resp.json()
                    
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    logger.warning(f"Auth error from {name} ({status}). Disabling provider.")
                    self.health_tracker.disable(name)
                    continue
                body = await e.response.aread()
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"raw": body.decode(errors="replace")}
                raise UpstreamClientError(status_code=status, body=parsed) from e
                    
            except httpx.RequestError as e:
                logger.error(f"Network error with {name}: {e}. Triggering failover.")
                self.health_tracker.record_failure(name)
                continue
                
        raise Exception("All providers failed or are in cooldown.")

    async def close(self):
        await self.client.aclose()