import asyncio
import hashlib
import hmac
import json
import os
import uuid

import boto3
from fastapi import FastAPI, HTTPException, Request, Security, status
from fastapi.responses import FileResponse
from fastapi.security.api_key import APIKeyHeader
from mangum import Mangum

from app.agent import _parse_owner_repo_pr, run_review_agent
from app.config import settings
from app.models import FocusArea, ReviewRequest, ReviewResponse, ReviewStarted
from app.storage import create_review, finish_review, get_review
from app.tools import post_pr_comment

app = FastAPI(title="Arbiter", version="1.0.0")

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


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


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


# ── Demo endpoints (no API key required — for the public UI) ────────────────

@app.post("/demo/review", response_model=ReviewStarted, status_code=202)
async def demo_start_review(body: ReviewRequest):
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


@app.get("/demo/review/{review_id}", response_model=ReviewResponse)
async def demo_get_review(review_id: str):
    result = get_review(review_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return result


# ── GitHub Webhook ───────────────────────────────────────────────────────────

@app.post("/webhook/github", status_code=202)
async def github_webhook(request: Request):
    payload_bytes = await request.body()

    # Verify signature (skip if no secret configured)
    if settings.github_webhook_secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            settings.github_webhook_secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type != "pull_request":
        return {"ignored": True, "reason": f"event '{event_type}' not handled"}

    payload = json.loads(payload_bytes)
    action = payload.get("action", "")

    # Only trigger on new PRs or when new commits are pushed
    if action not in ("opened", "synchronize", "reopened"):
        return {"ignored": True, "reason": f"action '{action}' not handled"}

    pr       = payload["pull_request"]
    pr_url   = pr["html_url"]
    pr_number = pr["number"]
    repo_data = payload["repository"]
    owner    = repo_data["owner"]["login"]
    repo     = repo_data["name"]

    review_id = str(uuid.uuid4())
    comment_target = {"owner": owner, "repo": repo, "pr_number": pr_number}
    create_review(review_id=review_id, pr_url=pr_url, comment_target=comment_target)

    body = ReviewRequest(pr_url=pr_url)
    await _kick_off_processing(review_id, body, comment_target=comment_target)

    return {"review_id": review_id, "status": "processing"}


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _kick_off_processing(
    review_id: str,
    body: ReviewRequest,
    comment_target: dict | None = None,
) -> None:
    """Start processing: async self-invoke on Lambda, background task locally."""
    if _IS_LAMBDA:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _invoke_lambda_async, review_id, body, comment_target
        )
    else:
        asyncio.create_task(_process_review(review_id, body, comment_target))


def _invoke_lambda_async(
    review_id: str,
    body: ReviewRequest,
    comment_target: dict | None = None,
) -> None:
    """Invoke this same Lambda function with InvocationType=Event (fire-and-forget)."""
    client = boto3.client("lambda", region_name=settings.aws_region)
    payload: dict = {
        "type": "process_review",
        "review_id": review_id,
        "pr_url": body.pr_url,
        "focus": [f.value for f in body.focus],
    }
    if comment_target:
        payload["comment_target"] = comment_target
    client.invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


async def _process_review(
    review_id: str,
    body: ReviewRequest,
    comment_target: dict | None = None,
) -> None:
    try:
        review_result = await run_review_agent(
            pr_url=body.pr_url, focus=body.focus, review_id=review_id
        )
        finish_review(
            review_id=review_id,
            status="complete",
            review=review_result.model_dump(),
        )
        if comment_target:
            comment_body = _format_pr_comment(review_result, review_id)
            await post_pr_comment(
                owner=comment_target["owner"],
                repo=comment_target["repo"],
                pr_number=comment_target["pr_number"],
                body=comment_body,
            )
    except Exception as exc:
        finish_review(
            review_id=review_id,
            status="failed",
            error=str(exc),
        )


def _format_pr_comment(review, review_id: str) -> str:
    """Format the review result as a GitHub-flavoured markdown comment."""
    verdict_icons = {
        "approved":      "✅",
        "needs_changes": "⚠️",
        "informational": "ℹ️",
    }
    verdict_labels = {
        "approved":      "Approved",
        "needs_changes": "Needs Changes",
        "informational": "Informational",
    }
    icon  = verdict_icons.get(review.verdict.value, "📋")
    label = verdict_labels.get(review.verdict.value, review.verdict.value)

    sev_icons = {"high": "🔴", "medium": "🟡", "low": "🔵"}

    lines = [
        "## ⚖ Arbiter — AI Code Review",
        "",
        f"**Verdict:** {icon} {label}",
        "",
        "### Summary",
        review.summary,
    ]

    if review.issues:
        lines += ["", f"### Issues ({len(review.issues)})"]
        for issue in review.issues:
            sev_icon = sev_icons.get(issue.severity.value, "•")
            loc = f"`{issue.file}`" + (f":{issue.line}" if issue.line else "")
            lines += [
                "",
                f"#### {sev_icon} {issue.severity.value.capitalize()} — {loc}",
                issue.description,
                f"> **Fix:** {issue.suggestion}",
            ]
    else:
        lines += ["", "### Issues", "✨ No issues found — clean PR!"]

    if review.suggestions:
        lines += ["", "### Suggestions"]
        for s in review.suggestions:
            lines.append(f"- {s}")

    lines += [
        "",
        "---",
        f"<sub>🤖 Generated by [Arbiter](https://github.com/siddhu-uta/arbiter) "
        f"· [View full review](https://lr8mey8x75.execute-api.us-east-1.amazonaws.com/demo/review/{review_id})</sub>",
    ]

    return "\n".join(lines)


# ── Lambda entry point ──────────────────────────────────────────────────────
# Routes between API Gateway HTTP requests and background processing events.

_http_handler = Mangum(app, lifespan="off")


def handler(event, context):
    if event.get("type") == "process_review":
        review_id    = event["review_id"]
        pr_url       = event["pr_url"]
        focus        = [FocusArea(f) for f in event.get("focus", [])]
        comment_target = event.get("comment_target")
        body = ReviewRequest(pr_url=pr_url, focus=focus)
        asyncio.run(_process_review(review_id, body, comment_target))
        return {"statusCode": 200}

    # Python 3.10+ no longer auto-creates an event loop — Mangum requires one
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return _http_handler(event, context)
    finally:
        loop.close()
