import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_healthz_liveness():
    """Verify liveness probe returns HTTP 200 and alive status."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_metrics_endpoint():
    """Verify Prometheus metrics are properly exposed for scraping."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text or "process_cpu_seconds_total" in response.text


def test_readyz_diagnostics():
    """
    Verify readiness probe returns structured component status
    for both PostgreSQL and Redis connectivity.
    """
    response = client.get("/readyz")
    assert response.status_code in [200, 503]
    data = response.json()
    assert "status" in data
    assert "postgres" in data
    assert "redis" in data
