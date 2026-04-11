"""Unit tests for agent.py — mock Anthropic client and tool execution."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent import _parse_owner_repo_pr, _parse_review
from app.models import FocusArea, Verdict


def test_parse_owner_repo_pr_valid():
    owner, repo, pr_num = _parse_owner_repo_pr("https://github.com/octocat/Hello-World/pull/42")
    assert owner == "octocat"
    assert repo == "Hello-World"
    assert pr_num == 42


def test_parse_owner_repo_pr_invalid():
    with pytest.raises(ValueError):
        _parse_owner_repo_pr("https://example.com/not-a-pr")


def test_parse_review_clean_json():
    raw = json.dumps({
        "summary": "Looks good overall.",
        "issues": [
            {
                "severity": "high",
                "file": "src/auth.py",
                "line": 42,
                "description": "SQL injection risk",
                "suggestion": "Use parameterized queries.",
            }
        ],
        "suggestions": ["Add type hints"],
        "verdict": "needs_changes",
    })
    result = _parse_review(raw)
    assert result.verdict == Verdict.needs_changes
    assert len(result.issues) == 1
    assert result.issues[0].severity.value == "high"


def test_parse_review_strips_markdown_fences():
    raw = "```json\n{\"summary\": \"ok\", \"issues\": [], \"suggestions\": [], \"verdict\": \"approved\"}\n```"
    result = _parse_review(raw)
    assert result.verdict == Verdict.approved
    assert result.issues == []


async def test_run_review_agent_tool_use():
    """Integration-style test: mock Anthropic responses through a tool-use loop."""
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tu_123"
    tool_use_block.name = "fetch_pr_diff"
    tool_use_block.input = {"owner": "octocat", "repo": "Hello-World", "pr_number": 1}

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = json.dumps({
        "summary": "Small PR, no issues.",
        "issues": [],
        "suggestions": [],
        "verdict": "approved",
    })

    tool_use_response = MagicMock()
    tool_use_response.stop_reason = "tool_use"
    tool_use_response.content = [tool_use_block]

    end_turn_response = MagicMock()
    end_turn_response.stop_reason = "end_turn"
    end_turn_response.content = [text_block]

    with (
        patch("app.agent.anthropic.AsyncAnthropicBedrock") as mock_anthropic,
        patch("app.agent.execute_tool", new_callable=AsyncMock) as mock_exec,
    ):
        mock_client = AsyncMock()
        mock_anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = [tool_use_response, end_turn_response]
        mock_exec.return_value = json.dumps({"diff": "...", "title": "Test PR"})

        from app.agent import run_review_agent
        result = await run_review_agent(
            pr_url="https://github.com/octocat/Hello-World/pull/1",
            focus=[FocusArea.security],
        )

    assert result.verdict.value == "approved"
    mock_exec.assert_awaited_once_with("fetch_pr_diff", tool_use_block.input)
