import asyncio
import json
import os
import uuid

import boto3
from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from mangum import Mangum

from app.agent import _parse_owner_repo_pr, run_review_agent
from app.config import settings
from app.models import FocusArea, ReviewRequest, ReviewResponse, ReviewStarted
from app.storage import create_review, finish_review, get_review

app = FastAPI(title="AI Code Review Agent", version="1.0.0")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# True when running inside AWS Lambda
_IS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _require_api_key(api_key: str = Security(api_key_header)) -> str:
    if api_key != settings.api_secret_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return api_key


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/review", response_model=ReviewStarted, status_code=202)
async def start_review(
    body: ReviewRequest,
    _: str = Security(_require_api_key),
):
    try:
        _parse_owner_repo_pr(body.pr_url)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="pr_url must be a valid GitHub PR URL (https://github.com/owner/repo/pull/N)",
        )

    review_id = str(uuid.uuid4())
    create_review(review_id=review_id, pr_url=body.pr_url)
    await _kick_off_processing(review_id, body)

    return ReviewStarted(review_id=review_id)


@app.get("/review/{review_id}", response_model=ReviewResponse)
async def get_review_result(
    review_id: str,
    _: str = Security(_require_api_key),
):
    result = get_review(review_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return result


async def _kick_off_processing(review_id: str, body: ReviewRequest) -> None:
    """Start processing: async self-invoke on Lambda, background task locally."""
    if _IS_LAMBDA:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _invoke_lambda_async, review_id, body)
    else:
        asyncio.create_task(_process_review(review_id, body))


def _invoke_lambda_async(review_id: str, body: ReviewRequest) -> None:
    """Invoke this same Lambda function with InvocationType=Event (fire-and-forget)."""
    client = boto3.client("lambda", region_name=settings.aws_region)
    client.invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({
            "type": "process_review",
            "review_id": review_id,
            "pr_url": body.pr_url,
            "focus": [f.value for f in body.focus],
        }).encode(),
    )


async def _process_review(review_id: str, body: ReviewRequest) -> None:
    try:
        review_result = await run_review_agent(pr_url=body.pr_url, focus=body.focus)
        finish_review(
            review_id=review_id,
            status="complete",
            review=review_result.model_dump(),
        )
    except Exception as exc:
        finish_review(
            review_id=review_id,
            status="failed",
            error=str(exc),
        )


# ── Lambda entry point ──────────────────────────────────────────────────────
# Routes between API Gateway HTTP requests and background processing events.

_http_handler = Mangum(app, lifespan="off")


def handler(event, context):
    if event.get("type") == "process_review":
        review_id = event["review_id"]
        pr_url = event["pr_url"]
        focus = [FocusArea(f) for f in event.get("focus", [])]
        body = ReviewRequest(pr_url=pr_url, focus=focus)
        asyncio.run(_process_review(review_id, body))
        return {"statusCode": 200}

    # Python 3.10+ no longer auto-creates an event loop — Mangum requires one
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return _http_handler(event, context)
    finally:
        loop.close()
