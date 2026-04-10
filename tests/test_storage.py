"""Tests for storage.py — DynamoDB interactions mocked with moto."""
import boto3
import pytest
from moto import mock_aws

from app.config import settings


@pytest.fixture
def dynamodb_table():
    """Spin up a mocked DynamoDB table for each test."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name=settings.aws_region)
        client.create_table(
            TableName=settings.dynamodb_table,
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def test_create_and_get_review(dynamodb_table):
    from app.storage import create_review, get_review

    create_review(review_id="abc123", pr_url="https://github.com/o/r/pull/1")

    result = get_review("abc123")
    assert result is not None
    assert result.review_id == "abc123"
    assert result.status == "processing"
    assert result.pr_url == "https://github.com/o/r/pull/1"
    assert result.created_at is not None
    assert result.completed_at is None
    assert result.review is None


def test_finish_review_complete(dynamodb_table):
    from app.storage import create_review, finish_review, get_review

    create_review(review_id="abc123", pr_url="https://github.com/o/r/pull/1")
    finish_review(
        review_id="abc123",
        status="complete",
        review={
            "summary": "Looks good.",
            "issues": [],
            "suggestions": [],
            "verdict": "approved",
        },
    )

    result = get_review("abc123")
    assert result.status == "complete"
    assert result.review.verdict == "approved"
    assert result.completed_at is not None
    assert result.error is None


def test_finish_review_failed(dynamodb_table):
    from app.storage import create_review, finish_review, get_review

    create_review(review_id="xyz", pr_url="https://github.com/o/r/pull/2")
    finish_review(review_id="xyz", status="failed", error="GitHub rate limit hit")

    result = get_review("xyz")
    assert result.status == "failed"
    assert result.error == "GitHub rate limit hit"
    assert result.completed_at is not None
    assert result.review is None


def test_get_review_not_found(dynamodb_table):
    from app.storage import get_review

    assert get_review("does-not-exist") is None


def test_finish_preserves_created_at(dynamodb_table):
    """created_at written on create must survive the finish_review update."""
    from app.storage import create_review, finish_review, get_review

    create_review(review_id="ts-test", pr_url="https://github.com/o/r/pull/3")
    before = get_review("ts-test").created_at

    finish_review(review_id="ts-test", status="complete", review={
        "summary": "ok", "issues": [], "suggestions": [], "verdict": "approved"
    })
    after = get_review("ts-test").created_at

    assert before == after  # update_item must not clobber created_at
