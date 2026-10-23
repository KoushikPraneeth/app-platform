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


def test_slow_cache_population_does_not_hold_database_connection(monkeypatch, tmp_path):
    """A stalled Redis write must not starve another read of the sole SQL connection."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import QueuePool
    from app.database import Base
    from app.models import Item

    engine = create_engine('sqlite:///' + str(tmp_path / 'pool.db'),
                           connect_args={'check_same_thread': False}, poolclass=QueuePool,
                           pool_size=1, max_overflow=0, pool_timeout=0.2)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(Item(username='@test', community='r/general', content='fixture'))
        db.commit()

    def dependency():
        with sessions() as db:
            yield db

    entered = threading.Event(); release = threading.Event(); lock = threading.Lock()
    first = True

    def slow_first_population(items):
        nonlocal first
        with lock:
            should_block = first
            first = False
        if should_block:
            entered.set()
            release.wait(timeout=3)
        return True

    monkeypatch.setattr(main, 'get_cached_items', lambda: None)
    monkeypatch.setattr(main, 'set_cached_items', slow_first_population)
    previous = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[rate_limiter] = lambda: None
    main.app.dependency_overrides[get_db] = dependency

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            blocked = asyncio.create_task(client.get('/api/items'))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                second = await asyncio.wait_for(client.get('/api/items'), timeout=1)
                assert second.status_code == 200
                assert not blocked.done()
            finally:
                release.set()
                await blocked

    try:
        asyncio.run(exercise())
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
        engine.dispose()
