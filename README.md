# Arbiter

A REST API that accepts a GitHub Pull Request URL and returns a structured, actionable code review — powered by Claude's tool-use API and deployed on AWS Lambda.

> Built as a portfolio project to demonstrate Anthropic API (agentic tool use), FastAPI, and AWS cloud skills.

---

## How It Works

```
POST /review  { "pr_url": "https://github.com/owner/repo/pull/42" }
       │
       ▼
AWS API Gateway
       │
       ▼
AWS Lambda  ──►  Anthropic API (Claude + tool use)
       │                  │
       │                  └──► GitHub API  (fetch PR diff)
       │
       └──►  DynamoDB  (store review result)
       │
       ▼
GET /review/{id}  →  structured review JSON
```

Claude autonomously calls the `fetch_pr_diff` tool, retrieves the PR diff from GitHub, then returns a structured review broken into: **summary**, **issues by severity**, **suggestions**, and a **verdict**.

---

## Demo

```bash
# Submit a PR for review
curl -X POST https://<api-url>/review \
  -H "X-API-Key: your-secret" \
  -H "Content-Type: application/json" \
  -d '{"pr_url": "https://github.com/owner/repo/pull/42", "focus": ["security", "performance"]}'

# Response
{
  "review_id": "e3b0c442-...",
  "status": "processing",
  "estimated_seconds": 15
}

# Poll for the result
curl https://<api-url>/review/e3b0c442-... \
  -H "X-API-Key: your-secret"

# Response
{
  "review_id": "e3b0c442-...",
  "status": "complete",
  "pr_url": "https://github.com/owner/repo/pull/42",
  "latency_ms": 12340,
  "review": {
    "summary": "Overall solid PR. One high-severity SQL injection risk found in auth.py.",
    "issues": [
      {
        "severity": "high",
        "file": "src/auth.py",
        "line": 42,
        "description": "User input passed directly to SQL query — SQL injection risk.",
        "suggestion": "Use parameterized queries or an ORM."
      }
    ],
    "suggestions": ["Add type hints to public functions", "Consider adding integration tests"],
    "verdict": "needs_changes"
  }
}
```

---

## Tech Stack

| Layer      | Technology                                    |
|------------|-----------------------------------------------|
| Language   | Python 3.11                                   |
| Framework  | FastAPI + Mangum (ASGI → Lambda adapter)      |
| AI         | Anthropic API — Claude, tool use              |
| Cloud      | AWS Lambda, API Gateway (HTTP), DynamoDB      |
| Auth       | API key via `X-API-Key` header                |
| Packaging  | Docker + AWS ECR                              |
| Infra      | AWS CloudFormation / SAM                      |
| CI/CD      | GitHub Actions (OIDC — no long-lived secrets) |

---

## Project Structure

```
ai-code-review-agent/
├── app/
│   ├── main.py       # FastAPI routes + Lambda entry point
│   ├── agent.py      # Claude tool-use agentic loop
│   ├── tools.py      # fetch_pr_diff tool (GitHub API)
│   ├── models.py     # Pydantic request/response models
│   ├── storage.py    # DynamoDB read/write
│   └── config.py     # Environment variable config
├── tests/
│   ├── test_agent.py   # Unit tests: agent loop + parser
│   ├── test_api.py     # FastAPI route tests
│   └── test_storage.py # DynamoDB tests (moto mock)
├── .github/
│   └── workflows/
│       └── deploy.yml  # CI/CD: test → ECR → CloudFormation
├── template.yaml     # SAM/CloudFormation infrastructure
├── Dockerfile        # Lambda container image
├── Makefile          # Developer shortcuts
└── requirements.txt
```

---

## API Reference

### `POST /review`
Start a new code review.

**Headers:** `X-API-Key: <secret>`

**Request body:**
```json
{
  "pr_url": "https://github.com/owner/repo/pull/42",
  "focus": ["security", "performance", "readability"]
}
```
`focus` options: `security` · `performance` · `readability` · `correctness` · `maintainability`

**Response** `202 Accepted`:
```json
{
  "review_id": "e3b0c442-...",
  "status": "processing",
  "estimated_seconds": 15
}
```

---

### `GET /review/{review_id}`
Fetch a completed review. Poll until `status` is `complete` or `failed`.

**Response** `200 OK`:
```json
{
  "review_id": "e3b0c442-...",
  "status": "complete",
  "pr_url": "...",
  "latency_ms": 12340,
  "created_at": "2026-04-10T10:00:00+00:00",
  "completed_at": "2026-04-10T10:00:12+00:00",
  "review": {
    "summary": "...",
    "issues": [
      {
        "severity": "high | medium | low",
        "file": "src/foo.py",
        "line": 42,
        "description": "...",
        "suggestion": "..."
      }
    ],
    "suggestions": ["..."],
    "verdict": "approved | needs_changes | informational"
  }
}
```

---

### `GET /health`
Health check (no auth required).

---

## Local Setup

```bash
# 1. Clone and enter the project
git clone https://github.com/siddhu-uta/arbiter
cd ai-code-review-agent

# 2. Create virtualenv and install dependencies
make install

# 3. Configure environment
cp .env.example .env
# Fill in: GITHUB_TOKEN, API_SECRET_KEY
# Ensure AWS credentials are configured (aws configure) for Bedrock access

# 4. Run the server
make run
# → http://localhost:8000

# 5. Run tests
make test
```

---

## AWS Deployment

### First-time bootstrap (run once)

**Prerequisites:**
- AWS CLI configured (`aws configure`)
- Docker running
- An ECR repository created (or `make push` creates it automatically)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_TOKEN=ghp_...
export API_SECRET_KEY=your-random-secret

make bootstrap
# Creates: ECR repo → Docker image → DynamoDB table → Lambda → API Gateway
# Prints the live API URL when done
```

### CI/CD (automatic on every push to `main`)

The GitHub Actions pipeline handles everything after first-time setup:

1. Run tests
2. Build Docker image and push to ECR
3. Deploy CloudFormation stack with the new image

**Required GitHub Secrets:**

| Secret | Description |
|---|---|
| `AWS_ROLE_ARN` | IAM role ARN (used via OIDC — no long-lived keys) |
| `GITHUB_TOKEN_SECRET` | GitHub PAT for fetching PR diffs |
| `API_SECRET_KEY` | Secret for `X-API-Key` header |

> No `ANTHROPIC_API_KEY` needed — Claude is invoked via AWS Bedrock using the Lambda's IAM role.

---

## Key Design Decisions

**Async processing on Lambda**
`asyncio.create_task` doesn't survive after a Lambda handler returns. Instead, `POST /review` invokes the *same* Lambda function asynchronously (`InvocationType=Event`) with a custom event payload. The handler routes on `event["type"]` — HTTP requests go to FastAPI via Mangum, background events run the agent directly.

**Claude Tool Use**
Claude autonomously decides when and how to call `fetch_pr_diff`. The agent loop continues until Claude returns `stop_reason: end_turn` with a JSON review — no hardcoded orchestration.

**Infrastructure as Code**
A single `template.yaml` (SAM/CloudFormation) defines all AWS resources. `make bootstrap` provisions everything from scratch.

---

## Environment Variables

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | GitHub PAT (needs `repo` scope for private repos, none for public) |
| `API_SECRET_KEY` | Shared secret for API key auth |
| `DYNAMODB_TABLE` | DynamoDB table name (default: `code-reviews`) |
| `AWS_REGION` | AWS region (default: `us-east-1`) |
| `BEDROCK_MODEL_ID` | Bedrock model ID (default: `us.anthropic.claude-sonnet-4-5-20250514-v1:0`) |

> Claude is accessed via **AWS Bedrock** — no Anthropic API key required. Auth is handled by the IAM role attached to the Lambda function (or your local AWS credentials).
