"""Integration tests for FastAPI routes — mock storage and agent."""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app
from app.models import ReviewResponse, ReviewResult, Verdict
from app.config import settings


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": settings.api_secret_key}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_post_review_requires_auth(client):
    resp = client.post("/review", json={"pr_url": "https://github.com/o/r/pull/1"})
    assert resp.status_code == 401


def test_post_review_invalid_url(client, auth_headers):
    resp = client.post(
        "/review",
        json={"pr_url": "https://example.com/not-a-pr"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "pr_url" in resp.json()["detail"].lower()


def test_post_review_returns_202(client, auth_headers):
    def _consume_coro(coro):
        coro.close()  # prevent "coroutine never awaited" warning

    with (
        patch("app.main.create_review"),
        patch("app.main.asyncio.create_task", side_effect=_consume_coro),
    ):
        resp = client.post(
            "/review",
            json={"pr_url": "https://github.com/octocat/Hello-World/pull/1"},
            headers=auth_headers,
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "processing"
    assert "review_id" in body


def test_get_review_not_found(client, auth_headers):
    with patch("app.main.get_review", return_value=None):
        resp = client.get("/review/nonexistent-id", headers=auth_headers)
    assert resp.status_code == 404


def test_get_review_complete(client, auth_headers):
    mock_review = ReviewResponse(
        review_id="abc123",
        status="complete",
        pr_url="https://github.com/octocat/Hello-World/pull/1",
        created_at="2026-04-09T10:00:00+00:00",
        completed_at="2026-04-09T10:00:15+00:00",
        review=ReviewResult(
            summary="Looks good.",
            issues=[],
            suggestions=[],
            verdict=Verdict.approved,
        ),
    )
    with patch("app.main.get_review", return_value=mock_review):
        resp = client.get("/review/abc123", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["review"]["verdict"] == "approved"
    assert body["created_at"] == "2026-04-09T10:00:00+00:00"
    assert body["completed_at"] == "2026-04-09T10:00:15+00:00"
