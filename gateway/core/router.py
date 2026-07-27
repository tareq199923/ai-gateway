# gateway/core/router.py
import os
import httpx
import yaml
import logging
from gateway.core.provider_health import HealthTracker

logger = logging.getLogger("gateway.router")

class Router:
    def __init__(self, config_path=None):
        if config_path is None:
            # Resolve providers.yaml relative to this file's location
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            config_path = os.path.join(base_dir, "providers.yaml")
            
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        self.providers = config.get("providers", [])
        self.providers.sort(key=lambda p: p["tier"])
        self.health_tracker = HealthTracker()
        self.client = httpx.AsyncClient()

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
            payload = {
                "model": provider["model_id"],
                "messages": messages
            }

            try:
                resp = await self.client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=30.0
                )
                
                if resp.status_code == 429 or resp.status_code >= 500:
                    logger.warning(f"Provider {name} returned {resp.status_code}. Triggering failover.")
                    self.health_tracker.record_failure(name)
                    continue
                    
                resp.raise_for_status()
                self.health_tracker.record_success(name)
                return resp.json()
                    
            except httpx.RequestError as e:
                logger.error(f"Network error with {name}: {e}. Triggering failover.")
                self.health_tracker.record_failure(name)
                continue
                
        raise Exception("All providers failed or are in cooldown.")

    async def close(self):
        await self.client.aclose()