import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cache import (
    CACHE_ENABLED,
    get_cached_items,
    invalidate_items_cache,
    rate_limiter,
    redis_client,
    set_cached_items,
)
from app.database import Base, engine, get_db
from app.models import Item
from app.schemas import (
    HealthResponse,
    ItemCreate,
    ItemListResponse,
    ItemResponse,
    ReadinessResponse,
    UpvoteResponse,
)

logger = logging.getLogger("core-api")
logging.basicConfig(level=logging.INFO)

NOTIFY_SERVICE_URL = os.getenv("NOTIFY_SERVICE_URL", "http://notify-service:8001")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context:
    1. Auto-create database tables on startup.
    2. Seamlessly ensure table columns exist and relax legacy NOT NULL constraints.
    """
    try:
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        -- Relax legacy NOT NULL constraints if table was created previously
                        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='title') THEN
                            ALTER TABLE items ALTER COLUMN title DROP NOT NULL;
                        END IF;
                        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='author') THEN
                            ALTER TABLE items ALTER COLUMN author DROP NOT NULL;
                        END IF;

                        -- Ensure new community columns exist
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='username') THEN
                            ALTER TABLE items ADD COLUMN IF NOT EXISTS username VARCHAR(100) DEFAULT '@anonymous';
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='community') THEN
                            ALTER TABLE items ADD COLUMN IF NOT EXISTS community VARCHAR(100) DEFAULT 'r/general';
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='content') THEN
                            ALTER TABLE items ADD COLUMN IF NOT EXISTS content TEXT DEFAULT '';
                            IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='title') THEN
                                UPDATE items SET content = title WHERE content = '';
                            END IF;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='items' AND column_name='upvotes') THEN
                            ALTER TABLE items ADD COLUMN IF NOT EXISTS upvotes INTEGER DEFAULT 0;
                        END IF;
                    END $$;
                    """
                )
            )
            # create_all does not add an index to an already-existing table.
            # Serialize this small schema upgrade across API startup processes.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('threadops:feed-order-index'))"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_items_created_at_desc ON items (created_at DESC)"))
            conn.commit()
        logger.info("Database tables and community schema synchronized successfully.")
    except Exception as exc:
        logger.error("Failed to initialize database tables on startup: %s", exc)
    with httpx.Client(timeout=2.0) as notify_client:
        app.state.notify_client = notify_client
        yield


app = FastAPI(
    title="ThreadOps Core API",
    description="Primary API service providing persistence, Redis caching, rate limiting, and community threads",
    version="2.0.0",
    lifespan=lifespan,
)

# Configure CORS
is_wildcard = CORS_ORIGINS == "*"
origins = ["*"] if is_wildcard else [origin.strip() for origin in CORS_ORIGINS.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=not is_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Cache"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Ensure unexpected errors return structured JSON with CORS headers."""
    logger.error("Unhandled API exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": str(exc)},
    )


# Instrument Prometheus metrics
Instrumentator().instrument(app).expose(app)


@app.get("/healthz", response_model=HealthResponse, tags=["Health"])
async def healthz():
    """Liveness probe: returns HTTP 200 if container process is running."""
    return {"status": "alive"}


@app.get("/readyz", tags=["Health"])
@app.get("/api/readyz", tags=["Health"])
def readyz():
    """
    Readiness probe: validates connectivity to PostgreSQL and Redis.
    Returns HTTP 200 if both are reachable, otherwise HTTP 503 with diagnostic details.
    """
    postgres_ok = False
    redis_ok = False
    errors: List[str] = []

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        postgres_ok = True
    except Exception as exc:
        errors.append(f"PostgreSQL connection failed: {exc}")

    try:
        if redis_client.ping():
            redis_ok = True
        else:
            errors.append("Redis ping returned false")
    except Exception as exc:
        errors.append(f"Redis connection failed: {exc}")

    if not (postgres_ok and redis_ok):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "postgres": "connected" if postgres_ok else "disconnected",
                "redis": "connected" if redis_ok else "disconnected",
                "details": "; ".join(errors),
            },
        )

    return {
        "status": "ready",
        "postgres": "connected",
        "redis": "connected",
    }


@app.get(
    "/api/items",
    response_model=ItemListResponse,
    dependencies=[Depends(rate_limiter)],
    tags=["Community Posts"],
)
def list_items(response: Response, db: Session = Depends(get_db)):
    """
    Retrieve latest 25 community posts using Cache-Aside pattern:
    1. Check Redis key 'items:latest'
    2. On Cache HIT: Return items from Redis, set header 'X-Cache: HIT'
    3. On Cache MISS: Query PostgreSQL, write to Redis (60s TTL), set header 'X-Cache: MISS'
    Rate-limited to 30 requests/minute per client IP.
    """
    cached_items = get_cached_items()
    if cached_items is not None:
        response.headers["X-Cache"] = "HIT"
        return {"source": "cache", "items": cached_items}

    # Query PostgreSQL
    db_items = db.query(Item).order_by(Item.created_at.desc()).limit(25).all()

    serialized_items = [
        {
            "id": item.id,
            "username": item.username or "@anonymous",
            "community": item.community or "r/general",
            "content": item.content or item.title or "",
            "upvotes": item.upvotes or 0,
            "created_at": item.created_at.isoformat() if hasattr(item.created_at, "isoformat") else str(item.created_at),
        }
        for item in db_items
    ]

    # Populate cache
    set_cached_items(serialized_items)

    response.headers["X-Cache"] = "MISS" if CACHE_ENABLED else "BYPASS"
    return {"source": "database", "items": serialized_items}


@app.post(
    "/api/items",
    response_model=ItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter)],
    tags=["Community Posts"],
)
def create_item(item_in: ItemCreate, request: Request, db: Session = Depends(get_db)):
    """
    Publish a new community post:
    1. Persist to PostgreSQL (populating legacy columns for backward compatibility)
    2. Invalidate Redis cache key 'items:latest'
    3. Emit a best-effort audit event to notify-service
    Rate-limited to 30 requests/minute per client IP.
    """
    handle = item_in.username.strip()
    if not handle.startswith("@"):
        handle = f"@{handle}"

    content_str = item_in.content.strip()

    new_item = Item(
        username=handle,
        community=item_in.community.strip() or "r/general",
        content=content_str,
        upvotes=0,
        title=content_str[:255],  # populates legacy column if exists
        author=handle,            # populates legacy column if exists
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)

    # Invalidate cache
    invalidate_items_cache()

    # Emit best-effort event using the shared notification connection pool
    event_payload = {
        "event_type": "POST_CREATED",
        "item_id": new_item.id,
        "title": f"[{new_item.community}] {new_item.content[:60]}",
        "author": new_item.username,
        "timestamp": new_item.created_at.isoformat() if hasattr(new_item.created_at, "isoformat") else str(new_item.created_at),
    }

    try:
        endpoint = f"{NOTIFY_SERVICE_URL.rstrip('/')}/events"
        request.app.state.notify_client.post(endpoint, json=event_payload)
    except Exception as exc:
        print(f"Failed to deliver event to notify-service: {exc}", flush=True)

    return new_item


@app.post(
    "/api/items/{item_id}/upvote",
    response_model=UpvoteResponse,
    dependencies=[Depends(rate_limiter)],
    tags=["Community Posts"],
)
def upvote_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Upvote a community post:
    1. Increment upvote counter in PostgreSQL
    2. Invalidate Redis cache key 'items:latest'
    3. Emit a best-effort upvote audit event to notify-service
    """
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post #{item_id} not found",
        )

    item.upvotes = (item.upvotes or 0) + 1
    db.commit()
    db.refresh(item)

    # Invalidate cache so next read reflects updated counter
    invalidate_items_cache()

    # Emit upvote audit event to notify-service
    event_payload = {
        "event_type": "POST_UPVOTED",
        "item_id": item.id,
        "title": f"Upvoted post in {item.community} (Score: {item.upvotes})",
        "author": item.username,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        endpoint = f"{NOTIFY_SERVICE_URL.rstrip('/')}/events"
        request.app.state.notify_client.post(endpoint, json=event_payload)
    except Exception as exc:
        print(f"Failed to deliver upvote event to notify-service: {exc}", flush=True)

    return {"id": item.id, "upvotes": item.upvotes, "status": "success"}
