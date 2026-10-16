import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_healthz_liveness():
    """Verify liveness probe returns HTTP 200 and alive status."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readyz_readiness():
    """Verify readiness probe returns HTTP 200 and ready status."""
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_metrics_endpoint():
    """Verify Prometheus metrics are properly exposed for scraping."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text or "process_cpu_seconds_total" in response.text


def test_post_valid_event():
    """Verify POST /events accepts a valid audit event payload and returns HTTP 202."""
    payload = {
        "event_type": "ITEM_CREATED",
        "item_id": 42,
        "title": "Deployed Helm umbrella chart to production",
        "author": "argo-cd",
        "timestamp": "2026-10-16T10:00:00Z",
    }
    response = client.post("/events", json=payload)
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


def test_post_empty_body_validation_failure():
    """Verify POST /events rejects an empty payload with HTTP 422 Unprocessable Entity."""
    response = client.post("/events", json={})
    assert response.status_code == 422
    data = response.json()
    assert "detail" in data
