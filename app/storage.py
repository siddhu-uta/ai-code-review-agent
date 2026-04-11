from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.config import settings
from app.models import ReviewResponse


def _table():
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    return dynamodb.Table(settings.dynamodb_table)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_review(
    review_id: str,
    pr_url: str,
    comment_target: Optional[dict] = None,
) -> None:
    """Write the initial processing record."""
    item: dict = {
        "review_id": review_id,
        "pr_url": pr_url,
        "status": "processing",
        "created_at": _now(),
    }
    if comment_target:
        item["comment_target"] = comment_target
    _table().put_item(Item=item)


def finish_review(
    review_id: str,
    status: str,
    review: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Update an existing record with the final result, preserving created_at."""
    update_expr = "SET #s = :status, completed_at = :completed_at"
    expr_values: dict = {":status": status, ":completed_at": _now()}
    expr_names: dict = {"#s": "status"}  # 'status' is a DynamoDB reserved word

    if review is not None:
        update_expr += ", review = :review"
        expr_values[":review"] = review
    if error is not None:
        update_expr += ", #err = :error"
        expr_values[":error"] = error
        expr_names["#err"] = "error"

    _table().update_item(
        Key={"review_id": review_id},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
        ExpressionAttributeNames=expr_names,
    )


def append_step(review_id: str, text: str) -> None:
    """Atomically append a progress step to the review record."""
    _table().update_item(
        Key={"review_id": review_id},
        UpdateExpression="SET steps = list_append(if_not_exists(steps, :empty), :step)",
        ExpressionAttributeValues={
            ":step": [{"text": text, "at": _now()}],
            ":empty": [],
        },
    )


def get_review(review_id: str) -> Optional[ReviewResponse]:
    try:
        resp = _table().get_item(Key={"review_id": review_id})
    except ClientError:
        return None

    item = resp.get("Item")
    if not item:
        return None

    created_at = item.get("created_at")
    completed_at = item.get("completed_at")

    latency_ms: Optional[int] = None
    if created_at and completed_at:
        delta = datetime.fromisoformat(completed_at) - datetime.fromisoformat(created_at)
        latency_ms = int(delta.total_seconds() * 1000)

    return ReviewResponse(
        review_id=item["review_id"],
        status=item["status"],
        pr_url=item["pr_url"],
        review=item.get("review"),
        error=item.get("error"),
        created_at=created_at,
        completed_at=completed_at,
        latency_ms=latency_ms,
        steps=item.get("steps", []),
        comment_target=item.get("comment_target"),
    )
