"""Hosted-mode auth: shared-secret gate on every API route except health."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge.app import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
        api_token="test-secret",
    )
    return TestClient(app)


def test_health_stays_open(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200


def test_api_routes_reject_missing_or_wrong_token(client: TestClient) -> None:
    assert client.get("/api/cases").status_code == 401
    assert client.get("/api/cases", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/requests", json={"source": "form", "request": {}}).status_code == 401


def test_bearer_header_grants_access(client: TestClient) -> None:
    response = client.get("/api/cases", headers={"Authorization": "Bearer test-secret"})
    assert response.status_code == 200
    assert response.json() == []


def test_query_token_works_for_browser_navigated_routes(client: TestClient) -> None:
    # SSE stream and document downloads cannot carry headers.
    assert client.get("/api/telemetry?token=test-secret").status_code == 200
    assert client.get("/api/documents/missing.docx?token=test-secret").status_code == 404
    assert client.get("/api/documents/missing.docx").status_code == 401


def test_no_token_configured_means_local_dev_open(tmp_path: Path) -> None:
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
        api_token="",
    )
    assert TestClient(app).get("/api/cases").status_code == 200
