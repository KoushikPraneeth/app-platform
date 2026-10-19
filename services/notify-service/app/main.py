import json
import sys

from fastapi import FastAPI, status
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field

# Initialize FastAPI app with metadata for OpenAPI documentation
app = FastAPI(
    title="Notify Service",
    description="Internal audit microservice logging asynchronous events from the platform",
    version="1.0.0",
)

# Instrument FastAPI app for Prometheus metric scraping
Instrumentator().instrument(app).expose(app)


class EventPayload(BaseModel):
    event_type: str = Field(..., description="Type of event, e.g. ITEM_CREATED")
    item_id: int = Field(..., description="Unique ID of the item")
    title: str = Field(..., description="Title or message of the event")
    author: str = Field(..., description="Author or triggering microservice")
    timestamp: str = Field(..., description="ISO 8601 timestamp string")


@app.get("/healthz", status_code=status.HTTP_200_OK, tags=["Health"])
async def healthz():
    """Liveness probe returning 200 if service process is running."""
    return {"status": "alive"}


@app.get("/readyz", status_code=status.HTTP_200_OK, tags=["Health"])
async def readyz():
    """Readiness probe returning 200 when ready to receive traffic."""
    return {"status": "ready"}


@app.post("/events", status_code=status.HTTP_202_ACCEPTED, tags=["Audit"])
async def receive_event(payload: EventPayload):
    """
    Accept an audit event, serialize to a single-line structured JSON log,
    and flush directly to stdout for log aggregators (FluentBit, Promtail, etc.).
    """
    log_entry = {
        "level": "INFO",
        "service": "notify-service",
        "event": "AUDIT_LOGGED",
        "data": payload.model_dump(),
    }
    print(json.dumps(log_entry), flush=True)
    return {"status": "accepted"}
