from enum import Enum
from typing import Optional
from pydantic import BaseModel, HttpUrl


class FocusArea(str, Enum):
    security = "security"
    performance = "performance"
    readability = "readability"
    correctness = "correctness"
    maintainability = "maintainability"


class ReviewRequest(BaseModel):
    pr_url: str
    focus: list[FocusArea] = [FocusArea.security, FocusArea.performance, FocusArea.readability]


class ReviewStarted(BaseModel):
    review_id: str
    status: str = "processing"
    estimated_seconds: int = 15


class Severity(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class Issue(BaseModel):
    severity: Severity
    file: str
    line: Optional[int] = None
    description: str
    suggestion: str


class Verdict(str, Enum):
    approved = "approved"
    needs_changes = "needs_changes"
    informational = "informational"


class ReviewResult(BaseModel):
    summary: str
    issues: list[Issue]
    suggestions: list[str] = []
    verdict: Verdict


class ReviewResponse(BaseModel):
    review_id: str
    status: str
    pr_url: str
    review: Optional[ReviewResult] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    latency_ms: Optional[int] = None
    steps: list[dict] = []
    comment_target: Optional[dict] = None
