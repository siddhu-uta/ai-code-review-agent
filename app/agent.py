import json
import re
from typing import Optional
import anthropic

from app.config import settings
from app.models import FocusArea, Issue, ReviewResult, Severity, Verdict
from app.tools import TOOLS, execute_tool
from app import storage

MAX_TOKENS = 4096


def _build_system_prompt(focus_areas: list[FocusArea]) -> str:
    focus_str = ", ".join(a.value for a in focus_areas)
    return f"""You are a senior software engineer performing a thorough code review.
Your focus areas for this review are: {focus_str}.

Use the fetch_pr_diff tool to retrieve the pull request diff and metadata, then analyze it carefully.

After fetching and reviewing the diff, respond with a structured JSON object (and nothing else) in this exact format:
{{
  "summary": "<2–3 sentence overall assessment>",
  "issues": [
    {{
      "severity": "high" | "medium" | "low",
      "file": "<filename>",
      "line": <line_number or null>,
      "description": "<what the problem is>",
      "suggestion": "<how to fix it>"
    }}
  ],
  "suggestions": ["<general improvement suggestion>"],
  "verdict": "approved" | "needs_changes" | "informational"
}}

Rules:
- Only output the JSON object — no markdown fences, no prose before or after.
- If there are no issues, return an empty issues array.
- Be specific: name the file and describe the exact problem.
- Focus on {focus_str} concerns. Skip trivial nits.
"""


def _parse_owner_repo_pr(pr_url: str) -> tuple[str, str, int]:
    """Parse https://github.com/owner/repo/pull/42 into (owner, repo, 42)."""
    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        raise ValueError(f"Cannot parse GitHub PR URL: {pr_url}")
    owner, repo, pr_num = match.groups()
    return owner, repo, int(pr_num)


async def run_review_agent(
    pr_url: str,
    focus: list[FocusArea],
    review_id: Optional[str] = None,
) -> ReviewResult:
    """Run the Claude tool-use agentic loop to produce a structured code review."""

    def _step(text: str) -> None:
        if review_id:
            storage.append_step(review_id, text)

    owner, repo, pr_number = _parse_owner_repo_pr(pr_url)

    client = anthropic.AsyncAnthropicBedrock()

    messages = [
        {
            "role": "user",
            "content": (
                f"Please review this pull request: {pr_url}\n"
                f"GitHub owner: {owner}, repo: {repo}, PR number: {pr_number}\n"
                "Use the fetch_pr_diff tool to get the diff, then return your structured review."
            ),
        }
    ]

    _step(f"Claude invoked — model: {settings.bedrock_model_id.split('.')[-1]}")

    # Agentic loop — Claude may call tools multiple times
    while True:
        response = await client.messages.create(
            model=settings.bedrock_model_id,
            max_tokens=MAX_TOKENS,
            system=_build_system_prompt(focus),
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            _step("Writing structured review...")
            text_block = next(
                (b for b in response.content if b.type == "text"), None
            )
            if not text_block:
                raise ValueError("Claude returned no text content")
            return _parse_review(text_block.text)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                _step(f"Tool called → {block.name}")
                tool_output = await execute_tool(block.name, block.input)

                # Surface diff stats in the activity feed
                if block.name == "fetch_pr_diff":
                    try:
                        diff_data = json.loads(tool_output)
                        files = len(diff_data.get("changed_files", []))
                        additions = diff_data.get("additions", 0)
                        deletions = diff_data.get("deletions", 0)
                        _step(
                            f"Diff fetched — {files} file{'s' if files != 1 else ''}, "
                            f"+{additions} −{deletions} lines"
                        )
                    except Exception:
                        _step("Diff fetched from GitHub")

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    }
                )
            _step("Claude is analyzing the diff...")
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        raise ValueError(f"Unexpected stop_reason: {response.stop_reason}")


def _parse_review(raw: str) -> ReviewResult:
    """Parse Claude's JSON output into a ReviewResult."""
    # Strip accidental markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

    data = json.loads(cleaned)

    issues = [
        Issue(
            severity=Severity(i["severity"]),
            file=i["file"],
            line=i.get("line"),
            description=i["description"],
            suggestion=i["suggestion"],
        )
        for i in data.get("issues", [])
    ]

    return ReviewResult(
        summary=data["summary"],
        issues=issues,
        suggestions=data.get("suggestions", []),
        verdict=Verdict(data["verdict"]),
    )
