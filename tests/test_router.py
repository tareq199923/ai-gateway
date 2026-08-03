import httpx
import pytest

from invincible.core.router import UpstreamClientError
from tests.conftest import default_providers, provider_body

MESSAGES = [{"role": "user", "content": "hi"}]


class _TrackingResponse(httpx.Response):
    """httpx.Response subclass that records whether aclose() was awaited."""

    def __init__(self, *args, **kwargs):
        self.aclosed = False
        super().__init__(*args, **kwargs)

    async def aclose(self):
        self.aclosed = True
        await super().aclose()


async def test_success_returns_lowest_tier_provider(make_router):
    alpha_body = provider_body("alpha")
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(200, json=alpha_body),
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
            "gamma.example.com": httpx.Response(200, json=provider_body("gamma")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert result == alpha_body
    assert router.health_tracker.get("alpha").consecutive_failures == 0


async def test_providers_sorted_by_tier(make_router):
    providers = list(reversed(default_providers()))
    alpha_body = provider_body("alpha")
    router = make_router(
        providers=providers,
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)},
    )
    assert [p["name"] for p in router.providers] == ["alpha", "beta", "gamma"]
    result = await router.route_request(MESSAGES)
    assert result == alpha_body


async def test_failover_on_429(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(429)

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert calls == ["alpha", "beta"]
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")


async def test_failover_on_5xx(make_router):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(503),
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")


async def test_failover_closes_unread_response_on_429(make_router):
    responses = []

    def alpha_handler(request):
        resp = _TrackingResponse(429)
        responses.append(resp)
        return resp

    def beta_handler(request):
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    assert responses[0].aclosed


async def test_auth_failure_closes_unread_response(make_router):
    responses = []

    def alpha_handler(request):
        resp = _TrackingResponse(401)
        responses.append(resp)
        return resp

    def beta_handler(request):
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")
    assert responses[0].aclosed


async def test_failover_on_network_error(make_router):
    def alpha_handler(request):
        raise httpx.ConnectError("connection refused")

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")


async def test_skips_provider_in_cooldown(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    router.health_tracker.record_failure("alpha")
    result = await router.route_request(MESSAGES)
    assert calls == []
    assert result == provider_body("beta")


async def test_skips_provider_with_missing_api_key(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        },
        missing_keys=["ALPHA_API_KEY"],
    )
    result = await router.route_request(MESSAGES)
    assert calls == []
    assert result == provider_body("beta")


async def test_auth_failure_disables_provider(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(401, json={"error": "unauthorized"})

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert calls == ["alpha"]
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")

    result = await router.route_request(MESSAGES)
    assert calls == ["alpha"]
    assert result == provider_body("beta")


async def test_non_failover_upstream_error_raises_client_error(make_router):
    error_body = {"error": {"message": "bad request"}}
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(400, json=error_body)

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    with pytest.raises(UpstreamClientError) as excinfo:
        await router.route_request(MESSAGES)
    assert excinfo.value.status_code == 400
    assert excinfo.value.body == error_body
    assert calls == ["alpha"]


async def test_all_providers_fail_raises(make_router):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(503),
            "gamma.example.com": httpx.Response(500),
        }
    )
    with pytest.raises(Exception, match="All providers failed"):
        await router.route_request(MESSAGES)


def test_missing_required_field_raises(make_router):
    providers = [
        {"name": "bad", "tier": 1, "base_url": "https://bad.example.com/v1"}
    ]
    with pytest.raises(ValueError, match="api_key_env"):
        make_router(providers=providers, handlers={})
