import time

class ProviderHealth:
    def __init__(self):
        self.consecutive_failures = 0
        self.cooldown_until = None
        self.recent_request_count = 0

class HealthTracker:
    def __init__(self):
        self.healths = {}

    def get(self, provider_name: str) -> ProviderHealth:
        if provider_name not in self.healths:
            self.healths[provider_name] = ProviderHealth()
        return self.healths[provider_name]

    def record_failure(self, provider_name: str):
        health = self.get(provider_name)
        health.consecutive_failures += 1
        # 30s, 60s, 120s, 240s, capped at 300s
        cooldown_seconds = min(30 * (2 ** health.consecutive_failures), 300)
        health.cooldown_until = time.monotonic() + cooldown_seconds

    def record_success(self, provider_name: str):
        health = self.get(provider_name)
        health.consecutive_failures = 0
        health.cooldown_until = None

    def is_available(self, provider_name: str) -> bool:
        health = self.get(provider_name)
        if health.cooldown_until is None:
            return True
        if time.monotonic() > health.cooldown_until:
            return True
        return False