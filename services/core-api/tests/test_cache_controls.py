from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import cache


def test_production_configuration_defaults():
    assert cache.CACHE_ENABLED is True
    assert cache.CACHE_KEY_ITEMS == "items:latest"
    assert cache.RATE_LIMIT_MAX_REQUESTS == 30
    assert cache.RATE_LIMIT_WINDOW_SECONDS == 60


def test_cache_bypass_does_not_call_redis(monkeypatch):
    client = Mock()
    monkeypatch.setattr(cache, "redis_client", client)
    monkeypatch.setattr(cache, "CACHE_ENABLED", False)
    assert cache.get_cached_items() is None
    assert cache.set_cached_items([{"id": 1}]) is False
    client.get.assert_not_called()
    client.setex.assert_not_called()


def test_rate_limit_is_still_enforced_when_cache_disabled(monkeypatch):
    client = Mock()
    client.pipeline.return_value.execute.return_value = [31, 59]
    monkeypatch.setattr(cache, "redis_client", client)
    monkeypatch.setattr(cache, "CACHE_ENABLED", False)
    request = Request({"type": "http", "headers": [], "client": ("192.0.2.1", 1234)})
    with pytest.raises(HTTPException) as error:
        cache.rate_limiter(request)
    assert error.value.status_code == 429


def test_benchmark_rate_limit_uses_separate_keyspace(monkeypatch):
    client = Mock()
    client.pipeline.return_value.execute.return_value = [50, 59]
    monkeypatch.setattr(cache, "redis_client", client)
    monkeypatch.setattr(cache, "RATE_LIMIT_MAX_REQUESTS", 1000000)
    monkeypatch.setattr(cache, "REDIS_KEY_PREFIX", "bench:test:")
    request = Request({"type": "http", "headers": [], "client": ("192.0.2.1", 1234)})
    cache.rate_limiter(request)
    client.pipeline.return_value.incr.assert_called_once_with("bench:test:ratelimit:192.0.2.1")


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_rate_limit_configuration_is_rejected(monkeypatch, value):
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", value)
    with pytest.raises(ValueError):
        cache.positive_int_setting("RATE_LIMIT_MAX_REQUESTS", 30)


def test_invalid_cache_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("CACHE_ENABLED", "yes")
    with pytest.raises(ValueError):
        cache.boolean_setting("CACHE_ENABLED", True)
