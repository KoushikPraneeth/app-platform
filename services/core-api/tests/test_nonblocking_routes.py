import asyncio
import threading

import httpx

from app import main
from app.cache import rate_limiter
from app.database import get_db


def test_liveness_remains_responsive_while_cache_io_is_blocked(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_cache_read():
        started.set()
        release.wait(timeout=2)
        return [{"id": 1, "username": "@probe", "community": "r/general", "content": "fixture",
                 "upvotes": 0, "created_at": "2026-01-01T00:00:00Z"}]

    monkeypatch.setattr(main, "get_cached_items", blocked_cache_read)
    previous = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[rate_limiter] = lambda: None
    main.app.dependency_overrides[get_db] = lambda: None

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            read = asyncio.create_task(client.get("/api/items"))
            try:
                assert await asyncio.to_thread(started.wait, 1)
                assert not read.done(), "Blocking cache I/O prevented the event loop from running"
                health = await client.get("/healthz")
                assert health.status_code == 200
                assert not read.done(), "Liveness waited for the blocked business request"
            finally:
                release.set()
            assert (await read).status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
