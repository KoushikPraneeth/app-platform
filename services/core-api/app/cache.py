import json
import logging
import os
from typing import Any, List, Optional
from fastapi import HTTPException, Request, status
import redis

logger = logging.getLogger("core-api.cache")
logging.basicConfig(level=logging.INFO)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Redis client with decode_responses=True for string handling
redis_client = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=2.0,
    socket_timeout=2.0,
)

def positive_int_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def boolean_setting(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "")
CACHE_ENABLED = boolean_setting("CACHE_ENABLED", True)
CACHE_KEY_ITEMS = f"{REDIS_KEY_PREFIX}items:latest"
CACHE_TTL_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = positive_int_setting("RATE_LIMIT_MAX_REQUESTS", 30)
RATE_LIMIT_WINDOW_SECONDS = 60


def get_client_ip(request: Request) -> str:
    """Extract client IP handling forward proxies or fallback to client host."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def rate_limiter(request: Request) -> None:
    """
    FastAPI dependency that enforces a rate limit of 30 requests/minute per client IP.
    Increments key 'ratelimit:{client_ip}' in Redis with a 60-second expiry.
    If limit is exceeded, raises HTTP 429.
    If Redis is unreachable, logs a warning and fails open.
    """
    client_ip = get_client_ip(request)
    rate_key = f"{REDIS_KEY_PREFIX}ratelimit:{client_ip}"

    try:
        pipe = redis_client.pipeline()
        pipe.incr(rate_key)
        pipe.ttl(rate_key)
        results = pipe.execute()
        current_count = results[0]
        current_ttl = results[1]

        # If key was just created or has no TTL (-1), set the 60-second window
        if current_ttl == -1:
            redis_client.expire(rate_key, RATE_LIMIT_WINDOW_SECONDS)

        if current_count > RATE_LIMIT_MAX_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
    except HTTPException:
        # Re-raise rate limit HTTP 429 without swallowing
        raise
    except Exception as exc:
        # Fail-open if Redis is unreachable or throws a connection error
        logger.warning("Redis rate limiter unavailable (failing open): %s", exc)


def get_cached_items() -> Optional[List[dict]]:
    """Retrieve cached latest items from Redis, or return None on cache miss or error."""
    if not CACHE_ENABLED:
        return None
    try:
        data = redis_client.get(CACHE_KEY_ITEMS)
        if data:
            return json.loads(data)
    except Exception as exc:
        logger.warning("Redis cache read failure: %s", exc)
    return None


def set_cached_items(items: List[dict]) -> bool:
    """Store serialized items in Redis with 60-second TTL."""
    if not CACHE_ENABLED:
        return False
    try:
        serialized = json.dumps(items)
        redis_client.setex(CACHE_KEY_ITEMS, CACHE_TTL_SECONDS, serialized)
        return True
    except Exception as exc:
        logger.warning("Redis cache write failure: %s", exc)
        return False


def invalidate_items_cache() -> bool:
    """Delete items cache key from Redis."""
    try:
        redis_client.delete(CACHE_KEY_ITEMS)
        return True
    except Exception as exc:
        logger.warning("Redis cache invalidation failure: %s", exc)
        return False
