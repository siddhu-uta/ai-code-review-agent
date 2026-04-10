import httpx
from app.config import settings

# Tool definition passed to Claude
TOOLS = [
    {
        "name": "fetch_pr_diff",
        "description": (
            "Fetches the diff and metadata for a GitHub Pull Request using the GitHub API. "
            "Returns the PR title, description, changed files, and the unified diff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "GitHub repo owner (user or org)"},
                "repo": {"type": "string", "description": "Repository name"},
                "pr_number": {"type": "integer", "description": "Pull request number"},
            },
            "required": ["owner", "repo", "pr_number"],
        },
    }
]


async def fetch_pr_diff(owner: str, repo: str, pr_number: int) -> dict:
    """Fetch PR metadata and diff from GitHub API."""
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch PR metadata
        pr_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        pr_resp.raise_for_status()
        pr_data = pr_resp.json()

        # Fetch diff
        diff_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={**headers, "Accept": "application/vnd.github.diff"},
        )
        diff_resp.raise_for_status()

        # Fetch changed files list
        files_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files",
            headers=headers,
        )
        files_resp.raise_for_status()
        files_data = files_resp.json()

    changed_files = [
        {
            "filename": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
        }
        for f in files_data
    ]

    return {
        "title": pr_data["title"],
        "description": pr_data.get("body") or "",
        "author": pr_data["user"]["login"],
        "base_branch": pr_data["base"]["ref"],
        "head_branch": pr_data["head"]["ref"],
        "changed_files": changed_files,
        "additions": pr_data["additions"],
        "deletions": pr_data["deletions"],
        "diff": diff_resp.text,
    }


async def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch a tool call from Claude and return a string result."""
    if tool_name == "fetch_pr_diff":
        result = await fetch_pr_diff(**tool_input)
        # Truncate diff if extremely large (Lambda memory limits)
        diff = result["diff"]
        if len(diff) > 60_000:
            diff = diff[:60_000] + "\n\n[diff truncated — too large]"
        result["diff"] = diff
        import json
        return json.dumps(result, indent=2)
    raise ValueError(f"Unknown tool: {tool_name}")
