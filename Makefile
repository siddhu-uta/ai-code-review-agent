VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

AWS_REGION   ?= us-east-1
ECR_REPO     ?= arbiter
STACK_NAME   ?= arbiter
AWS_ACCOUNT  := $(shell aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY := $(AWS_ACCOUNT).dkr.ecr.$(AWS_REGION).amazonaws.com
IMAGE_URI    := $(ECR_REGISTRY)/$(ECR_REPO):latest

.PHONY: install test run build ecr-login push bootstrap deploy logs

## Local development ──────────────────────────────────────────────────────────

install:
	python3 -m venv $(VENV)
	$(PIP) install -r requirements.txt pytest pytest-asyncio

test:
	$(PYTHON) -m pytest tests/ -v

run:
	$(VENV)/bin/uvicorn app.main:app --reload

## Docker ─────────────────────────────────────────────────────────────────────

build:
	docker build -t $(ECR_REPO):latest .

ecr-login:
	aws ecr get-login-password --region $(AWS_REGION) | \
	  docker login --username AWS --password-stdin $(ECR_REGISTRY)

push: ecr-login
	@# Create ECR repo if it doesn't exist
	aws ecr describe-repositories --repository-names $(ECR_REPO) --region $(AWS_REGION) 2>/dev/null || \
	  aws ecr create-repository --repository-name $(ECR_REPO) --region $(AWS_REGION)
	docker tag $(ECR_REPO):latest $(IMAGE_URI)
	docker push $(IMAGE_URI)

## AWS Deployment ─────────────────────────────────────────────────────────────

# First-time setup: build + push image, then deploy the full stack.
# Requires: ANTHROPIC_API_KEY, GITHUB_TOKEN, API_SECRET_KEY in environment.
bootstrap: build push
	aws cloudformation deploy \
	  --template-file template.yaml \
	  --stack-name $(STACK_NAME) \
	  --parameter-overrides \
	    ImageUri=$(IMAGE_URI) \
	    AnthropicApiKey=$(ANTHROPIC_API_KEY) \
	    GithubToken=$(GITHUB_TOKEN) \
	    ApiSecretKey=$(API_SECRET_KEY) \
	  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
	  --no-fail-on-empty-changeset
	@echo "\nStack outputs:"
	@aws cloudformation describe-stacks \
	  --stack-name $(STACK_NAME) \
	  --query "Stacks[0].Outputs" --output table

# Subsequent deploys: rebuild + push + update stack.
deploy: build push
	aws cloudformation deploy \
	  --template-file template.yaml \
	  --stack-name $(STACK_NAME) \
	  --parameter-overrides \
	    ImageUri=$(IMAGE_URI) \
	    AnthropicApiKey=$(ANTHROPIC_API_KEY) \
	    GithubToken=$(GITHUB_TOKEN) \
	    ApiSecretKey=$(API_SECRET_KEY) \
	  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
	  --no-fail-on-empty-changeset

logs:
	aws logs tail /aws/lambda/$(STACK_NAME) --follow
